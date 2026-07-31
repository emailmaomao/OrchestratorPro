"""Tests for the tool interface, dispatch, and the safety guards."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

from orchestrator.agent.tools import (
    CommandRefusedError,
    FinishTool,
    ListDirTool,
    PathEscapeError,
    ReadFileTool,
    Tool,
    ToolCall,
    ToolContext,
    ToolError,
    ToolRegistry,
    ToolResult,
    WriteFileTool,
    check_command,
    default_registry,
    render_arguments,
    resolve_within,
)

from tests.agent.conftest import run


class TestPathConfinement:
    """NFR-3.1, checked on the resolved path."""

    def test_a_relative_path_resolves_inside(self, workspace: Path) -> None:
        assert resolve_within(workspace, "src/a.py").parent.name == "src"

    def test_the_root_itself_is_allowed(self, workspace: Path) -> None:
        assert resolve_within(workspace, ".") == workspace.resolve()

    @pytest.mark.parametrize(
        "candidate",
        ["../outside.txt", "../../etc/passwd", "src/../../escape.txt", "./../x"],
    )
    def test_traversal_is_refused(self, workspace: Path, candidate: str) -> None:
        with pytest.raises(PathEscapeError, match="outside the workspace"):
            resolve_within(workspace, candidate)

    def test_an_absolute_path_elsewhere_is_refused(self, workspace: Path) -> None:
        outside = workspace.parent / "elsewhere.txt"
        with pytest.raises(PathEscapeError):
            resolve_within(workspace, outside)

    def test_an_absolute_path_inside_is_allowed(self, workspace: Path) -> None:
        inside = workspace / "ok.txt"
        assert resolve_within(workspace, inside) == inside.resolve()

    def test_a_symlink_escape_is_refused(self, workspace: Path) -> None:
        """Checking the raw string instead of the resolved path is the classic bypass."""
        secret = workspace.parent / "secret.txt"
        secret.write_text("classified", encoding="utf-8")
        link = workspace / "innocent.txt"
        try:
            os.symlink(secret, link)
        except (OSError, NotImplementedError) as exc:
            # Windows needs Developer Mode or elevation. Skip only when the
            # platform actually refuses, so the check still runs wherever it can.
            pytest.skip(f"symlinks are unavailable here: {exc}")

        with pytest.raises(PathEscapeError):
            resolve_within(workspace, "innocent.txt")

    def test_a_symlinked_parent_directory_is_refused(self, workspace: Path) -> None:
        """The leaf need not be the link: a symlinked *component* escapes too.

        The existing case links the file itself. This links a directory and
        reaches through it, which is the vector that matters for writing a
        *new* file: the leaf does not exist yet, so only the resolved parent
        can catch it.
        """
        outside_dir = workspace.parent / "outside"
        outside_dir.mkdir(exist_ok=True)
        link = workspace / "gateway"
        try:
            os.symlink(outside_dir, link, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"symlinks are unavailable here: {exc}")

        # Both an existing target and one that does not exist yet.
        (outside_dir / "existing.txt").write_text("secret", encoding="utf-8")
        for candidate in ("gateway/existing.txt", "gateway/brand-new.txt"):
            with pytest.raises(PathEscapeError, match="outside the workspace"):
                resolve_within(workspace, candidate)

    def test_the_refusal_tells_the_agent_what_happened(
        self, workspace: Path
    ) -> None:
        """The agent reads this text; it has to be actionable, not a code."""
        with pytest.raises(PathEscapeError) as excinfo:
            resolve_within(workspace, "../../etc/passwd")

        message = str(excinfo.value)
        assert "outside the workspace" in message
        assert "refused" in message
        detail = excinfo.value.detail
        assert detail["root"] == str(workspace.resolve())
        assert detail["resolved"], "the agent cannot self-correct without this"

    def test_the_error_names_what_was_requested(self, workspace: Path) -> None:
        with pytest.raises(PathEscapeError) as excinfo:
            resolve_within(workspace, "../nope")
        assert excinfo.value.detail["requested"] == "../nope"


class TestCommandAllowlist:
    """NFR-3.2: an allowlist, because a blocklist cannot be completed."""

    def test_an_allowlisted_executable_passes(self) -> None:
        assert check_command(["python", "-c", "print(1)"], {"python"}) == (
            "python",
            "-c",
            "print(1)",
        )

    def test_a_full_path_matches_on_its_basename(self) -> None:
        check_command(["/usr/bin/python", "-V"], {"python"})

    def test_an_unlisted_executable_is_refused(self) -> None:
        with pytest.raises(CommandRefusedError, match="not on the executable allowlist"):
            check_command(["curl", "http://example.com"], {"python"})

    def test_an_empty_command_is_refused(self) -> None:
        with pytest.raises(CommandRefusedError, match="empty command"):
            check_command([], {"python"})

    @pytest.mark.parametrize(
        "argument", ["a;b", "a&&b", "a|b", "`x`", "$(x)", "a>b", "a<b", "a\nb"]
    )
    def test_shell_metacharacters_are_refused(self, argument: str) -> None:
        with pytest.raises(CommandRefusedError, match="metacharacters"):
            check_command(["python", argument], {"python"})

    def test_the_error_lists_what_is_allowed(self) -> None:
        with pytest.raises(CommandRefusedError) as excinfo:
            check_command(["rm", "-rf", "/"], {"python", "pytest"})
        assert excinfo.value.detail["allowed"] == ["pytest", "python"]


class TestRegistry:
    """Registration, specs, and dispatch."""

    def test_registration_and_lookup(self) -> None:
        registry = ToolRegistry([ReadFileTool()])
        assert "read_file" in registry
        assert registry.get("read_file") is not None
        assert len(registry) == 1

    def test_a_duplicate_name_is_refused(self) -> None:
        """A silently replaced tool changes behaviour invisibly."""
        registry = ToolRegistry([ReadFileTool()])
        with pytest.raises(ToolError, match="already registered"):
            registry.register(ReadFileTool())

    def test_specs_are_sorted_by_name(self) -> None:
        """Tool order is prompt-prefix order; reordering busts the cache."""
        registry = default_registry()
        names = [spec.name for spec in registry.specs()]
        assert names == sorted(names)

    def test_specs_carry_descriptions_and_schemas(self) -> None:
        spec = next(s for s in default_registry().specs() if s.name == "write_file")
        assert spec.description
        assert spec.schema["required"] == ["path", "content"]

    def test_the_default_registry_has_the_builtins(self) -> None:
        assert set(default_registry().names) == {
            "finish",
            "list_dir",
            "read_file",
            "write_file",
        }

    def test_an_unknown_tool_returns_an_error_result(
        self, tool_ctx: ToolContext
    ) -> None:
        """The model gets a correction, not a dead attempt."""
        registry = default_registry()
        result = run(registry.dispatch(ToolCall(id="1", name="nope"), tool_ctx))

        assert result.is_error
        assert "no tool named" in result.content
        assert "read_file" in result.content

    def test_a_crashing_tool_becomes_an_error_result(
        self, tool_ctx: ToolContext
    ) -> None:
        class Exploding:
            name = "boom"
            description = "always raises"
            schema: dict[str, Any] = {"type": "object"}

            async def __call__(self, arguments: Any, ctx: Any) -> ToolResult:
                raise RuntimeError("kaboom")

        registry = ToolRegistry([Exploding()])
        result = run(registry.dispatch(ToolCall(id="1", name="boom"), tool_ctx))

        assert result.is_error
        assert "kaboom" in result.content
        assert result.detail["code"] == "tool_crashed"

    def test_a_tool_error_keeps_its_code(self, tool_ctx: ToolContext) -> None:
        result = run(
            default_registry().dispatch(
                ToolCall(id="1", name="read_file", arguments={"path": "../escape"}),
                tool_ctx,
            )
        )
        assert result.is_error
        assert result.detail["code"] == "path_escape"

    def test_a_string_return_becomes_a_success_result(
        self, tool_ctx: ToolContext
    ) -> None:
        class Plain:
            name = "plain"
            description = "returns a string"
            schema: dict[str, Any] = {"type": "object"}

            async def __call__(self, arguments: Any, ctx: Any) -> str:
                return "hello"

        result = run(ToolRegistry([Plain()]).dispatch(ToolCall(id="1", name="plain"), tool_ctx))
        assert not result.is_error
        assert result.content == "hello"

    def test_cancellation_short_circuits_dispatch(self, workspace: Path) -> None:
        ctx = ToolContext(workspace_root=workspace, cancelled=lambda: True)
        result = run(
            default_registry().dispatch(ToolCall(id="1", name="list_dir"), ctx)
        )
        assert result.is_error
        assert result.detail["code"] == "cancelled"

    def test_an_unnamed_tool_is_refused(self) -> None:
        class Nameless:
            name = "  "
            description = "x"
            schema: dict[str, Any] = {}

            async def __call__(self, arguments: Any, ctx: Any) -> str:
                return ""

        with pytest.raises(ToolError, match="must have a name"):
            ToolRegistry([Nameless()])


class TestBuiltinTools:
    """The workspace-scoped built-ins."""

    def test_read_file_returns_contents(self, tool_ctx: ToolContext) -> None:
        (tool_ctx.workspace_root / "a.txt").write_text("hello", encoding="utf-8")
        result = run(ReadFileTool()({"path": "a.txt"}, tool_ctx))
        assert result.content == "hello"

    def test_read_file_requires_a_path(self, tool_ctx: ToolContext) -> None:
        with pytest.raises(ToolError, match="requires a 'path'"):
            run(ReadFileTool()({}, tool_ctx))

    def test_read_file_refuses_a_directory(self, tool_ctx: ToolContext) -> None:
        (tool_ctx.workspace_root / "sub").mkdir()
        with pytest.raises(ToolError, match="not a file"):
            run(ReadFileTool()({"path": "sub"}, tool_ctx))

    def test_read_file_enforces_the_size_limit(self, workspace: Path) -> None:
        ctx = ToolContext(workspace_root=workspace, max_read_bytes=10)
        (workspace / "big.txt").write_text("x" * 100, encoding="utf-8")
        with pytest.raises(ToolError, match="read limit"):
            run(ReadFileTool()({"path": "big.txt"}, ctx))

    def test_write_file_creates_parents_and_reports_the_path(
        self, tool_ctx: ToolContext
    ) -> None:
        result = run(
            WriteFileTool()({"path": "src/deep/a.py", "content": "x = 1\n"}, tool_ctx)
        )
        assert (tool_ctx.workspace_root / "src" / "deep" / "a.py").is_file()
        assert result.changed_paths and result.changed_paths[0].endswith("a.py")

    def test_write_file_requires_string_content(self, tool_ctx: ToolContext) -> None:
        with pytest.raises(ToolError, match="string 'content'"):
            run(WriteFileTool()({"path": "a.txt", "content": 42}, tool_ctx))

    def test_write_file_is_confined(self, tool_ctx: ToolContext) -> None:
        with pytest.raises(PathEscapeError):
            run(WriteFileTool()({"path": "../escape.txt", "content": "x"}, tool_ctx))

    def test_list_dir_sorts_and_marks_directories(self, tool_ctx: ToolContext) -> None:
        (tool_ctx.workspace_root / "b.txt").write_text("", encoding="utf-8")
        (tool_ctx.workspace_root / "a_dir").mkdir()
        result = run(ListDirTool()({}, tool_ctx))
        assert result.content.splitlines() == ["a_dir/", "b.txt"]

    def test_list_dir_on_an_empty_directory(self, tool_ctx: ToolContext) -> None:
        assert "empty" in run(ListDirTool()({}, tool_ctx)).content

    def test_finish_marks_the_result_terminal(self, tool_ctx: ToolContext) -> None:
        result = run(FinishTool()({"summary": "added the greeting"}, tool_ctx))
        assert result.terminal
        assert result.detail["summary"] == "added the greeting"

    def test_finish_requires_a_summary(self, tool_ctx: ToolContext) -> None:
        with pytest.raises(ToolError, match="non-empty 'summary'"):
            run(FinishTool()({"summary": "  "}, tool_ctx))


def test_arguments_render_deterministically() -> None:
    """Transcript lines must be comparable across runs."""
    arguments = {"zebra": 1, "alpha": {"b": 2, "a": 1}}
    assert render_arguments(arguments) == render_arguments(dict(reversed(list(arguments.items()))))


def test_the_builtin_tools_satisfy_the_protocol() -> None:
    for tool in (ReadFileTool(), WriteFileTool(), ListDirTool(), FinishTool()):
        assert isinstance(tool.name, str) and tool.name
        assert isinstance(tool.description, str) and tool.description
        assert isinstance(tool.schema, dict)

"""The tool-call interface: registry, dispatch, and the safety guards.

This is the boundary where model output stops being text and starts being
actions, which makes it the security surface of the whole system. Two guards
live here, and everything that touches the filesystem or a shell must go through
them:

:func:`resolve_within`
    Canonicalizes a model-supplied path and verifies it stays inside the
    workspace root — **after** symlink resolution, because checking the raw
    string is the classic bypass (NFR-3.1).
:func:`check_command`
    Enforces an executable **allowlist** and rejects shell metacharacters. A
    blocklist is not acceptable and cannot be made complete (NFR-3.2).

None of this is a statement about agent intent. A harness that assumes
well-formed output breaks the first time output is malformed, and at fleet scale
that is every run.

**A tool that raises is not a crash.** :meth:`ToolRegistry.dispatch` converts an
exception into an error result the model can read and react to. An agent that
called a tool wrongly should get a correction, not take the attempt down with it.

Only workspace-scoped file tools ship here. ``run_command`` and ``run_tests``
wait for the ``shell`` and ``build`` provider domains (``docs/030`` §5.4, §5.3)
rather than growing a second, unguarded way to execute things.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol

from orchestrator.core.events import OrchestratorError
from orchestrator.provider.base import ToolSpec

__all__ = [
    "FinishTool",
    "ListDirTool",
    "ReadFileTool",
    "Tool",
    "ToolCall",
    "ToolContext",
    "ToolError",
    "ToolRegistry",
    "ToolResult",
    "WriteFileTool",
    "check_command",
    "default_registry",
    "resolve_within",
]

#: Characters that let a downstream shell reinterpret an argument. Rejected even
#: though nothing here invokes a shell, because a tool we call might.
_SHELL_METACHARACTERS: Final = frozenset(";&|`$><\n\r")

#: Refuse to read more than this into a model's context in one call.
_DEFAULT_MAX_READ_BYTES: Final = 256_000


class ToolError(OrchestratorError):
    """A tool refused or failed to run."""

    code = "tool"
    retryable = False


class PathEscapeError(ToolError):
    """A path argument pointed outside the workspace."""

    code = "path_escape"
    retryable = False


class CommandRefusedError(ToolError):
    """A command was not on the allowlist, or was malformed."""

    code = "command_refused"
    retryable = False


# --------------------------------------------------------------------------- #
# Guards
# --------------------------------------------------------------------------- #


def resolve_within(root: Path, candidate: str | Path) -> Path:
    """Resolve a model-supplied path and confine it to ``root``.

    Containment is checked on the **resolved** path, so ``..`` traversal, a
    symlink pointing outside, and an absolute path elsewhere are all rejected.

    Args:
        root: The workspace root.
        candidate: The path the model asked for, absolute or relative.

    Returns:
        The resolved, contained path.

    Raises:
        PathEscapeError: If the path escapes the root.
    """
    base = Path(root).resolve()
    target = Path(candidate)
    combined = target if target.is_absolute() else base / target
    resolved = combined.resolve()

    if resolved != base and not resolved.is_relative_to(base):
        raise PathEscapeError(
            f"the path {candidate!r} resolves outside the workspace and was refused",
            detail={"requested": str(candidate), "resolved": str(resolved), "root": str(base)},
        )
    return resolved


def check_command(argv: Iterable[str], allowlist: Iterable[str]) -> tuple[str, ...]:
    """Verify a command against an executable allowlist.

    Args:
        argv: The argument vector.
        allowlist: Executables permitted to run.

    Returns:
        The validated argument vector.

    Raises:
        CommandRefusedError: If the vector is empty, the executable is not
            allowlisted, or any argument carries a shell metacharacter.
    """
    args = tuple(argv)
    if not args:
        raise CommandRefusedError("an empty command was refused")

    permitted = frozenset(allowlist)
    executable = Path(args[0]).name
    if executable not in permitted and args[0] not in permitted:
        raise CommandRefusedError(
            f"{args[0]!r} is not on the executable allowlist",
            detail={"executable": args[0], "allowed": sorted(permitted)},
        )

    for argument in args:
        offending = _SHELL_METACHARACTERS & set(argument)
        if offending:
            raise CommandRefusedError(
                f"the argument {argument!r} contains shell metacharacters "
                f"({''.join(sorted(offending))}) and was refused",
                detail={"argument": argument},
            )
    return args


# --------------------------------------------------------------------------- #
# Interface
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One request from the model to invoke a tool."""

    id: str
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What a tool produced, in the form the model will read.

    Attributes:
        call_id: The call this answers.
        content: Text handed back to the model.
        is_error: Whether the tool refused or failed. Errors are returned, not
            raised, so the model can correct itself.
        terminal: Whether this result ends the attempt. Set by ``finish``.
        changed_paths: Repository-relative paths the tool modified, so the
            runtime can report what an attempt actually touched.
        detail: Structured extras for the transcript.
    """

    call_id: str
    content: str
    is_error: bool = False
    terminal: bool = False
    changed_paths: tuple[str, ...] = ()
    detail: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Ambient state a tool is allowed to see.

    Attributes:
        workspace_root: The confinement boundary for every path.
        allowlist: Executables a command-running tool may invoke.
        max_read_bytes: Ceiling on how much one read may pull into context.
        cancelled: Consulted before each dispatch so a cancelled attempt stops
            promptly rather than finishing its tool queue.
    """

    workspace_root: Path
    allowlist: frozenset[str] = frozenset()
    max_read_bytes: int = _DEFAULT_MAX_READ_BYTES
    cancelled: Callable[[], bool] = bool

    def resolve(self, candidate: str | Path) -> Path:
        """Resolve a path inside this context's workspace."""
        return resolve_within(self.workspace_root, candidate)

    def relative(self, path: Path) -> str:
        """Render a resolved path relative to the workspace, for reporting."""
        try:
            return str(path.resolve().relative_to(self.workspace_root.resolve()))
        except ValueError:  # pragma: no cover - resolve_within prevents this
            return str(path)


class Tool(Protocol):
    """A capability the model may invoke."""

    name: str
    description: str
    schema: Mapping[str, Any]

    async def __call__(
        self, arguments: Mapping[str, Any], ctx: ToolContext
    ) -> ToolResult | str:
        """Run the tool.

        Returning a bare string is shorthand for a successful text result.
        Raising is permitted: the registry converts it into an error result.
        """
        ...


class ToolRegistry:
    """Holds the tools available to an agent, and dispatches calls to them."""

    __slots__ = ("_tools",)

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        """Create a registry, optionally pre-populated."""
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        """Add a tool.

        Raises:
            ToolError: If the name is blank or already registered. A silently
                replaced tool would change an agent's behaviour invisibly.
        """
        if not tool.name.strip():
            raise ToolError("a tool must have a name")
        if tool.name in self._tools:
            raise ToolError(
                f"a tool named {tool.name!r} is already registered",
                detail={"tool": tool.name},
            )
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        """Return a tool by name, or ``None``."""
        return self._tools.get(name)

    @property
    def names(self) -> tuple[str, ...]:
        """Every registered tool name, sorted."""
        return tuple(sorted(self._tools))

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def specs(self) -> tuple[ToolSpec, ...]:
        """Return provider-neutral tool specifications, **sorted by name**.

        The ordering is not cosmetic: tool definitions render at the very front
        of a prompt, so a set that reorders between requests invalidates the
        entire prefix cache (spec §9).
        """
        return tuple(
            ToolSpec(
                name=name,
                description=self._tools[name].description,
                schema=dict(self._tools[name].schema),
            )
            for name in sorted(self._tools)
        )

    async def dispatch(self, call: ToolCall, ctx: ToolContext) -> ToolResult:
        """Run one tool call, converting any failure into an error result.

        Args:
            call: What the model asked for.
            ctx: The ambient state the tool may see.

        Returns:
            The result. Unknown tools, refused arguments, and unexpected
            exceptions all come back as ``is_error`` results rather than
            propagating — an agent that called a tool wrongly deserves a
            correction, not a dead attempt.
        """
        if ctx.cancelled():
            return ToolResult(
                call_id=call.id,
                content="The attempt was cancelled before this tool ran.",
                is_error=True,
                detail={"code": "cancelled"},
            )

        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult(
                call_id=call.id,
                content=(
                    f"There is no tool named {call.name!r}. Available tools: "
                    f"{', '.join(self.names)}."
                ),
                is_error=True,
                detail={"code": "unknown_tool"},
            )

        try:
            produced = await tool(dict(call.arguments), ctx)
        except ToolError as exc:
            return ToolResult(
                call_id=call.id,
                content=f"{exc.code}: {exc}",
                is_error=True,
                detail={"code": exc.code, **exc.detail},
            )
        except Exception as exc:  # noqa: BLE001 - a tool bug must not end the run
            return ToolResult(
                call_id=call.id,
                content=f"The tool {call.name!r} raised {type(exc).__name__}: {exc}",
                is_error=True,
                detail={"code": "tool_crashed", "exception": type(exc).__name__},
            )

        if isinstance(produced, str):
            return ToolResult(call_id=call.id, content=produced)
        return produced


# --------------------------------------------------------------------------- #
# Built-in tools
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ReadFileTool:
    """Reads a file from the workspace."""

    name: str = "read_file"
    description: str = (
        "Read a UTF-8 text file from the workspace. Paths are relative to the "
        "workspace root."
    )
    schema: Mapping[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "File to read."}},
            "required": ["path"],
        }
    )

    async def __call__(
        self, arguments: Mapping[str, Any], ctx: ToolContext
    ) -> ToolResult:
        """Read the requested file."""
        raw = arguments.get("path")
        if not isinstance(raw, str) or not raw:
            raise ToolError("read_file requires a 'path' argument")

        target = ctx.resolve(raw)
        if not target.is_file():
            raise ToolError(f"{raw!r} is not a file in this workspace")

        size = target.stat().st_size
        if size > ctx.max_read_bytes:
            raise ToolError(
                f"{raw!r} is {size} bytes, over the {ctx.max_read_bytes}-byte read "
                "limit; read a smaller portion or use search"
            )
        return ToolResult(call_id="", content=target.read_text(encoding="utf-8", errors="replace"))


@dataclass(frozen=True, slots=True)
class WriteFileTool:
    """Writes a file into the workspace."""

    name: str = "write_file"
    description: str = (
        "Write a UTF-8 text file into the workspace, creating parent directories "
        "as needed. Overwrites an existing file."
    )
    schema: Mapping[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File to write."},
                "content": {"type": "string", "description": "Full file contents."},
            },
            "required": ["path", "content"],
        }
    )

    async def __call__(
        self, arguments: Mapping[str, Any], ctx: ToolContext
    ) -> ToolResult:
        """Write the requested file."""
        raw = arguments.get("path")
        content = arguments.get("content")
        if not isinstance(raw, str) or not raw:
            raise ToolError("write_file requires a 'path' argument")
        if not isinstance(content, str):
            raise ToolError("write_file requires a string 'content' argument")

        target = ctx.resolve(raw)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

        relative = ctx.relative(target)
        return ToolResult(
            call_id="",
            content=f"Wrote {len(content)} character(s) to {relative}.",
            changed_paths=(relative,),
        )


@dataclass(frozen=True, slots=True)
class ListDirTool:
    """Lists a directory in the workspace."""

    name: str = "list_dir"
    description: str = "List the entries of a directory in the workspace."
    schema: Mapping[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory, defaulting to '.'."}
            },
        }
    )

    async def __call__(
        self, arguments: Mapping[str, Any], ctx: ToolContext
    ) -> ToolResult:
        """List the requested directory."""
        target = ctx.resolve(arguments.get("path") or ".")
        if not target.is_dir():
            raise ToolError(f"{arguments.get('path')!r} is not a directory")

        entries = sorted(
            f"{child.name}/" if child.is_dir() else child.name
            for child in target.iterdir()
        )
        return ToolResult(
            call_id="", content="\n".join(entries) if entries else "(empty directory)"
        )


@dataclass(frozen=True, slots=True)
class FinishTool:
    """Ends the attempt.

    An explicit terminator, rather than inferring completion from a turn with no
    tool calls. Inference conflates "I am done" with "I have nothing further to
    say", and those want different handling.
    """

    name: str = "finish"
    description: str = (
        "Declare the task complete and end the attempt. Provide a short summary "
        "of what was done."
    )
    schema: Mapping[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "What was accomplished."}
            },
            "required": ["summary"],
        }
    )

    async def __call__(
        self, arguments: Mapping[str, Any], ctx: ToolContext
    ) -> ToolResult:
        """Record the summary and signal completion."""
        summary = arguments.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise ToolError("finish requires a non-empty 'summary' argument")
        return ToolResult(
            call_id="",
            content="Attempt marked complete.",
            terminal=True,
            detail={"summary": summary.strip()},
        )


def default_registry() -> ToolRegistry:
    """Return a registry with the workspace-scoped built-in tools."""
    return ToolRegistry(
        [ReadFileTool(), WriteFileTool(), ListDirTool(), FinishTool()]
    )


def render_arguments(arguments: Mapping[str, Any]) -> str:
    """Render tool arguments deterministically, for transcripts and hashing."""
    return json.dumps(dict(arguments), sort_keys=True, separators=(",", ":"), default=repr)

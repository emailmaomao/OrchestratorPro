"""Tests for the Claude Code harness adapter.

Every test is offline: the CLI is a scripted :class:`CliRunner` and the
changed-file listing is injected, so nothing here spends a subscription call
or needs a real checkout.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from orchestrator.adapter.claude_code import ClaudeCodeHarness, HarnessConfig
from orchestrator.agent.memory import TranscriptEntry
from orchestrator.agent.model import (
    AttemptStatus,
    Budget,
    BudgetLedger,
    TaskSpec,
)
from orchestrator.agent.tools import ToolContext
from orchestrator.core.events import TaskId
from orchestrator.provider.claude_cli import CliResult
from tests.provider.conftest import run


class ScriptedRunner:
    """Records the invocation; plays a scripted result."""

    def __init__(
        self, result: CliResult | None = None, *, error: BaseException | None = None
    ) -> None:
        """Script one CLI invocation."""
        self.result = result or CliResult(exit_code=0, stdout="done", stderr="")
        self.error = error
        self.calls: list[tuple[list[str], Path | None, float]] = []

    async def run(
        self, argv: Sequence[str], *, timeout_s: float, cwd: Path | None = None
    ) -> CliResult:
        """Record and reply."""
        self.calls.append((list(argv), cwd, timeout_s))
        if self.error is not None:
            raise self.error
        return self.result


def spec(**overrides: Any) -> TaskSpec:
    """A minimal task spec."""
    fields: dict[str, Any] = {
        "task_id": TaskId.generate(),
        "title": "Make saving atomic",
        "prompt": "Replace the truncate-then-write with a temp file and rename.",
    }
    fields.update(overrides)
    return TaskSpec(**fields)


def ledger(seconds: float = 600.0, tool_calls: int = 10) -> BudgetLedger:
    """A ledger with room unless a test says otherwise."""
    return BudgetLedger(
        Budget(seconds=seconds, tokens=2_000_000, tool_calls=tool_calls)
    )


def harness(
    runner: ScriptedRunner, changed: Sequence[str] = ("src/a.py",), **config: Any
) -> ClaudeCodeHarness:
    """A harness over a scripted runner and a fixed changed-file listing."""
    return ClaudeCodeHarness(
        HarnessConfig(**config), runner=runner, list_changed=lambda _root: list(changed)
    )


class TestInvocation:
    """What the CLI is actually asked to do."""

    def test_it_runs_in_the_worktree(self, tmp_dir: Path) -> None:
        """The cwd *is* the blast radius; nothing else confines the harness."""
        runner = ScriptedRunner()
        result = run(
            harness(runner).run(spec(), ToolContext(workspace_root=tmp_dir), ledger())
        )

        assert result.status is AttemptStatus.SUCCEEDED
        _argv, cwd, _timeout = runner.calls[0]
        assert cwd == tmp_dir

    def test_edits_are_permitted_without_prompting(self, tmp_dir: Path) -> None:
        """An interactive permission prompt would hang until the timeout."""
        runner = ScriptedRunner()
        run(harness(runner).run(spec(), ToolContext(workspace_root=tmp_dir), ledger()))

        argv = runner.calls[0][0]
        assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"

    def test_bash_is_not_an_allowed_tool(self, tmp_dir: Path) -> None:
        """The engine runs the suite; the harness does not need a shell."""
        runner = ScriptedRunner()
        run(harness(runner).run(spec(), ToolContext(workspace_root=tmp_dir), ledger()))

        argv = runner.calls[0][0]
        tools = argv[argv.index("--allowedTools") + 1 :]
        assert "Bash" not in tools
        assert "Edit" in tools and "Write" in tools

    def test_the_prompt_forbids_committing_and_gating(self, tmp_dir: Path) -> None:
        """The agent must not accept its own work, nor pre-empt the gate."""
        runner = ScriptedRunner()
        run(harness(runner).run(spec(), ToolContext(workspace_root=tmp_dir), ledger()))

        prompt = runner.calls[0][0][2]
        assert "Do not commit" in prompt
        assert "Do not run the test suite" in prompt

    def test_feedback_from_a_failed_attempt_is_carried(self, tmp_dir: Path) -> None:
        """FR-2.5: attempt n+1 is told why attempt n failed."""
        runner = ScriptedRunner()
        run(
            harness(runner).run(
                spec(feedback=("tests failed: test_atomic_save",)),
                ToolContext(workspace_root=tmp_dir),
                ledger(),
            )
        )

        prompt = runner.calls[0][0][2]
        assert "previous attempt failed" in prompt
        assert "test_atomic_save" in prompt


class TestOutcomes:
    """Every ending is a result the workflow can record, never an exception."""

    def test_success_reports_the_changed_files(self, tmp_dir: Path) -> None:
        """The engine cannot know what the harness touched; it asks Git."""
        result = run(
            harness(ScriptedRunner(), changed=("src/a.py", "tests/test_a.py")).run(
                spec(), ToolContext(workspace_root=tmp_dir), ledger()
            )
        )
        assert result.status is AttemptStatus.SUCCEEDED
        assert result.changed_files == ("src/a.py", "tests/test_a.py")

    def test_usage_is_estimated_and_labelled(self, tmp_dir: Path) -> None:
        """OP-004: the CLI reports no usage, so the estimate says so."""
        result = run(
            harness(ScriptedRunner()).run(
                spec(), ToolContext(workspace_root=tmp_dir), ledger()
            )
        )
        assert result.usage.estimated is True
        assert result.usage.cost_usd is None
        assert result.usage.total_tokens > 0

    def test_a_timeout_preserves_partial_work(self, tmp_dir: Path) -> None:
        """FR-2.7: whatever it managed stays in the worktree."""
        runner = ScriptedRunner(CliResult(-1, "", "", timed_out=True))
        result = run(
            harness(runner, changed=("src/half.py",)).run(
                spec(), ToolContext(workspace_root=tmp_dir), ledger()
            )
        )

        assert result.status is AttemptStatus.BUDGET_EXHAUSTED
        assert result.error_code == "timeout"
        assert result.changed_files == ("src/half.py",)

    def test_an_expired_login_is_named(self, tmp_dir: Path) -> None:
        """An operator must be told to run /login, not left guessing."""
        runner = ScriptedRunner(CliResult(1, "", "Failed to authenticate: OAuth"))
        result = run(
            harness(runner).run(spec(), ToolContext(workspace_root=tmp_dir), ledger())
        )

        assert result.status is AttemptStatus.ERRORED
        assert result.error_code == "auth_failed"
        assert "/login" in result.summary

    def test_a_missing_executable_is_a_result_not_a_crash(self, tmp_dir: Path) -> None:
        """An adapter reports; it does not raise through the workflow."""
        runner = ScriptedRunner(error=FileNotFoundError("claude"))
        result = run(
            harness(runner).run(spec(), ToolContext(workspace_root=tmp_dir), ledger())
        )

        assert result.status is AttemptStatus.ERRORED
        assert result.error_code == "unavailable"

    def test_an_exhausted_budget_stops_before_spending(self, tmp_dir: Path) -> None:
        """A ledger with no tool calls left never reaches the CLI."""
        runner = ScriptedRunner()
        spent = ledger(tool_calls=1)
        spent.record_tool_call()

        result = run(
            harness(runner).run(spec(), ToolContext(workspace_root=tmp_dir), spent)
        )

        assert result.status is AttemptStatus.BUDGET_EXHAUSTED
        assert runner.calls == [], "the CLI was invoked despite an exhausted budget"

    def test_the_timeout_never_outlives_the_wall_clock_budget(
        self, tmp_dir: Path
    ) -> None:
        """A harness outliving its budget leaves an orphan holding the worktree."""
        runner = ScriptedRunner()
        run(
            harness(runner, timeout_s=99_999.0).run(
                spec(), ToolContext(workspace_root=tmp_dir), ledger(seconds=30.0)
            )
        )

        _argv, _cwd, timeout = runner.calls[0]
        assert timeout <= 30.0


class TestTranscript:
    """What the harness gives up in resolution, it still records."""

    def test_the_prompt_and_reply_are_transcribed(self, tmp_dir: Path) -> None:
        """Less resolution than a tool loop, but not nothing."""
        entries: list[TranscriptEntry] = []
        run(
            harness(ScriptedRunner(CliResult(0, "I edited save().", ""))).run(
                spec(),
                ToolContext(workspace_root=tmp_dir),
                ledger(),
                transcript_sink=entries.append,
            )
        )

        contents = [entry.content for entry in entries]
        assert any("Do not commit" in text for text in contents)
        assert any("I edited save()." in text for text in contents)

    def test_a_failure_transcribes_its_stderr(self, tmp_dir: Path) -> None:
        """A failed attempt must be diagnosable from the transcript alone."""
        entries: list[TranscriptEntry] = []
        run(
            harness(ScriptedRunner(CliResult(2, "", "exploded"))).run(
                spec(),
                ToolContext(workspace_root=tmp_dir),
                ledger(),
                transcript_sink=entries.append,
            )
        )
        assert any("exploded" in entry.content for entry in entries)


class TestExecutorCompatibility:
    """The seam is the signature: the executor must be able to hold either."""

    def test_it_matches_the_agent_runtime_call(self) -> None:
        """StepExecutor calls run(spec, ctx, ledger, transcript_sink=...)."""
        import inspect

        from orchestrator.agent.runtime import AgentRuntime

        harness_sig = inspect.signature(ClaudeCodeHarness.run)
        runtime_sig = inspect.signature(AgentRuntime.run)
        assert list(harness_sig.parameters) == list(runtime_sig.parameters)


@pytest.mark.live
class TestLive:
    """Opt-in only; spends a real subscription call. Never required."""

    def test_it_edits_a_real_file(self, tmp_dir: Path) -> None:
        """The probe that justified this adapter, as a repeatable test."""
        import os

        if not os.environ.get("ORCHESTRATORPRO_TEST_CLAUDE_CLI"):
            pytest.skip("set ORCHESTRATORPRO_TEST_CLAUDE_CLI=1 to run")

        (tmp_dir / "target.txt").write_text("original", encoding="utf-8")
        result = run(
            ClaudeCodeHarness(HarnessConfig(timeout_s=180.0)).run(
                spec(
                    title="Patch the file",
                    prompt=(
                        "Replace the contents of target.txt with exactly the "
                        "word: patched. Then stop."
                    ),
                ),
                ToolContext(workspace_root=tmp_dir),
                ledger(),
            )
        )

        assert result.status is AttemptStatus.SUCCEEDED
        assert (tmp_dir / "target.txt").read_text(encoding="utf-8").strip() == "patched"


class TestExecutableResolution:
    """The Windows shim problem that failed the first real run."""

    def test_the_executable_is_resolved_to_a_launchable_path(
        self, tmp_dir: Path
    ) -> None:
        """`Popen(["claude"])` fails where a shell succeeds.

        npm installs the CLI on Windows as `claude.CMD`; CreateProcess
        searches PATH but only appends `.exe`, so the bare name is "not
        found" on a machine where `claude` works perfectly in a terminal.
        """
        runner = ScriptedRunner()
        run(harness(runner).run(spec(), ToolContext(workspace_root=tmp_dir), ledger()))

        executable = runner.calls[0][0][0]
        # On a machine with the CLI installed this is an absolute path; on one
        # without, it falls back to the bare name so the caller's own
        # not-found handling produces the error.
        assert executable.endswith("claude") or "claude" in executable.lower()

    def test_an_unresolvable_name_is_returned_unchanged(self) -> None:
        """The helper never invents an error; the caller owns that."""
        from orchestrator.provider.claude_cli import resolve_executable

        assert resolve_executable("definitely-not-a-real-binary-xyz") == (
            "definitely-not-a-real-binary-xyz"
        )


class TestShimResolution:
    """The silent truncation that produced a vacuous green run.

    A `.cmd` shim passes its arguments through `cmd.exe` as `%*`, which cuts
    every argument at its first newline WITHOUT error. A multi-line prompt
    arrives as its opening line, so the agent gets a preamble with no task and
    reports — correctly, from its side — that no instruction was given. The
    run then went green because nothing changed and the unchanged suite passes.
    """

    def test_a_batch_shim_resolves_to_its_target(self, tmp_dir: Path) -> None:
        """Resolution must not stop at the shim."""
        from orchestrator.provider.claude_cli import resolve_executable

        real = tmp_dir / "real-tool.exe"
        real.write_bytes(b"")
        shim = tmp_dir / "tool.cmd"
        # The shape npm actually writes, %dp0% indirection included. The
        # separator is spliced in rather than escaped, so no editing accident
        # can silently turn it into a control character — which is exactly
        # what happened while writing this test the first time.
        sep = chr(92)
        shim.write_text(
            "@ECHO off\r\nGOTO start\r\n:find_dp0\r\nSET dp0=%~dp0\r\n"
            f':start\r\nSETLOCAL\r\n"%dp0%{sep}real-tool.exe"   %*\r\n',
            encoding="utf-8",
        )

        assert resolve_executable(str(shim)) == str(real)

    def test_a_shim_with_no_findable_target_is_used_anyway(
        self, tmp_dir: Path
    ) -> None:
        """Refusing to run an installed CLI is worse than a truncated prompt."""
        from orchestrator.provider.claude_cli import resolve_executable

        sep = chr(92)
        shim = tmp_dir / "orphan.cmd"
        shim.write_text(
            f'@ECHO off\r\n"C:{sep}nowhere{sep}missing.exe" %*\r\n', encoding="utf-8"
        )

        assert resolve_executable(str(shim)) == str(shim)

    def test_a_real_executable_is_returned_untouched(self, tmp_dir: Path) -> None:
        """Only shims are followed; a real binary is already the target."""
        from orchestrator.provider.claude_cli import resolve_executable

        exe = tmp_dir / "plain.exe"
        exe.write_bytes(b"")
        assert resolve_executable(str(exe)) == str(exe)

    def test_a_multiline_argument_survives_a_direct_executable(
        self, tmp_dir: Path
    ) -> None:
        """The property the fix exists to preserve, proven end to end.

        Runs a real process (python, not the CLI) with a multi-line argument
        and asserts every line arrived. Through a `.cmd` shim this fails.
        """
        import json
        import sys

        from orchestrator.provider.claude_cli import SubprocessCliRunner

        dump = tmp_dir / "dump.py"
        out = tmp_dir / "argv.json"
        dump.write_text(
            "import sys, json, pathlib\n"
            f"pathlib.Path(r'{out}').write_text(json.dumps(sys.argv[1:]), encoding='utf-8')\n",
            encoding="utf-8",
        )
        prompt = "line one\n\n# Task: the real instruction\n\nline three"

        result = run(
            SubprocessCliRunner().run(
                [sys.executable, str(dump), "-p", prompt], timeout_s=60, cwd=tmp_dir
            )
        )

        assert result.exit_code == 0, result.stderr
        received = json.loads(out.read_text(encoding="utf-8"))
        assert received[-1] == prompt, "the multi-line argument was truncated"


class TestPermissionScoping:
    """Write access is granted, and granted narrowly.

    The operator's requirement: the narrowest mechanism that works. The
    spawned agent gets `--permission-mode acceptEdits` plus an explicit
    `--add-dir` for its own worktree, and nothing wider — no
    `--dangerously-skip-permissions`, which would drop every check for the
    whole process rather than scoping one directory.
    """

    def test_the_worktree_is_granted_explicitly(self, tmp_dir: Path) -> None:
        """--add-dir names the attempt's own worktree and nothing else."""
        runner = ScriptedRunner()
        run(harness(runner).run(spec(), ToolContext(workspace_root=tmp_dir), ledger()))

        argv = runner.calls[0][0]
        assert "--add-dir" in argv
        assert argv[argv.index("--add-dir") + 1] == str(tmp_dir)

    def test_permissions_are_never_skipped_wholesale(self, tmp_dir: Path) -> None:
        """The blunt instrument must not appear at all."""
        runner = ScriptedRunner()
        run(harness(runner).run(spec(), ToolContext(workspace_root=tmp_dir), ledger()))

        argv = [part.lower() for part in runner.calls[0][0]]
        assert not any("dangerously" in part for part in argv)
        assert not any("bypasspermissions" in part for part in argv)

    def test_the_edit_tools_are_granted(self, tmp_dir: Path) -> None:
        """Editing is the whole point; the grant must actually include it."""
        runner = ScriptedRunner()
        run(harness(runner).run(spec(), ToolContext(workspace_root=tmp_dir), ledger()))

        argv = runner.calls[0][0]
        tools = argv[argv.index("--allowedTools") + 1 :]
        assert {"Read", "Edit", "Write"} <= set(tools)


def envelope_json(
    *,
    text: str = "done",
    usage: dict[str, Any] | None = None,
    cost: float | None = 0.0421,
    denials: list[Any] | None = None,
    is_error: bool = False,
) -> str:
    """Render a CLI `--output-format json` envelope, shaped like the real one."""
    body: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": is_error,
        "result": text,
        "permission_denials": denials if denials is not None else [],
    }
    if cost is not None:
        body["total_cost_usd"] = cost
    if usage is not None:
        body["usage"] = usage
    return json.dumps(body)


#: The usage block from the probe that motivated OP-014, to scale.
PROBE_USAGE = {
    "input_tokens": 2,
    "cache_creation_input_tokens": 5967,
    "cache_read_input_tokens": 20570,
    "output_tokens": 22,
}


class TestMeasuredUsage:
    """OP-014: the CLI reports what it consumed; we no longer guess."""

    def test_the_envelope_is_requested(self, tmp_dir: Path) -> None:
        """Text mode gave nothing to count. That was the whole problem."""
        runner = ScriptedRunner()
        argv = harness(runner).build_argv("do the thing")
        assert argv[argv.index("--output-format") + 1] == "json"

    def test_measured_tokens_are_not_labelled_estimated(self, tmp_dir: Path) -> None:
        runner = ScriptedRunner(
            CliResult(
                exit_code=0,
                stdout=envelope_json(text="all done", usage=PROBE_USAGE),
                stderr="",
            )
        )
        result = run(harness(runner).run(spec(), ToolContext(workspace_root=tmp_dir), ledger()))

        assert result.status is AttemptStatus.SUCCEEDED
        assert result.usage.estimated is False
        assert result.usage.output_tokens == 22
        # Everything the model read, not the uncached remainder: 2 fresh plus
        # 5967 written to cache plus 20570 read from it. Budgeting on the `2`
        # the CLI calls `input_tokens` would never bind.
        assert result.usage.input_tokens == 2 + 5967 + 20570
        assert result.summary.startswith("all done")

    def test_the_text_comes_from_the_envelope_not_raw_stdout(self, tmp_dir: Path) -> None:
        """A JSON blob must never become the attempt's summary."""
        runner = ScriptedRunner(
            CliResult(
                exit_code=0,
                stdout=envelope_json(text="rewrote save()", usage=PROBE_USAGE),
                stderr="",
            )
        )
        result = run(harness(runner).run(spec(), ToolContext(workspace_root=tmp_dir), ledger()))

        assert result.summary == "rewrote save()"
        assert "input_tokens" not in result.summary

    def test_cost_is_never_reported_as_a_charge(self, tmp_dir: Path) -> None:
        """A subscription call costs nothing marginal, whatever the CLI says.

        Surfacing the envelope's figure as `cost_usd` would tell an operator
        they spent money they did not spend — the $0.00 error inverted.
        """
        runner = ScriptedRunner(
            CliResult(
                exit_code=0,
                stdout=envelope_json(usage=PROBE_USAGE, cost=0.14103),
                stderr="",
            )
        )
        result = run(harness(runner).run(spec(), ToolContext(workspace_root=tmp_dir), ledger()))

        assert result.usage.cost_usd is None
        assert result.detail["notional_cost_usd"] == 0.14103


class TestUsageDegradesRatherThanFails:
    """A reporting detail must never fail an attempt that did the work."""

    def test_a_missing_usage_block_falls_back_to_estimates(self, tmp_dir: Path) -> None:
        runner = ScriptedRunner(
            CliResult(exit_code=0, stdout=envelope_json(text="done", usage=None), stderr="")
        )
        result = run(harness(runner).run(spec(), ToolContext(workspace_root=tmp_dir), ledger()))

        assert result.status is AttemptStatus.SUCCEEDED
        assert result.usage.estimated is True
        # Never zero. Zero is a measurement, and a false one about a call that
        # produced an answer.
        assert result.usage.input_tokens > 0
        assert result.usage.output_tokens > 0
        assert result.summary == "done"

    def test_an_unreadable_envelope_falls_back_to_estimates(self, tmp_dir: Path) -> None:
        """A truncated write, or a version that ignored the flag."""
        runner = ScriptedRunner(
            CliResult(exit_code=0, stdout='{"result": "half a jso', stderr="")
        )
        result = run(harness(runner).run(spec(), ToolContext(workspace_root=tmp_dir), ledger()))

        assert result.status is AttemptStatus.SUCCEEDED
        assert result.usage.estimated is True
        assert result.usage.input_tokens > 0

    def test_plain_text_output_still_works(self, tmp_dir: Path) -> None:
        """An older CLI that does not know the flag is not a failed attempt."""
        runner = ScriptedRunner(
            CliResult(exit_code=0, stdout="I rewrote save()", stderr="")
        )
        result = run(harness(runner).run(spec(), ToolContext(workspace_root=tmp_dir), ledger()))

        assert result.status is AttemptStatus.SUCCEEDED
        assert result.summary == "I rewrote save()"
        assert result.usage.estimated is True


class TestPermissionDenialsAreRecorded:
    """The run-3 failure mode, made diagnosable.

    An agent refused a tool, changed nothing, exited zero, and the gate then
    verified an unchanged tree. Diagnosing that cost two runs and an
    argv-dumping shim. The CLI was willing to say so the whole time.
    """

    def test_denials_reach_the_attempt_detail(self, tmp_dir: Path) -> None:
        runner = ScriptedRunner(
            CliResult(
                exit_code=0,
                stdout=envelope_json(
                    text="I was not allowed to edit anything.",
                    usage=PROBE_USAGE,
                    denials=[{"tool_name": "Edit"}, {"tool_name": "Write"}],
                ),
                stderr="",
            )
        )
        result = run(harness(runner, changed=()).run(spec(), ToolContext(workspace_root=tmp_dir), ledger()))

        assert result.detail["permission_denials"] == ["Edit", "Write"]

    def test_denials_reach_the_transcript(self, tmp_dir: Path) -> None:
        """A reviewer reading the transcript sees the cause, not just silence."""
        entries: list[Any] = []
        runner = ScriptedRunner(
            CliResult(
                exit_code=0,
                stdout=envelope_json(
                    text="blocked", usage=PROBE_USAGE, denials=[{"tool_name": "Write"}]
                ),
                stderr="",
            )
        )
        run(
            harness(runner, changed=()).run(
                spec(), ToolContext(workspace_root=tmp_dir), ledger(), transcript_sink=entries.append
            )
        )

        assert any("refused these tools" in e.content for e in entries), [
            e.content for e in entries
        ]
        assert any("Write" in e.content for e in entries)

    def test_no_denials_adds_nothing(self, tmp_dir: Path) -> None:
        """The ordinary case stays clean: absent, not an empty list."""
        runner = ScriptedRunner(
            CliResult(exit_code=0, stdout=envelope_json(usage=PROBE_USAGE), stderr="")
        )
        result = run(harness(runner).run(spec(), ToolContext(workspace_root=tmp_dir), ledger()))

        assert "permission_denials" not in result.detail


class TestModelSelection:
    """The agent's model is a decision the engine makes, not one it inherits."""

    def test_sonnet_is_the_default(self, tmp_dir: Path) -> None:
        """Named, so it is recorded and reproducible.

        Before 2026-07-28 this was empty and no ``--model`` was passed, which
        sounds neutral: it silently took whatever the operator's CLI was set to
        — ``claude-fable-5`` in run 5 — so the engine's behaviour depended on a
        setting outside it that nothing recorded.
        """
        argv = harness(ScriptedRunner()).build_argv("do it")
        assert argv[argv.index("--model") + 1] == "sonnet"

    def test_a_stronger_model_can_be_asked_for(self, tmp_dir: Path) -> None:
        """The per-workflow override, for a genuinely hard step."""
        argv = harness(ScriptedRunner(), model="opus").build_argv("do it")
        assert argv[argv.index("--model") + 1] == "opus"

    def test_an_empty_model_passes_no_flag_at_all(self, tmp_dir: Path) -> None:
        """Empty still means "let the CLI decide" — the old behaviour, on request."""
        argv = harness(ScriptedRunner(), model="").build_argv("do it")
        assert "--model" not in argv

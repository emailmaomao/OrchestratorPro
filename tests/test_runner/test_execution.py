"""Tests for command execution, timeouts, and process-group termination."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from orchestrator.test_runner.execution import (
    ExecutionError,
    ProcessResult,
    ScriptedRunner,
    SubprocessRunner,
    split_command,
)

from tests.test_runner.conftest import process, run


class TestCommandSplitting:
    """Commands are split without a shell."""

    def test_a_simple_command(self) -> None:
        assert split_command("pytest -q") == ["pytest", "-q"]

    def test_quoted_arguments_stay_together(self) -> None:
        argv = split_command('pytest -k "test one"')
        assert argv[-1] == "test one"

    def test_an_empty_command_is_refused(self) -> None:
        with pytest.raises(ExecutionError, match="empty"):
            split_command("   ")

    def test_unbalanced_quoting_is_reported(self) -> None:
        with pytest.raises(ExecutionError, match="could not parse"):
            split_command('pytest -k "unclosed')

    def test_shell_metacharacters_are_not_interpreted(self) -> None:
        """No shell is involved, so these are literal arguments."""
        argv = split_command("pytest && rm -rf /")
        assert "&&" in argv


class TestProcessResult:
    """The value type carrying what a run produced."""

    def test_ok_requires_zero_and_no_timeout(self) -> None:
        assert process(0).ok
        assert not process(1).ok
        assert not process(0, timed_out=True).ok

    def test_combined_output_joins_streams(self) -> None:
        result = process(0, stdout="out", stderr="err")
        assert "out" in result.combined_output
        assert "err" in result.combined_output

    def test_combined_output_with_one_stream(self) -> None:
        assert process(0, stdout="only").combined_output == "only"
        assert process(0, stderr="only").combined_output == "only"


class TestScriptedRunner:
    """The mocked process runner every other test relies on."""

    def test_it_returns_queued_results_in_order(self, workdir: Path) -> None:
        runner = ScriptedRunner([process(0, "first"), process(1, "second")])
        assert run(runner.run("cmd", cwd=workdir)).stdout == "first"
        assert run(runner.run("cmd", cwd=workdir)).stdout == "second"

    def test_it_falls_back_to_the_default(self, workdir: Path) -> None:
        runner = ScriptedRunner(default=process(3, "fallback"))
        assert run(runner.run("cmd", cwd=workdir)).exit_code == 3

    def test_it_records_invocations(self, workdir: Path) -> None:
        runner = ScriptedRunner()
        run(runner.run("pytest -q", cwd=workdir, timeout_s=42.0, env={"A": "1"}))

        assert len(runner.runs) == 1
        recorded = runner.runs[0]
        assert recorded.command == "pytest -q"
        assert recorded.cwd == workdir
        assert recorded.timeout_s == 42.0
        assert recorded.env == {"A": "1"}


class TestSubprocessRunner:
    """The real runner, exercised with short-lived commands."""

    def test_a_missing_directory_is_refused(self, tmp_dir: Path) -> None:
        with pytest.raises(ExecutionError, match="not a directory"):
            run(SubprocessRunner().run("echo hi", cwd=tmp_dir / "nope"))

    def test_a_missing_executable_is_reported(self, workdir: Path) -> None:
        """A missing runner is a harness problem, surfaced as such."""
        with pytest.raises(ExecutionError, match="was not found"):
            run(SubprocessRunner().run("definitely-not-a-real-binary-xyz", cwd=workdir))

    def test_a_successful_command_captures_stdout(self, workdir: Path) -> None:
        result = run(
            SubprocessRunner().run(
                f'"{sys.executable}" -c "print(\'hello\')"', cwd=workdir
            )
        )
        assert result.exit_code == 0
        assert "hello" in result.stdout
        assert not result.timed_out

    def test_a_failing_command_reports_its_exit_code(self, workdir: Path) -> None:
        result = run(
            SubprocessRunner().run(
                f'"{sys.executable}" -c "import sys; sys.exit(3)"', cwd=workdir
            )
        )
        assert result.exit_code == 3
        assert not result.ok

    def test_stderr_is_captured_separately(self, workdir: Path) -> None:
        result = run(
            SubprocessRunner().run(
                f'"{sys.executable}" -c "import sys; sys.stderr.write(\'bad\')"',
                cwd=workdir,
            )
        )
        assert "bad" in result.stderr

    def test_the_environment_is_extended(self, workdir: Path) -> None:
        result = run(
            SubprocessRunner().run(
                f'"{sys.executable}" -c "import os; print(os.environ[\'OP_MARKER\'])"',
                cwd=workdir,
                env={"OP_MARKER": "present"},
            )
        )
        assert "present" in result.stdout

    def test_a_hanging_command_is_killed_at_the_timeout(self, workdir: Path) -> None:
        """FR-4.5: the limit is enforced, and reported rather than raised."""
        started = time.monotonic()
        result = run(
            SubprocessRunner().run(
                f'"{sys.executable}" -c "import time; time.sleep(30)"',
                cwd=workdir,
                timeout_s=1.0,
            )
        )
        elapsed = time.monotonic() - started

        assert result.timed_out
        assert not result.ok
        assert elapsed < 20.0

    def test_a_timeout_kills_spawned_children(self, workdir: Path) -> None:
        """Killing only the parent leaves the child holding the worktree open."""
        marker = workdir / "child-finished.txt"
        child = workdir / "child.py"
        child.write_text(
            "import sys, time\n"
            "from pathlib import Path\n"
            "time.sleep(20)\n"
            "Path(sys.argv[1]).write_text('survived')\n",
            encoding="utf-8",
        )
        parent = workdir / "parent.py"
        parent.write_text(
            "import subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
            "time.sleep(20)\n",
            encoding="utf-8",
        )

        result = run(
            SubprocessRunner().run(
                f'"{sys.executable}" "{parent}" "{child}" "{marker}"',
                cwd=workdir,
                timeout_s=2.0,
            )
        )
        assert result.timed_out

        # Give the orphan far longer than it needs, if it survived at all.
        time.sleep(3.0)
        assert not marker.exists(), "a spawned child outlived its process group"

    def test_duration_is_measured(self, workdir: Path) -> None:
        result = run(
            SubprocessRunner().run(
                f'"{sys.executable}" -c "import time; time.sleep(0.2)"', cwd=workdir
            )
        )
        assert result.duration_s >= 0.15

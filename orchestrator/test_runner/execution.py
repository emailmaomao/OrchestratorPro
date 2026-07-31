"""Running a test command with a real timeout.

The hard part is not starting a process; it is stopping one. A test suite that
hangs will not be stopped by killing the command you launched, because the thing
that hung is usually a child of it — a spawned server, a pytest-xdist worker, a
container. Kill only the parent and the child keeps the worktree open, and the
next attempt fails for a reason that has nothing to do with its code.

So :class:`SubprocessRunner` puts each command in its own process group (or
Windows job-style group) and kills the **whole group** on timeout (FR-4.5).

A timeout is reported, never raised. Exceeding the limit is an outcome of the
run — the workflow engine needs to record it and move on, not catch an
exception thrown from three layers down.

Commands are split with :func:`shlex.split` and executed **without a shell**.
The command comes from operator configuration rather than from an agent, but
there is no reason to leave a shell in the path when nothing needs one.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol

from orchestrator.test_runner.base import TestRunnerError

__all__ = [
    "ExecutionError",
    "ProcessResult",
    "ProcessRunner",
    "SubprocessRunner",
    "split_command",
]

#: Grace period between asking a process group to stop and killing it outright.
_TERMINATE_GRACE_S: Final = 5.0

#: How long to wait for output after killing a group before giving up on it.
_DRAIN_TIMEOUT_S: Final = 10.0


class ExecutionError(TestRunnerError):
    """A test command could not be launched."""

    code = "test_execution"
    retryable = False


def _unquote(token: str) -> str:
    """Strip one layer of matching surrounding quotes from a token."""
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    return token


def split_command(command: str) -> list[str]:
    """Split a command line into argv without invoking a shell.

    Splitting is done in non-POSIX mode and quotes are stripped afterwards.
    POSIX mode would consume backslashes, which mangles every Windows path;
    non-POSIX mode alone leaves the quotes attached to the token, which makes
    ``"C:\\Program Files\\python.exe"`` an executable that does not exist. Doing
    both gives correct handling of quoted arguments *and* of native paths.

    Args:
        command: The command line.

    Returns:
        The argument vector.

    Raises:
        ExecutionError: If the command is empty or has unbalanced quoting.
    """
    try:
        argv = [_unquote(token) for token in shlex.split(command, posix=False)]
    except ValueError as exc:
        raise ExecutionError(
            f"could not parse the test command {command!r}: {exc}",
            detail={"command": command},
        ) from exc
    if not argv:
        raise ExecutionError(
            "the test command is empty", detail={"command": command}
        )
    return argv


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """What one finished (or killed) process produced."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    timed_out: bool = False
    command: str = ""

    @property
    def ok(self) -> bool:
        """Whether the process exited zero and was not killed."""
        return self.exit_code == 0 and not self.timed_out

    @property
    def combined_output(self) -> str:
        """Standard output followed by standard error."""
        if self.stdout and self.stderr:
            return f"{self.stdout}\n{self.stderr}"
        return self.stdout or self.stderr


class ProcessRunner(Protocol):
    """Executes a command in a directory. The seam that keeps tests offline."""

    async def run(
        self,
        command: str,
        *,
        cwd: Path,
        timeout_s: float = 900.0,
        env: Mapping[str, str] | None = None,
    ) -> ProcessResult:
        """Run ``command`` and return its result."""
        ...


def _spawn_kwargs() -> dict[str, object]:
    """Return the platform's flags for putting a child in its own group."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _kill_group(process: subprocess.Popen[str]) -> None:
    """Terminate a process and everything it spawned.

    Best effort by necessity — a process can die between the check and the
    signal — but it must never raise, because it runs on the timeout path where
    something has already gone wrong.
    """
    if process.poll() is not None:
        return

    if sys.platform == "win32":
        # taskkill /T walks the child tree, which CREATE_NEW_PROCESS_GROUP
        # alone does not give us on Windows.
        subprocess.run(  # noqa: S603 - argv only, never a shell
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],  # noqa: S607 - system tool
            capture_output=True,
            check=False,
        )
    else:
        try:
            group = os.getpgid(process.pid)
        except ProcessLookupError:
            return
        try:
            os.killpg(group, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        try:
            process.wait(timeout=_TERMINATE_GRACE_S)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(group, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            return

    try:
        process.wait(timeout=_TERMINATE_GRACE_S)
    except subprocess.TimeoutExpired:  # pragma: no cover - the OS has given up too
        pass


def _run_blocking(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout_s: float,
    env: Mapping[str, str] | None,
    command: str,
) -> ProcessResult:
    """Run a command synchronously, killing its whole group on timeout."""
    environment = {**os.environ, **(env or {})}
    started = time.monotonic()

    try:
        process = subprocess.Popen(  # noqa: S603 - argv is a list; no shell
            list(argv),
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
            **_spawn_kwargs(),  # type: ignore[arg-type]
        )
    except FileNotFoundError as exc:
        raise ExecutionError(
            f"could not run {argv[0]!r}: the executable was not found",
            detail={"command": command, "cwd": str(cwd)},
        ) from exc
    except OSError as exc:
        raise ExecutionError(
            f"could not launch {command!r}: {exc}",
            detail={"command": command, "cwd": str(cwd)},
        ) from exc

    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(process)
        try:
            stdout, stderr = process.communicate(timeout=_DRAIN_TIMEOUT_S)
        except subprocess.TimeoutExpired:  # pragma: no cover - pipes wedged
            stdout, stderr = "", ""

    return ProcessResult(
        exit_code=process.returncode if process.returncode is not None else -1,
        stdout=stdout or "",
        stderr=stderr or "",
        duration_s=time.monotonic() - started,
        timed_out=timed_out,
        command=command,
    )


class SubprocessRunner:
    """Runs test commands as subprocesses, in their own process group."""

    __slots__ = ()

    async def run(
        self,
        command: str,
        *,
        cwd: Path,
        timeout_s: float = 900.0,
        env: Mapping[str, str] | None = None,
    ) -> ProcessResult:
        """Run a command, killing its process group if it overruns.

        Args:
            command: The command line, split without a shell.
            cwd: The directory to run in.
            timeout_s: Wall-clock limit.
            env: Extra environment variables.

        Returns:
            The result. A timeout is reported on the result, not raised.

        Raises:
            ExecutionError: If the command could not be launched at all.
        """
        if not cwd.is_dir():
            raise ExecutionError(
                f"cannot run tests in {cwd}: it is not a directory",
                detail={"cwd": str(cwd), "command": command},
            )
        argv = split_command(command)
        # Blocking work off the event loop, per CLAUDE.md: a synchronous wait
        # here would stall every other attempt in flight.
        return await asyncio.to_thread(
            _run_blocking,
            argv,
            cwd=cwd,
            timeout_s=timeout_s,
            env=env,
            command=command,
        )


@dataclass(slots=True)
class RecordedRun:
    """One invocation captured by :class:`ScriptedRunner`."""

    command: str
    cwd: Path
    timeout_s: float
    env: Mapping[str, str] = field(default_factory=dict)


class ScriptedRunner:
    """A runner that returns prepared results instead of launching anything.

    Shipped alongside the real runner rather than confined to the test suite:
    anything embedding OrchestratorPro needs a way to exercise gate handling
    without executing a suite, and a fake defined in ``tests/`` is not
    importable by them.
    """

    __slots__ = ("_default", "_queue", "runs")

    def __init__(
        self,
        results: Sequence[ProcessResult] = (),
        *,
        default: ProcessResult | None = None,
    ) -> None:
        self._queue = list(results)
        self._default = default or ProcessResult(exit_code=0)
        self.runs: list[RecordedRun] = []

    async def run(
        self,
        command: str,
        *,
        cwd: Path,
        timeout_s: float = 900.0,
        env: Mapping[str, str] | None = None,
    ) -> ProcessResult:
        """Record the invocation and return the next scripted result."""
        self.runs.append(
            RecordedRun(command=command, cwd=cwd, timeout_s=timeout_s, env=dict(env or {}))
        )
        if self._queue:
            return self._queue.pop(0)
        return self._default

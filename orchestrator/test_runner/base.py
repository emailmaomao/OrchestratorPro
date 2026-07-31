"""Core types for gate execution: verdicts, cases, coverage, and the protocols.

A gate is what turns "the agent says it is done" into "the project's own tests
agree" (FR-4.1, FR-4.2). Everything here exists to make one distinction
impossible to blur:

    **A failing suite and a broken harness are not the same event.**

:attr:`Outcome.FAILED` means the tests ran and some were red.
:attr:`Outcome.ERRORED` means the runner never got that far — a missing
interpreter, a collection error, a syntax error in ``conftest.py``. Reporting
the second as the first is the single most expensive mistake this package could
make (FR-4.4): it teaches an agent that the way to fix a broken harness is to
edit the tests, and the retry-with-feedback loop then optimizes for exactly that.

The types are vendor- and framework-neutral. A pytest run, a JUnit XML file, and
a bare exit code all reduce to the same :class:`Verdict`, so the workflow engine
never learns which framework produced it.

This package imports only :mod:`orchestrator.core`. It deliberately does not
import :mod:`orchestrator.task` (a layer above) or
:mod:`orchestrator.git_manager` (a sibling): a gate runs in a *directory*, and
taking a plain :class:`~pathlib.Path` keeps it usable outside a worktree and
trivially testable.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from orchestrator.core.events import OrchestratorError

__all__ = [
    "CaseResult",
    "CaseStatus",
    "CoverageReport",
    "FileCoverage",
    "GateRunner",
    "Outcome",
    "ParseError",
    "ParsedResults",
    "ResultParser",
    "SuiteSpec",
    "TestRunnerError",
    "Verdict",
]


class TestRunnerError(OrchestratorError):
    """A gate could not be executed."""

    code = "test_runner"
    retryable = False


class ParseError(TestRunnerError):
    """Test output could not be parsed into structured results."""

    code = "test_parse"
    retryable = False


class Outcome(StrEnum):
    """How a gate resolved.

    The distinction between :attr:`FAILED` and :attr:`ERRORED` is load-bearing:
    the first is a verdict about the *code*, the second about the *harness*.
    """

    PASSED = "passed"
    FAILED = "failed"
    ERRORED = "errored"
    TIMED_OUT = "timed_out"
    SKIPPED = "skipped"

    @property
    def blocks_acceptance(self) -> bool:
        """Whether this outcome must stop an attempt from being accepted.

        Everything except a clean pass blocks. A gate that errored has verified
        nothing, so it cannot stand in for a pass.
        """
        return self is not Outcome.PASSED

    @property
    def is_harness_problem(self) -> bool:
        """Whether the failure is with the runner rather than the code.

        Retry-with-feedback should say so: an agent told "tests failed" when the
        interpreter is missing will start editing tests.
        """
        return self in (Outcome.ERRORED, Outcome.TIMED_OUT)


class CaseStatus(StrEnum):
    """The status of one individual test case."""

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"
    XFAILED = "xfailed"
    XPASSED = "xpassed"

    @property
    def is_bad(self) -> bool:
        """Whether this status should count against the suite.

        ``xpassed`` counts: a test marked as expected-to-fail that passed is a
        stale expectation, and silently tolerating it lets the marker rot.
        """
        return self in (CaseStatus.FAILED, CaseStatus.ERROR, CaseStatus.XPASSED)


@dataclass(frozen=True, slots=True)
class CaseResult:
    """One test case's result.

    Attributes:
        id: The framework's identifier, e.g. ``tests/test_x.py::test_login``.
        status: How it resolved.
        name: The bare test name, when it can be separated from the path.
        file: The file the test lives in, when known.
        duration_s: How long it took, when the framework reports it.
        message: A short failure message, suitable for retry feedback.
        detail: Longer output — a traceback, a diff — for the dashboard.
    """

    id: str
    status: CaseStatus
    name: str = ""
    file: str = ""
    duration_s: float | None = None
    message: str = ""
    detail: str = ""

    @property
    def failed(self) -> bool:
        """Whether this case counts against the suite."""
        return self.status.is_bad


@dataclass(frozen=True, slots=True)
class FileCoverage:
    """Coverage for one source file."""

    path: str
    line_rate: float
    lines_covered: int = 0
    lines_valid: int = 0

    @property
    def percent(self) -> float:
        """Line coverage as a percentage."""
        return self.line_rate * 100.0


@dataclass(frozen=True, slots=True)
class CoverageReport:
    """Coverage for a whole run.

    Absent coverage is represented by *no report at all* rather than a report of
    zero. A project without coverage configured has unknown coverage, not none,
    and the difference matters when a policy gates on a threshold.
    """

    line_rate: float
    branch_rate: float | None = None
    lines_covered: int = 0
    lines_valid: int = 0
    files: tuple[FileCoverage, ...] = ()
    source: str = ""

    @property
    def percent(self) -> float:
        """Line coverage as a percentage."""
        return self.line_rate * 100.0

    def meets(self, threshold_percent: float) -> bool:
        """Whether coverage reaches a threshold expressed in percent."""
        return self.percent >= threshold_percent

    def worst_files(self, limit: int = 5) -> tuple[FileCoverage, ...]:
        """Return the least-covered files, for a report or a nudge."""
        return tuple(sorted(self.files, key=lambda f: f.line_rate)[:limit])


@dataclass(frozen=True, slots=True)
class ParsedResults:
    """What a parser extracted from one run's output."""

    cases: tuple[CaseResult, ...] = ()
    counts: Mapping[str, int] = field(default_factory=dict)
    duration_s: float | None = None
    collection_error: str = ""

    @property
    def failures(self) -> tuple[CaseResult, ...]:
        """Cases that count against the suite."""
        return tuple(case for case in self.cases if case.failed)

    @property
    def has_cases(self) -> bool:
        """Whether any case was reported at all."""
        return bool(self.cases)


@dataclass(frozen=True, slots=True)
class SuiteSpec:
    """How to run one gate.

    Attributes:
        command: The command line, split later without a shell.
        parser: Which parser interprets the output.
        timeout_s: Wall-clock limit. Exceeding it is :attr:`Outcome.TIMED_OUT`,
            never a failure of the code under test.
        name: The gate's label, used in reports and events.
        coverage: Whether to look for a coverage report afterwards.
        coverage_threshold: Minimum percent, or ``None`` to record without
            gating on it.
        env: Extra environment variables for the child process.
    """

    command: str = "pytest -q"
    parser: str = "pytest"
    timeout_s: float = 900.0
    name: str = "tests"
    coverage: bool = False
    coverage_threshold: float | None = None
    env: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.command.strip():
            raise TestRunnerError("suite.command must not be empty")
        if self.timeout_s <= 0:
            raise TestRunnerError(
                f"suite.timeout_s must be positive, got {self.timeout_s}"
            )
        if self.coverage_threshold is not None and not 0 <= self.coverage_threshold <= 100:
            raise TestRunnerError(
                f"suite.coverage_threshold must be a percent in [0, 100], got "
                f"{self.coverage_threshold}"
            )


@dataclass(frozen=True, slots=True)
class Verdict:
    """The result of running one gate."""

    outcome: Outcome
    gate: str = "tests"
    cases: tuple[CaseResult, ...] = ()
    counts: Mapping[str, int] = field(default_factory=dict)
    duration_s: float = 0.0
    exit_code: int | None = None
    command: str = ""
    output: str = ""
    coverage: CoverageReport | None = None
    reason: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        """Whether the gate was cleared."""
        return self.outcome is Outcome.PASSED

    @property
    def blocks(self) -> bool:
        """Whether this verdict must block acceptance (FR-4.2)."""
        return self.outcome.blocks_acceptance

    @property
    def failures(self) -> tuple[CaseResult, ...]:
        """The cases that counted against the suite."""
        return tuple(case for case in self.cases if case.failed)

    @property
    def failed_names(self) -> tuple[str, ...]:
        """Identifiers of the failing cases (FR-4.3)."""
        return tuple(case.id for case in self.failures)

    def summary(self) -> str:
        """A one-line account, suitable for a log or a retry prompt."""
        if self.outcome is Outcome.PASSED:
            total = self.counts.get("passed", len(self.cases))
            return f"{self.gate}: passed ({total} test(s) in {self.duration_s:.1f}s)"
        if self.outcome is Outcome.TIMED_OUT:
            return f"{self.gate}: timed out after {self.duration_s:.1f}s"
        if self.outcome is Outcome.ERRORED:
            return f"{self.gate}: the test runner itself failed — {self.reason}"
        if self.outcome is Outcome.SKIPPED:
            return f"{self.gate}: skipped — {self.reason}"
        names = ", ".join(self.failed_names[:5])
        more = "" if len(self.failed_names) <= 5 else f" (+{len(self.failed_names) - 5} more)"
        return f"{self.gate}: {len(self.failures)} test(s) failed: {names}{more}"

    def feedback(self, *, max_chars: int = 4000) -> str:
        """Render the verdict as feedback for the next attempt (FR-2.5).

        A harness problem says so explicitly, so the next attempt does not try
        to fix the code when the runner is what broke.
        """
        lines = [self.summary()]
        if self.outcome.is_harness_problem:
            lines.append(
                "This is a problem with the test harness or environment, not "
                "necessarily with the code under test. Do not modify tests to "
                "make this pass."
            )
        for case in self.failures[:10]:
            lines.append(f"- {case.id}: {case.message or case.status.value}")
            if case.detail:
                lines.append(f"  {case.detail.strip()[:400]}")
        if not self.failures and self.output:
            lines.append(self.output.strip()[:2000])
        return "\n".join(lines)[:max_chars]


class ResultParser(Protocol):
    """Turns a finished process's output into structured results."""

    name: str

    def parse(
        self, *, exit_code: int, stdout: str, stderr: str, cwd: Path
    ) -> ParsedResults:
        """Extract cases and counts from one run's output."""
        ...

    def outcome_for(self, *, exit_code: int, results: ParsedResults) -> tuple[Outcome, str]:
        """Classify the run, returning the outcome and a reason.

        This is where FR-4.4 is decided: a parser must be able to say that the
        runner broke rather than that the tests failed.
        """
        ...


class GateRunner(Protocol):
    """Runs a gate in a directory and returns a verdict."""

    async def run(self, cwd: Path, spec: SuiteSpec) -> Verdict:
        """Execute the gate and report what happened."""
        ...


def counts_from_cases(cases: Sequence[CaseResult]) -> dict[str, int]:
    """Tally case statuses, for parsers that report cases but no summary."""
    tally: dict[str, int] = {}
    for case in cases:
        tally[case.status.value] = tally.get(case.status.value, 0) + 1
    return tally

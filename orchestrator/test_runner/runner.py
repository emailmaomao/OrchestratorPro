"""The verdict engine — execution, parsing, and coverage into one answer.

This is where the pieces meet: a command is run, its output is parsed, coverage
is collected if it exists, and the whole thing collapses into a single
:class:`~orchestrator.test_runner.base.Verdict` the workflow engine can act on.

Two rules shape everything below.

**A gate that could not run never reports a pass.** A missing executable, a
suite that would not import, a run killed by its timeout — all of them produce
:attr:`~orchestrator.test_runner.base.Outcome.ERRORED` or ``TIMED_OUT``, never
``FAILED`` and certainly never ``PASSED`` (FR-4.4). The engine catches launch
failures and converts them into a verdict rather than raising, because "the
harness broke" is information the run needs recorded, not an exception thrown
past it.

**The engine reports; it does not decide policy.** Whether a failing advisory
gate should stop an attempt is the workflow engine's business (M7). Everything
here answers only "what happened", which is why this package can sit at layer 1
and be tested without a task graph, an agent, or a repository.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from orchestrator.test_runner.base import (
    CoverageReport,
    Outcome,
    ParsedResults,
    SuiteSpec,
    TestRunnerError,
    Verdict,
)
from orchestrator.test_runner.execution import (
    ExecutionError,
    ProcessResult,
    ProcessRunner,
    SubprocessRunner,
)
from orchestrator.test_runner.parsers import CoverageCollector, get_parser

__all__ = ["TestRunner", "aggregate"]

#: How much captured output to keep on a verdict. Enough for a human to see the
#: tail of a traceback; short of pasting a megabyte of pytest noise into an
#: event payload.
_OUTPUT_LIMIT = 20_000


class TestRunner:
    """Runs gates and turns them into verdicts."""

    __slots__ = ("_coverage", "_process_runner")

    def __init__(
        self,
        *,
        process_runner: ProcessRunner | None = None,
        coverage: CoverageCollector | None = None,
    ) -> None:
        """Create the runner.

        Args:
            process_runner: Executes the test command. Defaults to a real
                subprocess runner; tests inject a scripted one.
            coverage: Locates coverage reports. A default is used if omitted.
        """
        self._process_runner = process_runner or SubprocessRunner()
        self._coverage = coverage or CoverageCollector()

    @property
    def process_runner(self) -> ProcessRunner:
        """The underlying process runner."""
        return self._process_runner

    async def run(self, cwd: Path, spec: SuiteSpec) -> Verdict:
        """Run one gate and return its verdict.

        Args:
            cwd: The directory to run in — an attempt's worktree in practice,
                though the runner neither knows nor cares.
            spec: What to run, how to parse it, and how long to allow.

        Returns:
            The verdict. This method does not raise for a failing or broken
            gate; both are outcomes.
        """
        try:
            process = await self._process_runner.run(
                spec.command, cwd=cwd, timeout_s=spec.timeout_s, env=spec.env
            )
        except ExecutionError as exc:
            # The harness could not start. That is emphatically not a test
            # failure, and must never be reported as one.
            return Verdict(
                outcome=Outcome.ERRORED,
                gate=spec.name,
                command=spec.command,
                reason=str(exc),
                detail={"error_code": exc.code, **exc.detail},
            )

        if process.timed_out:
            return self._timeout_verdict(spec, process)

        return self._verdict_from(cwd, spec, process)

    def _timeout_verdict(self, spec: SuiteSpec, process: ProcessResult) -> Verdict:
        """Build the verdict for a run killed by its timeout (FR-4.5)."""
        return Verdict(
            outcome=Outcome.TIMED_OUT,
            gate=spec.name,
            command=spec.command,
            exit_code=process.exit_code,
            duration_s=process.duration_s,
            output=_truncate(process.combined_output),
            reason=(
                f"the gate exceeded its {spec.timeout_s:g}s limit and its process "
                "group was terminated"
            ),
            detail={"timeout_s": spec.timeout_s},
        )

    def _verdict_from(
        self, cwd: Path, spec: SuiteSpec, process: ProcessResult
    ) -> Verdict:
        """Parse a finished run and classify it."""
        try:
            parser = get_parser(spec.parser)
            results = parser.parse(
                exit_code=process.exit_code,
                stdout=process.stdout,
                stderr=process.stderr,
                cwd=cwd,
            )
            outcome, reason = parser.outcome_for(
                exit_code=process.exit_code, results=results
            )
        except TestRunnerError as exc:
            # The output could not be understood. Reporting that as a failure
            # would blame the code for the parser's problem.
            return Verdict(
                outcome=Outcome.ERRORED,
                gate=spec.name,
                command=spec.command,
                exit_code=process.exit_code,
                duration_s=process.duration_s,
                output=_truncate(process.combined_output),
                reason=f"the test output could not be parsed: {exc}",
                detail={"error_code": exc.code},
            )

        coverage = self._collect_coverage(cwd, spec, process)
        outcome, reason = self._apply_coverage_policy(spec, coverage, outcome, reason)

        return Verdict(
            outcome=outcome,
            gate=spec.name,
            cases=results.cases,
            counts=dict(results.counts),
            duration_s=results.duration_s or process.duration_s,
            exit_code=process.exit_code,
            command=spec.command,
            output=_truncate(process.combined_output),
            coverage=coverage,
            reason=reason,
            detail=self._detail(results, process),
        )

    def _collect_coverage(
        self, cwd: Path, spec: SuiteSpec, process: ProcessResult
    ) -> CoverageReport | None:
        """Look for a coverage report, if the gate asked for one.

        A parse failure here is swallowed deliberately: coverage is a
        measurement, and a broken measurement must not turn a green suite red.
        """
        if not spec.coverage:
            return None
        try:
            return self._coverage.collect(cwd, process.stdout)
        except TestRunnerError:
            return None

    def _apply_coverage_policy(
        self,
        spec: SuiteSpec,
        coverage: CoverageReport | None,
        outcome: Outcome,
        reason: str,
    ) -> tuple[Outcome, str]:
        """Apply a coverage threshold, if one is configured.

        Only a passing suite is judged on coverage — a failing suite already has
        a more important problem, and reporting the coverage shortfall instead
        would bury it.
        """
        if spec.coverage_threshold is None or outcome is not Outcome.PASSED:
            return outcome, reason

        if coverage is None:
            # A threshold with nothing to measure cannot be satisfied, and
            # cannot honestly be called a pass either.
            return (
                Outcome.ERRORED,
                f"a coverage threshold of {spec.coverage_threshold:g}% is configured, "
                "but the run produced no coverage report",
            )
        if not coverage.meets(spec.coverage_threshold):
            return (
                Outcome.FAILED,
                f"tests passed but coverage is {coverage.percent:.1f}%, below the "
                f"configured {spec.coverage_threshold:g}%",
            )
        return outcome, reason

    @staticmethod
    def _detail(results: ParsedResults, process: ProcessResult) -> dict[str, object]:
        """Assemble structured extras for the event payload."""
        detail: dict[str, object] = {
            "cases_reported": len(results.cases),
            "failures": len(results.failures),
        }
        if results.collection_error:
            detail["collection_error"] = results.collection_error
        if process.stderr.strip():
            detail["stderr_tail"] = process.stderr.strip()[-2000:]
        return detail

    async def run_all(
        self, cwd: Path, specs: Sequence[SuiteSpec], *, stop_on_failure: bool = False
    ) -> tuple[Verdict, ...]:
        """Run several gates in order.

        Args:
            cwd: The directory to run in.
            specs: The gates to run.
            stop_on_failure: Stop after the first blocking verdict. Useful when
                a slow integration suite should not run after a fast unit suite
                already went red.

        Returns:
            One verdict per gate attempted.
        """
        verdicts: list[Verdict] = []
        for spec in specs:
            verdict = await self.run(cwd, spec)
            verdicts.append(verdict)
            if stop_on_failure and verdict.blocks:
                break
        return tuple(verdicts)


#: Severity order, worst last. Used to collapse several gates into one answer.
_SEVERITY = (
    Outcome.PASSED,
    Outcome.SKIPPED,
    Outcome.FAILED,
    Outcome.TIMED_OUT,
    Outcome.ERRORED,
)


def aggregate(verdicts: Sequence[Verdict]) -> Outcome:
    """Collapse several verdicts into the worst outcome among them.

    ``ERRORED`` outranks ``FAILED``: when one gate is red and another could not
    run at all, the broken harness is the thing to fix first, and saying so
    keeps a retry from being aimed at the wrong problem.

    Args:
        verdicts: The verdicts to combine.

    Returns:
        The worst outcome. An empty sequence yields
        :attr:`~orchestrator.test_runner.base.Outcome.SKIPPED` — no gate ran, so
        nothing was verified, and calling that a pass would be a lie.
    """
    if not verdicts:
        return Outcome.SKIPPED
    return max((v.outcome for v in verdicts), key=_SEVERITY.index)


def _truncate(text: str, limit: int = _OUTPUT_LIMIT) -> str:
    """Trim captured output, keeping the tail where failures appear."""
    if len(text) <= limit:
        return text
    return f"[... {len(text) - limit} characters truncated ...]\n{text[-limit:]}"

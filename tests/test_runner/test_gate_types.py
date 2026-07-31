"""Tests for the core gate types and the failed/errored distinction."""

from __future__ import annotations

import pytest

from orchestrator.core.events import OrchestratorError
from orchestrator.test_runner.base import (
    CaseResult,
    CaseStatus,
    CoverageReport,
    FileCoverage,
    Outcome,
    ParsedResults,
    SuiteSpec,
    Verdict,
    counts_from_cases,
)

# Aliased on import: pytest tries to collect any module-level name beginning
# with "Test", and warns when it cannot. Renaming here keeps that concern out
# of the source module.
from orchestrator.test_runner.base import TestRunnerError as GateConfigError


class TestOutcome:
    """The distinction FR-4.4 exists to protect."""

    def test_only_passing_clears_a_gate(self) -> None:
        assert not Outcome.PASSED.blocks_acceptance
        for outcome in (Outcome.FAILED, Outcome.ERRORED, Outcome.TIMED_OUT, Outcome.SKIPPED):
            assert outcome.blocks_acceptance, outcome

    def test_errored_and_timed_out_are_harness_problems(self) -> None:
        assert Outcome.ERRORED.is_harness_problem
        assert Outcome.TIMED_OUT.is_harness_problem

    def test_a_failing_suite_is_not_a_harness_problem(self) -> None:
        """Red tests are a verdict about the code, not about the runner."""
        assert not Outcome.FAILED.is_harness_problem
        assert not Outcome.PASSED.is_harness_problem

    def test_failed_and_errored_are_distinct_values(self) -> None:
        assert Outcome.FAILED is not Outcome.ERRORED
        assert Outcome.FAILED.value != Outcome.ERRORED.value


class TestCaseStatus:
    """Which case statuses count against a suite."""

    @pytest.mark.parametrize(
        "status", [CaseStatus.FAILED, CaseStatus.ERROR, CaseStatus.XPASSED]
    )
    def test_bad_statuses(self, status: CaseStatus) -> None:
        assert status.is_bad

    @pytest.mark.parametrize(
        "status", [CaseStatus.PASSED, CaseStatus.SKIPPED, CaseStatus.XFAILED]
    )
    def test_acceptable_statuses(self, status: CaseStatus) -> None:
        assert not status.is_bad

    def test_an_unexpected_pass_counts_against_the_suite(self) -> None:
        """A stale xfail marker should be noticed, not tolerated."""
        assert CaseStatus.XPASSED.is_bad


class TestCoverageReport:
    """Coverage is a measurement with an explicit absent state."""

    def test_percent_conversion(self) -> None:
        assert CoverageReport(line_rate=0.855).percent == pytest.approx(85.5)

    def test_meets_threshold(self) -> None:
        report = CoverageReport(line_rate=0.9)
        assert report.meets(90.0)
        assert report.meets(85.0)
        assert not report.meets(95.0)

    def test_worst_files_are_ranked(self) -> None:
        report = CoverageReport(
            line_rate=0.7,
            files=(
                FileCoverage(path="good.py", line_rate=0.95),
                FileCoverage(path="bad.py", line_rate=0.10),
                FileCoverage(path="mid.py", line_rate=0.50),
            ),
        )
        assert [f.path for f in report.worst_files(2)] == ["bad.py", "mid.py"]

    def test_file_percent(self) -> None:
        assert FileCoverage(path="a.py", line_rate=0.5).percent == pytest.approx(50.0)


class TestSuiteSpec:
    """Gate configuration is validated where it is declared."""

    def test_defaults_target_pytest(self) -> None:
        spec = SuiteSpec()
        assert spec.command == "pytest -q"
        assert spec.parser == "pytest"

    def test_an_empty_command_is_refused(self) -> None:
        with pytest.raises(GateConfigError, match="must not be empty"):
            SuiteSpec(command="   ")

    @pytest.mark.parametrize("timeout", [0, -1.0])
    def test_a_non_positive_timeout_is_refused(self, timeout: float) -> None:
        with pytest.raises(GateConfigError, match="timeout_s"):
            SuiteSpec(timeout_s=timeout)

    @pytest.mark.parametrize("threshold", [-1.0, 101.0])
    def test_an_out_of_range_threshold_is_refused(self, threshold: float) -> None:
        with pytest.raises(GateConfigError, match="percent"):
            SuiteSpec(coverage_threshold=threshold)

    def test_a_valid_threshold_is_accepted(self) -> None:
        assert SuiteSpec(coverage_threshold=80.0).coverage_threshold == 80.0

    def test_errors_derive_from_the_orchestrator_base(self) -> None:
        assert issubclass(GateConfigError, OrchestratorError)


class TestVerdict:
    """What a verdict exposes to the layers above."""

    def _verdict(self, outcome: Outcome, **kwargs: object) -> Verdict:
        return Verdict(outcome=outcome, **kwargs)  # type: ignore[arg-type]

    def test_passing_does_not_block(self) -> None:
        verdict = self._verdict(Outcome.PASSED)
        assert verdict.passed
        assert not verdict.blocks

    @pytest.mark.parametrize(
        "outcome", [Outcome.FAILED, Outcome.ERRORED, Outcome.TIMED_OUT, Outcome.SKIPPED]
    )
    def test_everything_else_blocks(self, outcome: Outcome) -> None:
        """FR-4.2: only a clean pass clears a gate."""
        assert self._verdict(outcome).blocks

    def test_failed_names_lists_only_bad_cases(self) -> None:
        verdict = self._verdict(
            Outcome.FAILED,
            cases=(
                CaseResult(id="a::one", status=CaseStatus.PASSED),
                CaseResult(id="a::two", status=CaseStatus.FAILED),
                CaseResult(id="a::three", status=CaseStatus.SKIPPED),
                CaseResult(id="a::four", status=CaseStatus.ERROR),
            ),
        )
        assert verdict.failed_names == ("a::two", "a::four")

    def test_summary_of_a_pass_reports_the_count(self) -> None:
        verdict = self._verdict(
            Outcome.PASSED, counts={"passed": 12}, duration_s=1.5, gate="unit"
        )
        summary = verdict.summary()
        assert "unit" in summary
        assert "12" in summary

    def test_summary_of_a_failure_names_the_tests(self) -> None:
        verdict = self._verdict(
            Outcome.FAILED,
            cases=(CaseResult(id="tests/test_a.py::test_login", status=CaseStatus.FAILED),),
        )
        assert "tests/test_a.py::test_login" in verdict.summary()

    def test_summary_truncates_a_long_failure_list(self) -> None:
        cases = tuple(
            CaseResult(id=f"t::{index}", status=CaseStatus.FAILED) for index in range(9)
        )
        summary = self._verdict(Outcome.FAILED, cases=cases).summary()
        assert "+4 more" in summary

    def test_summary_of_an_error_says_the_runner_failed(self) -> None:
        verdict = self._verdict(Outcome.ERRORED, reason="conftest.py raised")
        assert "test runner itself failed" in verdict.summary()

    def test_summary_of_a_timeout_reports_the_duration(self) -> None:
        assert "timed out" in self._verdict(Outcome.TIMED_OUT, duration_s=900.0).summary()


class TestFeedback:
    """Retry feedback must not point the next attempt at the wrong problem."""

    def test_a_harness_problem_says_so_explicitly(self) -> None:
        """Otherwise the next attempt starts editing tests to go green."""
        verdict = Verdict(outcome=Outcome.ERRORED, reason="pytest: no such option")
        feedback = verdict.feedback()
        assert "not necessarily with the code under test" in feedback
        assert "Do not modify tests" in feedback

    def test_a_failing_suite_does_not_carry_that_warning(self) -> None:
        verdict = Verdict(
            outcome=Outcome.FAILED,
            cases=(CaseResult(id="a::b", status=CaseStatus.FAILED, message="boom"),),
        )
        assert "Do not modify tests" not in verdict.feedback()

    def test_feedback_includes_failure_messages(self) -> None:
        verdict = Verdict(
            outcome=Outcome.FAILED,
            cases=(
                CaseResult(
                    id="tests/test_a.py::test_x",
                    status=CaseStatus.FAILED,
                    message="assert 1 == 2",
                ),
            ),
        )
        feedback = verdict.feedback()
        assert "tests/test_a.py::test_x" in feedback
        assert "assert 1 == 2" in feedback

    def test_feedback_is_bounded(self) -> None:
        cases = tuple(
            CaseResult(
                id=f"t::{index}", status=CaseStatus.FAILED, detail="x" * 5_000
            )
            for index in range(50)
        )
        feedback = Verdict(outcome=Outcome.FAILED, cases=cases).feedback(max_chars=1_000)
        assert len(feedback) <= 1_000

    def test_output_is_used_when_no_cases_parsed(self) -> None:
        verdict = Verdict(
            outcome=Outcome.FAILED, output="something went wrong in the runner"
        )
        assert "something went wrong" in verdict.feedback()


def test_counts_from_cases_tallies_statuses() -> None:
    counts = counts_from_cases(
        [
            CaseResult(id="1", status=CaseStatus.PASSED),
            CaseResult(id="2", status=CaseStatus.PASSED),
            CaseResult(id="3", status=CaseStatus.FAILED),
        ]
    )
    assert counts == {"passed": 2, "failed": 1}


def test_parsed_results_expose_failures() -> None:
    results = ParsedResults(
        cases=(
            CaseResult(id="a", status=CaseStatus.PASSED),
            CaseResult(id="b", status=CaseStatus.FAILED),
        )
    )
    assert results.has_cases
    assert [case.id for case in results.failures] == ["b"]

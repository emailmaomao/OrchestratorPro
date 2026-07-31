"""Tests for the verdict engine."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from orchestrator.test_runner.base import Outcome, SuiteSpec
from orchestrator.test_runner.execution import ExecutionError, ScriptedRunner
from orchestrator.test_runner.runner import TestRunner as VerdictEngine
from orchestrator.test_runner.runner import aggregate

from tests.test_runner.conftest import (
    COLLECTION_ERROR_OUTPUT,
    COVERAGE_TERMINAL_OUTPUT,
    COVERAGE_XML,
    FAILING_OUTPUT,
    JUNIT_XML,
    NO_TESTS_OUTPUT,
    PASSING_OUTPUT,
    process,
    run,
)


def runner_for(*results: object) -> VerdictEngine:
    """Build a runner over scripted process results."""
    return VerdictEngine(process_runner=ScriptedRunner(list(results)))  # type: ignore[arg-type]


class TestHappyPath:
    """A green suite clears the gate."""

    def test_a_passing_suite_passes(self, workdir: Path) -> None:
        verdict = run(runner_for(process(0, PASSING_OUTPUT)).run(workdir, SuiteSpec()))

        assert verdict.outcome is Outcome.PASSED
        assert verdict.passed
        assert not verdict.blocks
        assert verdict.counts == {"passed": 3}
        assert verdict.exit_code == 0

    def test_the_gate_name_is_carried_through(self, workdir: Path) -> None:
        spec = SuiteSpec(name="unit-tests")
        verdict = run(runner_for(process(0, PASSING_OUTPUT)).run(workdir, spec))
        assert verdict.gate == "unit-tests"

    def test_the_command_runs_in_the_given_directory(self, workdir: Path) -> None:
        scripted = ScriptedRunner([process(0, PASSING_OUTPUT)])
        run(VerdictEngine(process_runner=scripted).run(workdir, SuiteSpec()))
        assert scripted.runs[0].cwd == workdir

    def test_the_configured_timeout_is_passed_down(self, workdir: Path) -> None:
        scripted = ScriptedRunner([process(0, PASSING_OUTPUT)])
        run(VerdictEngine(process_runner=scripted).run(workdir, SuiteSpec(timeout_s=42.0)))
        assert scripted.runs[0].timeout_s == 42.0

    def test_duration_prefers_the_frameworks_own_number(self, workdir: Path) -> None:
        verdict = run(
            runner_for(process(0, PASSING_OUTPUT, duration_s=9.9)).run(
                workdir, SuiteSpec()
            )
        )
        assert verdict.duration_s == pytest.approx(0.12)


class TestFailureVersusError:
    """FR-4.4, the distinction this package exists to protect."""

    def test_red_tests_are_a_failure(self, workdir: Path) -> None:
        verdict = run(runner_for(process(1, FAILING_OUTPUT)).run(workdir, SuiteSpec()))

        assert verdict.outcome is Outcome.FAILED
        assert verdict.blocks
        assert verdict.failed_names == ("tests/test_a.py::test_two",)
        assert not verdict.outcome.is_harness_problem

    @pytest.mark.parametrize("exit_code", [2, 3, 4])
    def test_a_broken_runner_is_an_error_not_a_failure(
        self, workdir: Path, exit_code: int
    ) -> None:
        verdict = run(
            runner_for(process(exit_code, "", "usage: pytest [options]")).run(
                workdir, SuiteSpec()
            )
        )
        assert verdict.outcome is Outcome.ERRORED
        assert verdict.outcome.is_harness_problem

    def test_a_collection_error_is_an_error(self, workdir: Path) -> None:
        verdict = run(
            runner_for(process(2, COLLECTION_ERROR_OUTPUT)).run(workdir, SuiteSpec())
        )
        assert verdict.outcome is Outcome.ERRORED
        assert "could not be collected" in verdict.reason

    def test_an_empty_suite_does_not_pass(self, workdir: Path) -> None:
        """Zero tests verified nothing; calling it green would be a lie."""
        verdict = run(runner_for(process(5, NO_TESTS_OUTPUT)).run(workdir, SuiteSpec()))
        assert verdict.outcome is Outcome.ERRORED
        assert verdict.blocks

    def test_a_missing_executable_becomes_an_errored_verdict(
        self, workdir: Path
    ) -> None:
        """The harness could not start; that is not a test failure."""

        class Failing(ScriptedRunner):
            async def run(self, command: str, **kwargs: object):  # type: ignore[override]
                raise ExecutionError("the executable was not found")

        verdict = run(VerdictEngine(process_runner=Failing()).run(workdir, SuiteSpec()))
        assert verdict.outcome is Outcome.ERRORED
        assert "not found" in verdict.reason

    def test_an_unparseable_output_is_an_error(self, workdir: Path) -> None:
        """Blaming the code for a parser problem would misdirect the retry."""
        spec = SuiteSpec(parser="junit")
        (workdir / "junit.xml").write_text("<broken", encoding="utf-8")
        verdict = run(runner_for(process(0)).run(workdir, spec))

        assert verdict.outcome is Outcome.ERRORED
        assert "could not be parsed" in verdict.reason


class TestTimeout:
    """FR-4.5: exceeding the limit is its own outcome."""

    def test_a_timeout_is_reported_as_timed_out(self, workdir: Path) -> None:
        verdict = run(
            runner_for(process(-1, timed_out=True, duration_s=900.0)).run(
                workdir, SuiteSpec(timeout_s=900.0)
            )
        )
        assert verdict.outcome is Outcome.TIMED_OUT
        assert verdict.blocks
        assert verdict.outcome.is_harness_problem

    def test_the_reason_names_the_limit(self, workdir: Path) -> None:
        verdict = run(
            runner_for(process(-1, timed_out=True)).run(
                workdir, SuiteSpec(timeout_s=30.0)
            )
        )
        assert "30s" in verdict.reason
        assert "process group" in verdict.reason

    def test_a_timeout_is_never_reported_as_a_failure(self, workdir: Path) -> None:
        verdict = run(
            runner_for(process(1, FAILING_OUTPUT, timed_out=True)).run(
                workdir, SuiteSpec()
            )
        )
        assert verdict.outcome is Outcome.TIMED_OUT


class TestParserSelection:
    """The spec chooses how output is interpreted."""

    def test_the_junit_parser_reads_a_report(self, workdir: Path) -> None:
        (workdir / "junit.xml").write_text(JUNIT_XML, encoding="utf-8")
        verdict = run(runner_for(process(1)).run(workdir, SuiteSpec(parser="junit")))

        assert verdict.outcome is Outcome.FAILED
        assert verdict.failed_names == ("tests.test_a::test_two",)

    def test_the_exit_code_parser_reports_no_cases(self, workdir: Path) -> None:
        verdict = run(
            runner_for(process(1, "some output")).run(
                workdir, SuiteSpec(parser="exit_code")
            )
        )
        assert verdict.outcome is Outcome.FAILED
        assert verdict.cases == ()

    def test_an_unknown_parser_produces_an_errored_verdict(self, workdir: Path) -> None:
        verdict = run(runner_for(process(0)).run(workdir, SuiteSpec(parser="bogus")))
        assert verdict.outcome is Outcome.ERRORED


class TestCoverage:
    """Coverage is collected when asked for, and never invented."""

    def test_coverage_is_absent_unless_requested(self, workdir: Path) -> None:
        (workdir / "coverage.xml").write_text(COVERAGE_XML, encoding="utf-8")
        verdict = run(runner_for(process(0, PASSING_OUTPUT)).run(workdir, SuiteSpec()))
        assert verdict.coverage is None

    def test_coverage_is_read_from_xml(self, workdir: Path) -> None:
        (workdir / "coverage.xml").write_text(COVERAGE_XML, encoding="utf-8")
        verdict = run(
            runner_for(process(0, PASSING_OUTPUT)).run(
                workdir, SuiteSpec(coverage=True)
            )
        )
        assert verdict.coverage is not None
        assert verdict.coverage.percent == pytest.approx(85.0)

    def test_coverage_is_read_from_terminal_output(self, workdir: Path) -> None:
        verdict = run(
            runner_for(process(0, COVERAGE_TERMINAL_OUTPUT)).run(
                workdir, SuiteSpec(coverage=True)
            )
        )
        assert verdict.coverage is not None
        assert verdict.coverage.percent == pytest.approx(90.0)

    def test_missing_coverage_is_not_an_error_without_a_threshold(
        self, workdir: Path
    ) -> None:
        verdict = run(
            runner_for(process(0, PASSING_OUTPUT)).run(
                workdir, SuiteSpec(coverage=True)
            )
        )
        assert verdict.outcome is Outcome.PASSED
        assert verdict.coverage is None

    def test_a_met_threshold_keeps_the_pass(self, workdir: Path) -> None:
        (workdir / "coverage.xml").write_text(COVERAGE_XML, encoding="utf-8")
        verdict = run(
            runner_for(process(0, PASSING_OUTPUT)).run(
                workdir, SuiteSpec(coverage=True, coverage_threshold=80.0)
            )
        )
        assert verdict.outcome is Outcome.PASSED

    def test_an_unmet_threshold_fails_the_gate(self, workdir: Path) -> None:
        (workdir / "coverage.xml").write_text(COVERAGE_XML, encoding="utf-8")
        verdict = run(
            runner_for(process(0, PASSING_OUTPUT)).run(
                workdir, SuiteSpec(coverage=True, coverage_threshold=95.0)
            )
        )
        assert verdict.outcome is Outcome.FAILED
        assert "below the configured" in verdict.reason

    def test_a_threshold_with_no_report_is_an_error(self, workdir: Path) -> None:
        """A threshold that cannot be measured is not a pass."""
        verdict = run(
            runner_for(process(0, PASSING_OUTPUT)).run(
                workdir, SuiteSpec(coverage=True, coverage_threshold=80.0)
            )
        )
        assert verdict.outcome is Outcome.ERRORED
        assert "no coverage report" in verdict.reason

    def test_a_failing_suite_is_not_relabelled_by_coverage(
        self, workdir: Path
    ) -> None:
        """The red suite is the important problem; do not bury it."""
        (workdir / "coverage.xml").write_text(COVERAGE_XML, encoding="utf-8")
        verdict = run(
            runner_for(process(1, FAILING_OUTPUT)).run(
                workdir, SuiteSpec(coverage=True, coverage_threshold=99.0)
            )
        )
        assert verdict.outcome is Outcome.FAILED
        assert "below the configured" not in verdict.reason

    def test_a_broken_coverage_report_does_not_fail_a_green_suite(
        self, workdir: Path
    ) -> None:
        (workdir / "coverage.xml").write_text("<broken", encoding="utf-8")
        verdict = run(
            runner_for(process(0, PASSING_OUTPUT)).run(
                workdir, SuiteSpec(coverage=True)
            )
        )
        assert verdict.outcome is Outcome.PASSED
        assert verdict.coverage is None


class TestMultipleGates:
    """Running several gates, and collapsing them."""

    def test_gates_run_in_order(self, workdir: Path) -> None:
        scripted = ScriptedRunner([process(0, PASSING_OUTPUT), process(1, FAILING_OUTPUT)])
        verdicts = run(
            VerdictEngine(process_runner=scripted).run_all(
                workdir, [SuiteSpec(name="unit"), SuiteSpec(name="integration")]
            )
        )
        assert [v.gate for v in verdicts] == ["unit", "integration"]
        assert [v.outcome for v in verdicts] == [Outcome.PASSED, Outcome.FAILED]

    def test_stop_on_failure_halts_the_batch(self, workdir: Path) -> None:
        scripted = ScriptedRunner(
            [process(1, FAILING_OUTPUT), process(0, PASSING_OUTPUT)]
        )
        verdicts = run(
            VerdictEngine(process_runner=scripted).run_all(
                workdir,
                [SuiteSpec(name="fast"), SuiteSpec(name="slow")],
                stop_on_failure=True,
            )
        )
        assert len(verdicts) == 1
        assert len(scripted.runs) == 1

    def test_aggregate_takes_the_worst(self, workdir: Path) -> None:
        scripted = ScriptedRunner([process(0, PASSING_OUTPUT), process(1, FAILING_OUTPUT)])
        verdicts = run(
            VerdictEngine(process_runner=scripted).run_all(
                workdir, [SuiteSpec(), SuiteSpec()]
            )
        )
        assert aggregate(verdicts) is Outcome.FAILED

    def test_an_error_outranks_a_failure(self, workdir: Path) -> None:
        """A broken harness is the thing to fix first."""
        scripted = ScriptedRunner([process(1, FAILING_OUTPUT), process(4, "usage")])
        verdicts = run(
            VerdictEngine(process_runner=scripted).run_all(
                workdir, [SuiteSpec(), SuiteSpec()]
            )
        )
        assert aggregate(verdicts) is Outcome.ERRORED

    def test_aggregating_nothing_is_skipped_not_passed(self) -> None:
        """No gate ran, so nothing was verified."""
        assert aggregate([]) is Outcome.SKIPPED

    def test_all_passing_aggregates_to_passed(self, workdir: Path) -> None:
        scripted = ScriptedRunner([process(0, PASSING_OUTPUT), process(0, PASSING_OUTPUT)])
        verdicts = run(
            VerdictEngine(process_runner=scripted).run_all(
                workdir, [SuiteSpec(), SuiteSpec()]
            )
        )
        assert aggregate(verdicts) is Outcome.PASSED


class TestOutputHandling:
    """Captured output is useful without being unbounded."""

    def test_output_is_captured(self, workdir: Path) -> None:
        verdict = run(runner_for(process(0, PASSING_OUTPUT)).run(workdir, SuiteSpec()))
        assert "3 passed" in verdict.output

    def test_enormous_output_is_truncated_keeping_the_tail(
        self, workdir: Path
    ) -> None:
        huge = "x" * 100_000 + "\nFINAL LINE"
        verdict = run(runner_for(process(1, huge)).run(workdir, SuiteSpec()))

        assert len(verdict.output) < 30_000
        assert "FINAL LINE" in verdict.output
        assert "truncated" in verdict.output

    def test_stderr_is_kept_in_detail(self, workdir: Path) -> None:
        verdict = run(
            runner_for(process(1, FAILING_OUTPUT, "a warning")).run(
                workdir, SuiteSpec()
            )
        )
        assert "a warning" in str(verdict.detail.get("stderr_tail", ""))


def test_the_package_does_not_import_layers_above_it() -> None:
    """test_runner sits at layer 1: no task, agent, or git_manager imports."""
    import orchestrator.test_runner as package

    root = Path(package.__path__[0])
    forbidden = ("orchestrator.task", "orchestrator.agent", "orchestrator.git_manager")
    offenders: list[str] = []

    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            offenders.extend(
                f"{path.name}: {name}" for name in names if name.startswith(forbidden)
            )

    assert offenders == [], f"layering violations: {offenders}"

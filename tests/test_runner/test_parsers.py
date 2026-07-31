"""Tests for the result parsers and the coverage collector."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.test_runner.base import CaseStatus, Outcome, ParseError, ParsedResults
from orchestrator.test_runner.parsers import (
    PYTEST_EXIT_CODES,
    CoverageCollector,
    ExitCodeParser,
    JUnitXmlParser,
    PytestParser,
    get_parser,
)

from tests.test_runner.conftest import (
    COLLECTION_ERROR_OUTPUT,
    COVERAGE_TERMINAL_OUTPUT,
    COVERAGE_XML,
    FAILING_OUTPUT,
    JUNIT_XML,
    MIXED_OUTPUT,
    NO_TESTS_OUTPUT,
    PASSING_OUTPUT,
)


@pytest.fixture
def pytest_parser() -> PytestParser:
    """The pytest output parser."""
    return PytestParser()


class TestPytestCaseExtraction:
    """Individual cases and counts come out of pytest's text output."""

    def test_passing_run(self, pytest_parser: PytestParser, workdir: Path) -> None:
        results = pytest_parser.parse(
            exit_code=0, stdout=PASSING_OUTPUT, stderr="", cwd=workdir
        )
        assert len(results.cases) == 3
        assert all(case.status is CaseStatus.PASSED for case in results.cases)
        assert results.counts == {"passed": 3}
        assert results.duration_s == pytest.approx(0.12)

    def test_failing_run_names_the_failure(
        self, pytest_parser: PytestParser, workdir: Path
    ) -> None:
        """FR-4.3: the failing case must be identifiable."""
        results = pytest_parser.parse(
            exit_code=1, stdout=FAILING_OUTPUT, stderr="", cwd=workdir
        )
        assert [case.id for case in results.failures] == ["tests/test_a.py::test_two"]
        assert results.failures[0].message == "assert 1 == 2"
        assert results.counts == {"failed": 1, "passed": 2}

    def test_case_identifiers_split_into_file_and_name(
        self, pytest_parser: PytestParser, workdir: Path
    ) -> None:
        results = pytest_parser.parse(
            exit_code=1, stdout=FAILING_OUTPUT, stderr="", cwd=workdir
        )
        failure = results.failures[0]
        assert failure.file == "tests/test_a.py"
        assert failure.name == "test_two"

    def test_mixed_statuses(self, pytest_parser: PytestParser, workdir: Path) -> None:
        results = pytest_parser.parse(
            exit_code=1, stdout=MIXED_OUTPUT, stderr="", cwd=workdir
        )
        statuses = {case.id: case.status for case in results.cases}
        assert statuses["tests/test_a.py::test_two"] is CaseStatus.SKIPPED
        assert statuses["tests/test_a.py::test_three"] is CaseStatus.XFAILED
        assert statuses["tests/test_a.py::test_four"] is CaseStatus.FAILED
        assert len(results.failures) == 1

    def test_the_summary_section_wins_over_the_verbose_line(
        self, pytest_parser: PytestParser, workdir: Path
    ) -> None:
        """Both mention test_two; the summary carries the message."""
        results = pytest_parser.parse(
            exit_code=1, stdout=FAILING_OUTPUT, stderr="", cwd=workdir
        )
        matches = [c for c in results.cases if c.id == "tests/test_a.py::test_two"]
        assert len(matches) == 1
        assert matches[0].message

    def test_stderr_is_searched_too(
        self, pytest_parser: PytestParser, workdir: Path
    ) -> None:
        results = pytest_parser.parse(
            exit_code=1, stdout="", stderr=FAILING_OUTPUT, cwd=workdir
        )
        assert results.failures

    def test_empty_output_yields_no_cases(
        self, pytest_parser: PytestParser, workdir: Path
    ) -> None:
        results = pytest_parser.parse(exit_code=0, stdout="", stderr="", cwd=workdir)
        assert results.cases == ()


class TestPytestExitCodes:
    """The exit-code table is what makes FR-4.4 possible."""

    def test_every_documented_code_is_mapped(self) -> None:
        assert set(PYTEST_EXIT_CODES) == {0, 1, 2, 3, 4, 5}

    def test_zero_is_a_pass(self, pytest_parser: PytestParser) -> None:
        outcome, _ = pytest_parser.outcome_for(exit_code=0, results=ParsedResults())
        assert outcome is Outcome.PASSED

    def test_one_is_a_failure_not_an_error(
        self, pytest_parser: PytestParser, workdir: Path
    ) -> None:
        results = pytest_parser.parse(
            exit_code=1, stdout=FAILING_OUTPUT, stderr="", cwd=workdir
        )
        outcome, _ = pytest_parser.outcome_for(exit_code=1, results=results)
        assert outcome is Outcome.FAILED

    @pytest.mark.parametrize("exit_code", [2, 3, 4])
    def test_runner_problems_are_errors_not_failures(
        self, pytest_parser: PytestParser, exit_code: int
    ) -> None:
        """A usage error is not the code's fault and must not read as one."""
        outcome, reason = pytest_parser.outcome_for(
            exit_code=exit_code, results=ParsedResults()
        )
        assert outcome is Outcome.ERRORED
        assert reason

    def test_collecting_nothing_is_an_error(self, pytest_parser: PytestParser) -> None:
        """A gate that ran zero tests verified nothing; it must not read green."""
        outcome, reason = pytest_parser.outcome_for(exit_code=5, results=ParsedResults())
        assert outcome is Outcome.ERRORED
        assert "verified nothing" in reason

    def test_an_unknown_exit_code_is_an_error(
        self, pytest_parser: PytestParser
    ) -> None:
        outcome, reason = pytest_parser.outcome_for(
            exit_code=99, results=ParsedResults()
        )
        assert outcome is Outcome.ERRORED
        assert "99" in reason

    def test_a_collection_error_outranks_the_exit_code(
        self, pytest_parser: PytestParser, workdir: Path
    ) -> None:
        results = pytest_parser.parse(
            exit_code=1, stdout=COLLECTION_ERROR_OUTPUT, stderr="", cwd=workdir
        )
        outcome, reason = pytest_parser.outcome_for(exit_code=1, results=results)
        assert outcome is Outcome.ERRORED
        assert "could not be collected" in reason

    def test_a_failure_exit_with_no_parsed_cases_says_so(
        self, pytest_parser: PytestParser
    ) -> None:
        outcome, reason = pytest_parser.outcome_for(exit_code=1, results=ParsedResults())
        assert outcome is Outcome.FAILED
        assert "none could be parsed" in reason

    def test_no_tests_output_parses_without_cases(
        self, pytest_parser: PytestParser, workdir: Path
    ) -> None:
        results = pytest_parser.parse(
            exit_code=5, stdout=NO_TESTS_OUTPUT, stderr="", cwd=workdir
        )
        assert results.cases == ()


class TestJUnitParser:
    """A framework-agnostic report format."""

    def test_missing_report_is_reported_not_raised(self, workdir: Path) -> None:
        results = JUnitXmlParser().parse(
            exit_code=0, stdout="", stderr="", cwd=workdir
        )
        assert "no JUnit XML report" in results.collection_error

    def test_cases_are_extracted(self, workdir: Path) -> None:
        (workdir / "junit.xml").write_text(JUNIT_XML, encoding="utf-8")
        results = JUnitXmlParser().parse(
            exit_code=1, stdout="", stderr="", cwd=workdir
        )
        assert len(results.cases) == 3
        assert [c.id for c in results.failures] == ["tests.test_a::test_two"]
        assert results.failures[0].message == "assert 1 == 2"
        assert results.failures[0].detail == "traceback here"

    def test_durations_are_summed(self, workdir: Path) -> None:
        (workdir / "junit.xml").write_text(JUNIT_XML, encoding="utf-8")
        results = JUnitXmlParser().parse(exit_code=1, stdout="", stderr="", cwd=workdir)
        assert results.duration_s == pytest.approx(0.06)

    def test_malformed_xml_raises(self, workdir: Path) -> None:
        (workdir / "junit.xml").write_text("<not-closed", encoding="utf-8")
        with pytest.raises(ParseError, match="not valid XML"):
            JUnitXmlParser().parse(exit_code=0, stdout="", stderr="", cwd=workdir)

    def test_a_custom_report_path_is_honoured(self, workdir: Path) -> None:
        (workdir / "custom.xml").write_text(JUNIT_XML, encoding="utf-8")
        results = JUnitXmlParser("custom.xml").parse(
            exit_code=1, stdout="", stderr="", cwd=workdir
        )
        assert len(results.cases) == 3

    def test_outcomes(self, workdir: Path) -> None:
        parser = JUnitXmlParser()
        (workdir / "junit.xml").write_text(JUNIT_XML, encoding="utf-8")
        results = parser.parse(exit_code=1, stdout="", stderr="", cwd=workdir)
        assert parser.outcome_for(exit_code=1, results=results)[0] is Outcome.FAILED

    def test_an_empty_report_is_an_error(self, workdir: Path) -> None:
        (workdir / "junit.xml").write_text(
            '<?xml version="1.0"?><testsuites></testsuites>', encoding="utf-8"
        )
        parser = JUnitXmlParser()
        results = parser.parse(exit_code=0, stdout="", stderr="", cwd=workdir)
        assert parser.outcome_for(exit_code=0, results=results)[0] is Outcome.ERRORED

    def test_green_cases_with_a_nonzero_exit_is_an_error(self, workdir: Path) -> None:
        """The report and the exit code disagree; that is worth surfacing."""
        (workdir / "junit.xml").write_text(
            '<?xml version="1.0"?><testsuites><testsuite>'
            '<testcase classname="a" name="b" time="0.1"/>'
            "</testsuite></testsuites>",
            encoding="utf-8",
        )
        parser = JUnitXmlParser()
        results = parser.parse(exit_code=3, stdout="", stderr="", cwd=workdir)
        outcome, reason = parser.outcome_for(exit_code=3, results=results)
        assert outcome is Outcome.ERRORED
        assert "exited 3" in reason


class TestExitCodeParser:
    """Honest about knowing almost nothing."""

    def test_it_reports_no_cases(self, workdir: Path) -> None:
        """Synthesizing a case would let a caller believe detail exists."""
        results = ExitCodeParser().parse(
            exit_code=1, stdout="lots of output", stderr="", cwd=workdir
        )
        assert results.cases == ()

    def test_zero_passes(self) -> None:
        assert (
            ExitCodeParser().outcome_for(exit_code=0, results=ParsedResults())[0]
            is Outcome.PASSED
        )

    def test_non_zero_fails(self) -> None:
        outcome, reason = ExitCodeParser().outcome_for(
            exit_code=7, results=ParsedResults()
        )
        assert outcome is Outcome.FAILED
        assert "no per-test detail" in reason


class TestParserRegistry:
    """Parsers are resolved by name."""

    @pytest.mark.parametrize(
        ("name", "expected"),
        [("pytest", PytestParser), ("junit", JUnitXmlParser), ("exit_code", ExitCodeParser)],
    )
    def test_known_parsers_resolve(self, name: str, expected: type) -> None:
        assert isinstance(get_parser(name), expected)

    def test_an_unknown_parser_is_refused(self) -> None:
        with pytest.raises(ParseError, match="unknown result parser"):
            get_parser("nosuchparser")

    def test_the_error_lists_what_is_available(self) -> None:
        with pytest.raises(ParseError) as excinfo:
            get_parser("bogus")
        assert "pytest" in str(excinfo.value)


class TestCoverageCollector:
    """Coverage is optional, and its absence is not zero."""

    def test_no_report_means_none(self, workdir: Path) -> None:
        """Unknown coverage must not be reported as zero coverage."""
        assert CoverageCollector().collect(workdir) is None

    def test_xml_report_is_read(self, workdir: Path) -> None:
        (workdir / "coverage.xml").write_text(COVERAGE_XML, encoding="utf-8")
        report = CoverageCollector().collect(workdir)

        assert report is not None
        assert report.percent == pytest.approx(85.0)
        assert report.branch_rate == pytest.approx(0.75)
        assert report.lines_covered == 85
        assert len(report.files) == 2

    def test_worst_files_come_from_the_xml(self, workdir: Path) -> None:
        (workdir / "coverage.xml").write_text(COVERAGE_XML, encoding="utf-8")
        report = CoverageCollector().collect(workdir)
        assert report is not None
        assert report.worst_files(1)[0].path == "src/bad.py"

    def test_terminal_output_is_the_fallback(self, workdir: Path) -> None:
        report = CoverageCollector().collect(workdir, COVERAGE_TERMINAL_OUTPUT)
        assert report is not None
        assert report.percent == pytest.approx(90.0)
        assert report.lines_valid == 80
        assert report.source == "terminal"

    def test_xml_is_preferred_over_terminal(self, workdir: Path) -> None:
        (workdir / "coverage.xml").write_text(COVERAGE_XML, encoding="utf-8")
        report = CoverageCollector().collect(workdir, COVERAGE_TERMINAL_OUTPUT)
        assert report is not None
        assert report.percent == pytest.approx(85.0)

    def test_malformed_xml_raises(self, workdir: Path) -> None:
        (workdir / "coverage.xml").write_text("<broken", encoding="utf-8")
        with pytest.raises(ParseError, match="not valid XML"):
            CoverageCollector().collect(workdir)

    def test_a_custom_path_is_honoured(self, workdir: Path) -> None:
        (workdir / "cov.xml").write_text(COVERAGE_XML, encoding="utf-8")
        report = CoverageCollector("cov.xml").collect(workdir)
        assert report is not None

    def test_unrelated_output_yields_none(self, workdir: Path) -> None:
        assert CoverageCollector().collect(workdir, PASSING_OUTPUT) is None

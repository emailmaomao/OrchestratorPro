"""Parsers: turning a test run's output into structured results and coverage.

Three parsers ship, in descending order of how much they can tell you:

``pytest``
    Reads pytest's own output. Knows individual case names, failure messages,
    counts, and — crucially — pytest's exit-code vocabulary, which is what makes
    "the suite is red" distinguishable from "the suite would not import".
``junit``
    Reads a JUnit XML report. Framework-agnostic and precise about cases, but
    silent about *why* the runner exited as it did.
``exit_code``
    Knows only zero versus non-zero. Honest about its own blindness: it reports
    no cases at all rather than inventing one.

**The exit-code table is the point.** pytest distinguishes "tests failed" (1)
from "usage error" (4), "internal error" (3), "interrupted" (2), and "nothing
collected" (5). Collapsing those into a boolean is what produces the failure
mode FR-4.4 exists to prevent, so they are mapped explicitly here.

**A suite that collected nothing is an error, not a pass.** pytest exits 5 when
it found no tests. A gate that ran zero tests has verified nothing, and letting
that read as success would make an empty suite the easiest way to go green.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ElementTree
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from orchestrator.test_runner.base import (
    CaseResult,
    CaseStatus,
    CoverageReport,
    FileCoverage,
    Outcome,
    ParsedResults,
    ParseError,
    counts_from_cases,
)

__all__ = [
    "PYTEST_EXIT_CODES",
    "CoverageCollector",
    "ExitCodeParser",
    "JUnitXmlParser",
    "PytestParser",
    "get_parser",
]

#: pytest's documented exit codes, and what each one actually means for a gate.
PYTEST_EXIT_CODES: Final[Mapping[int, tuple[Outcome, str]]] = {
    0: (Outcome.PASSED, "all tests passed"),
    1: (Outcome.FAILED, "one or more tests failed"),
    2: (Outcome.ERRORED, "the test run was interrupted before it finished"),
    3: (Outcome.ERRORED, "pytest hit an internal error"),
    4: (Outcome.ERRORED, "pytest was invoked incorrectly (usage error)"),
    5: (
        Outcome.ERRORED,
        "no tests were collected, so this gate verified nothing",
    ),
}

#: ``FAILED tests/test_x.py::test_y - AssertionError: boom``
_OUTCOME_LINE: Final = re.compile(
    r"^(?P<status>FAILED|ERROR|PASSED|SKIPPED|XFAIL|XPASS)\s+"
    r"(?P<id>[^\s]+)(?:\s+-\s+(?P<message>.*))?$"
)

#: ``tests/test_x.py::test_y PASSED  [ 50%]`` — pytest's verbose per-test line.
_VERBOSE_LINE: Final = re.compile(
    r"^(?P<id>\S+::\S+)\s+(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b"
)

#: ``=========== 1 failed, 2 passed in 0.12s ===========``
_SUMMARY_LINE: Final = re.compile(r"^=+.*\bin\s+[\d.]+s.*=+$")

_COUNT_PAIR: Final = re.compile(
    r"(\d+)\s+(passed|failed|error|errors|skipped|xfailed|xpassed|deselected|warning|warnings)"
)

_DURATION: Final = re.compile(r"\bin\s+([\d.]+)s")

#: ``TOTAL    120    12    90%`` — pytest-cov's terminal summary.
_COVERAGE_TOTAL: Final = re.compile(
    r"^TOTAL\s+(?P<statements>\d+)\s+(?P<missed>\d+)\s+(?P<percent>\d+(?:\.\d+)?)%"
)

_STATUS_BY_NAME: Final[Mapping[str, CaseStatus]] = {
    "PASSED": CaseStatus.PASSED,
    "FAILED": CaseStatus.FAILED,
    "ERROR": CaseStatus.ERROR,
    "SKIPPED": CaseStatus.SKIPPED,
    "XFAIL": CaseStatus.XFAILED,
    "XPASS": CaseStatus.XPASSED,
}


def _split_id(identifier: str) -> tuple[str, str]:
    """Split a test identifier into ``(file, name)``."""
    if "::" in identifier:
        file_part, _, name = identifier.partition("::")
        return file_part, name
    return identifier, identifier


class PytestParser:
    """Parses pytest's text output."""

    name = "pytest"

    def parse(
        self, *, exit_code: int, stdout: str, stderr: str, cwd: Path
    ) -> ParsedResults:
        """Extract cases, counts, and duration from pytest output."""
        text = f"{stdout}\n{stderr}" if stderr else stdout
        cases: dict[str, CaseResult] = {}

        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue

            verbose = _VERBOSE_LINE.match(line)
            if verbose is not None:
                identifier = verbose.group("id")
                status = _STATUS_BY_NAME[verbose.group("status")]
                file_part, name = _split_id(identifier)
                cases[identifier] = CaseResult(
                    id=identifier, status=status, name=name, file=file_part
                )
                continue

            summary = _OUTCOME_LINE.match(line)
            if summary is not None and "::" in summary.group("id"):
                identifier = summary.group("id")
                status = _STATUS_BY_NAME[summary.group("status")]
                file_part, name = _split_id(identifier)
                # The short-summary section is authoritative: it carries the
                # message, and it wins over a verbose line for the same test.
                cases[identifier] = CaseResult(
                    id=identifier,
                    status=status,
                    name=name,
                    file=file_part,
                    message=(summary.group("message") or "").strip(),
                )

        counts = self._counts(text) or counts_from_cases(tuple(cases.values()))
        return ParsedResults(
            cases=tuple(cases.values()),
            counts=counts,
            duration_s=self._duration(text),
            collection_error=self._collection_error(text),
        )

    @staticmethod
    def _summary_line(text: str) -> str:
        """Return pytest's final summary line, or ``""``."""
        found = ""
        for raw in text.splitlines():
            line = raw.strip()
            if _SUMMARY_LINE.match(line):
                found = line
        return found

    def _counts(self, text: str) -> dict[str, int]:
        """Extract the counts pytest reported."""
        line = self._summary_line(text)
        if not line:
            return {}
        counts: dict[str, int] = {}
        for value, label in _COUNT_PAIR.findall(line):
            key = {"errors": "error", "warnings": "warning"}.get(label, label)
            counts[key] = counts.get(key, 0) + int(value)
        return counts

    def _duration(self, text: str) -> float | None:
        """Extract the reported wall-clock duration."""
        line = self._summary_line(text)
        match = _DURATION.search(line) if line else None
        return float(match.group(1)) if match else None

    @staticmethod
    def _collection_error(text: str) -> str:
        """Return a description when the suite could not be imported."""
        for raw in text.splitlines():
            line = raw.strip()
            if "error" in line.lower() and "during collection" in line.lower():
                return line
            if line.startswith("ERROR ") and "::" not in line:
                return line
        return ""

    def outcome_for(
        self, *, exit_code: int, results: ParsedResults
    ) -> tuple[Outcome, str]:
        """Classify the run using pytest's exit-code vocabulary (FR-4.4)."""
        if results.collection_error:
            return (
                Outcome.ERRORED,
                f"the suite could not be collected: {results.collection_error}",
            )

        mapped = PYTEST_EXIT_CODES.get(exit_code)
        if mapped is not None:
            outcome, reason = mapped
            # Exit 1 is definitive about failure, but if nothing parsed we say
            # so rather than reporting an empty failure list as a clean red run.
            if outcome is Outcome.FAILED and not results.failures:
                return (
                    Outcome.FAILED,
                    "pytest reported failures, but none could be parsed from its "
                    "output; see the captured output",
                )
            return outcome, reason

        return (
            Outcome.ERRORED,
            f"pytest exited with the unrecognized code {exit_code}",
        )


class JUnitXmlParser:
    """Parses a JUnit XML report.

    Framework-agnostic and precise about individual cases, but it cannot see how
    the runner exited, so it is paired with the exit code rather than trusted
    alone.
    """

    name = "junit"

    #: Report locations tried in order when none is configured.
    DEFAULT_PATHS: Final = ("junit.xml", "test-results.xml", "reports/junit.xml")

    __slots__ = ("_report_path",)

    def __init__(self, report_path: str | None = None) -> None:
        self._report_path = report_path

    def _locate(self, cwd: Path) -> Path | None:
        """Find the report file, if one exists."""
        candidates = (self._report_path,) if self._report_path else self.DEFAULT_PATHS
        for candidate in candidates:
            if candidate and (cwd / candidate).is_file():
                return cwd / candidate
        return None

    def parse(
        self, *, exit_code: int, stdout: str, stderr: str, cwd: Path
    ) -> ParsedResults:
        """Read the report and extract its cases.

        Raises:
            ParseError: If the report exists but is not valid XML.
        """
        report = self._locate(cwd)
        if report is None:
            return ParsedResults(
                collection_error="no JUnit XML report was produced"
            )

        try:
            tree = ElementTree.parse(report)  # noqa: S314 - a local report we produced
        except ElementTree.ParseError as exc:
            raise ParseError(
                f"the JUnit report at {report} is not valid XML: {exc}",
                detail={"path": str(report)},
            ) from exc

        cases: list[CaseResult] = []
        total_time = 0.0
        for element in tree.getroot().iter("testcase"):
            classname = element.get("classname", "")
            name = element.get("name", "")
            identifier = f"{classname}::{name}" if classname else name
            duration = float(element.get("time", "0") or 0)
            total_time += duration

            status = CaseStatus.PASSED
            message = ""
            detail = ""
            for child in element:
                if child.tag == "failure":
                    status, message, detail = (
                        CaseStatus.FAILED,
                        child.get("message", ""),
                        (child.text or "").strip(),
                    )
                elif child.tag == "error":
                    status, message, detail = (
                        CaseStatus.ERROR,
                        child.get("message", ""),
                        (child.text or "").strip(),
                    )
                elif child.tag == "skipped":
                    status, message = CaseStatus.SKIPPED, child.get("message", "")

            cases.append(
                CaseResult(
                    id=identifier,
                    status=status,
                    name=name,
                    file=classname,
                    duration_s=duration,
                    message=message,
                    detail=detail,
                )
            )

        return ParsedResults(
            cases=tuple(cases),
            counts=counts_from_cases(cases),
            duration_s=total_time or None,
        )

    def outcome_for(
        self, *, exit_code: int, results: ParsedResults
    ) -> tuple[Outcome, str]:
        """Classify from the report, falling back to the exit code."""
        if results.collection_error:
            return Outcome.ERRORED, results.collection_error
        if results.failures:
            return Outcome.FAILED, f"{len(results.failures)} test(s) failed"
        if not results.has_cases:
            return Outcome.ERRORED, "the report contained no test cases"
        if exit_code != 0:
            return (
                Outcome.ERRORED,
                f"every reported case passed, but the runner exited {exit_code}",
            )
        return Outcome.PASSED, "all reported tests passed"


class ExitCodeParser:
    """Knows only whether the command succeeded.

    The fallback for frameworks with no supported report format. It reports **no
    cases at all** rather than synthesizing one, so a caller can tell that the
    detail is genuinely unavailable instead of believing a suite had exactly one
    test.
    """

    name = "exit_code"

    def parse(
        self, *, exit_code: int, stdout: str, stderr: str, cwd: Path
    ) -> ParsedResults:
        """Return empty results; this parser extracts nothing."""
        return ParsedResults()

    def outcome_for(
        self, *, exit_code: int, results: ParsedResults
    ) -> tuple[Outcome, str]:
        """Classify purely from the exit code."""
        if exit_code == 0:
            return Outcome.PASSED, "the test command exited zero"
        return (
            Outcome.FAILED,
            f"the test command exited {exit_code}; no per-test detail is available "
            "from this parser",
        )


def get_parser(name: str, **kwargs: object):  # noqa: ANN201 - a protocol, not a class
    """Return a parser by name.

    Args:
        name: ``"pytest"``, ``"junit"``, or ``"exit_code"``.
        **kwargs: Passed to the parser's constructor where it takes any.

    Returns:
        The parser.

    Raises:
        ParseError: If the name is not registered.
    """
    if name == "pytest":
        return PytestParser()
    if name == "junit":
        return JUnitXmlParser(**kwargs)  # type: ignore[arg-type]
    if name == "exit_code":
        return ExitCodeParser()
    raise ParseError(
        f"unknown result parser {name!r}; known parsers are: exit_code, junit, pytest",
        detail={"parser": name},
    )


class CoverageCollector:
    """Finds and reads a coverage report, if the run produced one.

    Coverage is opportunistic. A project without it configured has *unknown*
    coverage, and this returns ``None`` rather than a report of zero — a zero
    would fail a threshold check that should never have run.
    """

    #: Report locations tried in order.
    DEFAULT_PATHS: Final = ("coverage.xml", "reports/coverage.xml")

    __slots__ = ("_report_path",)

    def __init__(self, report_path: str | None = None) -> None:
        self._report_path = report_path

    def collect(self, cwd: Path, stdout: str = "") -> CoverageReport | None:
        """Return a coverage report, or ``None`` if none is available.

        Prefers an XML report, which carries per-file detail, and falls back to
        pytest-cov's terminal summary, which carries only a total.
        """
        report = self.from_xml(cwd)
        if report is not None:
            return report
        return self.from_terminal(stdout)

    def from_xml(self, cwd: Path) -> CoverageReport | None:
        """Read a Cobertura-style ``coverage.xml``.

        Raises:
            ParseError: If a report exists but cannot be parsed — a malformed
                report is a real problem worth surfacing, unlike an absent one.
        """
        candidates = (self._report_path,) if self._report_path else self.DEFAULT_PATHS
        path = next(
            (cwd / c for c in candidates if c and (cwd / c).is_file()), None
        )
        if path is None:
            return None

        try:
            root = ElementTree.parse(path).getroot()  # noqa: S314 - local report
        except ElementTree.ParseError as exc:
            raise ParseError(
                f"the coverage report at {path} is not valid XML: {exc}",
                detail={"path": str(path)},
            ) from exc

        files = tuple(
            FileCoverage(
                path=element.get("filename", ""),
                line_rate=float(element.get("line-rate", "0") or 0),
            )
            for element in root.iter("class")
            if element.get("filename")
        )
        branch_rate = root.get("branch-rate")

        return CoverageReport(
            line_rate=float(root.get("line-rate", "0") or 0),
            branch_rate=float(branch_rate) if branch_rate not in (None, "") else None,
            lines_covered=int(root.get("lines-covered", "0") or 0),
            lines_valid=int(root.get("lines-valid", "0") or 0),
            files=files,
            source=str(path),
        )

    def from_terminal(self, stdout: str) -> CoverageReport | None:
        """Read pytest-cov's ``TOTAL`` line from terminal output."""
        for raw in stdout.splitlines():
            match = _COVERAGE_TOTAL.match(raw.strip())
            if match is None:
                continue
            statements = int(match.group("statements"))
            missed = int(match.group("missed"))
            return CoverageReport(
                line_rate=float(match.group("percent")) / 100.0,
                lines_covered=statements - missed,
                lines_valid=statements,
                source="terminal",
            )
        return None

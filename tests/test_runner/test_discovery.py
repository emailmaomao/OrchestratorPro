"""Tests for framework detection and test enumeration."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.test_runner.discovery import (
    UNKNOWN,
    Detection,
    DiscoveryError,
)

# Aliased on import so pytest does not attempt to collect it as a test class.
from orchestrator.test_runner.discovery import TestDiscovery as FrameworkDiscovery
from orchestrator.test_runner.execution import ScriptedRunner

from tests.test_runner.conftest import process, run


@pytest.fixture
def discovery() -> FrameworkDiscovery:
    """Discovery bound to a scripted runner."""
    return FrameworkDiscovery(ScriptedRunner())


class TestFrameworkDetection:
    """What kind of project is this?"""

    def test_an_empty_directory_is_unknown(
        self, discovery: FrameworkDiscovery, workdir: Path
    ) -> None:
        """An honest 'I do not know' beats a plausible guess."""
        detection = discovery.detect(workdir)
        assert detection.framework == "unknown"
        assert not detection.usable

    def test_pytest_ini_is_conclusive(
        self, discovery: FrameworkDiscovery, workdir: Path
    ) -> None:
        (workdir / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        detection = discovery.detect(workdir)

        assert detection.framework == "pytest"
        assert detection.confidence == "high"
        assert detection.supported
        assert detection.usable
        assert detection.command == "pytest -q"

    def test_conftest_is_conclusive(
        self, discovery: FrameworkDiscovery, workdir: Path
    ) -> None:
        (workdir / "conftest.py").write_text("", encoding="utf-8")
        assert discovery.detect(workdir).confidence == "high"

    def test_a_pytest_section_in_pyproject_is_conclusive(
        self, discovery: FrameworkDiscovery, workdir: Path
    ) -> None:
        (workdir / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\naddopts = "-q"\n', encoding="utf-8"
        )
        assert discovery.detect(workdir).confidence == "high"

    def test_a_bare_pyproject_is_not_evidence(
        self, discovery: FrameworkDiscovery, workdir: Path
    ) -> None:
        """Every Python project has one; it says nothing about tests."""
        (workdir / "pyproject.toml").write_text(
            '[project]\nname = "x"\n', encoding="utf-8"
        )
        assert discovery.detect(workdir).framework == "unknown"

    def test_a_tests_directory_gives_medium_confidence(
        self, discovery: FrameworkDiscovery, workdir: Path
    ) -> None:
        (workdir / "pyproject.toml").write_text(
            '[project]\nname = "x"\n', encoding="utf-8"
        )
        (workdir / "tests").mkdir()
        detection = discovery.detect(workdir)

        assert detection.framework == "pytest"
        assert detection.confidence == "medium"
        assert detection.usable

    def test_a_malformed_pyproject_is_simply_not_evidence(
        self, discovery: FrameworkDiscovery, workdir: Path
    ) -> None:
        (workdir / "pyproject.toml").write_text("[broken\n", encoding="utf-8")
        assert discovery.detect(workdir).framework == "unknown"

    def test_other_ecosystems_are_recognized_but_unsupported(
        self, discovery: FrameworkDiscovery, workdir: Path
    ) -> None:
        """Recognizing a Cargo project lets us refuse confidently."""
        (workdir / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
        detection = discovery.detect(workdir)

        assert detection.framework == "cargo"
        assert not detection.supported
        assert not detection.usable

    @pytest.mark.parametrize(
        ("marker", "framework"),
        [("Cargo.toml", "cargo"), ("package.json", "npm"), ("go.mod", "go")],
    )
    def test_recognized_markers(
        self, discovery: FrameworkDiscovery, workdir: Path, marker: str, framework: str
    ) -> None:
        (workdir / marker).write_text("{}", encoding="utf-8")
        assert discovery.detect(workdir).framework == framework

    def test_pytest_wins_over_other_markers(
        self, discovery: FrameworkDiscovery, workdir: Path
    ) -> None:
        (workdir / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        (workdir / "package.json").write_text("{}", encoding="utf-8")
        assert discovery.detect(workdir).framework == "pytest"

    def test_detecting_a_non_directory_is_refused(
        self, discovery: FrameworkDiscovery, workdir: Path
    ) -> None:
        target = workdir / "file.txt"
        target.write_text("x", encoding="utf-8")
        with pytest.raises(DiscoveryError, match="not a directory"):
            discovery.detect(target)

    def test_test_directories_are_listed(
        self, discovery: FrameworkDiscovery, workdir: Path
    ) -> None:
        (workdir / "tests").mkdir()
        assert [p.name for p in discovery.test_directories(workdir)] == ["tests"]


class TestRequireUsable:
    """Turning a detection into a decision."""

    def test_a_usable_detection_passes(self) -> None:
        Detection(
            framework="pytest",
            command="pytest -q",
            parser="pytest",
            supported=True,
            confidence="high",
        ).require_usable()

    def test_an_unsupported_framework_is_refused(self) -> None:
        with pytest.raises(DiscoveryError, match="cannot gate yet"):
            Detection(
                framework="cargo",
                command="cargo test",
                parser="exit_code",
                supported=False,
                confidence="high",
            ).require_usable()

    def test_an_unknown_project_is_refused(self) -> None:
        with pytest.raises(DiscoveryError, match="configure gates.test_command"):
            UNKNOWN.require_usable()


class TestCollection:
    """Enumerating tests without running them."""

    def test_identifiers_are_extracted(self, workdir: Path) -> None:
        stdout = (
            "tests/test_a.py::test_one\n"
            "tests/test_a.py::test_two\n"
            "tests/test_b.py::test_three\n"
            "\n"
            "3 tests collected in 0.02s\n"
        )
        discovery = FrameworkDiscovery(ScriptedRunner([process(0, stdout)]))
        assert run(discovery.collect(workdir)) == (
            "tests/test_a.py::test_one",
            "tests/test_a.py::test_two",
            "tests/test_b.py::test_three",
        )

    def test_the_summary_line_is_not_a_test(self, workdir: Path) -> None:
        discovery = FrameworkDiscovery(
            ScriptedRunner([process(0, "tests/test_a.py::test_one\n\n1 test collected\n")])
        )
        assert len(run(discovery.collect(workdir))) == 1

    def test_an_empty_suite_collects_nothing(self, workdir: Path) -> None:
        """Exit 5 means an empty suite, which is not a broken one."""
        discovery = FrameworkDiscovery(
            ScriptedRunner([process(5, "no tests ran in 0.01s\n")])
        )
        assert run(discovery.collect(workdir)) == ()

    def test_a_collection_failure_is_reported_as_a_harness_problem(
        self, workdir: Path
    ) -> None:
        discovery = FrameworkDiscovery(
            ScriptedRunner([process(2, "", "ImportError: no module named x")])
        )
        with pytest.raises(DiscoveryError, match="could not be imported"):
            run(discovery.collect(workdir))

    def test_a_collection_timeout_is_reported(self, workdir: Path) -> None:
        discovery = FrameworkDiscovery(ScriptedRunner([process(-1, timed_out=True)]))
        with pytest.raises(DiscoveryError, match="timed out"):
            run(discovery.collect(workdir))

    def test_count_returns_the_number_of_tests(self, workdir: Path) -> None:
        discovery = FrameworkDiscovery(
            ScriptedRunner([process(0, "a.py::one\nb.py::two\n")])
        )
        assert run(discovery.count(workdir)) == 2

    def test_the_collection_command_is_configurable(self, workdir: Path) -> None:
        runner = ScriptedRunner([process(0, "")])
        discovery = FrameworkDiscovery(runner)
        run(discovery.collect(workdir, command="pytest --collect-only -q tests/unit"))
        assert runner.runs[0].command.endswith("tests/unit")

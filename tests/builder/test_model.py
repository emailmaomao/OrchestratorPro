"""Tests for the build domain objects."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.builder.model import (
    Artifact,
    BuildConfigError,
    BuildReport,
    BuildStatus,
    BuildUnit,
    Diagnostic,
    ProjectKind,
    ProjectLayout,
    Severity,
    SourceFile,
    UnitResult,
    digest_bytes,
    digest_text,
    normalize_path,
    stable_json,
)

from tests.builder.conftest import layout, unit


class TestDigests:
    """Change is detected by content, so the digest has to be honest."""

    def test_the_same_bytes_digest_the_same(self) -> None:
        assert digest_bytes(b"hello") == digest_bytes(b"hello")

    def test_different_bytes_digest_differently(self) -> None:
        assert digest_bytes(b"hello") != digest_bytes(b"hell0")

    def test_line_endings_do_not_change_a_source(self) -> None:
        """Otherwise every checkout on another platform is a full rebuild."""
        assert digest_text("a\nb\n") == digest_text("a\r\nb\r\n")

    def test_stable_json_sorts_keys(self) -> None:
        assert stable_json({"b": 1, "a": 2}) == stable_json({"a": 2, "b": 1})

    def test_stable_json_is_compact(self) -> None:
        assert " " not in stable_json({"a": 1, "b": [1, 2]})


class TestNormalizePath:
    """Unit definitions can be written by an agent, so paths are a boundary."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("src/main.py", "src/main.py"),
            ("src\\main.py", "src/main.py"),
            ("./src/main.py", "src/main.py"),
            ("  src/main.py  ", "src/main.py"),
        ],
    )
    def test_acceptable_paths(self, raw: str, expected: str) -> None:
        assert normalize_path(raw) == expected

    @pytest.mark.parametrize(
        "raw", ["/etc/passwd", "../outside", "src/../../escape", "C:/Windows", ""]
    )
    def test_refused_paths(self, raw: str) -> None:
        with pytest.raises(BuildConfigError):
            normalize_path(raw)

    def test_the_error_names_the_field(self) -> None:
        with pytest.raises(BuildConfigError, match="artifact path"):
            normalize_path("../x", what="artifact path")


class TestBuildUnit:
    """A unit validates itself where it is declared."""

    def test_defaults(self) -> None:
        declared = BuildUnit(name="core", command="make core")
        assert declared.incremental
        assert declared.timeout_s == 600.0
        assert declared.sources == ()

    def test_a_blank_name_is_refused(self) -> None:
        with pytest.raises(BuildConfigError, match="must have a name"):
            BuildUnit(name="  ", command="make")

    def test_a_blank_command_is_refused(self) -> None:
        with pytest.raises(BuildConfigError, match="must have a command"):
            BuildUnit(name="core", command="   ")

    def test_a_self_dependency_is_refused(self) -> None:
        with pytest.raises(BuildConfigError, match="depends on itself"):
            BuildUnit(name="core", command="make", depends_on=("core",))

    def test_a_duplicate_dependency_is_refused(self) -> None:
        with pytest.raises(BuildConfigError, match="twice"):
            BuildUnit(name="a", command="make", depends_on=("b", "b"))

    def test_a_non_positive_timeout_is_refused(self) -> None:
        with pytest.raises(BuildConfigError, match="positive timeout"):
            BuildUnit(name="a", command="make", timeout_s=0)

    def test_source_paths_are_normalized(self) -> None:
        assert BuildUnit(name="a", command="make", sources=("src\\a",)).sources == ("src/a",)

    def test_an_escaping_source_is_refused(self) -> None:
        with pytest.raises(BuildConfigError, match="escape"):
            BuildUnit(name="a", command="make", sources=("../secrets",))

    def test_an_escaping_artifact_is_refused(self) -> None:
        with pytest.raises(BuildConfigError, match="escape"):
            BuildUnit(name="a", command="make", artifacts=("../out",))

    def test_ownership_matches_a_directory_prefix(self) -> None:
        declared = BuildUnit(name="a", command="make", sources=("src/a",))
        assert declared.owns("src/a/deep/file.py")
        assert declared.owns("src/a")
        assert not declared.owns("src/ab/file.py")

    def test_ownership_does_not_match_a_partial_segment(self) -> None:
        """'src/a' must not claim 'src/abc'."""
        assert not BuildUnit(name="a", command="m", sources=("src/a",)).owns("src/abc")

    def test_identity_ignores_the_timeout(self) -> None:
        """Raising a timeout is a scheduling change, not a rebuild."""
        one = BuildUnit(name="a", command="make", timeout_s=10)
        two = BuildUnit(name="a", command="make", timeout_s=900)
        assert one.identity() == two.identity()

    def test_identity_tracks_the_command(self) -> None:
        one = BuildUnit(name="a", command="make")
        two = BuildUnit(name="a", command="make --release")
        assert one.identity() != two.identity()

    def test_identity_tracks_the_environment(self) -> None:
        one = BuildUnit(name="a", command="make", env={"MODE": "debug"})
        two = BuildUnit(name="a", command="make", env={"MODE": "release"})
        assert one.identity() != two.identity()

    def test_identity_is_order_independent(self) -> None:
        one = BuildUnit(name="a", command="m", sources=("x", "y"))
        two = BuildUnit(name="a", command="m", sources=("y", "x"))
        assert one.identity() == two.identity()


class TestBuildStatus:
    """The FAILED / ERRORED distinction is load-bearing (FR-4.4)."""

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (BuildStatus.SUCCEEDED, True),
            (BuildStatus.CACHED, True),
            (BuildStatus.SKIPPED, True),
            (BuildStatus.FAILED, False),
            (BuildStatus.ERRORED, False),
            (BuildStatus.TIMED_OUT, False),
            (BuildStatus.BLOCKED, False),
        ],
    )
    def test_which_statuses_can_be_relied_on(
        self, status: BuildStatus, expected: bool
    ) -> None:
        assert status.ok is expected

    def test_a_broken_tool_is_not_a_broken_program(self) -> None:
        assert BuildStatus.ERRORED.is_harness_problem
        assert BuildStatus.TIMED_OUT.is_harness_problem
        assert not BuildStatus.FAILED.is_harness_problem

    def test_only_executed_statuses_report_as_ran(self) -> None:
        assert BuildStatus.FAILED.ran
        assert not BuildStatus.CACHED.ran
        assert not BuildStatus.BLOCKED.ran


class TestDiagnostic:
    """A diagnostic is only useful if it says where."""

    def test_a_full_location(self) -> None:
        d = Diagnostic(message="boom", file="src/a.c", line=12, column=5)
        assert d.location() == "src/a.c:12:5"

    def test_a_partial_location(self) -> None:
        assert Diagnostic(message="b", file="a.c", line=3).location() == "a.c:3"

    def test_no_location_at_all(self) -> None:
        assert Diagnostic(message="b").location() == ""

    def test_rendering_looks_like_a_compiler(self) -> None:
        d = Diagnostic(message="bad", file="a.c", line=1, column=2, code="E1")
        assert d.render() == "a.c:1:2: error [E1]: bad"

    def test_only_errors_count_as_errors(self) -> None:
        assert Diagnostic(message="x").is_error
        assert not Diagnostic(message="x", severity=Severity.WARNING).is_error


class TestUnitResult:
    """What one unit produced, and what to tell the next attempt."""

    def test_errors_exclude_warnings(self) -> None:
        result = UnitResult(
            unit="a",
            status=BuildStatus.FAILED,
            diagnostics=(
                Diagnostic(message="e"),
                Diagnostic(message="w", severity=Severity.WARNING),
            ),
        )
        assert len(result.errors) == 1

    def test_a_cached_unit_says_it_is_up_to_date(self) -> None:
        assert "up to date" in UnitResult(unit="a", status=BuildStatus.CACHED).summary()

    def test_a_failure_counts_its_errors(self) -> None:
        result = UnitResult(
            unit="a", status=BuildStatus.FAILED, diagnostics=(Diagnostic(message="e"),)
        )
        assert "1 error(s)" in result.summary()

    def test_feedback_names_the_diagnostics(self) -> None:
        result = UnitResult(
            unit="a",
            status=BuildStatus.FAILED,
            diagnostics=(Diagnostic(message="bad thing", file="a.c", line=3),),
        )
        assert "a.c:3" in result.feedback()

    def test_feedback_truncates_a_flood(self) -> None:
        many = tuple(Diagnostic(message=f"e{i}", file="a.c", line=i) for i in range(30))
        text = UnitResult(unit="a", status=BuildStatus.FAILED, diagnostics=many).feedback()
        assert "and 20 more" in text

    def test_a_broken_tool_says_so_in_the_feedback(self) -> None:
        """FR-4.4: otherwise the next attempt rewrites code that compiles."""
        result = UnitResult(
            unit="a", status=BuildStatus.ERRORED, reason="cargo is not installed"
        )
        text = result.feedback()
        assert "build tool" in text
        assert "Do not change source code" in text

    def test_feedback_falls_back_to_the_output(self) -> None:
        result = UnitResult(unit="a", status=BuildStatus.FAILED, output="something odd")
        assert "something odd" in result.feedback()


class TestBuildReport:
    """The whole build, summarized honestly."""

    def _report(self) -> BuildReport:
        return BuildReport(
            results=(
                UnitResult(unit="a", status=BuildStatus.SUCCEEDED, duration_s=1.0),
                UnitResult(unit="b", status=BuildStatus.CACHED),
                UnitResult(
                    unit="c",
                    status=BuildStatus.FAILED,
                    diagnostics=(Diagnostic(message="no", file="c.py", line=1),),
                ),
                UnitResult(unit="d", status=BuildStatus.BLOCKED),
            ),
            duration_s=2.0,
        )

    def test_units_are_partitioned_by_outcome(self) -> None:
        report = self._report()
        assert report.rebuilt_units == ("a",)
        assert report.cached_units == ("b",)
        assert report.failed_units == ("c", "d")

    def test_a_build_with_a_failure_is_not_ok(self) -> None:
        assert not self._report().ok

    def test_an_all_green_build_is_ok(self) -> None:
        report = BuildReport(results=(UnitResult(unit="a", status=BuildStatus.CACHED),))
        assert report.ok

    def test_a_cancelled_build_is_never_ok(self) -> None:
        report = BuildReport(
            results=(UnitResult(unit="a", status=BuildStatus.SUCCEEDED),), cancelled=True
        )
        assert not report.ok

    def test_harness_problems_are_reported_separately(self) -> None:
        report = BuildReport(
            results=(
                UnitResult(unit="a", status=BuildStatus.ERRORED),
                UnitResult(unit="b", status=BuildStatus.FAILED),
            )
        )
        assert report.harness_problems == ("a",)

    def test_the_summary_counts_both_kinds_of_work(self) -> None:
        summary = self._report().summary()
        assert "1 rebuilt" in summary
        assert "1 cached" in summary
        assert "2 failed" in summary

    def test_an_empty_build_says_so(self) -> None:
        assert BuildReport().summary() == "nothing to build"

    def test_feedback_mentions_what_was_not_attempted(self) -> None:
        assert "a dependency failed: d" in self._report().feedback()

    def test_diagnostics_are_flattened(self) -> None:
        assert len(self._report().diagnostics) == 1


class TestProjectLayout:
    """The project as the builder sees it."""

    def test_duplicate_unit_names_are_refused(self) -> None:
        with pytest.raises(BuildConfigError, match="share a name"):
            ProjectLayout(root=Path("/p"), units=(unit("a"), unit("a")))

    def test_units_are_looked_up_by_name(self) -> None:
        assert layout(unit("a")).unit("a").name == "a"

    def test_an_unknown_unit_is_refused(self) -> None:
        with pytest.raises(BuildConfigError, match="no build unit named"):
            layout(unit("a")).unit("ghost")

    def test_sources_are_grouped_by_unit(self) -> None:
        project = layout(unit("a"), unit("b"))
        assert [s.path for s in project.sources_of("a")] == ["a/main.py"]

    def test_ownership_prefers_the_most_specific_claim(self) -> None:
        """A nested unit must not be shadowed by its parent."""
        project = layout(
            unit("outer", sources=("src",)),
            unit("inner", sources=("src/inner",)),
            sources={"src/inner/x.py": "x", "src/other.py": "y"},
        )
        assert project.owner_of("src/inner/x.py") == "inner"
        assert project.owner_of("src/other.py") == "outer"

    def test_an_unowned_path_has_no_owner(self) -> None:
        assert layout(unit("a")).owner_of("docs/readme.md") is None

    def test_a_digest_over_paths_is_order_independent(self) -> None:
        project = layout(unit("a"), unit("b"))
        one = project.digest_of(["a/main.py", "b/main.py"])
        two = project.digest_of(["b/main.py", "a/main.py"])
        assert one == two

    def test_the_kind_defaults_to_unknown(self) -> None:
        assert layout(unit("a")).kind is ProjectKind.UNKNOWN


class TestArtifact:
    """Outputs are identified by content too."""

    def test_presence_is_checked_against_disk(self, project: Path) -> None:
        (project / "out.bin").write_bytes(b"x")
        artifact = Artifact(path="out.bin", digest=digest_bytes(b"x"), size=1)
        assert artifact.exists_in(project)
        assert not Artifact(path="gone.bin", digest="d").exists_in(project)

    def test_an_escaping_artifact_path_is_refused(self) -> None:
        with pytest.raises(BuildConfigError):
            Artifact(path="../out.bin", digest="d")


class TestSourceFile:
    """Sources normalize their paths like everything else."""

    def test_paths_are_normalized(self) -> None:
        assert SourceFile(path="src\\a.py", digest="d").path == "src/a.py"

    def test_an_absolute_path_is_refused(self) -> None:
        with pytest.raises(BuildConfigError):
            SourceFile(path="/etc/passwd", digest="d")

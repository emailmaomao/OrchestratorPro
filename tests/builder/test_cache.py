"""Tests for the build cache, fingerprints, and artifact tracking."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.builder.analysis import UnitGraph
from orchestrator.builder.cache import (
    ArtifactTracker,
    CacheEntry,
    MemoryCache,
    SqliteCache,
    fingerprint_for,
    fingerprints_for,
)
from orchestrator.builder.model import (
    Artifact,
    BuildStatus,
    Diagnostic,
    ProjectLayout,
    Severity,
    digest_bytes,
)
from orchestrator.core.storage import Database

from tests.builder.conftest import layout, unit


def chain() -> tuple[ProjectLayout, UnitGraph]:
    """A two-unit project where ``app`` depends on ``core``."""
    project = layout(
        unit("core", sources=("core",)),
        unit("app", sources=("app",), depends_on=("core",)),
        sources={"core/a.py": "core v1", "app/b.py": "app v1"},
    )
    return project, UnitGraph(project.units)


class TestFingerprints:
    """The key has to change exactly when the answer would."""

    def test_the_same_inputs_fingerprint_the_same(self) -> None:
        project, graph = chain()
        one = fingerprints_for(project, graph.names)
        two = fingerprints_for(project, graph.names)
        assert one == two

    def test_a_changed_source_changes_the_key(self) -> None:
        project, graph = chain()
        before = fingerprints_for(project, graph.names)["core"]

        edited = layout(
            *project.units, sources={"core/a.py": "core v2", "app/b.py": "app v1"}
        )
        after = fingerprints_for(edited, graph.names)["core"]

        assert before != after

    def test_a_dependency_change_reaches_its_dependents(self) -> None:
        """This is what makes rebuild propagation correct without walking twice."""
        project, graph = chain()
        before = fingerprints_for(project, graph.names)["app"]

        edited = layout(
            *project.units, sources={"core/a.py": "core v2", "app/b.py": "app v1"}
        )
        after = fingerprints_for(edited, graph.names)["app"]

        assert before != after

    def test_an_unrelated_change_leaves_a_unit_alone(self) -> None:
        project, graph = chain()
        before = fingerprints_for(project, graph.names)["core"]

        edited = layout(
            *project.units, sources={"core/a.py": "core v1", "app/b.py": "app v2"}
        )
        after = fingerprints_for(edited, graph.names)["core"]

        assert before == after

    def test_a_changed_command_changes_the_key(self) -> None:
        project, _graph = chain()
        rebuilt = project.with_units(
            (
                unit("core", sources=("core",), command="make core --release"),
                project.unit("app"),
            )
        )
        assert (
            fingerprints_for(project, ("core", "app"))["core"]
            != fingerprints_for(rebuilt, ("core", "app"))["core"]
        )

    def test_a_dependency_must_be_fingerprinted_first(self) -> None:
        project, _graph = chain()
        with pytest.raises(KeyError):
            fingerprint_for(project.unit("app"), project, {})

    def test_a_unit_with_no_sources_still_has_a_key(self) -> None:
        project = layout(unit("empty", sources=()), sources={})
        assert fingerprints_for(project, ("empty",))["empty"]


class TestCacheEntry:
    """A hit is verified before it is trusted."""

    def test_only_a_success_is_reusable(self) -> None:
        assert CacheEntry(key="k", unit="a").usable
        assert not CacheEntry(key="k", unit="a", status=BuildStatus.FAILED).usable

    def test_an_entry_with_present_artifacts_verifies(self, project: Path) -> None:
        (project / "out.bin").write_bytes(b"content")
        entry = CacheEntry(
            key="k",
            unit="a",
            artifacts=(Artifact(path="out.bin", digest=digest_bytes(b"content")),),
        )
        valid, stale = entry.verify(project)

        assert valid
        assert stale == ()

    def test_a_deleted_artifact_invalidates_the_entry(self, project: Path) -> None:
        """A cache that lies about outputs is worse than no cache."""
        entry = CacheEntry(
            key="k", unit="a", artifacts=(Artifact(path="gone.bin", digest="d"),)
        )
        valid, stale = entry.verify(project)

        assert not valid
        assert stale == ("gone.bin",)

    def test_an_altered_artifact_invalidates_the_entry(self, project: Path) -> None:
        (project / "out.bin").write_bytes(b"tampered")
        entry = CacheEntry(
            key="k",
            unit="a",
            artifacts=(Artifact(path="out.bin", digest=digest_bytes(b"original")),),
        )
        assert not entry.verify(project)[0]

    def test_an_entry_with_no_artifacts_verifies_trivially(self, project: Path) -> None:
        assert CacheEntry(key="k", unit="a").verify(project)[0]


class TestMemoryCache:
    """The in-process cache."""

    def test_put_then_get(self, cache: MemoryCache) -> None:
        cache.put(CacheEntry(key="k", unit="a"))
        entry = cache.get("k")
        assert entry is not None
        assert entry.unit == "a"

    def test_a_miss_is_none(self, cache: MemoryCache) -> None:
        assert cache.get("nope") is None

    def test_the_same_key_is_replaced(self, cache: MemoryCache) -> None:
        cache.put(CacheEntry(key="k", unit="a", command="one"))
        cache.put(CacheEntry(key="k", unit="a", command="two"))
        entry = cache.get("k")
        assert entry is not None
        assert entry.command == "two"
        assert len(cache) == 1

    def test_eviction_is_by_unit(self, cache: MemoryCache) -> None:
        cache.put(CacheEntry(key="k1", unit="a"))
        cache.put(CacheEntry(key="k2", unit="a"))
        cache.put(CacheEntry(key="k3", unit="b"))

        assert cache.evict("a") == 2
        assert cache.get("k3") is not None

    def test_clearing_forgets_everything(self, cache: MemoryCache) -> None:
        cache.put(CacheEntry(key="k", unit="a"))
        assert cache.clear() == 1
        assert len(cache) == 0


class TestSqliteCache:
    """The cache that survives the process."""

    def test_an_entry_round_trips(self, db: Database) -> None:
        cache = SqliteCache(db)
        cache.put(
            CacheEntry(
                key="k",
                unit="a",
                artifacts=(Artifact(path="out.bin", digest="d", size=3, unit="a"),),
                diagnostics=(
                    Diagnostic(
                        message="careful",
                        file="a.py",
                        line=4,
                        severity=Severity.WARNING,
                        code="W1",
                    ),
                ),
                command="make a",
                duration_s=1.5,
                output="log",
            )
        )
        entry = cache.get("k")

        assert entry is not None
        assert entry.unit == "a"
        assert entry.artifacts[0].path == "out.bin"
        assert entry.artifacts[0].size == 3
        assert entry.diagnostics[0].severity is Severity.WARNING
        assert entry.diagnostics[0].line == 4
        assert entry.duration_s == pytest.approx(1.5)
        assert entry.command == "make a"

    def test_a_miss_is_none(self, db: Database) -> None:
        assert SqliteCache(db).get("nope") is None

    def test_the_tables_survive_a_reopen(self, tmp_dir: Path) -> None:
        """The point of persisting: a rebuild after a restart is still skipped."""
        path = tmp_dir / "build.db"
        with Database(path) as first:
            first.migrate()
            SqliteCache(first).put(CacheEntry(key="k", unit="a"))

        with Database(path) as second:
            second.migrate()
            assert SqliteCache(second).get("k") is not None

    def test_construction_is_idempotent(self, db: Database) -> None:
        SqliteCache(db)
        cache = SqliteCache(db)
        cache.put(CacheEntry(key="k", unit="a"))
        assert cache.get("k") is not None

    def test_replacing_an_entry_replaces_its_artifacts(self, db: Database) -> None:
        cache = SqliteCache(db)
        cache.put(
            CacheEntry(key="k", unit="a", artifacts=(Artifact(path="old", digest="d"),))
        )
        cache.put(
            CacheEntry(key="k", unit="a", artifacts=(Artifact(path="new", digest="d"),))
        )

        assert [a.path for a in cache.artifacts_of("a")] == ["new"]

    def test_eviction_removes_artifacts_too(self, db: Database) -> None:
        cache = SqliteCache(db)
        cache.put(
            CacheEntry(key="k", unit="a", artifacts=(Artifact(path="out", digest="d"),))
        )
        assert cache.evict("a") == 1
        assert cache.artifacts_of("a") == ()
        assert cache.get("k") is None

    def test_clearing_forgets_everything(self, db: Database) -> None:
        cache = SqliteCache(db)
        cache.put(CacheEntry(key="k1", unit="a"))
        cache.put(CacheEntry(key="k2", unit="b"))

        assert cache.clear() == 2
        assert len(cache) == 0

    def test_the_build_tables_do_not_disturb_the_run_tables(self, db: Database) -> None:
        """Clearing a build cache must never touch the event log."""
        cache = SqliteCache(db)
        cache.put(CacheEntry(key="k", unit="a"))
        cache.clear()

        assert db.query("SELECT COUNT(*) AS n FROM events")[0]["n"] == 0
        assert db.schema_version > 0


class TestArtifactTracker:
    """What the build actually produced, read from disk."""

    def test_a_named_file_is_collected(self, project: Path) -> None:
        (project / "out.bin").write_bytes(b"hello")
        found = ArtifactTracker().collect(project, unit("a", artifacts=("out.bin",)))

        assert [a.path for a in found] == ["out.bin"]
        assert found[0].digest == digest_bytes(b"hello")
        assert found[0].unit == "a"

    def test_a_directory_is_collected_recursively(self, project: Path) -> None:
        (project / "dist/nested").mkdir(parents=True)
        (project / "dist/one.js").write_bytes(b"1")
        (project / "dist/nested/two.js").write_bytes(b"2")
        found = ArtifactTracker().collect(project, unit("a", artifacts=("dist",)))

        assert [a.path for a in found] == ["dist/nested/two.js", "dist/one.js"]

    def test_a_pattern_that_matched_nothing_yields_nothing(self, project: Path) -> None:
        assert ArtifactTracker().collect(project, unit("a", artifacts=("gone",))) == ()

    def test_ignored_suffixes_are_skipped(self, project: Path) -> None:
        (project / "dist").mkdir()
        (project / "dist/app.js").write_bytes(b"1")
        (project / "dist/app.js.map").write_bytes(b"2")
        tracker = ArtifactTracker(ignored_suffixes=(".map",))

        found = tracker.collect(project, unit("a", artifacts=("dist",)))
        assert [a.path for a in found] == ["dist/app.js"]

    def test_collection_is_ordered_by_path(self, project: Path) -> None:
        (project / "dist").mkdir()
        for name in ("c", "a", "b"):
            (project / f"dist/{name}.js").write_bytes(b"x")
        found = ArtifactTracker().collect(project, unit("a", artifacts=("dist",)))

        assert [a.path for a in found] == sorted(a.path for a in found)

    def test_missing_artifacts_are_named(self, project: Path) -> None:
        artifacts = (Artifact(path="a.bin", digest="d"), Artifact(path="b.bin", digest="d"))
        (project / "a.bin").write_bytes(b"x")

        assert ArtifactTracker.missing(project, artifacts) == ("b.bin",)

    def test_drifted_artifacts_are_named(self, project: Path) -> None:
        (project / "a.bin").write_bytes(b"changed")
        artifacts = (Artifact(path="a.bin", digest=digest_bytes(b"original")),)

        assert ArtifactTracker.drifted(project, artifacts) == ("a.bin",)

    def test_an_unchanged_artifact_has_not_drifted(self, project: Path) -> None:
        (project / "a.bin").write_bytes(b"same")
        artifacts = (Artifact(path="a.bin", digest=digest_bytes(b"same")),)

        assert ArtifactTracker.drifted(project, artifacts) == ()

    def test_the_same_tree_collects_identically(self, project: Path) -> None:
        (project / "dist").mkdir()
        (project / "dist/app.js").write_bytes(b"content")
        declared = unit("a", artifacts=("dist",))

        assert ArtifactTracker().collect(project, declared) == ArtifactTracker().collect(
            project, declared
        )

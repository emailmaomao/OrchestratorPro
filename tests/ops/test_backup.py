"""Tests for backup, verification, restore, and retention."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from orchestrator.core.events import Event, EventType, RunId
from orchestrator.core.run_store import RunStore
from orchestrator.core.storage import Database
from orchestrator.ops.backup import (
    BackupError,
    BackupManifest,
    create_backup,
    digest_of,
    list_backups,
    manifest_path,
    prune_backups,
    restore_backup,
    total_size,
    verify_backup,
)

from tests.ops.conftest import populated_database


class TestCreate:
    """Taking a snapshot."""

    def test_a_snapshot_is_written(self, database: Database, backups: Path) -> None:
        manifest = create_backup(database, backups)

        assert Path(manifest.path).is_file()
        assert manifest.runs == 2
        assert manifest.events == 6

    def test_the_manifest_is_written_beside_it(
        self, database: Database, backups: Path
    ) -> None:
        manifest = create_backup(database, backups)
        assert manifest_path(Path(manifest.path)).is_file()

    def test_the_manifest_records_the_digest(
        self, database: Database, backups: Path
    ) -> None:
        manifest = create_backup(database, backups)
        assert manifest.digest == digest_of(Path(manifest.path))

    def test_a_label_reaches_the_filename(
        self, database: Database, backups: Path
    ) -> None:
        manifest = create_backup(database, backups, label="pre-upgrade")
        assert "pre-upgrade" in Path(manifest.path).name

    def test_the_directory_is_created(self, database: Database, tmp_dir: Path) -> None:
        manifest = create_backup(database, tmp_dir / "deep" / "nested")
        assert Path(manifest.path).is_file()

    def test_the_source_keeps_working_afterwards(
        self, database: Database, backups: Path
    ) -> None:
        """An online backup must not disturb the database it copied."""
        create_backup(database, backups)
        store = RunStore(database)
        store.record(
            Event.new(
                EventType.RUN_CREATED,
                run_id=RunId.generate(),
                payload={"goal": "after", "repo_path": "/r"},
            )
        )
        assert len(store.run_ids()) == 3

    def test_the_snapshot_does_not_grow_with_the_source(
        self, database: Database, backups: Path
    ) -> None:
        """It is a point in time, not a live mirror."""
        manifest = create_backup(database, backups)
        RunStore(database).record(
            Event.new(
                EventType.RUN_CREATED,
                run_id=RunId.generate(),
                payload={"goal": "later", "repo_path": "/r"},
            )
        )
        assert verify_backup(Path(manifest.path)).runs == 2

    def test_an_in_memory_database_is_refused(self, backups: Path) -> None:
        db = Database.in_memory()
        db.migrate()

        with pytest.raises(BackupError, match="in-memory"):
            create_backup(db, backups)

    def test_a_missing_database_is_refused(self, tmp_dir: Path, backups: Path) -> None:
        with pytest.raises(BackupError, match="no database"):
            create_backup(tmp_dir / "absent.db", backups)

    def test_it_refuses_to_overwrite_a_snapshot(
        self, database: Database, backups: Path
    ) -> None:
        when = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
        create_backup(database, backups, now=when)

        with pytest.raises(BackupError, match="already exists"):
            create_backup(database, backups, now=when)


class TestVerify:
    """Checking a snapshot before relying on it."""

    def test_a_fresh_snapshot_verifies(self, database: Database, backups: Path) -> None:
        manifest = create_backup(database, backups)
        assert verify_backup(Path(manifest.path)).digest == manifest.digest

    def test_a_missing_snapshot_is_refused(self, tmp_dir: Path) -> None:
        with pytest.raises(BackupError, match="no snapshot"):
            verify_backup(tmp_dir / "absent.db")

    def test_a_snapshot_with_no_manifest_cannot_be_verified(
        self, database: Database, backups: Path
    ) -> None:
        """"It exists" is not verification."""
        manifest = create_backup(database, backups)
        manifest_path(Path(manifest.path)).unlink()

        with pytest.raises(BackupError, match="no manifest"):
            verify_backup(Path(manifest.path))

    def test_a_tampered_snapshot_is_caught(
        self, database: Database, backups: Path
    ) -> None:
        manifest = create_backup(database, backups)
        snapshot = Path(manifest.path)
        snapshot.write_bytes(snapshot.read_bytes() + b"tampered")

        with pytest.raises(BackupError, match="does not match its manifest"):
            verify_backup(snapshot)

    def test_a_truncated_snapshot_is_caught(
        self, database: Database, backups: Path
    ) -> None:
        manifest = create_backup(database, backups)
        snapshot = Path(manifest.path)
        snapshot.write_bytes(snapshot.read_bytes()[: len(snapshot.read_bytes()) // 2])

        with pytest.raises(BackupError):
            verify_backup(snapshot)

    def test_an_unreadable_manifest_is_refused(
        self, database: Database, backups: Path
    ) -> None:
        manifest = create_backup(database, backups)
        manifest_path(Path(manifest.path)).write_text("not json", encoding="utf-8")

        with pytest.raises(BackupError, match="not readable"):
            verify_backup(Path(manifest.path))


class TestRestore:
    """Putting a snapshot back."""

    def test_a_snapshot_is_restored(
        self, database: Database, backups: Path, tmp_dir: Path
    ) -> None:
        manifest = create_backup(database, backups)
        target = tmp_dir / "restored.db"

        restore_backup(Path(manifest.path), target)

        restored = Database(target)
        try:
            assert len(RunStore(restored).run_ids()) == 2
        finally:
            restored.close()

    def test_the_existing_database_is_moved_aside(
        self, database: Database, backups: Path, tmp_dir: Path
    ) -> None:
        """Restoring the wrong snapshot at four in the morning is recoverable."""
        manifest = create_backup(database, backups)
        target = tmp_dir / "existing.db"
        target.write_bytes(b"the old one")

        restore_backup(Path(manifest.path), target)

        aside = [p for p in tmp_dir.glob("existing.db.replaced-*")]
        assert aside and aside[0].read_bytes() == b"the old one"

    def test_it_can_be_told_not_to_keep_the_old_one(
        self, database: Database, backups: Path, tmp_dir: Path
    ) -> None:
        manifest = create_backup(database, backups)
        target = tmp_dir / "existing.db"
        target.write_bytes(b"the old one")

        restore_backup(Path(manifest.path), target, keep_existing=False)

        assert not list(tmp_dir.glob("existing.db.replaced-*"))

    def test_stale_journals_are_removed(
        self, database: Database, backups: Path, tmp_dir: Path
    ) -> None:
        """They describe the database that was there before, not this one."""
        manifest = create_backup(database, backups)
        target = tmp_dir / "existing.db"
        target.write_bytes(b"old")
        Path(f"{target}-wal").write_bytes(b"stale")
        Path(f"{target}-shm").write_bytes(b"stale")

        restore_backup(Path(manifest.path), target)

        assert not Path(f"{target}-wal").exists()
        assert not Path(f"{target}-shm").exists()

    def test_a_corrupt_snapshot_is_never_restored(
        self, database: Database, backups: Path, tmp_dir: Path
    ) -> None:
        """Verified before anything is touched."""
        manifest = create_backup(database, backups)
        snapshot = Path(manifest.path)
        snapshot.write_bytes(snapshot.read_bytes() + b"x")
        target = tmp_dir / "target.db"
        target.write_bytes(b"still here")

        with pytest.raises(BackupError):
            restore_backup(snapshot, target)

        assert target.read_bytes() == b"still here"

    def test_a_future_schema_is_refused(
        self, database: Database, backups: Path, tmp_dir: Path
    ) -> None:
        """A snapshot from a newer build must not be opened by an older one."""
        import sqlite3

        manifest = create_backup(database, backups)
        snapshot = Path(manifest.path)

        connection = sqlite3.connect(snapshot)
        try:
            connection.execute("PRAGMA user_version = 999")
            connection.commit()
        finally:
            connection.close()

        rewritten = BackupManifest(
            path=manifest.path,
            created_at=manifest.created_at,
            size_bytes=snapshot.stat().st_size,
            digest=digest_of(snapshot),
            schema_version=999,
            runs=manifest.runs,
            events=manifest.events,
        )
        manifest_path(snapshot).write_text(rewritten.to_json(), encoding="utf-8")

        with pytest.raises(BackupError, match="newer build"):
            restore_backup(snapshot, tmp_dir / "target.db")

    def test_a_round_trip_preserves_every_event(
        self, tmp_dir: Path, backups: Path
    ) -> None:
        """The point of the whole module."""
        source = populated_database(tmp_dir / "source.db", runs=3, events_per_run=5)
        before = source.query("SELECT COUNT(*) AS n FROM events")[0]["n"]
        manifest = create_backup(source, backups)
        source.close()

        target = tmp_dir / "restored.db"
        restore_backup(Path(manifest.path), target)

        restored = Database(target)
        try:
            after = restored.query("SELECT COUNT(*) AS n FROM events")[0]["n"]
        finally:
            restored.close()

        assert after == before == 15


class TestListAndPrune:
    """Managing the directory over time."""

    def test_an_empty_directory_lists_nothing(self, backups: Path) -> None:
        assert list_backups(backups) == ()

    def test_a_missing_directory_lists_nothing(self, tmp_dir: Path) -> None:
        assert list_backups(tmp_dir / "absent") == ()

    def test_snapshots_are_listed_newest_first(
        self, database: Database, backups: Path
    ) -> None:
        for hour in range(3):
            create_backup(database, backups, now=datetime(2026, 7, 26, hour, tzinfo=UTC))

        listed = list_backups(backups)
        assert [m.created_at for m in listed] == sorted(
            (m.created_at for m in listed), reverse=True
        )

    def test_a_snapshot_without_a_manifest_is_skipped(
        self, database: Database, backups: Path
    ) -> None:
        """One unreadable file should not hide the others."""
        good = create_backup(database, backups, now=datetime(2026, 7, 26, 1, tzinfo=UTC))
        orphan = backups / "orchestrator-orphan.db"
        orphan.write_bytes(b"no manifest")

        listed = list_backups(backups)
        assert [m.path for m in listed] == [good.path]

    def test_pruning_keeps_the_newest(self, database: Database, backups: Path) -> None:
        for hour in range(5):
            create_backup(database, backups, now=datetime(2026, 7, 26, hour, tzinfo=UTC))

        removed = prune_backups(backups, keep=2)

        assert len(removed) == 3
        assert len(list_backups(backups)) == 2

    def test_pruning_removes_the_manifests_too(
        self, database: Database, backups: Path
    ) -> None:
        for hour in range(3):
            create_backup(database, backups, now=datetime(2026, 7, 26, hour, tzinfo=UTC))

        prune_backups(backups, keep=1)
        assert len(list(backups.glob("*.manifest.json"))) == 1

    def test_a_dry_run_deletes_nothing(self, database: Database, backups: Path) -> None:
        for hour in range(3):
            create_backup(database, backups, now=datetime(2026, 7, 26, hour, tzinfo=UTC))

        removed = prune_backups(backups, keep=1, dry_run=True)

        assert len(removed) == 2
        assert len(list_backups(backups)) == 3

    def test_keeping_nothing_is_refused(self, backups: Path) -> None:
        """A retention policy that can empty the directory is a deletion policy."""
        with pytest.raises(BackupError, match="at least 1"):
            prune_backups(backups, keep=0)

    def test_pruning_below_the_count_removes_nothing(
        self, database: Database, backups: Path
    ) -> None:
        create_backup(database, backups)
        assert prune_backups(backups, keep=10) == ()

    def test_the_total_size_is_reported(
        self, database: Database, backups: Path
    ) -> None:
        for hour in range(2):
            create_backup(database, backups, now=datetime(2026, 7, 26, hour, tzinfo=UTC))

        assert total_size(list_backups(backups)) > 0


class TestManifest:
    """The record beside each snapshot."""

    def test_it_round_trips(self) -> None:
        manifest = BackupManifest(
            path="/b/x.db",
            created_at="2026-07-26T00:00:00+00:00",
            size_bytes=1024,
            digest="abc",
            schema_version=1,
            runs=2,
            events=9,
            source="/var/runs.db",
        )
        assert BackupManifest.from_json(manifest.to_json()) == manifest

    def test_it_is_written_deterministically(self) -> None:
        manifest = BackupManifest(
            path="/b/x.db",
            created_at="t",
            size_bytes=1,
            digest="d",
            schema_version=1,
            runs=0,
            events=0,
        )
        assert manifest.to_json() == manifest.to_json()

    def test_a_malformed_manifest_is_refused(self) -> None:
        with pytest.raises(BackupError, match="not readable"):
            BackupManifest.from_json("{}")

    def test_the_summary_reads_naturally(self) -> None:
        manifest = BackupManifest(
            path="/b/orchestrator-x.db",
            created_at="2026-07-26T00:00:00+00:00",
            size_bytes=2_097_152,
            digest="d",
            schema_version=1,
            runs=3,
            events=40,
        )
        summary = manifest.summary()

        assert "40 event(s)" in summary
        assert "3 run(s)" in summary
        assert "2.0 MiB" in summary

"""Tests for archiving, pruning, restoring, and the policy that drives them."""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from orchestrator.core.events import Event, EventType, RunId, TaskId
from orchestrator.core.run_store import RunStore
from orchestrator.core.storage import Database
from orchestrator.ops.retention import (
    ARCHIVE_SUFFIX,
    ArchiveManifest,
    RetentionError,
    RetentionPolicy,
    apply_retention,
    archive_path,
    archive_run,
    list_archives,
    plan_retention,
    prune_run,
    read_archive,
    restore_run,
    verify_all,
    verify_archive,
)


@pytest.fixture
def store(tmp_dir: Path) -> Iterator[RunStore]:
    """A file-backed run store, closed afterwards.

    Closed explicitly: Windows will not delete a file that is still open, so a
    fixture that leaks the handle turns every test in the module into a
    cleanup error.
    """
    database = Database(tmp_dir / "runs.db")
    database.migrate()
    yield RunStore(database)
    database.close()


@pytest.fixture
def archives(tmp_dir: Path) -> Path:
    """An empty archive directory."""
    directory = tmp_dir / "archives"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def finished_run(
    store: RunStore, *, goal: str = "g", tasks: int = 1, when: datetime | None = None
) -> RunId:
    """Record a complete run and return its identifier."""
    run_id = RunId.generate()
    store.record(
        Event.new(
            EventType.RUN_CREATED, run_id=run_id, payload={"goal": goal, "repo_path": "/r"}
        )
    )
    for index in range(tasks):
        store.record(
            Event.new(
                EventType.TASK_CREATED,
                run_id=run_id,
                task_id=TaskId.generate(),
                payload={"title": f"task {index}", "prompt": "p"},
            )
        )
    event = Event.new(
        EventType.RUN_FINISHED, run_id=run_id, payload={"outcome": "succeeded"}
    )
    if when is not None:
        from dataclasses import replace

        event = replace(event, ts=when)
    store.record(event)
    return run_id


def running_run(store: RunStore) -> RunId:
    """Record a run that has not finished."""
    run_id = RunId.generate()
    store.record(
        Event.new(EventType.RUN_CREATED, run_id=run_id, payload={"goal": "g", "repo_path": "/r"})
    )
    store.record(Event.new(EventType.RUN_STARTED, run_id=run_id))
    return run_id


class TestPolicy:
    """What to keep."""

    def test_the_defaults_are_conservative(self) -> None:
        policy = RetentionPolicy()

        assert policy.archive is True
        assert policy.include_cancelled is False
        assert policy.keep_days >= 7

    def test_keeping_everything_is_expressible(self) -> None:
        assert RetentionPolicy(keep_runs=None).keeps_everything

    def test_a_negative_count_is_refused(self) -> None:
        with pytest.raises(RetentionError, match="not be negative"):
            RetentionPolicy(keep_runs=-1)

    def test_emptying_the_backup_directory_is_refused(self) -> None:
        with pytest.raises(RetentionError, match="deletion policy"):
            RetentionPolicy(keep_backups=0)

    def test_it_describes_itself(self) -> None:
        assert "keep every run" in RetentionPolicy(keep_runs=None).describe()

    def test_deleting_without_archiving_says_so_loudly(self) -> None:
        """A policy an operator can misread is one they will."""
        assert "DELETE WITHOUT ARCHIVING" in RetentionPolicy(archive=False).describe()


class TestPlanning:
    """Which runs are eligible."""

    def test_a_recent_run_is_kept(self, store: RunStore) -> None:
        finished_run(store)
        plan = plan_retention(store, RetentionPolicy(keep_runs=0, keep_days=30))

        assert plan.is_empty

    def test_an_old_run_beyond_the_count_is_eligible(self, store: RunStore) -> None:
        old = datetime.now(UTC) - timedelta(days=60)
        run_id = finished_run(store, when=old)

        plan = plan_retention(store, RetentionPolicy(keep_runs=0, keep_days=30))
        assert plan.eligible == (run_id,)

    def test_an_active_run_is_never_eligible(self, store: RunStore) -> None:
        """Archiving a run mid-flight would archive half of it."""
        running_run(store)
        plan = plan_retention(store, RetentionPolicy(keep_runs=0, keep_days=0))

        assert plan.is_empty

    def test_the_newest_are_kept_by_count(self, store: RunStore) -> None:
        old = datetime.now(UTC) - timedelta(days=60)
        for _ in range(5):
            finished_run(store, when=old)

        plan = plan_retention(store, RetentionPolicy(keep_runs=2, keep_days=30))
        assert len(plan.eligible) == 3
        assert len(plan.kept) == 2

    def test_both_conditions_must_permit_removal(self, store: RunStore) -> None:
        """A busy week must not evict this morning's run."""
        finished_run(store)
        finished_run(store)
        finished_run(store)

        plan = plan_retention(store, RetentionPolicy(keep_runs=1, keep_days=30))
        assert plan.is_empty

    def test_keeping_everything_removes_nothing(self, store: RunStore) -> None:
        finished_run(store, when=datetime.now(UTC) - timedelta(days=999))
        plan = plan_retention(store, RetentionPolicy(keep_runs=None))

        assert plan.is_empty
        assert len(plan.kept) == 1

    def test_a_cancelled_run_is_kept_by_default(self, store: RunStore) -> None:
        """Usually one somebody means to resume."""
        run_id = RunId.generate()
        store.record(
            Event.new(EventType.RUN_CREATED, run_id=run_id, payload={"goal": "g", "repo_path": ""})
        )
        from dataclasses import replace

        store.record(
            replace(
                Event.new(EventType.RUN_CANCELLED, run_id=run_id, payload={"reason": "x"}),
                ts=datetime.now(UTC) - timedelta(days=99),
            )
        )

        assert plan_retention(store, RetentionPolicy(keep_runs=0, keep_days=1)).is_empty

    def test_cancelled_runs_can_be_included(self, store: RunStore) -> None:
        from dataclasses import replace

        run_id = RunId.generate()
        store.record(
            Event.new(EventType.RUN_CREATED, run_id=run_id, payload={"goal": "g", "repo_path": ""})
        )
        store.record(
            replace(
                Event.new(EventType.RUN_CANCELLED, run_id=run_id, payload={"reason": "x"}),
                ts=datetime.now(UTC) - timedelta(days=99),
            )
        )

        plan = plan_retention(
            store, RetentionPolicy(keep_runs=0, keep_days=1, include_cancelled=True)
        )
        assert plan.eligible == (run_id,)

    def test_the_plan_summarizes_itself(self, store: RunStore) -> None:
        assert "nothing to remove" in plan_retention(store, RetentionPolicy()).summary()


class TestArchiving:
    """Writing a run out."""

    def test_an_archive_is_written(self, store: RunStore, archives: Path) -> None:
        run_id = finished_run(store, tasks=2)
        manifest = archive_run(store, run_id, archives)

        assert archive_path(archives, run_id).is_file()
        assert manifest.events == 4
        assert manifest.tasks == 2

    def test_it_carries_a_digest(self, store: RunStore, archives: Path) -> None:
        run_id = finished_run(store)
        assert len(archive_run(store, run_id, archives).digest) == 64

    def test_it_is_compressed(self, store: RunStore, archives: Path) -> None:
        run_id = finished_run(store)
        archive_run(store, run_id, archives)

        with gzip.open(archive_path(archives, run_id), "rt", encoding="utf-8") as handle:
            assert "manifest" in json.load(handle)

    def test_a_run_with_no_events_is_refused(self, store: RunStore, archives: Path) -> None:
        with pytest.raises(RetentionError, match="no events"):
            archive_run(store, RunId.generate(), archives)

    def test_it_refuses_to_overwrite(self, store: RunStore, archives: Path) -> None:
        run_id = finished_run(store)
        archive_run(store, run_id, archives)

        with pytest.raises(RetentionError, match="already exists"):
            archive_run(store, run_id, archives)

    def test_the_directory_is_created(self, store: RunStore, tmp_dir: Path) -> None:
        run_id = finished_run(store)
        archive_run(store, run_id, tmp_dir / "deep" / "nested")

        assert archive_path(tmp_dir / "deep" / "nested", run_id).is_file()


class TestVerification:
    """Proving an archive is worth something."""

    def test_a_fresh_archive_verifies(self, store: RunStore, archives: Path) -> None:
        run_id = finished_run(store)
        archive_run(store, run_id, archives)

        assert verify_archive(archive_path(archives, run_id)).run_id == str(run_id)

    def test_a_tampered_archive_is_caught(self, store: RunStore, archives: Path) -> None:
        run_id = finished_run(store)
        archive_run(store, run_id, archives)
        path = archive_path(archives, run_id)

        with gzip.open(path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
        payload["events"][0]["payload"]["goal"] = "something else"
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)

        with pytest.raises(RetentionError, match="has been altered"):
            verify_archive(path)

    def test_a_corrupt_file_is_caught(self, archives: Path) -> None:
        path = archives / f"run_x{ARCHIVE_SUFFIX}"
        path.write_bytes(b"not gzip at all")

        with pytest.raises(RetentionError, match="could not be read"):
            verify_archive(path)

    def test_a_missing_archive_is_refused(self, archives: Path) -> None:
        with pytest.raises(RetentionError, match="no archive"):
            verify_archive(archives / "absent.run.json.gz")

    def test_verification_replays_as_well_as_digests(
        self, store: RunStore, archives: Path
    ) -> None:
        """An archive that decompresses but no longer replays is not a backup."""
        run_id = finished_run(store, tasks=2)
        archive_run(store, run_id, archives)
        manifest, events = read_archive(archive_path(archives, run_id))

        from orchestrator.core.projection import reconstruct

        state = reconstruct(events, run_id=run_id)
        assert len(state.tasks) == manifest.tasks

    def test_verifying_a_directory_reports_both_kinds(
        self, store: RunStore, archives: Path
    ) -> None:
        archive_run(store, finished_run(store), archives)
        (archives / f"broken{ARCHIVE_SUFFIX}").write_bytes(b"junk")

        good, bad = verify_all(archives)
        assert len(good) == 1
        assert len(bad) == 1


class TestPruning:
    """Removing what has been archived."""

    def test_a_run_is_removed(self, store: RunStore) -> None:
        run_id = finished_run(store)

        assert prune_run(store, run_id) == 3
        assert run_id not in store.run_ids()

    def test_the_rows_go_too(self, store: RunStore) -> None:
        run_id = finished_run(store, tasks=2)
        prune_run(store, run_id)

        assert store.db.query("SELECT COUNT(*) AS n FROM tasks")[0]["n"] == 0
        assert store.db.query("SELECT COUNT(*) AS n FROM runs")[0]["n"] == 0

    def test_other_runs_are_untouched(self, store: RunStore) -> None:
        keep = finished_run(store)
        remove = finished_run(store)

        prune_run(store, remove)
        assert store.run_ids() == (keep,)

    def test_the_append_only_triggers_are_restored(self, store: RunStore) -> None:
        """Retention is the only code permitted to touch them, and it puts them back."""
        keep = finished_run(store)
        prune_run(store, finished_run(store))

        # A row must survive: a row trigger does not fire on a DELETE that
        # matches nothing, so deleting from an emptied table proves nothing.
        assert store.events.count(keep)

        with pytest.raises(Exception, match="append-only"):
            with store.db.transaction() as conn:
                conn.execute("DELETE FROM events")

    def test_the_triggers_are_restored_even_after_a_failure(
        self, store: RunStore
    ) -> None:
        run_id = finished_run(store)
        store.db.close()

        with pytest.raises(Exception):
            prune_run(store, run_id)

    def test_pruning_an_absent_run_is_refused(self, store: RunStore) -> None:
        with pytest.raises(RetentionError, match="not in the live database"):
            prune_run(store, RunId.generate())


class TestRestoring:
    """Bringing one back."""

    def test_an_archived_run_is_restored(self, store: RunStore, archives: Path) -> None:
        run_id = finished_run(store, goal="recoverable", tasks=2)
        archive_run(store, run_id, archives)
        prune_run(store, run_id)

        restored = restore_run(store, archive_path(archives, run_id))

        assert restored == 4
        assert store.replay(run_id).goal == "recoverable"

    def test_the_state_matches_what_was_archived(
        self, store: RunStore, archives: Path
    ) -> None:
        run_id = finished_run(store, tasks=3)
        before = store.replay(run_id)
        archive_run(store, run_id, archives)
        prune_run(store, run_id)
        restore_run(store, archive_path(archives, run_id))

        after = store.replay(run_id)
        assert after.goal == before.goal
        assert len(after.tasks) == len(before.tasks)
        assert after.status == before.status

    def test_restoring_over_an_existing_run_is_refused(
        self, store: RunStore, archives: Path
    ) -> None:
        run_id = finished_run(store)
        archive_run(store, run_id, archives)

        with pytest.raises(RetentionError, match="already in this database"):
            restore_run(store, archive_path(archives, run_id))

    def test_a_damaged_archive_is_never_restored(
        self, store: RunStore, archives: Path
    ) -> None:
        path = archives / f"broken{ARCHIVE_SUFFIX}"
        path.write_bytes(b"junk")

        with pytest.raises(RetentionError):
            restore_run(store, path)


class TestApplying:
    """A whole pass."""

    def test_a_dry_run_changes_nothing(self, store: RunStore, archives: Path) -> None:
        """The default, because a destructive default gets run by accident once."""
        old = datetime.now(UTC) - timedelta(days=60)
        finished_run(store, when=old)

        report = apply_retention(
            store, RetentionPolicy(keep_runs=0, keep_days=30), directory=archives
        )

        assert report.pruned
        assert len(store.run_ids()) == 1
        assert not list(archives.glob(f"*{ARCHIVE_SUFFIX}"))

    def test_applying_archives_and_prunes(self, store: RunStore, archives: Path) -> None:
        old = datetime.now(UTC) - timedelta(days=60)
        run_id = finished_run(store, when=old)

        report = apply_retention(
            store,
            RetentionPolicy(keep_runs=0, keep_days=30),
            directory=archives,
            dry_run=False,
        )

        assert report.ok
        assert report.archived == (str(run_id),)
        assert report.pruned == (str(run_id),)
        assert store.run_ids() == ()
        assert archive_path(archives, run_id).is_file()

    def test_nothing_is_pruned_that_was_not_verified(
        self, store: RunStore, archives: Path
    ) -> None:
        """The archive is read back before the run is removed."""
        old = datetime.now(UTC) - timedelta(days=60)
        run_id = finished_run(store, when=old)

        apply_retention(
            store,
            RetentionPolicy(keep_runs=0, keep_days=30),
            directory=archives,
            dry_run=False,
        )
        assert verify_archive(archive_path(archives, run_id))

    def test_a_run_can_be_recovered_after_a_pass(
        self, store: RunStore, archives: Path
    ) -> None:
        """The whole point: retention is not deletion."""
        old = datetime.now(UTC) - timedelta(days=60)
        run_id = finished_run(store, goal="still here", when=old)

        apply_retention(
            store,
            RetentionPolicy(keep_runs=0, keep_days=30),
            directory=archives,
            dry_run=False,
        )
        restore_run(store, archive_path(archives, run_id))

        assert store.replay(run_id).goal == "still here"

    def test_sessions_are_pruned_when_a_store_is_given(
        self, store: RunStore, archives: Path
    ) -> None:
        from orchestrator.auth.models import Role
        from orchestrator.auth.store import AuthStore

        auth = AuthStore(store.db)
        auth.create_user("alice", "a-long-enough-password", Role.VIEWER)
        auth.create_session("alice", ttl_s=-10)

        report = apply_retention(
            store,
            RetentionPolicy(),
            directory=archives,
            dry_run=False,
            auth_store=auth,
        )
        assert report.sessions_pruned == 1

    def test_backups_are_pruned_when_a_directory_is_given(
        self, store: RunStore, archives: Path, tmp_dir: Path
    ) -> None:
        from orchestrator.ops.backup import create_backup

        backups = tmp_dir / "backups"
        for _ in range(3):
            create_backup(store.db, backups)

        report = apply_retention(
            store,
            RetentionPolicy(keep_backups=1),
            directory=archives,
            dry_run=False,
            backup_directory=backups,
        )
        assert len(report.backups_removed) == 2

    def test_a_failure_is_reported_not_swallowed(
        self, store: RunStore, archives: Path
    ) -> None:
        old = datetime.now(UTC) - timedelta(days=60)
        run_id = finished_run(store, when=old)
        archive_run(store, run_id, archives)  # so the pass collides

        report = apply_retention(
            store,
            RetentionPolicy(keep_runs=0, keep_days=30),
            directory=archives,
            dry_run=False,
        )

        assert not report.ok
        assert str(run_id) in report.failures[0]
        assert store.run_ids() == (run_id,)

    def test_the_report_summarizes_itself(self, store: RunStore, archives: Path) -> None:
        report = apply_retention(store, RetentionPolicy(), directory=archives)
        assert "archived" in report.summary()


class TestListing:
    """Seeing what has been archived."""

    def test_an_empty_directory_lists_nothing(self, archives: Path) -> None:
        assert list_archives(archives) == ()

    def test_archives_are_listed_newest_first(
        self, store: RunStore, archives: Path
    ) -> None:
        for _ in range(3):
            archive_run(store, finished_run(store), archives)

        listed = list_archives(archives)
        assert [m.created_at for m in listed] == sorted(
            (m.created_at for m in listed), reverse=True
        )

    def test_an_unreadable_archive_is_skipped(
        self, store: RunStore, archives: Path
    ) -> None:
        archive_run(store, finished_run(store), archives)
        (archives / f"broken{ARCHIVE_SUFFIX}").write_bytes(b"junk")

        assert len(list_archives(archives)) == 1

    def test_a_missing_directory_lists_nothing(self, tmp_dir: Path) -> None:
        assert list_archives(tmp_dir / "absent") == ()


class TestManifest:
    """The record inside each archive."""

    def test_it_round_trips(self) -> None:
        manifest = ArchiveManifest(
            run_id="run_1",
            goal="g",
            status="finished",
            events=5,
            tasks=2,
            created_at="2026-07-26T00:00:00+00:00",
            digest="d",
        )
        assert ArchiveManifest.from_dict(manifest.to_dict()) == manifest

    def test_a_malformed_manifest_is_refused(self) -> None:
        with pytest.raises(RetentionError, match="not readable"):
            ArchiveManifest.from_dict({"run_id": "x"})

    def test_it_summarizes_itself(self) -> None:
        manifest = ArchiveManifest(
            run_id="run_1",
            goal="g",
            status="finished",
            events=5,
            tasks=2,
            created_at="t",
            digest="d",
        )
        assert "5 event(s)" in manifest.summary()

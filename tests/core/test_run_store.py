"""Tests for the transactional store: materialization, rebuild, and recovery."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from orchestrator.core.event_store import DuplicateEventError
from orchestrator.core.events import AttemptId, Event, EventType, RunId, TaskId
from orchestrator.core.records import RunStatus
from orchestrator.core.run_store import RunStore, StateDivergenceError
from orchestrator.core.storage import Database
from orchestrator.task.model import TaskState
from tests.core.conftest import SequenceBuilder


@pytest.fixture
def store() -> Iterator[RunStore]:
    """A run store over a migrated in-memory database."""
    db = Database.in_memory()
    db.migrate()
    yield RunStore(db)
    db.close()


class TestRecord:
    """Recording writes the event and its implied rows in one transaction."""

    def test_recording_populates_the_materialized_tables(
        self, store: RunStore, run_id: RunId, build_sequence: SequenceBuilder
    ) -> None:
        events, task_id, attempt_id = build_sequence(run_id)
        store.record_all(events)

        run_row = store.db.query_one("SELECT * FROM runs WHERE id = ?", (str(run_id),))
        assert run_row is not None
        assert run_row["status"] == RunStatus.FINISHED.value
        assert run_row["goal"] == "migrate the client"
        assert run_row["finished_at"] is not None

        task_row = store.db.query_one("SELECT * FROM tasks WHERE id = ?", (str(task_id),))
        assert task_row is not None
        assert task_row["state"] == TaskState.SUCCEEDED.value
        assert int(task_row["attempts_made"]) == 1

        attempt_row = store.db.query_one(
            "SELECT * FROM attempts WHERE id = ?", (str(attempt_id),)
        )
        assert attempt_row is not None
        assert attempt_row["status"] == "succeeded"
        assert int(attempt_row["tokens_in"]) == 1000

        assert len(store.db.query("SELECT id FROM gate_results")) == 1

    def test_the_event_is_persisted_alongside_the_rows(
        self, store: RunStore, run_id: RunId, build_sequence: SequenceBuilder
    ) -> None:
        events, _, _ = build_sequence(run_id)
        store.record_all(events)
        assert store.events.count(run_id) == len(events)

    def test_an_event_without_a_run_is_refused(self, store: RunStore, build_sequence: SequenceBuilder) -> None:
        with pytest.raises(StateDivergenceError, match="no run_id"):
            store.record(Event.new(EventType.RUN_CREATED))

    def test_duplicate_records_are_refused(self, store: RunStore, run_id: RunId, build_sequence: SequenceBuilder) -> None:
        event = Event.new(EventType.RUN_CREATED, run_id=run_id, payload={"goal": "g"})
        store.record(event)
        with pytest.raises(DuplicateEventError):
            store.record(event)

    def test_a_failed_batch_writes_nothing(self, store: RunStore, run_id: RunId, build_sequence: SequenceBuilder) -> None:
        """Atomicity: the event and its rows land together or not at all."""
        first = Event.new(EventType.RUN_CREATED, run_id=run_id, payload={"goal": "g"})
        store.record(first)

        batch = [Event.new(EventType.RUN_STARTED, run_id=run_id), first]
        with pytest.raises(DuplicateEventError):
            store.record_all(batch)

        assert store.events.count(run_id) == 1
        row = store.db.query_one("SELECT status FROM runs WHERE id = ?", (str(run_id),))
        assert row is not None
        assert row["status"] == RunStatus.CREATED.value


class TestReplayAgreement:
    """The fast path and the truth path must produce the same answer."""

    def test_verify_passes_on_a_normally_recorded_run(
        self, store: RunStore, run_id: RunId, build_sequence: SequenceBuilder
    ) -> None:
        events, _, _ = build_sequence(run_id)
        store.record_all(events)

        agrees, differences = store.verify(run_id)
        assert agrees, differences
        assert differences == ()

    def test_verify_passes_over_a_long_sequence(self, store: RunStore, run_id: RunId, build_sequence: SequenceBuilder) -> None:
        """The incremental writer must not drift from the projection at scale."""
        events: list[Event] = [
            Event.new(EventType.RUN_CREATED, run_id=run_id, payload={"goal": "big"}),
            Event.new(EventType.RUN_STARTED, run_id=run_id),
        ]
        for index in range(30):
            task_id = TaskId.generate()
            attempt_id = AttemptId.generate()
            events += [
                Event.new(
                    EventType.TASK_CREATED,
                    run_id=run_id,
                    task_id=task_id,
                    payload={"title": f"task {index}", "max_attempts": 2},
                ),
                Event.new(EventType.TASK_READY, run_id=run_id, task_id=task_id),
                Event.new(EventType.TASK_STARTED, run_id=run_id, task_id=task_id),
                Event.new(
                    EventType.ATTEMPT_STARTED,
                    run_id=run_id,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    payload={"number": 1},
                ),
                Event.new(
                    EventType.ATTEMPT_FINISHED,
                    run_id=run_id,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    payload={"status": "succeeded", "tokens_in": 7, "tokens_out": 3},
                ),
                Event.new(EventType.TASK_SUCCEEDED, run_id=run_id, task_id=task_id),
            ]
        store.record_all(events)

        agrees, differences = store.verify(run_id)
        assert agrees, differences
        assert store.replay(run_id).usage.total_tokens == 30 * 10

    def test_verify_detects_tampering(self, store: RunStore, run_id: RunId, build_sequence: SequenceBuilder) -> None:
        events, task_id, _ = build_sequence(run_id)
        store.record_all(events)

        with store.db.transaction() as conn:
            conn.execute(
                "UPDATE tasks SET state = 'failed' WHERE id = ?", (str(task_id),)
            )

        agrees, differences = store.verify(run_id)
        assert not agrees
        assert any("tasks.state" in difference for difference in differences)

    def test_verify_reports_a_missing_run_row(self, store: RunStore, run_id: RunId, build_sequence: SequenceBuilder) -> None:
        events, _, _ = build_sequence(run_id)
        store.record_all(events)
        with store.db.transaction() as conn:
            conn.execute("DELETE FROM runs WHERE id = ?", (str(run_id),))

        agrees, differences = store.verify(run_id)
        assert not agrees
        assert "no row" in differences[0]


class TestRebuild:
    """Recovery is replay, not guesswork."""

    def test_rebuild_restores_wiped_tables(self, store: RunStore, run_id: RunId, build_sequence: SequenceBuilder) -> None:
        events, task_id, _ = build_sequence(run_id)
        store.record_all(events)
        expected = store.replay(run_id)

        # Simulate materialized-view loss. The event log is untouched, and the
        # triggers guarantee it cannot be deleted even by accident.
        with store.db.transaction() as conn:
            conn.execute("DELETE FROM gate_results WHERE run_id = ?", (str(run_id),))
            conn.execute("DELETE FROM attempts WHERE run_id = ?", (str(run_id),))
            conn.execute("DELETE FROM tasks WHERE run_id = ?", (str(run_id),))
            conn.execute("DELETE FROM runs WHERE id = ?", (str(run_id),))
        assert not store.exists(run_id)

        rebuilt = store.rebuild(run_id)
        assert rebuilt == expected
        assert store.exists(run_id)

        agrees, differences = store.verify(run_id)
        assert agrees, differences

        row = store.db.query_one("SELECT state FROM tasks WHERE id = ?", (str(task_id),))
        assert row is not None
        assert row["state"] == TaskState.SUCCEEDED.value

    def test_rebuild_is_idempotent(self, store: RunStore, run_id: RunId, build_sequence: SequenceBuilder) -> None:
        events, _, _ = build_sequence(run_id)
        store.record_all(events)
        assert store.rebuild(run_id) == store.rebuild(run_id)

    def test_rebuild_all_covers_every_run(self, store: RunStore, build_sequence: SequenceBuilder) -> None:
        first, second = RunId.generate(), RunId.generate()
        for rid in (first, second):
            store.record_all(build_sequence(rid)[0])
        assert store.rebuild_all() == 2

    def test_the_event_log_survives_a_table_wipe(self, store: RunStore, run_id: RunId, build_sequence: SequenceBuilder) -> None:
        events, _, _ = build_sequence(run_id)
        store.record_all(events)
        with store.db.transaction() as conn:
            conn.execute("DELETE FROM runs WHERE id = ?", (str(run_id),))
        assert store.events.count(run_id) == len(events)


class TestCrashRecovery:
    """A killed process leaves a database that reopens and tells the truth."""

    def test_state_is_recoverable_from_a_reopened_file(self, tmp_dir: Path, build_sequence: SequenceBuilder) -> None:
        path = tmp_dir / "state.db"
        run_id = RunId.generate()
        events, task_id, _ = build_sequence(run_id)

        # First process: record part of a run, then "die" without cleanup.
        first_db = Database(path)
        first_db.migrate()
        RunStore(first_db).record_all(events[:6])
        first_db.close()

        # Second process: reopen and reconstruct.
        with Database(path) as second_db:
            second_db.migrate()
            store = RunStore(second_db)
            state = store.replay(run_id)

            assert state.status is RunStatus.RUNNING
            assert state.tasks[task_id].state == TaskState.RUNNING.value
            assert len(state.attempts) == 1

            agrees, differences = store.verify(run_id)
            assert agrees, differences

    def test_resuming_appends_onto_the_existing_log(self, tmp_dir: Path, build_sequence: SequenceBuilder) -> None:
        path = tmp_dir / "state.db"
        run_id = RunId.generate()
        events, task_id, _ = build_sequence(run_id)

        with Database(path) as first_db:
            first_db.migrate()
            RunStore(first_db).record_all(events[:6])

        with Database(path) as second_db:
            second_db.migrate()
            store = RunStore(second_db)
            store.record_all(events[6:])

            state = store.replay(run_id)
            assert state.status is RunStatus.FINISHED
            assert state.tasks[task_id].state == TaskState.SUCCEEDED.value
            assert state.event_count == len(events)

            agrees, differences = store.verify(run_id)
            assert agrees, differences

    def test_replaying_a_redundant_event_is_harmless(
        self, store: RunStore, run_id: RunId, build_sequence: SequenceBuilder
    ) -> None:
        """Recovery may re-attempt a write it already made; that must be a no-op."""
        events, _, _ = build_sequence(run_id)
        store.record_all(events)

        assert store.events.append_if_absent(events[3]) is False
        assert store.events.count(run_id) == len(events)


class TestReads:
    """The read API answers from stored state."""

    def test_state_requires_a_materialized_run(self, store: RunStore, run_id: RunId, build_sequence: SequenceBuilder) -> None:
        with pytest.raises(StateDivergenceError, match="no materialized row"):
            store.state(run_id)

    def test_state_matches_replay(self, store: RunStore, run_id: RunId, build_sequence: SequenceBuilder) -> None:
        events, _, _ = build_sequence(run_id)
        store.record_all(events)
        assert store.state(run_id) == store.replay(run_id)

    def test_run_ids_lists_materialized_runs(self, store: RunStore, build_sequence: SequenceBuilder) -> None:
        first, second = RunId.generate(), RunId.generate()
        for rid in (first, second):
            store.record_all(build_sequence(rid)[0])
        assert set(store.run_ids()) == {first, second}

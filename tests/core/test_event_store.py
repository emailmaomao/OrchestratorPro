"""Tests for append-only event persistence."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from orchestrator.core.event_store import DuplicateEventError, EventStore
from orchestrator.core.events import (
    AttemptId,
    Event,
    EventDecodeError,
    EventType,
    RunId,
    TaskId,
)
from orchestrator.core.storage import Database


@pytest.fixture
def store() -> Iterator[EventStore]:
    """An event store over a migrated in-memory database."""
    db = Database.in_memory()
    db.migrate()
    yield EventStore(db)
    db.close()


@pytest.fixture
def run_id() -> RunId:
    """A fresh run identifier."""
    return RunId.generate()


class TestAppend:
    """Events persist exactly once, and reject silent overwrites."""

    def test_append_then_read_back(self, store: EventStore, run_id: RunId) -> None:
        event = Event.new(EventType.RUN_CREATED, run_id=run_id, payload={"goal": "ship it"})
        store.append(event)

        stored = store.get(event.id)
        assert stored is not None
        assert stored.id == event.id
        assert stored.type is EventType.RUN_CREATED
        assert stored.run_id == run_id
        assert dict(stored.payload) == {"goal": "ship it"}

    def test_timestamp_survives_the_round_trip(
        self, store: EventStore, run_id: RunId
    ) -> None:
        event = Event.new(EventType.RUN_STARTED, run_id=run_id)
        store.append(event)
        stored = store.get(event.id)
        assert stored is not None
        assert stored.ts == event.ts

    def test_all_identifiers_survive_typed(self, store: EventStore, run_id: RunId) -> None:
        event = Event.new(
            EventType.ATTEMPT_STARTED,
            run_id=run_id,
            task_id=TaskId.generate(),
            attempt_id=AttemptId.generate(),
        )
        store.append(event)
        stored = store.get(event.id)
        assert stored is not None
        assert isinstance(stored.run_id, RunId)
        assert isinstance(stored.task_id, TaskId)
        assert isinstance(stored.attempt_id, AttemptId)

    def test_appending_the_same_event_twice_is_refused(
        self, store: EventStore, run_id: RunId
    ) -> None:
        event = Event.new(EventType.RUN_CREATED, run_id=run_id)
        store.append(event)
        with pytest.raises(DuplicateEventError):
            store.append(event)

    def test_append_if_absent_is_idempotent(self, store: EventStore, run_id: RunId) -> None:
        """Crash recovery re-writes an event it may already have written."""
        event = Event.new(EventType.RUN_CREATED, run_id=run_id)
        assert store.append_if_absent(event) is True
        assert store.append_if_absent(event) is False
        assert store.count(run_id) == 1

    def test_append_many_is_atomic(self, store: EventStore, run_id: RunId) -> None:
        good = Event.new(EventType.RUN_CREATED, run_id=run_id)
        store.append(good)

        batch = [Event.new(EventType.RUN_STARTED, run_id=run_id), good]
        with pytest.raises(DuplicateEventError):
            store.append_many(batch)
        # The first of the batch must not have survived the failure.
        assert store.count(run_id) == 1

    def test_record_creates_appends_and_returns(
        self, store: EventStore, run_id: RunId
    ) -> None:
        event = store.record(EventType.TOOL_CALLED, run_id=run_id, payload={"tool": "read"})
        assert store.contains(event.id)
        assert dict(event.payload) == {"tool": "read"}


class TestRead:
    """Reads are ordered by identifier, which is ordered by creation."""

    def test_events_come_back_in_creation_order(
        self, store: EventStore, run_id: RunId
    ) -> None:
        events = [Event.new(EventType.TOOL_CALLED, run_id=run_id) for _ in range(25)]
        for event in events:
            store.append(event)
        assert [e.id for e in store.read_run(run_id)] == [e.id for e in events]

    def test_ordering_holds_when_written_out_of_order(
        self, store: EventStore, run_id: RunId
    ) -> None:
        events = [Event.new(EventType.TOOL_CALLED, run_id=run_id) for _ in range(10)]
        for event in reversed(events):
            store.append(event)
        assert [e.id for e in store.read_run(run_id)] == [e.id for e in events]

    def test_runs_are_isolated_from_each_other(self, store: EventStore) -> None:
        first, second = RunId.generate(), RunId.generate()
        store.record(EventType.RUN_CREATED, run_id=first)
        store.record(EventType.RUN_CREATED, run_id=second)
        store.record(EventType.RUN_STARTED, run_id=second)

        assert store.count(first) == 1
        assert store.count(second) == 2
        assert store.count() == 3

    def test_read_since_excludes_the_boundary(self, store: EventStore, run_id: RunId) -> None:
        events = [store.record(EventType.TOOL_CALLED, run_id=run_id) for _ in range(5)]
        later = store.read_since(run_id, events[1].id)
        assert [e.id for e in later] == [e.id for e in events[2:]]

    def test_read_since_none_reads_everything(self, store: EventStore, run_id: RunId) -> None:
        for _ in range(3):
            store.record(EventType.TOOL_CALLED, run_id=run_id)
        assert len(store.read_since(run_id, None)) == 3

    def test_last_event_id_tracks_the_tail(self, store: EventStore, run_id: RunId) -> None:
        assert store.last_event_id(run_id) is None
        last = None
        for _ in range(4):
            last = store.record(EventType.TOOL_CALLED, run_id=run_id)
        assert last is not None
        assert store.last_event_id(run_id) == last.id

    def test_get_returns_none_for_an_unknown_event(self, store: EventStore) -> None:
        from orchestrator.core.events import EventId

        assert store.get(EventId.generate()) is None

    def test_run_ids_lists_every_run_seen(self, store: EventStore) -> None:
        first, second = RunId.generate(), RunId.generate()
        store.record(EventType.RUN_CREATED, run_id=first)
        store.record(EventType.RUN_CREATED, run_id=second)
        assert set(store.run_ids()) == {first, second}

    def test_empty_run_reads_empty(self, store: EventStore, run_id: RunId) -> None:
        assert store.read_run(run_id) == ()
        assert store.count(run_id) == 0

    def test_ordering_verification_passes_on_a_clean_log(
        self, store: EventStore, run_id: RunId
    ) -> None:
        for _ in range(20):
            store.record(EventType.TOOL_CALLED, run_id=run_id)
        assert store.verify_ordering(run_id) is True


class TestDurability:
    """The log survives the process that wrote it."""

    def test_events_persist_across_reopen(self, tmp_dir: Path) -> None:
        path = tmp_dir / "state.db"
        run_id = RunId.generate()

        with Database(path) as first:
            first.migrate()
            EventStore(first).record(
                EventType.RUN_CREATED, run_id=run_id, payload={"goal": "persist"}
            )

        with Database(path) as second:
            second.migrate()
            events = EventStore(second).read_run(run_id)
            assert len(events) == 1
            assert dict(events[0].payload) == {"goal": "persist"}

    def test_a_corrupted_payload_is_reported_not_guessed(
        self, store: EventStore, run_id: RunId
    ) -> None:
        event = Event.new(EventType.RUN_CREATED, run_id=run_id)
        store.append(event)
        # Bypass the store to simulate on-disk damage. The triggers forbid
        # UPDATE on events, so damage is injected on a fresh row instead.
        with store.db.transaction() as conn:
            conn.execute(
                "INSERT INTO events (id, run_id, ts, type, payload_json) "
                "VALUES ('evt_00000000000000000000000000', ?, "
                "'2026-01-01T00:00:00+00:00', 'run.created', 'not-json')",
                (str(run_id),),
            )
        with pytest.raises(EventDecodeError):
            store.read_run(run_id)

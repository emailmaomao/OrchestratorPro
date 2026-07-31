"""Append-only persistence for the event log.

The event log is the system's source of truth (``ORCHESTRATOR_PRO_SPEC`` §5).
Everything else — the ``runs``, ``tasks``, and ``attempts`` tables, the
dashboard, a resumed scheduler — is derived from it. Two consequences shape
this module:

* **There is no update and no delete.** Correcting the record means appending a
  further event. The database enforces this with triggers; this module simply
  offers no API for it.
* **Reads are ordered by identifier.** Identifiers are monotonic, so ordering
  by ``id`` is ordering by creation time — without trusting a clock that may
  have been adjusted mid-run.

:meth:`EventStore.append_if_absent` exists for crash recovery. A process killed
between writing an event and acting on it will, on restart, try to write it
again; that must be a no-op rather than a duplicate-key failure
(``docs/020_ARCHITECTURE`` §5.1).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator, Sequence

from orchestrator.core.events import (
    AttemptId,
    Event,
    EventDecodeError,
    EventId,
    EventType,
    OrchestratorError,
    RunId,
    TaskId,
)
from orchestrator.core.storage import Database

__all__ = ["DuplicateEventError", "EventStore", "EventStoreError"]


class EventStoreError(OrchestratorError):
    """An event could not be persisted or read back."""

    code = "event_store"
    retryable = False


class DuplicateEventError(EventStoreError):
    """An event with this identifier is already in the log."""

    code = "duplicate_event"
    retryable = False


_INSERT = """
INSERT INTO events (id, run_id, task_id, attempt_id, ts, type, payload_json)
VALUES (?, ?, ?, ?, ?, ?, ?)
"""

_SELECT = """
SELECT id, run_id, task_id, attempt_id, ts, type, payload_json FROM events
"""


def _row_to_event(row: sqlite3.Row) -> Event:
    """Rebuild an :class:`Event` from a database row.

    Raises:
        EventDecodeError: If the stored row is malformed.
    """
    try:
        payload = json.loads(row["payload_json"])
    except json.JSONDecodeError as exc:
        raise EventDecodeError(
            f"stored payload for event {row['id']} is not valid JSON: {exc}",
            detail={"event_id": row["id"]},
        ) from exc
    return Event.from_dict(
        {
            "id": row["id"],
            "type": row["type"],
            "ts": row["ts"],
            "run_id": row["run_id"],
            "task_id": row["task_id"],
            "attempt_id": row["attempt_id"],
            "payload": payload,
        }
    )


def _params(event: Event) -> tuple[object, ...]:
    """Flatten an event into its insert parameters."""
    return (
        str(event.id),
        str(event.run_id) if event.run_id is not None else None,
        str(event.task_id) if event.task_id is not None else None,
        str(event.attempt_id) if event.attempt_id is not None else None,
        event.ts.isoformat(),
        event.type.value,
        json.dumps(dict(event.payload), sort_keys=True, separators=(",", ":")),
    )


class EventStore:
    """Reads and appends the durable event log."""

    __slots__ = ("_db",)

    def __init__(self, db: Database) -> None:
        """Bind the store to an open, migrated database."""
        self._db = db

    @property
    def db(self) -> Database:
        """The underlying database."""
        return self._db

    # ---------------------------------------------------------------- writes

    def append(self, event: Event) -> None:
        """Append one event in its own transaction.

        Args:
            event: The event to persist.

        Raises:
            DuplicateEventError: If an event with this identifier already exists.
            EventStoreError: If the write fails for any other reason.
        """
        with self._db.transaction() as conn:
            self.append_in(conn, event)

    def append_in(self, conn: sqlite3.Connection, event: Event) -> None:
        """Append one event inside a caller-supplied transaction.

        This is the seam that lets an event and the materialized rows it
        implies commit atomically — the property the whole recovery story rests
        on (``ORCHESTRATOR_PRO_SPEC`` §5).

        Args:
            conn: A connection already inside a transaction.
            event: The event to persist.

        Raises:
            DuplicateEventError: If an event with this identifier already exists.
            EventStoreError: If the write fails for any other reason.
        """
        try:
            conn.execute(_INSERT, _params(event))
        except sqlite3.IntegrityError as exc:
            raise DuplicateEventError(
                f"event {event.id} is already in the log",
                detail={"event_id": str(event.id)},
            ) from exc
        except sqlite3.Error as exc:
            raise EventStoreError(
                f"could not append event {event.id}: {exc}",
                detail={"event_id": str(event.id)},
            ) from exc

    def append_if_absent(self, event: Event) -> bool:
        """Append ``event`` unless it is already present.

        The idempotent form, for replaying a step after a crash.

        Args:
            event: The event to persist.

        Returns:
            ``True`` if it was written, ``False`` if it was already there.
        """
        try:
            self.append(event)
        except DuplicateEventError:
            return False
        return True

    def append_many(self, events: Iterable[Event]) -> int:
        """Append several events atomically.

        Either all are written or none are.

        Args:
            events: The events to persist, in order.

        Returns:
            How many were written.

        Raises:
            DuplicateEventError: If any identifier already exists.
        """
        batch = list(events)
        with self._db.transaction() as conn:
            for event in batch:
                self.append_in(conn, event)
        return len(batch)

    # ----------------------------------------------------------------- reads

    def read_run(self, run_id: RunId) -> tuple[Event, ...]:
        """Return every event for one run, in creation order."""
        rows = self._db.query(
            f"{_SELECT} WHERE run_id = ? ORDER BY id ASC", (str(run_id),)
        )
        return tuple(_row_to_event(row) for row in rows)

    def iter_run(self, run_id: RunId) -> Iterator[Event]:
        """Iterate a run's events in creation order."""
        yield from self.read_run(run_id)

    def read_all(self) -> tuple[Event, ...]:
        """Return every event in the log, in creation order."""
        return tuple(_row_to_event(row) for row in self._db.query(f"{_SELECT} ORDER BY id ASC"))

    def read_since(self, run_id: RunId, after: EventId | None) -> tuple[Event, ...]:
        """Return a run's events created after ``after``.

        Args:
            run_id: The run to read.
            after: Exclusive lower bound. ``None`` reads from the beginning.

        Returns:
            The matching events, in creation order.
        """
        if after is None:
            return self.read_run(run_id)
        rows = self._db.query(
            f"{_SELECT} WHERE run_id = ? AND id > ? ORDER BY id ASC",
            (str(run_id), str(after)),
        )
        return tuple(_row_to_event(row) for row in rows)

    def get(self, event_id: EventId) -> Event | None:
        """Return one event by identifier, or ``None`` if absent."""
        row = self._db.query_one(f"{_SELECT} WHERE id = ?", (str(event_id),))
        return _row_to_event(row) if row is not None else None

    def contains(self, event_id: EventId) -> bool:
        """Whether an event with this identifier is in the log."""
        return self._db.query_one("SELECT 1 FROM events WHERE id = ?", (str(event_id),)) is not None

    def count(self, run_id: RunId | None = None) -> int:
        """Count events, optionally scoped to one run."""
        if run_id is None:
            row = self._db.query_one("SELECT COUNT(*) AS n FROM events")
        else:
            row = self._db.query_one(
                "SELECT COUNT(*) AS n FROM events WHERE run_id = ?", (str(run_id),)
            )
        return int(row["n"]) if row is not None else 0

    def last_event_id(self, run_id: RunId) -> EventId | None:
        """Return the most recent event identifier for a run, if any."""
        row = self._db.query_one(
            "SELECT id FROM events WHERE run_id = ? ORDER BY id DESC LIMIT 1",
            (str(run_id),),
        )
        return EventId(row["id"]) if row is not None else None

    def run_ids(self) -> tuple[RunId, ...]:
        """Return every run identifier that appears in the log, in order."""
        rows = self._db.query(
            "SELECT DISTINCT run_id FROM events WHERE run_id IS NOT NULL ORDER BY run_id ASC"
        )
        return tuple(RunId(row["run_id"]) for row in rows)

    # ------------------------------------------------------------ convenience

    def record(
        self,
        type: EventType,
        *,
        run_id: RunId | None = None,
        task_id: TaskId | None = None,
        attempt_id: AttemptId | None = None,
        payload: dict[str, object] | None = None,
    ) -> Event:
        """Create an event, append it, and return it.

        Args:
            type: What happened.
            run_id: The run this belongs to, if any.
            task_id: The task this belongs to, if any.
            attempt_id: The attempt this belongs to, if any.
            payload: JSON-serializable structured detail.

        Returns:
            The persisted event.
        """
        event = Event.new(
            type,
            run_id=run_id,
            task_id=task_id,
            attempt_id=attempt_id,
            payload=payload or {},
        )
        self.append(event)
        return event

    def verify_ordering(self, run_id: RunId) -> bool:
        """Check that a run's stored events are strictly increasing by id.

        A false result means the log was written by something that bypassed
        :class:`EventStore`, or that identifier monotonicity was broken.

        Args:
            run_id: The run to check.

        Returns:
            ``True`` if ordering holds.
        """
        ids: Sequence[str] = [str(e.id) for e in self.read_run(run_id)]
        return all(earlier < later for earlier, later in zip(ids, ids[1:], strict=False))

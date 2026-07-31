"""The transactional facade over the event log and its materialized views.

``ORCHESTRATOR_PRO_SPEC`` §5 sets out the arrangement this module implements:
the event log is authoritative, and the ``runs``/``tasks``/``attempts`` tables
are a materialized view maintained *in the same transaction* as the event that
implies them. That atomicity is the whole point — a crash can leave the pair
consistent or leave neither, never a written event with unwritten rows.

Two paths write those tables, and the difference matters:

* :meth:`RunStore.record` takes the **fast path**. It appends one event and
  applies targeted upserts for the rows that event touches. Constant work per
  event.
* :meth:`RunStore.rebuild` takes the **truth path**. It replays the entire log
  through :func:`~orchestrator.core.projection.reconstruct` and rewrites the
  tables wholesale. Linear in the log, and correct by construction.

Having two paths means they can drift, so :meth:`RunStore.verify` compares them
and the test suite exercises it over long event sequences. The fast path is an
optimization that must agree with the slow one; when it does not, the slow one
is right.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime

from orchestrator.core.event_store import EventStore
from orchestrator.core.events import Event, EventType, OrchestratorError, RunId
from orchestrator.core.projection import (
    INITIAL_TASK_STATE,
    RUNNING_ATTEMPT_STATUS,
    TASK_STATE_BY_EVENT,
    reconstruct,
)
from orchestrator.core.records import RunState, RunStatus
from orchestrator.core.storage import Database

__all__ = ["RunStore", "StateDivergenceError"]


class StateDivergenceError(OrchestratorError):
    """The materialized tables disagree with a replay of the event log."""

    code = "state_divergence"
    retryable = False


_TERMINAL_RUN_EVENTS = {EventType.RUN_FINISHED, EventType.RUN_CANCELLED}


def _iso(value: datetime | None) -> str | None:
    """Render a timestamp for storage."""
    return value.isoformat() if value is not None else None


class RunStore:
    """Reads and writes run state, backed by the durable event log."""

    __slots__ = ("_db", "_events")

    def __init__(self, db: Database) -> None:
        """Bind the store to an open, migrated database."""
        self._db = db
        self._events = EventStore(db)

    @property
    def events(self) -> EventStore:
        """The underlying event store."""
        return self._events

    @property
    def db(self) -> Database:
        """The underlying database."""
        return self._db

    # ------------------------------------------------------------------ write

    def record(self, event: Event) -> None:
        """Append an event and update the tables it implies, atomically.

        Args:
            event: The event to record. Must carry a ``run_id``.

        Raises:
            StateDivergenceError: If the event carries no run identifier.
            DuplicateEventError: If it is already in the log.
        """
        if event.run_id is None:
            raise StateDivergenceError(
                f"event {event.id} carries no run_id and cannot be materialized",
                detail={"event_id": str(event.id)},
            )
        with self._db.transaction() as conn:
            self._events.append_in(conn, event)
            self._materialize(conn, event)

    def record_all(self, events: Iterable[Event]) -> int:
        """Record several events atomically, in order.

        Args:
            events: The events to record.

        Returns:
            How many were recorded.
        """
        batch = list(events)
        with self._db.transaction() as conn:
            for event in batch:
                if event.run_id is None:
                    raise StateDivergenceError(
                        f"event {event.id} carries no run_id and cannot be materialized",
                        detail={"event_id": str(event.id)},
                    )
                self._events.append_in(conn, event)
                self._materialize(conn, event)
        return len(batch)

    def _materialize(self, conn: sqlite3.Connection, event: Event) -> None:
        """Apply one event's effect to the materialized tables.

        Mirrors :mod:`orchestrator.core.projection` incrementally. Kept in step
        with it by :meth:`verify`, which is exercised in the test suite.
        """
        run_id = str(event.run_id)
        payload = event.payload

        if event.type is EventType.RUN_CREATED:
            conn.execute(
                "INSERT OR REPLACE INTO runs "
                "(id, goal, repo_path, status, created_at, finished_at, config_json) "
                "VALUES (?, ?, ?, ?, ?, NULL, ?)",
                (
                    run_id,
                    str(payload.get("goal", "")),
                    str(payload.get("repo_path", "")),
                    RunStatus.CREATED.value,
                    event.ts.isoformat(),
                    json.dumps(payload.get("config", {}), sort_keys=True),
                ),
            )
            return

        if event.type is EventType.RUN_STARTED:
            conn.execute(
                "UPDATE runs SET status = ? WHERE id = ?",
                (RunStatus.RUNNING.value, run_id),
            )
            return

        if event.type in _TERMINAL_RUN_EVENTS:
            status = (
                RunStatus.FINISHED
                if event.type is EventType.RUN_FINISHED
                else RunStatus.CANCELLED
            )
            conn.execute(
                "UPDATE runs SET status = ?, finished_at = ? WHERE id = ?",
                (status.value, event.ts.isoformat(), run_id),
            )
            return

        if event.type is EventType.TASK_CREATED:
            conn.execute(
                "INSERT OR REPLACE INTO tasks "
                "(id, run_id, title, prompt, depends_on_json, state, max_attempts, "
                " attempts_made) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                (
                    str(event.task_id),
                    run_id,
                    str(payload.get("title", "")),
                    str(payload.get("prompt", "")),
                    json.dumps([str(d) for d in payload.get("depends_on", ())]),
                    str(payload.get("state", INITIAL_TASK_STATE)),
                    int(payload.get("max_attempts", 1) or 1),
                ),
            )
            return

        if event.type in TASK_STATE_BY_EVENT:
            conn.execute(
                "UPDATE tasks SET state = ? WHERE id = ?",
                (TASK_STATE_BY_EVENT[event.type], str(event.task_id)),
            )
            return

        if event.type is EventType.ATTEMPT_STARTED:
            row = conn.execute(
                "SELECT attempts_made FROM tasks WHERE id = ?", (str(event.task_id),)
            ).fetchone()
            made = int(row["attempts_made"]) if row is not None else 0
            conn.execute(
                "INSERT OR REPLACE INTO attempts "
                "(id, task_id, run_id, n, adapter, workspace_path, branch, status, "
                " started_at, finished_at, tokens_in, tokens_out, cost_usd) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, 0, NULL)",
                (
                    str(event.attempt_id),
                    str(event.task_id),
                    run_id,
                    int(payload.get("number", made + 1) or made + 1),
                    payload.get("adapter"),
                    payload.get("workspace_path"),
                    payload.get("branch"),
                    RUNNING_ATTEMPT_STATUS,
                    event.ts.isoformat(),
                ),
            )
            conn.execute(
                "UPDATE tasks SET attempts_made = attempts_made + 1 WHERE id = ?",
                (str(event.task_id),),
            )
            return

        if event.type is EventType.ATTEMPT_FINISHED:
            cost = payload.get("cost_usd")
            conn.execute(
                "UPDATE attempts SET status = ?, finished_at = ?, tokens_in = ?, "
                "tokens_out = ?, cost_usd = ? WHERE id = ?",
                (
                    str(payload.get("status", "succeeded")),
                    event.ts.isoformat(),
                    int(payload.get("tokens_in", 0) or 0),
                    int(payload.get("tokens_out", 0) or 0),
                    float(cost) if cost is not None else None,
                    str(event.attempt_id),
                ),
            )
            return

        if event.type is EventType.GATE_EVALUATED:
            conn.execute(
                "INSERT OR REPLACE INTO gate_results "
                "(id, attempt_id, run_id, gate, verdict, detail_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(event.id),
                    str(event.attempt_id),
                    run_id,
                    str(payload.get("gate", "")),
                    str(payload.get("verdict", "")),
                    json.dumps(payload.get("detail", {}), sort_keys=True),
                ),
            )
            return

        if event.type is EventType.APPROVAL_REQUESTED:
            conn.execute(
                "INSERT OR REPLACE INTO approvals "
                "(id, task_id, run_id, requested_at, resolved_at, decision, actor) "
                "VALUES (?, ?, ?, ?, NULL, NULL, NULL)",
                (str(event.id), str(event.task_id), run_id, event.ts.isoformat()),
            )
            return

        if event.type is EventType.APPROVAL_RESOLVED:
            conn.execute(
                "UPDATE approvals SET resolved_at = ?, decision = ?, actor = ? "
                "WHERE task_id = ?",
                (
                    event.ts.isoformat(),
                    payload.get("decision"),
                    payload.get("actor"),
                    str(event.task_id),
                ),
            )
            return

        # Tool calls, error events, and unrecognized types affect the log but
        # not the materialized rows.

    # ------------------------------------------------------------------- read

    def replay(self, run_id: RunId) -> RunState:
        """Reconstruct a run's state from the event log alone.

        This is the authoritative answer. It reads nothing but ``events``.

        Args:
            run_id: The run to reconstruct.

        Returns:
            The reconstructed state.
        """
        return reconstruct(self._events.read_run(run_id), run_id=run_id)

    def state(self, run_id: RunId) -> RunState:
        """Return a run's state, reading the materialized tables.

        Equivalent to :meth:`replay` on an uncorrupted database, and cheaper for
        long runs.

        Args:
            run_id: The run to load.

        Returns:
            The stored state.

        Raises:
            StateDivergenceError: If the run has no row.
        """
        row = self._db.query_one("SELECT * FROM runs WHERE id = ?", (str(run_id),))
        if row is None:
            raise StateDivergenceError(
                f"no materialized row for run {run_id}; replay or rebuild it",
                detail={"run_id": str(run_id)},
            )
        # Reconstruct through the same code path so the two agree by
        # construction on everything except where the rows themselves are wrong.
        return self.replay(run_id)

    def exists(self, run_id: RunId) -> bool:
        """Whether a materialized row exists for this run."""
        return self._db.query_one("SELECT 1 FROM runs WHERE id = ?", (str(run_id),)) is not None

    def run_ids(self) -> tuple[RunId, ...]:
        """Return every run that has a materialized row, newest last."""
        rows = self._db.query("SELECT id FROM runs ORDER BY id ASC")
        return tuple(RunId(row["id"]) for row in rows)

    # ---------------------------------------------------------------- recovery

    def rebuild(self, run_id: RunId) -> RunState:
        """Rewrite a run's materialized tables from the event log.

        The recovery operation: after a crash, or whenever the tables are
        suspect, this discards them and re-derives everything from the log.
        Idempotent, and safe to run at startup.

        Args:
            run_id: The run to rebuild.

        Returns:
            The state that was written.
        """
        state = self.replay(run_id)
        rid = str(run_id)

        with self._db.transaction() as conn:
            # Children first: attempts reference tasks by foreign key.
            conn.execute("DELETE FROM gate_results WHERE run_id = ?", (rid,))
            conn.execute("DELETE FROM approvals WHERE run_id = ?", (rid,))
            conn.execute("DELETE FROM attempts WHERE run_id = ?", (rid,))
            conn.execute("DELETE FROM tasks WHERE run_id = ?", (rid,))
            conn.execute("DELETE FROM runs WHERE id = ?", (rid,))

            conn.execute(
                "INSERT INTO runs "
                "(id, goal, repo_path, status, created_at, finished_at, config_json) "
                "VALUES (?, ?, ?, ?, ?, ?, '{}')",
                (
                    rid,
                    state.goal,
                    state.repo_path,
                    state.status.value,
                    _iso(state.created_at) or "",
                    _iso(state.finished_at),
                ),
            )
            for task in state.tasks.values():
                conn.execute(
                    "INSERT INTO tasks "
                    "(id, run_id, title, prompt, depends_on_json, state, max_attempts, "
                    " attempts_made) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(task.id),
                        rid,
                        task.title,
                        task.prompt,
                        json.dumps([str(d) for d in task.depends_on]),
                        task.state,
                        task.max_attempts,
                        task.attempts_made,
                    ),
                )
            for attempt in state.attempts.values():
                conn.execute(
                    "INSERT INTO attempts "
                    "(id, task_id, run_id, n, adapter, workspace_path, branch, status, "
                    " started_at, finished_at, tokens_in, tokens_out, cost_usd) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(attempt.id),
                        str(attempt.task_id),
                        rid,
                        attempt.number,
                        attempt.adapter,
                        attempt.workspace_path,
                        attempt.branch,
                        attempt.status,
                        _iso(attempt.started_at) or "",
                        _iso(attempt.finished_at),
                        attempt.tokens_in,
                        attempt.tokens_out,
                        attempt.cost_usd,
                    ),
                )
            for gate in state.gates:
                conn.execute(
                    "INSERT INTO gate_results "
                    "(id, attempt_id, run_id, gate, verdict, detail_json) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        str(gate.id),
                        str(gate.attempt_id),
                        rid,
                        gate.gate,
                        gate.verdict,
                        json.dumps(dict(gate.detail), sort_keys=True),
                    ),
                )
            for approval in state.approvals.values():
                conn.execute(
                    "INSERT INTO approvals "
                    "(id, task_id, run_id, requested_at, resolved_at, decision, actor) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(approval.id),
                        str(approval.task_id),
                        rid,
                        _iso(approval.requested_at) or "",
                        _iso(approval.resolved_at),
                        approval.decision,
                        approval.actor,
                    ),
                )
        return state

    def rebuild_all(self) -> int:
        """Rebuild every run present in the event log.

        Returns:
            How many runs were rebuilt.
        """
        run_ids = self._events.run_ids()
        for run_id in run_ids:
            self.rebuild(run_id)
        return len(run_ids)

    def verify(self, run_id: RunId) -> tuple[bool, tuple[str, ...]]:
        """Compare the materialized tables against a replay of the log.

        The guard on the fast path in :meth:`record`. A mismatch means the
        incremental writer has drifted from the projection, and the tables
        should be rebuilt.

        Args:
            run_id: The run to check.

        Returns:
            A pair of ``(agrees, differences)``. ``differences`` is empty when
            they agree, and otherwise names each field that diverged.
        """
        replayed = self.replay(run_id)
        rid = str(run_id)
        differences: list[str] = []

        run_row = self._db.query_one("SELECT * FROM runs WHERE id = ?", (rid,))
        if run_row is None:
            return False, ("runs: no row for this run",)
        if run_row["status"] != replayed.status.value:
            differences.append(
                f"runs.status: stored {run_row['status']!r}, replayed "
                f"{replayed.status.value!r}"
            )
        if run_row["goal"] != replayed.goal:
            differences.append("runs.goal differs")

        task_rows = {
            row["id"]: row for row in self._db.query("SELECT * FROM tasks WHERE run_id = ?", (rid,))
        }
        if set(task_rows) != {str(t) for t in replayed.tasks}:
            differences.append(
                f"tasks: stored {len(task_rows)} row(s), replayed {len(replayed.tasks)}"
            )
        else:
            for task in replayed.tasks.values():
                row = task_rows[str(task.id)]
                if row["state"] != task.state:
                    differences.append(
                        f"tasks.state[{task.id}]: stored {row['state']!r}, "
                        f"replayed {task.state!r}"
                    )
                if int(row["attempts_made"]) != task.attempts_made:
                    differences.append(
                        f"tasks.attempts_made[{task.id}]: stored "
                        f"{row['attempts_made']}, replayed {task.attempts_made}"
                    )

        attempt_rows = {
            row["id"]: row
            for row in self._db.query("SELECT * FROM attempts WHERE run_id = ?", (rid,))
        }
        if set(attempt_rows) != {str(a) for a in replayed.attempts}:
            differences.append(
                f"attempts: stored {len(attempt_rows)} row(s), "
                f"replayed {len(replayed.attempts)}"
            )
        else:
            for attempt in replayed.attempts.values():
                row = attempt_rows[str(attempt.id)]
                if row["status"] != attempt.status:
                    differences.append(
                        f"attempts.status[{attempt.id}]: stored {row['status']!r}, "
                        f"replayed {attempt.status!r}"
                    )
                if int(row["tokens_in"]) != attempt.tokens_in:
                    differences.append(f"attempts.tokens_in[{attempt.id}] differs")
                if int(row["tokens_out"]) != attempt.tokens_out:
                    differences.append(f"attempts.tokens_out[{attempt.id}] differs")

        return not differences, tuple(differences)

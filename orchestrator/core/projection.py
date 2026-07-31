"""Reconstruction of run state by replaying the event log.

This module is the authoritative definition of what a run's state *means*. It
is a pure fold: a sequence of events in, a :class:`RunState` out, with no
database, no clock, and no filesystem involved. That purity is what makes
recovery testable — replaying a log must produce the same state every time,
whatever else the system was doing when the log was written.

Recovery is replay, not guesswork (``docs/020_ARCHITECTURE`` §5). If the
materialized tables ever disagree with a replay, the replay wins and the tables
are rebuilt from it.

Two deliberate asymmetries in how malformed input is treated:

* **An unknown event type is ignored.** A log written by a newer build must
  still replay on an older one, minus whatever it did not understand.
* **A structurally impossible event raises.** An attempt for a task that was
  never created is not a forward-compatibility question; it means the log is
  damaged, and continuing would produce a confidently wrong answer.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any, Final

from orchestrator.core.events import (
    AttemptId,
    Event,
    EventType,
    OrchestratorError,
    RunId,
    TaskId,
)
from orchestrator.core.records import (
    ApprovalRecord,
    AttemptProjection,
    GateOutcome,
    RunState,
    RunStatus,
    TaskProjection,
    UsageTotals,
)

__all__ = ["ReplayError", "TASK_STATE_BY_EVENT", "reconstruct"]


class ReplayError(OrchestratorError):
    """The event log could not be replayed into a coherent state."""

    code = "replay"
    retryable = False


#: The task state each lifecycle event moves a task into. Values are the string
#: forms of :class:`orchestrator.task.model.TaskState`; see the note in
#: :mod:`orchestrator.core.records` on why the enum is not imported here.
TASK_STATE_BY_EVENT: Final[Mapping[EventType, str]] = {
    EventType.TASK_READY: "ready",
    EventType.TASK_STARTED: "running",
    EventType.TASK_SUCCEEDED: "succeeded",
    EventType.TASK_FAILED: "failed",
    EventType.TASK_BLOCKED: "blocked",
    EventType.TASK_ABANDONED: "abandoned",
}

#: The state a task holds between creation and becoming runnable.
INITIAL_TASK_STATE: Final = "pending"

#: The status an attempt holds while in flight.
RUNNING_ATTEMPT_STATUS: Final = "running"

_RUN_STATUS_BY_EVENT: Final[Mapping[EventType, RunStatus]] = {
    EventType.RUN_CREATED: RunStatus.CREATED,
    EventType.RUN_STARTED: RunStatus.RUNNING,
    EventType.RUN_FINISHED: RunStatus.FINISHED,
    EventType.RUN_CANCELLED: RunStatus.CANCELLED,
}


def _require(event: Event, field: str) -> Any:
    """Return a required identifier from ``event``, or raise."""
    value = getattr(event, field)
    if value is None:
        raise ReplayError(
            f"event {event.id} of type {event.type.value!r} requires {field}",
            detail={"event_id": str(event.id), "type": event.type.value, "missing": field},
        )
    return value


def _as_int(payload: Mapping[str, Any], key: str, default: int = 0) -> int:
    """Read an integer from a payload, tolerating absent or null values."""
    value = payload.get(key, default)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_str_tuple(payload: Mapping[str, Any], key: str) -> tuple[str, ...]:
    """Read a list of strings from a payload, tolerating anything else.

    A malformed payload yields an empty tuple rather than an exception: replay
    must survive a log written by a build that got this wrong, or one run's bad
    event makes the whole run unreadable.
    """
    value = payload.get(key)
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value)


def _as_float(payload: Mapping[str, Any], key: str) -> float | None:
    """Read an optional float from a payload."""
    value = payload.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class _Builder:
    """Mutable accumulator behind the pure :func:`reconstruct`.

    Kept internal so the public surface stays a pure function while the fold
    itself remains linear — rebuilding frozen mappings per event would make
    replay quadratic in the length of the log.
    """

    __slots__ = (
        "approvals",
        "attempts",
        "created_at",
        "event_count",
        "finished_at",
        "gates",
        "goal",
        "last_event_id",
        "repo_path",
        "run_id",
        "status",
        "tasks",
        "tool_calls",
        "usage",
    )

    def __init__(self, run_id: RunId) -> None:
        self.run_id = run_id
        self.status = RunStatus.CREATED
        self.goal = ""
        self.repo_path = ""
        self.created_at: datetime | None = None
        self.finished_at: datetime | None = None
        self.tasks: dict[TaskId, TaskProjection] = {}
        self.attempts: dict[AttemptId, AttemptProjection] = {}
        self.gates: list[GateOutcome] = []
        self.approvals: dict[TaskId, ApprovalRecord] = {}
        self.usage = UsageTotals()
        self.tool_calls = 0
        self.event_count = 0
        self.last_event_id = None

    def task(self, event: Event) -> TaskProjection:
        """Return the task an event refers to, or raise if it is unknown."""
        task_id = _require(event, "task_id")
        try:
            return self.tasks[task_id]
        except KeyError:
            raise ReplayError(
                f"event {event.id} refers to task {task_id}, which was never created; "
                "the log is missing a task.created event or is damaged",
                detail={"event_id": str(event.id), "task_id": str(task_id)},
            ) from None

    def finish(self) -> RunState:
        """Freeze the accumulator into an immutable :class:`RunState`."""
        return RunState(
            run_id=self.run_id,
            status=self.status,
            goal=self.goal,
            repo_path=self.repo_path,
            created_at=self.created_at,
            finished_at=self.finished_at,
            tasks=dict(self.tasks),
            attempts=dict(self.attempts),
            gates=tuple(self.gates),
            approvals=dict(self.approvals),
            usage=self.usage,
            tool_calls=self.tool_calls,
            event_count=self.event_count,
            last_event_id=self.last_event_id,
        )


def _apply_run(builder: _Builder, event: Event) -> None:
    """Apply a run-lifecycle event."""
    builder.status = _RUN_STATUS_BY_EVENT[event.type]
    if event.type is EventType.RUN_CREATED:
        builder.goal = str(event.payload.get("goal", ""))
        builder.repo_path = str(event.payload.get("repo_path", ""))
        builder.created_at = event.ts
    elif builder.status.is_terminal:
        builder.finished_at = event.ts


def _apply_task_created(builder: _Builder, event: Event) -> None:
    """Register a newly declared task."""
    task_id = _require(event, "task_id")
    depends_on = tuple(TaskId(d) for d in event.payload.get("depends_on", ()))
    builder.tasks[task_id] = TaskProjection(
        id=task_id,
        run_id=builder.run_id,
        title=str(event.payload.get("title", "")),
        prompt=str(event.payload.get("prompt", "")),
        state=str(event.payload.get("state", INITIAL_TASK_STATE)),
        depends_on=depends_on,
        max_attempts=_as_int(event.payload, "max_attempts", 1),
    )


def _apply_task_state(builder: _Builder, event: Event) -> None:
    """Move a task into the state implied by a lifecycle event."""
    from dataclasses import replace

    task = builder.task(event)
    builder.tasks[task.id] = replace(task, state=TASK_STATE_BY_EVENT[event.type])


def _apply_attempt_started(builder: _Builder, event: Event) -> None:
    """Open an attempt and charge it against its task's allowance."""
    from dataclasses import replace

    attempt_id = _require(event, "attempt_id")
    task = builder.task(event)
    builder.attempts[attempt_id] = AttemptProjection(
        id=attempt_id,
        task_id=task.id,
        run_id=builder.run_id,
        number=_as_int(event.payload, "number", task.attempts_made + 1),
        status=RUNNING_ATTEMPT_STATUS,
        started_at=event.ts,
        adapter=event.payload.get("adapter"),
        workspace_path=event.payload.get("workspace_path"),
        branch=event.payload.get("branch"),
    )
    builder.tasks[task.id] = replace(task, attempts_made=task.attempts_made + 1)


def _apply_attempt_finished(builder: _Builder, event: Event) -> None:
    """Close an attempt and roll its usage into the run totals."""
    from dataclasses import replace

    attempt_id = _require(event, "attempt_id")
    attempt = builder.attempts.get(attempt_id)
    if attempt is None:
        raise ReplayError(
            f"event {event.id} finishes attempt {attempt_id}, which was never started",
            detail={"event_id": str(event.id), "attempt_id": str(attempt_id)},
        )

    tokens_in = _as_int(event.payload, "tokens_in")
    tokens_out = _as_int(event.payload, "tokens_out")
    cost = _as_float(event.payload, "cost_usd")
    estimated = bool(event.payload.get("tokens_estimated", False))

    # OP-011. Absent keys keep their "unrecorded" defaults, which is what makes
    # a log written before this change replay to exactly the state it did
    # before: `merged` stays None rather than becoming False, and the two path
    # lists stay empty. `merged` is read with a sentinel for the same reason —
    # `.get("merged", False)` would invent a verdict the log never gave.
    raw_merged = event.payload.get("merged")
    merged = None if raw_merged is None else bool(raw_merged)

    builder.attempts[attempt_id] = replace(
        attempt,
        status=str(event.payload.get("status", "succeeded")),
        finished_at=event.ts,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost,
        tokens_estimated=estimated,
        merged=merged,
        merge_status=str(event.payload.get("merge_status", "")),
        changed_files=_as_str_tuple(event.payload, "changed_files"),
        conflicted_paths=_as_str_tuple(event.payload, "conflicted_paths"),
    )
    builder.usage = builder.usage.plus(
        tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost, estimated=estimated
    )


def _apply_gate(builder: _Builder, event: Event) -> None:
    """Record a gate verdict."""
    builder.gates.append(
        GateOutcome(
            id=event.id,
            attempt_id=_require(event, "attempt_id"),
            run_id=builder.run_id,
            gate=str(event.payload.get("gate", "")),
            verdict=str(event.payload.get("verdict", "")),
            detail=dict(event.payload.get("detail", {})),
        )
    )


def _apply_approval_requested(builder: _Builder, event: Event) -> None:
    """Open a human approval gate."""
    task_id = _require(event, "task_id")
    builder.approvals[task_id] = ApprovalRecord(
        id=event.id, task_id=task_id, run_id=builder.run_id, requested_at=event.ts
    )


def _apply_approval_resolved(builder: _Builder, event: Event) -> None:
    """Resolve a human approval gate."""
    from dataclasses import replace

    task_id = _require(event, "task_id")
    existing = builder.approvals.get(task_id)
    if existing is None:
        raise ReplayError(
            f"event {event.id} resolves an approval for task {task_id} that was "
            "never requested",
            detail={"event_id": str(event.id), "task_id": str(task_id)},
        )
    builder.approvals[task_id] = replace(
        existing,
        resolved_at=event.ts,
        decision=event.payload.get("decision"),
        actor=event.payload.get("actor"),
    )


def _apply(builder: _Builder, event: Event) -> None:
    """Apply one event to the accumulator."""
    if event.type in _RUN_STATUS_BY_EVENT:
        _apply_run(builder, event)
    elif event.type is EventType.TASK_CREATED:
        _apply_task_created(builder, event)
    elif event.type in TASK_STATE_BY_EVENT:
        _apply_task_state(builder, event)
    elif event.type is EventType.ATTEMPT_STARTED:
        _apply_attempt_started(builder, event)
    elif event.type is EventType.ATTEMPT_FINISHED:
        _apply_attempt_finished(builder, event)
    elif event.type is EventType.GATE_EVALUATED:
        _apply_gate(builder, event)
    elif event.type is EventType.APPROVAL_REQUESTED:
        _apply_approval_requested(builder, event)
    elif event.type is EventType.APPROVAL_RESOLVED:
        _apply_approval_resolved(builder, event)
    elif event.type is EventType.TOOL_CALLED:
        builder.tool_calls += 1
    # Anything else — including error events and types introduced by a newer
    # build — contributes to the count but not to the projected state.


def reconstruct(events: Iterable[Event], *, run_id: RunId | None = None) -> RunState:
    """Replay events into the state they describe.

    Args:
        events: The run's events. Sorted by identifier before folding, so a
            caller that reads them out of order still gets the right answer.
        run_id: The run being reconstructed. Inferred from the events when
            omitted; supplying it turns a foreign event into an error rather
            than a silent contamination.

    Returns:
        The reconstructed state. Replaying an empty log for a known run yields
        a ``CREATED`` run with no tasks.

    Raises:
        ReplayError: If the log is empty and no ``run_id`` was supplied, if it
            mixes runs, or if it is structurally incoherent.
    """
    ordered = sorted(events, key=lambda e: str(e.id))

    resolved = run_id
    if resolved is None:
        for event in ordered:
            if event.run_id is not None:
                resolved = event.run_id
                break
    if resolved is None:
        raise ReplayError(
            "cannot reconstruct a run from events that carry no run identifier",
            detail={"event_count": len(ordered)},
        )

    builder = _Builder(resolved)
    for event in ordered:
        if event.run_id is not None and event.run_id != resolved:
            raise ReplayError(
                f"event {event.id} belongs to run {event.run_id}, not {resolved}; "
                "replay operates on one run at a time",
                detail={
                    "event_id": str(event.id),
                    "expected_run": str(resolved),
                    "found_run": str(event.run_id),
                },
            )
        _apply(builder, event)
        builder.event_count += 1
        builder.last_event_id = event.id

    return builder.finish()

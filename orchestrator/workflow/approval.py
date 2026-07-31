"""Human approval gates, attempt history, and transcripts.

Some work should not merge because a test passed. A migration, a credential
rotation, anything whose failure mode is discovered in production — those want
a person to look. The approval gate is where a run stops and waits.

The design follows from one decision: **an approval is an event, not a row.**
It is recorded in the same append-only log as everything else, which means it
replays, it survives a crash, it is attributable, and it cannot be quietly
changed afterwards. A pending approval is not a flag somewhere; it is the
absence of a resolution event for a request that exists.

What a reviewer needs to decide is assembled from the same log:

``AttemptHistory``
    Every attempt at a task, what each produced, and why it failed. A reviewer
    looking at attempt three should be able to see that attempts one and two
    failed the same gate.
``Transcript``
    The events of one attempt in order — tool calls, gate verdicts, the
    outcome. Not the model's raw output: that is the provider's business and
    is not what a reviewer is checking.
``Diff``
    What actually changed. Produced by the Git layer, because "what changed"
    is a question about a worktree and this module has no business answering it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from orchestrator.core.events import (
    AttemptId,
    Event,
    EventType,
    OrchestratorError,
    RunId,
    TaskId,
)
from orchestrator.core.records import RunState
from orchestrator.core.run_store import RunStore

__all__ = [
    "ApprovalDecision",
    "ApprovalError",
    "ApprovalRequest",
    "ApprovalService",
    "AttemptHistory",
    "AttemptSummary",
    "Transcript",
]


class ApprovalError(OrchestratorError):
    """An approval could not be requested or resolved."""

    code = "approval"
    retryable = False


class ApprovalDecision(StrEnum):
    """What a reviewer decided."""

    APPROVED = "approved"
    REJECTED = "rejected"
    RETRY = "retry"

    @property
    def accepts(self) -> bool:
        """Whether the work proceeds."""
        return self is ApprovalDecision.APPROVED

    @property
    def reopens(self) -> bool:
        """Whether the task should be attempted again."""
        return self is ApprovalDecision.RETRY


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """A task waiting for a person."""

    run_id: RunId
    task_id: TaskId
    title: str
    requested_at: str
    reason: str = ""
    attempt: int = 0
    resolved_at: str = ""
    decision: str = ""
    actor: str = ""
    note: str = ""

    @property
    def pending(self) -> bool:
        """Whether this is still waiting."""
        return not self.resolved_at

    @property
    def outcome(self) -> ApprovalDecision | None:
        """The decision, if one has been made."""
        if not self.decision:
            return None
        try:
            return ApprovalDecision(self.decision)
        except ValueError:
            return None

    def to_public(self) -> dict[str, Any]:
        """Render for a response."""
        return {
            "run_id": str(self.run_id),
            "task_id": str(self.task_id),
            "title": self.title,
            "attempt": self.attempt,
            "reason": self.reason,
            "requested_at": self.requested_at,
            "pending": self.pending,
            "resolved_at": self.resolved_at or None,
            "decision": self.decision or None,
            "actor": self.actor or None,
            "note": self.note or None,
        }


@dataclass(frozen=True, slots=True)
class AttemptSummary:
    """One attempt at a task."""

    id: str
    number: int
    status: str
    started_at: str
    finished_at: str = ""
    duration_s: float | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float | None = None
    #: Whether the token counts are approximations (OP-004). A reviewer
    #: weighing "attempt three burned 2M tokens" should know whether that
    #: number was measured or guessed at four characters per token.
    tokens_estimated: bool = False
    branch: str = ""
    workspace_path: str = ""
    gates: tuple[Mapping[str, Any], ...] = ()
    error_code: str = ""
    #: Whether this attempt's work reached the integration branch. ``None``
    #: means the log does not say — a run recorded before OP-011. Reviewers
    #: read this to answer "did it land?" without opening a terminal.
    merged: bool | None = None
    merge_status: str = ""
    changed_files: tuple[str, ...] = ()
    conflicted_paths: tuple[str, ...] = ()

    @property
    def finished(self) -> bool:
        """Whether this attempt has stopped."""
        return bool(self.finished_at)

    @property
    def failing_gates(self) -> tuple[str, ...]:
        """The gates this attempt did not clear."""
        return tuple(
            str(gate.get("gate", "")) for gate in self.gates if gate.get("verdict") != "passed"
        )

    def to_public(self) -> dict[str, Any]:
        """Render for a response."""
        return {
            "id": self.id,
            "number": self.number,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at or None,
            "duration_s": self.duration_s,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "tokens_estimated": self.tokens_estimated,
            "cost_usd": self.cost_usd,
            "branch": self.branch or None,
            "workspace_path": self.workspace_path or None,
            "gates": [dict(gate) for gate in self.gates],
            "failing_gates": list(self.failing_gates),
            "error_code": self.error_code or None,
            "merged": self.merged,
            "merge_status": self.merge_status or None,
            "changed_files": list(self.changed_files),
            "conflicted_paths": list(self.conflicted_paths),
        }


@dataclass(frozen=True, slots=True)
class AttemptHistory:
    """Every attempt at one task."""

    run_id: RunId
    task_id: TaskId
    title: str
    state: str
    max_attempts: int
    attempts: tuple[AttemptSummary, ...] = ()

    @property
    def latest(self) -> AttemptSummary | None:
        """The most recent attempt."""
        return self.attempts[-1] if self.attempts else None

    @property
    def repeated_failures(self) -> tuple[str, ...]:
        """Gates that failed in more than one attempt.

        The thing a reviewer most wants to know at attempt three: is this the
        same wall, or a different one.
        """
        counts: dict[str, int] = {}
        for attempt in self.attempts:
            for gate in attempt.failing_gates:
                counts[gate] = counts.get(gate, 0) + 1
        return tuple(sorted(gate for gate, count in counts.items() if count > 1))

    def to_public(self) -> dict[str, Any]:
        """Render for a response."""
        return {
            "run_id": str(self.run_id),
            "task_id": str(self.task_id),
            "title": self.title,
            "state": self.state,
            "max_attempts": self.max_attempts,
            "attempts": [attempt.to_public() for attempt in self.attempts],
            "repeated_failures": list(self.repeated_failures),
        }


@dataclass(frozen=True, slots=True)
class TranscriptEntry:
    """One thing that happened during an attempt."""

    at: str
    type: str
    summary: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def to_public(self) -> dict[str, Any]:
        """Render for a response."""
        return {
            "at": self.at,
            "type": self.type,
            "summary": self.summary,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True, slots=True)
class Transcript:
    """What one attempt did, in order."""

    run_id: RunId
    task_id: TaskId
    attempt_id: AttemptId | None
    entries: tuple[TranscriptEntry, ...] = ()

    @property
    def tool_calls(self) -> int:
        """How many tools the attempt called."""
        return sum(1 for entry in self.entries if entry.type == EventType.TOOL_CALLED.value)

    def to_public(self) -> dict[str, Any]:
        """Render for a response."""
        return {
            "run_id": str(self.run_id),
            "task_id": str(self.task_id),
            "attempt_id": str(self.attempt_id) if self.attempt_id else None,
            "tool_calls": self.tool_calls,
            "entries": [entry.to_public() for entry in self.entries],
        }


def _summarize(event: Event) -> str:
    """Render one event as a line a reviewer can read."""
    payload = event.payload
    kind = event.type

    if kind is EventType.TOOL_CALLED:
        return f"called {payload.get('tool', 'a tool')}"
    if kind is EventType.GATE_EVALUATED:
        return f"gate {payload.get('gate', '?')}: {payload.get('verdict', '?')}"
    if kind is EventType.ATTEMPT_STARTED:
        return f"attempt {payload.get('number', '?')} started"
    if kind is EventType.ATTEMPT_FINISHED:
        return f"attempt finished: {payload.get('status', '?')}"
    if kind is EventType.TASK_FAILED:
        return f"failed: {payload.get('error_code', 'unknown')}"
    if kind is EventType.TASK_SUCCEEDED:
        return "succeeded"
    if kind is EventType.APPROVAL_REQUESTED:
        return f"approval requested: {payload.get('reason', '')}".strip()
    if kind is EventType.APPROVAL_RESOLVED:
        return f"{payload.get('decision', '?')} by {payload.get('actor', 'somebody')}"
    return kind.value


class ApprovalService:
    """Requests, resolves, and reports on human approval gates."""

    __slots__ = ("_store",)

    def __init__(self, store: RunStore) -> None:
        """Bind the service to a run store."""
        self._store = store

    @property
    def store(self) -> RunStore:
        """The underlying store."""
        return self._store

    # ------------------------------------------------------------- requests

    def request(
        self, run_id: RunId, task_id: TaskId, *, reason: str = "", attempt: int = 0
    ) -> ApprovalRequest:
        """Record that a task is waiting for a person.

        Raises:
            ApprovalError: If one is already pending for this task. Two open
                requests for one task means two reviewers can disagree and both
                be recorded.
        """
        existing = self.for_task(run_id, task_id)
        if existing is not None and existing.pending:
            raise ApprovalError(
                f"task {task_id} is already waiting for approval",
                detail={"run_id": str(run_id), "task_id": str(task_id)},
            )

        event = Event.new(
            EventType.APPROVAL_REQUESTED,
            run_id=run_id,
            task_id=task_id,
            payload={"reason": reason, "attempt": attempt},
        )
        self._store.record(event)
        return ApprovalRequest(
            run_id=run_id,
            task_id=task_id,
            title=self._title_of(run_id, task_id),
            requested_at=event.ts.isoformat(),
            reason=reason,
            attempt=attempt,
        )

    def resolve(
        self,
        run_id: RunId,
        task_id: TaskId,
        decision: ApprovalDecision,
        *,
        actor: str,
        note: str = "",
    ) -> ApprovalRequest:
        """Record a reviewer's decision.

        Args:
            run_id: The run.
            task_id: The task waiting.
            decision: Approve, reject, or send it back for another attempt.
            actor: Who decided. Required: an unattributed approval is not one.
            note: Why, in their words.

        Returns:
            The resolved request.

        Raises:
            ApprovalError: If nothing is pending, or ``actor`` is empty.
        """
        if not actor.strip():
            raise ApprovalError("an approval must record who made it")

        pending = self.for_task(run_id, task_id)
        if pending is None:
            raise ApprovalError(
                f"task {task_id} is not waiting for approval",
                detail={"run_id": str(run_id), "task_id": str(task_id)},
            )
        if not pending.pending:
            raise ApprovalError(
                f"task {task_id} was already {pending.decision} by {pending.actor}",
                detail={"decision": pending.decision, "actor": pending.actor},
            )

        event = Event.new(
            EventType.APPROVAL_RESOLVED,
            run_id=run_id,
            task_id=task_id,
            payload={"decision": decision.value, "actor": actor, "note": note},
        )
        self._store.record(event)

        # The task's own state follows the decision. Rejecting abandons it;
        # retrying puts it back; approving leaves the engine to carry on.
        if decision is ApprovalDecision.REJECTED:
            self._store.record(
                Event.new(
                    EventType.TASK_ABANDONED,
                    run_id=run_id,
                    task_id=task_id,
                    payload={"reason": f"rejected by {actor}"},
                )
            )
        elif decision is ApprovalDecision.RETRY:
            self._store.record(
                Event.new(
                    EventType.TASK_READY,
                    run_id=run_id,
                    task_id=task_id,
                    payload={"retry": True, "reason": f"sent back by {actor}"},
                )
            )

        return ApprovalRequest(
            run_id=pending.run_id,
            task_id=pending.task_id,
            title=pending.title,
            requested_at=pending.requested_at,
            reason=pending.reason,
            attempt=pending.attempt,
            resolved_at=event.ts.isoformat(),
            decision=decision.value,
            actor=actor,
            note=note,
        )

    def for_task(self, run_id: RunId, task_id: TaskId) -> ApprovalRequest | None:
        """Return the most recent request for a task, resolved or not."""
        requests = [
            request for request in self.for_run(run_id) if request.task_id == task_id
        ]
        return requests[-1] if requests else None

    def for_run(self, run_id: RunId) -> tuple[ApprovalRequest, ...]:
        """Every approval request in a run, oldest first.

        Reconstructed from the log rather than read from a table: the log is
        authoritative, and a request that had been resolved twice would show up
        here as the contradiction it is rather than as one overwritten row.
        """
        events = self._store.events.read_run(run_id)
        titles = {
            task_id: task.title for task_id, task in self._store.replay(run_id).tasks.items()
        }

        opened: dict[TaskId, dict[str, Any]] = {}
        ordered: list[dict[str, Any]] = []

        for event in events:
            if event.task_id is None:
                continue
            if event.type is EventType.APPROVAL_REQUESTED:
                record = {
                    "task_id": event.task_id,
                    "requested_at": event.ts.isoformat(),
                    "reason": str(event.payload.get("reason", "")),
                    "attempt": int(event.payload.get("attempt", 0) or 0),
                    "resolved_at": "",
                    "decision": "",
                    "actor": "",
                    "note": "",
                }
                opened[event.task_id] = record
                ordered.append(record)
            elif event.type is EventType.APPROVAL_RESOLVED:
                record = opened.get(event.task_id)
                if record is None or record["resolved_at"]:
                    continue
                record["resolved_at"] = event.ts.isoformat()
                record["decision"] = str(event.payload.get("decision", ""))
                record["actor"] = str(event.payload.get("actor", ""))
                record["note"] = str(event.payload.get("note", ""))

        return tuple(
            ApprovalRequest(
                run_id=run_id,
                task_id=record["task_id"],
                title=titles.get(record["task_id"], ""),
                requested_at=record["requested_at"],
                reason=record["reason"],
                attempt=record["attempt"],
                resolved_at=record["resolved_at"],
                decision=record["decision"],
                actor=record["actor"],
                note=record["note"],
            )
            for record in ordered
        )

    def queue(self, *, limit: int = 100) -> tuple[ApprovalRequest, ...]:
        """Every pending approval across every run, oldest first.

        Oldest first on purpose: a queue sorted newest-first is a queue whose
        bottom never gets looked at.
        """
        pending: list[ApprovalRequest] = []
        for run_id in self._store.run_ids():
            pending.extend(
                request for request in self.for_run(run_id) if request.pending
            )
        pending.sort(key=lambda request: request.requested_at)
        return tuple(pending[:limit])

    # -------------------------------------------------------------- history

    def history(self, run_id: RunId, task_id: TaskId) -> AttemptHistory:
        """Assemble every attempt at a task.

        Raises:
            ApprovalError: If the task is not part of the run.
        """
        state = self._store.replay(run_id)
        task = state.tasks.get(task_id)
        if task is None:
            raise ApprovalError(
                f"run {run_id} has no task {task_id}",
                detail={"run_id": str(run_id), "task_id": str(task_id)},
            )

        gates_by_attempt: dict[str, list[dict[str, Any]]] = {}
        for gate in state.gates:
            gates_by_attempt.setdefault(str(gate.attempt_id), []).append(
                {"gate": gate.gate, "verdict": gate.verdict, "detail": dict(gate.detail)}
            )

        errors = self._error_codes(run_id, task_id)
        attempts = [
            AttemptSummary(
                id=str(attempt.id),
                number=attempt.number,
                status=attempt.status,
                started_at=attempt.started_at.isoformat(),
                finished_at=attempt.finished_at.isoformat() if attempt.finished_at else "",
                duration_s=(
                    (attempt.finished_at - attempt.started_at).total_seconds()
                    if attempt.finished_at
                    else None
                ),
                tokens_in=attempt.tokens_in,
                tokens_out=attempt.tokens_out,
                tokens_estimated=attempt.tokens_estimated,
                cost_usd=attempt.cost_usd,
                branch=attempt.branch or "",
                workspace_path=attempt.workspace_path or "",
                gates=tuple(gates_by_attempt.get(str(attempt.id), ())),
                error_code=errors.get(attempt.number, ""),
                merged=attempt.merged,
                merge_status=attempt.merge_status,
                changed_files=attempt.changed_files,
                conflicted_paths=attempt.conflicted_paths,
            )
            for attempt in sorted(
                (a for a in state.attempts.values() if a.task_id == task_id),
                key=lambda a: a.number,
            )
        ]

        return AttemptHistory(
            run_id=run_id,
            task_id=task_id,
            title=task.title,
            state=task.state,
            max_attempts=task.max_attempts,
            attempts=tuple(attempts),
        )

    def _error_codes(self, run_id: RunId, task_id: TaskId) -> dict[int, str]:
        """Map attempt numbers to the error each reported."""
        codes: dict[int, str] = {}
        for event in self._store.events.read_run(run_id):
            if event.task_id == task_id and event.type is EventType.TASK_FAILED:
                number = int(event.payload.get("attempt", 0) or 0)
                codes[number] = str(event.payload.get("error_code", ""))
        return codes

    def transcript(
        self, run_id: RunId, task_id: TaskId, *, attempt_id: AttemptId | None = None
    ) -> Transcript:
        """Assemble what one attempt did, in order.

        Args:
            run_id: The run.
            task_id: The task.
            attempt_id: One attempt. Omitted, every event for the task.

        Returns:
            The transcript.
        """
        entries = [
            TranscriptEntry(
                at=event.ts.isoformat(),
                type=event.type.value,
                summary=_summarize(event),
                payload=dict(event.payload),
            )
            for event in self._store.events.read_run(run_id)
            if event.task_id == task_id
            and (attempt_id is None or event.attempt_id in (attempt_id, None))
        ]
        return Transcript(
            run_id=run_id, task_id=task_id, attempt_id=attempt_id, entries=tuple(entries)
        )

    def _title_of(self, run_id: RunId, task_id: TaskId) -> str:
        """Return a task's title, or an empty string."""
        task = self._store.replay(run_id).tasks.get(task_id)
        return task.title if task else ""


def pending_in(state: RunState) -> tuple[TaskId, ...]:
    """Tasks in a replayed run that are waiting for approval.

    A convenience for the engine, which has a :class:`RunState` in hand and
    should not have to re-read the log to find out whether to stop.
    """
    return tuple(
        task_id
        for task_id, record in state.approvals.items()
        if record.resolved_at is None
    )


def decisions_of(requests: Sequence[ApprovalRequest]) -> Mapping[str, int]:
    """Count how requests were resolved, for a metrics view."""
    counts: dict[str, int] = {"pending": 0}
    for request in requests:
        if request.pending:
            counts["pending"] += 1
        else:
            counts[request.decision] = counts.get(request.decision, 0) + 1
    return counts

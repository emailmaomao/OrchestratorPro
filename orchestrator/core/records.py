"""Value objects for persisted rows and reconstructed run state.

These are the shapes that cross the storage boundary. They are deliberately
plain: no behaviour beyond derived properties, no database handles, no
identity beyond their fields.

**On stringly-typed state.** :attr:`TaskProjection.state` and
:attr:`AttemptProjection.status` are ``str``, not ``TaskState`` and
``AttemptStatus``. Those enums live in :mod:`orchestrator.task.model` and
:mod:`orchestrator.agent.model`, at layer 2; importing them here — at layer 0 —
would invert the dependency direction the architecture depends on
(``docs/020_ARCHITECTURE`` §1). Event payloads carry these values as strings
anyway, since events are JSON.

The vocabulary is still checked, just not by the type system: a test asserts
that every state string this module can produce is a member of the
corresponding layer-2 enum. Tests are not layered, so they can see both sides.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from orchestrator.core.events import AttemptId, EventId, RunId, TaskId

__all__ = [
    "ApprovalRecord",
    "AttemptProjection",
    "GateOutcome",
    "RunState",
    "RunStatus",
    "TaskProjection",
    "UsageTotals",
]


class RunStatus(StrEnum):
    """The lifecycle of a run as a whole.

    Distinct from a task's state: a run is ``FINISHED`` once no task can make
    further progress, whether or not every task succeeded.
    """

    CREATED = "created"
    RUNNING = "running"
    FINISHED = "finished"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        """Whether the run has stopped for good."""
        return self in (RunStatus.FINISHED, RunStatus.CANCELLED)


@dataclass(frozen=True, slots=True)
class UsageTotals:
    """Token and cost totals rolled up across attempts.

    ``cost_usd`` stays ``None`` when no attempt reported a cost, mirroring
    :class:`orchestrator.agent.model.TokenUsage`: reporting ``0.0`` for an
    unpriced run would understate it silently.
    """

    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float | None = None
    #: Sticky under aggregation: a total with one estimated contribution is an
    #: estimate, and must present itself as one.
    estimated: bool = False

    @property
    def total_tokens(self) -> int:
        """Input plus output tokens."""
        return self.tokens_in + self.tokens_out

    def plus(
        self,
        *,
        tokens_in: int,
        tokens_out: int,
        cost_usd: float | None,
        estimated: bool = False,
    ) -> UsageTotals:
        """Return these totals with one attempt's usage added."""
        if self.cost_usd is None and cost_usd is None:
            combined: float | None = None
        else:
            combined = (self.cost_usd or 0.0) + (cost_usd or 0.0)
        return UsageTotals(
            tokens_in=self.tokens_in + tokens_in,
            tokens_out=self.tokens_out + tokens_out,
            cost_usd=combined,
            estimated=self.estimated or estimated,
        )


@dataclass(frozen=True, slots=True)
class TaskProjection:
    """A task's reconstructed state within a run."""

    id: TaskId
    run_id: RunId
    title: str
    state: str
    prompt: str = ""
    depends_on: tuple[TaskId, ...] = ()
    max_attempts: int = 1
    attempts_made: int = 0

    @property
    def attempts_remaining(self) -> int:
        """How many further attempts the task is permitted."""
        return max(0, self.max_attempts - self.attempts_made)


@dataclass(frozen=True, slots=True)
class AttemptProjection:
    """One attempt's reconstructed state."""

    id: AttemptId
    task_id: TaskId
    run_id: RunId
    number: int
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    adapter: str | None = None
    workspace_path: str | None = None
    branch: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float | None = None
    #: Whether the token counts are approximations from a provider without
    #: exact counting. An estimate presented as a measurement is the quiet
    #: cousin of reporting $0.00 for an unpriced run.
    tokens_estimated: bool = False
    #: Whether the attempt's work reached the integration branch. **Three
    #: states, not two**: ``True`` landed, ``False`` did not, and ``None``
    #: means the log does not say — a run recorded before OP-011, or one with
    #: no merge step at all. Collapsing ``None`` into ``False`` would report
    #: "did not merge" about runs that merged perfectly well.
    merged: bool | None = None
    #: The merge verdict in the git_manager's own words (``merged``,
    #: ``conflict``, ``nothing_to_merge``, …). Empty when unrecorded.
    merge_status: str = ""
    #: What the attempt changed. An empty tuple on a *succeeded* attempt is
    #: the shape of a vacuous success, which is why it belongs in the log.
    changed_files: tuple[str, ...] = ()
    #: Files that collided, when the merge conflicted.
    conflicted_paths: tuple[str, ...] = ()

    @property
    def is_finished(self) -> bool:
        """Whether the attempt has completed."""
        return self.finished_at is not None


@dataclass(frozen=True, slots=True)
class GateOutcome:
    """The verdict one gate returned for one attempt."""

    id: EventId
    attempt_id: AttemptId
    run_id: RunId
    gate: str
    verdict: str
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        """Whether the gate was cleared."""
        return self.verdict == "passed"


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    """A human approval gate, requested and possibly resolved."""

    id: EventId
    task_id: TaskId
    run_id: RunId
    requested_at: datetime
    resolved_at: datetime | None = None
    decision: str | None = None
    actor: str | None = None

    @property
    def is_pending(self) -> bool:
        """Whether the approval is still awaiting a decision."""
        return self.resolved_at is None


@dataclass(frozen=True, slots=True)
class RunState:
    """The complete reconstructed state of one run.

    This is what replaying the event log produces, and what the materialized
    tables are expected to agree with. If they ever disagree, the log wins
    (``docs/020_ARCHITECTURE`` §5).
    """

    run_id: RunId
    status: RunStatus
    goal: str = ""
    repo_path: str = ""
    created_at: datetime | None = None
    finished_at: datetime | None = None
    tasks: Mapping[TaskId, TaskProjection] = field(default_factory=dict)
    attempts: Mapping[AttemptId, AttemptProjection] = field(default_factory=dict)
    gates: tuple[GateOutcome, ...] = ()
    approvals: Mapping[TaskId, ApprovalRecord] = field(default_factory=dict)
    usage: UsageTotals = field(default_factory=UsageTotals)
    tool_calls: int = 0
    event_count: int = 0
    last_event_id: EventId | None = None

    def tasks_in_state(self, state: str) -> tuple[TaskProjection, ...]:
        """Return every task currently in ``state``."""
        return tuple(task for task in self.tasks.values() if task.state == state)

    def state_counts(self) -> Mapping[str, int]:
        """Return a count of tasks per state, for dashboards and summaries."""
        counts: dict[str, int] = {}
        for task in self.tasks.values():
            counts[task.state] = counts.get(task.state, 0) + 1
        return counts

    def attempts_for(self, task_id: TaskId) -> tuple[AttemptProjection, ...]:
        """Return a task's attempts, oldest first."""
        return tuple(
            sorted(
                (a for a in self.attempts.values() if a.task_id == task_id),
                key=lambda a: a.number,
            )
        )

    @property
    def pending_approvals(self) -> tuple[ApprovalRecord, ...]:
        """Approvals still awaiting a decision."""
        return tuple(a for a in self.approvals.values() if a.is_pending)

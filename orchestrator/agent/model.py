"""The agent domain model: attempts, budgets, and their accounting.

An *attempt* is one try at one task by one agent. Attempts are the unit of
isolation (each gets its own workspace), the unit of retry, and the unit of cost
attribution — every token spent belongs to exactly one attempt (FR-5.4).

This module does not import :mod:`orchestrator.task`, and must not. Per
``docs/020_ARCHITECTURE`` §1 the agent runtime receives a :class:`TaskSpec` — a
narrow value object — rather than a ``Task``, so the runtime can be tested with
no task graph in sight and the scheduler can be tested with no agent. The
workflow engine performs the translation.

Like the task model, this module is pure: no I/O, no processes, no provider
calls. It is the vocabulary those layers will speak, defined ahead of them.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

from orchestrator.core.events import (
    AttemptId,
    Budget,
    BudgetAxis,
    BudgetExhaustedError,
    DomainValidationError,
    StateTransitionError,
    TaskId,
)

__all__ = [
    "Attempt",
    "AttemptResult",
    "AttemptStatus",
    "AgentRole",
    "BudgetLedger",
    "TaskSpec",
    "TokenUsage",
    "total_usage",
]


class AgentRole(StrEnum):
    """What an agent is being asked to do.

    Roles select model settings (``[provider.roles]`` in configuration) so one
    run can mix depths: expensive planning, cheap summarization.
    """

    PLANNER = "planner"
    WORKER = "worker"
    SUMMARIZER = "summarizer"
    REVIEWER = "reviewer"


class AttemptStatus(StrEnum):
    """How an attempt ended.

    Only :attr:`SUCCEEDED` means the agent finished its work; it does **not**
    mean the work was accepted. Gates are applied by the workflow engine after
    the attempt returns, because an agent must never be able to accept its own
    output (``docs/020_ARCHITECTURE`` §3.1).
    """

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    BUDGET_EXHAUSTED = "budget_exhausted"
    REFUSED = "refused"
    CANCELLED = "cancelled"
    ERRORED = "errored"

    @property
    def is_terminal(self) -> bool:
        """Whether the attempt has finished, however it ended."""
        return self is not AttemptStatus.RUNNING

    @property
    def produced_work(self) -> bool:
        """Whether output worth gating may exist.

        A budget-exhausted attempt often leaves partial but useful work, which
        is why it is preserved rather than discarded (FR-2.7).
        """
        return self in (AttemptStatus.SUCCEEDED, AttemptStatus.BUDGET_EXHAUSTED)


@dataclass(frozen=True, slots=True)
class TaskSpec:
    """The narrow instruction handed to an agent for one attempt.

    Deliberately not a ``Task``: it carries no dependencies, no gates, and no
    graph position. An agent does not need them, and withholding them keeps the
    agent runtime independent of the task package.

    Attributes:
        task_id: Which task this attempt serves, for correlation and events.
        title: Short human-readable summary.
        prompt: The instruction itself.
        role: Which role's model settings apply.
        feedback: Why the previous attempt failed. Empty on the first attempt.
            This is the mechanism behind retry-with-feedback (FR-2.5) — attempt
            *n+1* is told what went wrong with attempt *n* rather than starting
            blind.
        labels: Free-form tags carried through from the task.
    """

    task_id: TaskId
    title: str
    prompt: str
    role: AgentRole = AgentRole.WORKER
    feedback: tuple[str, ...] = ()
    labels: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise DomainValidationError("task_spec.title must not be empty")
        if not self.prompt.strip():
            raise DomainValidationError("task_spec.prompt must not be empty")

    def with_feedback(self, note: str) -> TaskSpec:
        """Return a copy carrying one additional feedback note.

        Args:
            note: What went wrong, in terms an agent can act on.

        Returns:
            A new spec; the original is unchanged.

        Raises:
            DomainValidationError: If ``note`` is blank.
        """
        if not note.strip():
            raise DomainValidationError("feedback note must not be empty")
        return replace(self, feedback=(*self.feedback, note))


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Token counts and cost for one attempt.

    ``cost_usd`` is ``None`` rather than ``0.0`` when a provider cannot price
    its own calls. Reporting zero would understate a run's cost silently, which
    is worse than reporting nothing.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cost_usd: float | None = None
    #: Whether the counts are approximations. Set when any contributing call
    #: came from a provider without exact token counting; sticky under
    #: addition, because a total with one estimated component is an estimate.
    estimated: bool = False

    def __post_init__(self) -> None:
        for name, value in (
            ("input_tokens", self.input_tokens),
            ("output_tokens", self.output_tokens),
            ("cached_input_tokens", self.cached_input_tokens),
        ):
            if value < 0:
                raise DomainValidationError(
                    f"token_usage.{name} must not be negative, got {value}",
                    detail={"field": name, "value": value},
                )
        if self.cost_usd is not None and self.cost_usd < 0:
            raise DomainValidationError(
                f"token_usage.cost_usd must not be negative, got {self.cost_usd}"
            )

    @property
    def total_tokens(self) -> int:
        """Input plus output. Cached input is already counted in input."""
        return self.input_tokens + self.output_tokens

    def __add__(self, other: TokenUsage) -> TokenUsage:
        """Combine two usage records.

        Cost is summed when either side reports one, and stays ``None`` only
        when neither does — so a partially-priced run still reports what is
        known rather than discarding it.
        """
        if self.cost_usd is None and other.cost_usd is None:
            cost: float | None = None
        else:
            cost = (self.cost_usd or 0.0) + (other.cost_usd or 0.0)
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            cost_usd=cost,
            estimated=self.estimated or other.estimated,
        )


class BudgetLedger:
    """Tracks consumption against a :class:`Budget` on all three axes.

    The ledger is the one mutable object in this module. It exists because
    budget enforcement is inherently stateful: consumption accumulates during an
    attempt and must be checkable at every step.

    The clock is injected so that time-based exhaustion can be tested without
    sleeping. Elapsed time uses a monotonic source, which cannot run backwards
    when the system clock is adjusted mid-attempt.
    """

    __slots__ = (
        "_budget",
        "_clock",
        "_started",
        "_tokens",
        "_tokens_estimated",
        "_tool_calls",
    )

    def __init__(self, budget: Budget, *, clock: Callable[[], float] | None = None) -> None:
        """Open a ledger against ``budget``.

        Args:
            budget: The allowance to enforce.
            clock: Monotonic time source in seconds. Defaults to
                :func:`time.monotonic`.
        """
        self._budget = budget
        self._clock = time.monotonic if clock is None else clock
        self._started = self._clock()
        self._tokens = 0
        self._tokens_estimated = False
        self._tool_calls = 0

    @property
    def budget(self) -> Budget:
        """The allowance being enforced."""
        return self._budget

    @property
    def elapsed_seconds(self) -> float:
        """Seconds since the ledger was opened."""
        return self._clock() - self._started

    @property
    def tokens_used(self) -> int:
        """Tokens consumed so far."""
        return self._tokens

    @property
    def tokens_estimated(self) -> bool:
        """Whether any recorded token count was an approximation.

        Sticky: one estimated contribution makes the whole figure an estimate.
        The token axis still binds on it — an unbounded axis under an
        unmetered backend is how an overnight run runs away — but everything
        that reports the figure must be able to say what it is.
        """
        return self._tokens_estimated

    @property
    def tool_calls_used(self) -> int:
        """Tool calls made so far."""
        return self._tool_calls

    def consumed(self, axis: BudgetAxis) -> float:
        """Return consumption on ``axis``."""
        match axis:
            case BudgetAxis.SECONDS:
                return self.elapsed_seconds
            case BudgetAxis.TOKENS:
                return float(self._tokens)
            case BudgetAxis.TOOL_CALLS:
                return float(self._tool_calls)

    def remaining(self, axis: BudgetAxis) -> float:
        """Return the headroom left on ``axis``, floored at zero."""
        return max(0.0, self._budget.limit_for(axis) - self.consumed(axis))

    @property
    def exhausted_axis(self) -> BudgetAxis | None:
        """The first axis at or over its limit, or ``None`` if none is.

        Axes are checked in a fixed order, so an attempt that blows two limits
        simultaneously reports the same axis every time. Stable attribution
        matters more here than picking the "most" exceeded axis.
        """
        for axis in (BudgetAxis.SECONDS, BudgetAxis.TOKENS, BudgetAxis.TOOL_CALLS):
            if self.consumed(axis) >= self._budget.limit_for(axis):
                return axis
        return None

    @property
    def is_exhausted(self) -> bool:
        """Whether any axis has reached its limit."""
        return self.exhausted_axis is not None

    def check(self) -> None:
        """Raise if any axis is exhausted.

        Raises:
            BudgetExhaustedError: Naming the axis, its limit, and consumption.
        """
        axis = self.exhausted_axis
        if axis is not None:
            raise BudgetExhaustedError(
                axis,
                limit=self._budget.limit_for(axis),
                consumed=self.consumed(axis),
                estimated=(axis is BudgetAxis.TOKENS and self._tokens_estimated),
            )

    def record_tokens(self, count: int, *, estimated: bool = False) -> None:
        """Record token consumption.

        Args:
            count: Tokens consumed. Must not be negative.
            estimated: Whether the count is an approximation from a provider
                without exact token counting. Sticky for the ledger's lifetime.

        Raises:
            DomainValidationError: If ``count`` is negative.
        """
        if count < 0:
            raise DomainValidationError(
                f"cannot record a negative token count: {count}"
            )
        self._tokens += count
        self._tokens_estimated = self._tokens_estimated or estimated

    def record_tool_call(self, count: int = 1) -> None:
        """Record one or more tool calls.

        Args:
            count: How many calls to record. Must be positive.

        Raises:
            DomainValidationError: If ``count`` is not positive.
        """
        if count < 1:
            raise DomainValidationError(
                f"cannot record a non-positive tool-call count: {count}"
            )
        self._tool_calls += count

    def snapshot(self) -> Mapping[str, float]:
        """Return current consumption on every axis, for logs and events."""
        return {axis.value: self.consumed(axis) for axis in BudgetAxis}


@dataclass(frozen=True, slots=True)
class AttemptResult:
    """What an attempt produced.

    Note the absence of any "passed" or "accepted" field. An attempt reports
    what it did; whether that is acceptable is decided afterwards by gates the
    agent does not control.

    Attributes:
        status: How the attempt ended.
        changed_files: Repository-relative paths the attempt modified.
        usage: Token consumption and cost.
        summary: Short account of what was done, for the dashboard.
        error_code: The stable error code when ``status`` is a failure.
        detail: Structured extras for the event payload.
    """

    status: AttemptStatus
    changed_files: tuple[str, ...] = ()
    usage: TokenUsage = field(default_factory=TokenUsage)
    summary: str = ""
    error_code: str | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status is AttemptStatus.RUNNING:
            raise DomainValidationError(
                "attempt_result.status must be terminal; 'running' is not a result"
            )
        if len(set(self.changed_files)) != len(self.changed_files):
            raise DomainValidationError("attempt_result.changed_files must be unique")

    @property
    def succeeded(self) -> bool:
        """Whether the agent completed its work without failing."""
        return self.status is AttemptStatus.SUCCEEDED


_MIN_ATTEMPT_NUMBER: Final = 1


@dataclass(frozen=True, slots=True)
class Attempt:
    """One try at one task, in its own workspace.

    Immutable: :meth:`finish` returns a new instance rather than mutating this
    one, so an attempt record cannot be edited after the fact. That matters
    because attempts are the cost-attribution unit and the audit trail.

    Attributes:
        id: Stable identifier, sortable by creation time.
        task_id: The task this attempt serves.
        number: 1-based ordinal within the task.
        status: Current status.
        started_at: When the attempt began (UTC).
        finished_at: When it ended (UTC), or ``None`` while running.
        workspace_path: Filesystem path of the isolated worktree.
        branch: Branch the attempt commits to.
        result: What it produced, or ``None`` while running.
    """

    id: AttemptId
    task_id: TaskId
    number: int
    status: AttemptStatus
    started_at: datetime
    finished_at: datetime | None = None
    workspace_path: str | None = None
    branch: str | None = None
    result: AttemptResult | None = None

    def __post_init__(self) -> None:
        if self.number < _MIN_ATTEMPT_NUMBER:
            raise DomainValidationError(
                f"attempt.number is 1-based; got {self.number}",
                detail={"attempt_id": str(self.id), "number": self.number},
            )
        if self.started_at.tzinfo is None:
            raise DomainValidationError("attempt.started_at must be timezone-aware")
        if self.finished_at is not None:
            if self.finished_at.tzinfo is None:
                raise DomainValidationError("attempt.finished_at must be timezone-aware")
            if self.finished_at < self.started_at:
                raise DomainValidationError(
                    "attempt.finished_at must not precede started_at",
                    detail={"attempt_id": str(self.id)},
                )
        if self.status.is_terminal and self.result is None:
            raise DomainValidationError(
                f"a terminal attempt must carry a result; status={self.status.value}",
                detail={"attempt_id": str(self.id)},
            )
        if not self.status.is_terminal and self.result is not None:
            raise DomainValidationError(
                "a running attempt must not carry a result",
                detail={"attempt_id": str(self.id)},
            )

    @classmethod
    def start(
        cls,
        *,
        task_id: TaskId,
        number: int,
        workspace_path: str | None = None,
        branch: str | None = None,
    ) -> Attempt:
        """Open a new attempt, minting its identifier and stamping the start.

        Args:
            task_id: The task being attempted.
            number: 1-based ordinal within the task.
            workspace_path: Path to the isolated worktree, once created.
            branch: Branch the attempt will commit to.

        Returns:
            A running attempt.
        """
        return cls(
            id=AttemptId.generate(),
            task_id=task_id,
            number=number,
            status=AttemptStatus.RUNNING,
            started_at=datetime.now(UTC),
            workspace_path=workspace_path,
            branch=branch,
        )

    def finish(self, result: AttemptResult, *, at: datetime | None = None) -> Attempt:
        """Close the attempt with ``result``.

        Args:
            result: What the attempt produced.
            at: Completion time (UTC). Defaults to now.

        Returns:
            A new, terminal attempt record.

        Raises:
            StateTransitionError: If this attempt has already finished.
        """
        if self.status.is_terminal:
            raise StateTransitionError(
                f"attempt {self.id} already finished with status "
                f"{self.status.value!r} and cannot be finished again",
                detail={"attempt_id": str(self.id), "status": self.status.value},
            )
        return replace(
            self,
            status=result.status,
            finished_at=datetime.now(UTC) if at is None else at,
            result=result,
        )

    @property
    def duration_seconds(self) -> float | None:
        """Wall-clock duration, or ``None`` while still running."""
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    @property
    def usage(self) -> TokenUsage:
        """Token usage, zeroed while the attempt is still running."""
        return self.result.usage if self.result is not None else TokenUsage()


def total_usage(attempts: Iterable[Attempt]) -> TokenUsage:
    """Sum token usage across attempts.

    Args:
        attempts: The attempts to total.

    Returns:
        Combined usage. Cost is ``None`` only if no attempt reported one.
    """
    total = TokenUsage()
    for attempt in attempts:
        total = total + attempt.usage
    return total

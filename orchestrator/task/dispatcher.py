"""The dispatcher — the only part of this package that actually does anything.

:mod:`orchestrator.task.scheduler` decides *what* should run; this drives it.
It owns the concurrency slots, the state transitions, the backoff timers, and
the event stream, and it is the one module here that awaits.

**It does not know what an attempt is.** Work is performed by an injected
:class:`TaskExecutor` returning a neutral :class:`AttemptOutcome`. That is not
indirection for its own sake: ``docs/020_ARCHITECTURE`` §1 requires that the
``task`` and ``agent`` packages never reference each other, with the workflow
engine translating between them. Keeping the executor abstract is what lets the
dispatcher be tested with no agent, no provider, and no network — and it is why
mocking a provider here is trivial: there is no provider to mock.

**Backoff does not hold a slot.** When an attempt fails and will be retried, the
slot is released immediately and a timer runs alongside the remaining work. A
sleeping retry that occupied a concurrency slot would quietly halve throughput
on a flaky run.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from orchestrator.core.events import (
    AttemptId,
    Event,
    EventType,
    OrchestratorError,
    RunId,
    TaskId,
)
from orchestrator.task.graph import TaskGraph
from orchestrator.task.model import Task, TaskState
from orchestrator.task.queue import ReadyQueue
from orchestrator.task.retry import RetryPolicy
from orchestrator.task.scheduler import SchedulerState, is_complete, next_ready

__all__ = [
    "AttemptOutcome",
    "DispatchReport",
    "DispatcherError",
    "TaskDispatcher",
    "TaskExecutor",
]


class DispatcherError(OrchestratorError):
    """The dispatcher could not proceed."""

    code = "dispatcher"
    retryable = False


@dataclass(frozen=True, slots=True)
class AttemptOutcome:
    """What one attempt produced, in terms the task package understands.

    Deliberately narrow. The workflow engine will map a richer
    ``agent.AttemptResult`` onto this; the dispatcher needs only to know whether
    the attempt worked and, if not, whether trying again could help.
    """

    ok: bool
    error_code: str | None = None
    retryable: bool = False
    detail: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def success(cls, **detail: Any) -> AttemptOutcome:
        """Build a successful outcome."""
        return cls(ok=True, detail=detail)

    @classmethod
    def failure(
        cls, error_code: str, *, retryable: bool = False, **detail: Any
    ) -> AttemptOutcome:
        """Build a failed outcome."""
        return cls(ok=False, error_code=error_code, retryable=retryable, detail=detail)


class TaskExecutor(Protocol):
    """Performs one attempt at one task."""

    async def __call__(self, task: Task, attempt: int) -> AttemptOutcome:
        """Run ``task`` and report what happened.

        Args:
            task: The task to perform.
            attempt: The 1-based attempt number.

        Returns:
            The outcome. Raising is also permitted — the dispatcher converts an
            unexpected exception into a non-retryable failure, on the reasoning
            that an executor which crashed has a bug rather than a blip.
        """
        ...


@dataclass(frozen=True, slots=True)
class DispatchReport:
    """The outcome of one dispatch run."""

    succeeded: tuple[TaskId, ...] = ()
    failed: tuple[TaskId, ...] = ()
    blocked: tuple[TaskId, ...] = ()
    abandoned: tuple[TaskId, ...] = ()
    attempts: Mapping[TaskId, int] = field(default_factory=dict)
    states: Mapping[TaskId, TaskState] = field(default_factory=dict)
    cancelled: bool = False
    deadlocked: bool = False

    @property
    def total_attempts(self) -> int:
        """How many attempts were made across every task."""
        return sum(self.attempts.values())

    @property
    def complete(self) -> bool:
        """Whether every task succeeded."""
        return not (self.failed or self.blocked or self.abandoned)

    @property
    def unfinished(self) -> tuple[TaskId, ...]:
        """Tasks that did not succeed, in identifier order."""
        return tuple(sorted({*self.failed, *self.blocked, *self.abandoned}))


#: What a pending future is waiting for.
_ATTEMPT = "attempt"
_RETRY_TIMER = "retry"

#: Outcome fields that belong in the attempt's log entry (OP-011). Named here
#: rather than inferred so that adding one is a deliberate act: everything in
#: ``outcome.detail`` is executor-private except what this list promotes into
#: the permanent record. The dispatcher does not interpret any of them — it
#: forwards them, exactly as it does ``usage``.
_ATTEMPT_RECORD_KEYS = (
    "merged",
    "merge_status",
    "changed_files",
    "conflicted_paths",
    # OP-014. `permission_denials` because a refused tool is the documented
    # cause of a silent no-op, and a cause nobody can find is not recorded.
    # `notional_cost_usd` under its own name, never as `cost_usd`: it is a
    # list-price equivalent for a subscription-billed call, not a charge.
    "permission_denials",
    "notional_cost_usd",
)


class TaskDispatcher:
    """Runs a task graph to completion, honouring dependencies and limits."""

    __slots__ = (
        "_attempt_ids",
        "_attempts",
        "_cancelled",
        "_event_sink",
        "_executor",
        "_graph",
        "_label_limits",
        "_max_concurrency",
        "_policy",
        "_queue",
        "_run_id",
        "_sleep",
        "_states",
    )

    def __init__(
        self,
        graph: TaskGraph,
        executor: TaskExecutor,
        *,
        max_concurrency: int = 4,
        policy: RetryPolicy | None = None,
        label_limits: Mapping[str, int] | None = None,
        run_id: RunId | None = None,
        event_sink: Callable[[Event], None] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        initial_states: Mapping[TaskId, TaskState] | None = None,
        initial_attempts: Mapping[TaskId, int] | None = None,
    ) -> None:
        """Create the dispatcher.

        Args:
            graph: The validated task graph.
            executor: Performs individual attempts.
            max_concurrency: Global cap on simultaneous attempts.
            policy: Retry and backoff policy. Defaults to exponential backoff.
            label_limits: Optional per-label concurrency caps.
            run_id: Stamped onto emitted events.
            event_sink: Receives an :class:`Event` per state transition. The
                dispatcher never touches the event store directly — the caller
                decides what durability means.
            sleep: Awaitable delay, injected so tests need not really wait.
            initial_states: Pre-existing task states, for resuming a run. Tasks
                already in a terminal state are never yielded by the scheduler,
                so completed work is not redone (FR-5.5).
            initial_attempts: Attempts each task has already had, so a resumed
                run does not hand a task a fresh allowance.

        Raises:
            DispatcherError: If ``max_concurrency`` is not positive, or the
                seeded state refers to a task outside the graph.
        """
        if max_concurrency < 1:
            raise DispatcherError(
                f"max_concurrency must be at least 1, got {max_concurrency}",
                detail={"max_concurrency": max_concurrency},
            )
        self._graph = graph
        self._executor = executor
        self._max_concurrency = max_concurrency
        self._policy = policy or RetryPolicy()
        self._label_limits = dict(label_limits or {})
        self._run_id = run_id
        self._event_sink = event_sink
        self._sleep = sleep or asyncio.sleep

        self._states: dict[TaskId, TaskState] = {
            task_id: TaskState.PENDING for task_id in graph.task_ids
        }
        self._attempts: dict[TaskId, int] = dict.fromkeys(graph.task_ids, 0)
        self._attempt_ids: dict[TaskId, AttemptId] = {}

        unknown = sorted(
            str(t) for t in {*(initial_states or {}), *(initial_attempts or {})}
            if t not in graph
        )
        if unknown:
            raise DispatcherError(
                f"seeded state refers to tasks outside the graph: {', '.join(unknown)}",
                detail={"unknown": unknown},
            )
        self._states.update(initial_states or {})
        self._attempts.update(initial_attempts or {})

        self._queue = ReadyQueue()
        self._cancelled = False

    # ---------------------------------------------------------------- helpers

    @property
    def states(self) -> Mapping[TaskId, TaskState]:
        """The current state of every task."""
        return dict(self._states)

    @property
    def attempts(self) -> Mapping[TaskId, int]:
        """How many attempts each task has had."""
        return dict(self._attempts)

    @property
    def cancelled(self) -> bool:
        """Whether cancellation has been requested."""
        return self._cancelled

    def request_cancel(self) -> None:
        """Stop starting new attempts; let in-flight ones finish (FR-2.9)."""
        self._cancelled = True

    def snapshot(self) -> SchedulerState:
        """Return an immutable view of the current state."""
        return SchedulerState(states=dict(self._states), attempts=dict(self._attempts))

    def report_now(self) -> DispatchReport:
        """Return a report of the current state without waiting for the run.

        For a caller that abandoned :meth:`run` — a run-level timeout, say — and
        still needs to record what had happened by then.
        """
        return self._report(deadlocked=False)

    @property
    def backlog(self) -> tuple[TaskId, ...]:
        """Tasks that are runnable but waiting for a concurrency slot.

        Ordered by how much downstream work each would unblock. The scheduler
        decides *what* is runnable; this records what is queued behind the cap,
        so an operator can see whether a run is dependency-bound or slot-bound.
        """
        return self._queue.snapshot()

    def _sync_backlog(self, ready: tuple[TaskId, ...]) -> None:
        """Make the backlog queue match the scheduler's deferred set."""
        deferred = set(ready)
        for task_id in self._queue.snapshot():
            if task_id not in deferred:
                self._queue.remove(task_id)
        self._queue.push_many(
            (task_id, self._graph.unblock_weight(task_id))
            for task_id in ready
            if task_id not in self._queue
        )

    def _emit(
        self,
        event_type: EventType,
        task_id: TaskId,
        *,
        attempt_id: AttemptId | None = None,
        **payload: Any,
    ) -> None:
        """Emit one event, if a sink was supplied."""
        if self._event_sink is None:
            return
        self._event_sink(
            Event.new(
                event_type,
                run_id=self._run_id,
                task_id=task_id,
                attempt_id=attempt_id,
                payload=payload,
            )
        )

    def _transition(self, task_id: TaskId, target: TaskState) -> None:
        """Move a task to ``target``, refusing an illegal transition."""
        current = self._states[task_id]
        current.assert_transition(target, task_id=task_id)
        self._states[task_id] = target

    # ------------------------------------------------------------------- run

    async def run(self) -> DispatchReport:
        """Execute the graph until nothing further can happen.

        Returns:
            A report of what succeeded, failed, blocked, or was abandoned.
        """
        pending: dict[asyncio.Task[Any], tuple[str, TaskId]] = {}
        deadlocked = False

        while True:
            decision = next_ready(
                self._graph,
                self.snapshot(),
                max_concurrency=self._max_concurrency,
                label_limits=self._label_limits,
            )

            for task_id in decision.block:
                self._mark_unreachable(task_id)

            self._sync_backlog(decision.ready)

            if not self._cancelled:
                for task_id in decision.start:
                    # Claim the slot *before* awaiting anything, or the next
                    # pass would see the task still startable and run it twice.
                    self._begin(task_id)
                    future = asyncio.ensure_future(self._attempt(task_id))
                    pending[future] = (_ATTEMPT, task_id)

            if not pending:
                if is_complete(self._graph, self.snapshot()):
                    break
                if self._cancelled:
                    # Cancelled with nothing in flight. There may still be
                    # startable tasks, but we are deliberately not starting
                    # them — looping to re-observe that would never terminate.
                    break
                if not decision.block and not decision.start:
                    # Nothing running, nothing startable, nothing blockable:
                    # the state cannot progress on its own.
                    deadlocked = True
                    break
                continue

            done, _ = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            for future in done:
                kind, task_id = pending.pop(future)
                if kind == _ATTEMPT:
                    self._settle(task_id, future, pending)
                else:
                    self._wake_from_backoff(task_id)

        return self._report(deadlocked=deadlocked)

    # -------------------------------------------------------------- internals

    def _mark_unreachable(self, task_id: TaskId) -> None:
        """Record that a task can never run, choosing a legal terminal state."""
        current = self._states[task_id]
        target = (
            TaskState.BLOCKED
            if current.can_transition_to(TaskState.BLOCKED)
            else TaskState.ABANDONED
        )
        self._transition(task_id, target)
        self._queue.remove(task_id)
        self._emit(
            EventType.TASK_BLOCKED if target is TaskState.BLOCKED else EventType.TASK_ABANDONED,
            task_id,
            reason="a dependency will never succeed"
            if target is TaskState.BLOCKED
            else "no attempts remain",
        )

    async def _attempt(self, task_id: TaskId) -> AttemptOutcome:
        """Perform one attempt, converting a crash into a failed outcome."""
        task = self._graph.get(task_id)
        attempt = self._attempts[task_id]
        try:
            return await self._executor(task, attempt)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - an executor crash is an outcome
            return AttemptOutcome.failure(
                "executor_error", retryable=False, message=str(exc)
            )

    def _begin(self, task_id: TaskId) -> None:
        """Move a task into RUNNING and charge it an attempt."""
        if self._states[task_id] is not TaskState.READY:
            self._transition(task_id, TaskState.READY)
            self._emit(EventType.TASK_READY, task_id)
        self._transition(task_id, TaskState.RUNNING)
        self._attempts[task_id] += 1
        self._emit(EventType.TASK_STARTED, task_id, attempt=self._attempts[task_id])

        # The attempt is opened in the log as well as the state machine: replay
        # counts attempts from these events, so without them a resumed run would
        # hand a task a fresh allowance it had already spent (FR-5.5).
        attempt_id = AttemptId.generate()
        self._attempt_ids[task_id] = attempt_id
        self._emit(
            EventType.ATTEMPT_STARTED,
            task_id,
            attempt_id=attempt_id,
            number=self._attempts[task_id],
        )

    def _close_attempt(
        self,
        task_id: TaskId,
        status: str,
        usage: Mapping[str, Any] | None = None,
        record: Mapping[str, Any] | None = None,
    ) -> None:
        """Close the task's open attempt in the log, if one is open.

        ``usage`` and ``record`` are spread into the payload verbatim. The
        executor supplies ``tokens_in`` / ``tokens_out`` / ``cost_usd`` /
        ``tokens_estimated`` in the first and the OP-011 outcome fields in the
        second; the dispatcher stays ignorant of what any of them mean and only
        refuses to lose them. That is precisely what went wrong twice — this
        payload once carried only ``status``, so every attempt replayed with
        zero tokens, and later still dropped the merge verdict, so a healthy
        run's log could not say whether the work had landed.
        """
        attempt_id = self._attempt_ids.pop(task_id, None)
        if attempt_id is not None:
            self._emit(
                EventType.ATTEMPT_FINISHED,
                task_id,
                attempt_id=attempt_id,
                status=status,
                **(dict(usage) if usage else {}),
                **(dict(record) if record else {}),
            )

    def _settle(
        self,
        task_id: TaskId,
        future: asyncio.Task[Any],
        pending: dict[asyncio.Task[Any], tuple[str, TaskId]],
    ) -> None:
        """Apply one finished attempt's outcome."""
        outcome: AttemptOutcome = future.result()
        raw_usage = outcome.detail.get("usage")
        self._close_attempt(
            task_id,
            "succeeded" if outcome.ok else "failed",
            usage=raw_usage if isinstance(raw_usage, Mapping) else None,
            record={
                key: outcome.detail[key]
                for key in _ATTEMPT_RECORD_KEYS
                if key in outcome.detail
            },
        )

        if outcome.ok:
            self._transition(task_id, TaskState.GATING)
            self._transition(task_id, TaskState.SUCCEEDED)
            self._emit(
                EventType.TASK_SUCCEEDED, task_id, attempt=self._attempts[task_id]
            )
            return

        self._transition(task_id, TaskState.FAILED)
        self._emit(
            EventType.TASK_FAILED,
            task_id,
            attempt=self._attempts[task_id],
            error_code=outcome.error_code,
            retryable=outcome.retryable,
            # Without the detail, an executor crash reads as a bare
            # "executor_error" and the one fact needed to diagnose it — the
            # exception message captured in _attempt — never reaches the log.
            # That is exactly how the merge-serialization race stayed
            # undiagnosable from the API.
            detail=dict(outcome.detail),
        )

        decision = self._policy.decide(
            attempt=self._attempts[task_id],
            max_attempts=self._graph.get(task_id).max_attempts,
            retryable=outcome.retryable,
            error_code=outcome.error_code or "",
        )
        if not decision.retry or self._cancelled:
            self._transition(task_id, TaskState.ABANDONED)
            self._emit(
                EventType.TASK_ABANDONED,
                task_id,
                reason="cancelled" if self._cancelled else decision.reason,
            )
            return

        timer = asyncio.ensure_future(self._backoff(decision.delay_s))
        pending[timer] = (_RETRY_TIMER, task_id)

    async def _backoff(self, delay_s: float) -> None:
        """Wait out a retry delay without occupying a concurrency slot."""
        if delay_s > 0:
            await self._sleep(delay_s)

    def _wake_from_backoff(self, task_id: TaskId) -> None:
        """Return a task to the runnable pool after its backoff expired."""
        if self._states[task_id] is not TaskState.FAILED:
            return
        self._transition(task_id, TaskState.RETRYING)
        self._emit(EventType.TASK_READY, task_id, retry=True)

    def _report(self, *, deadlocked: bool) -> DispatchReport:
        """Summarize the final state."""
        by_state: dict[TaskState, list[TaskId]] = {}
        for task_id, state in self._states.items():
            by_state.setdefault(state, []).append(task_id)

        return DispatchReport(
            succeeded=tuple(sorted(by_state.get(TaskState.SUCCEEDED, ()))),
            failed=tuple(sorted(by_state.get(TaskState.FAILED, ()))),
            blocked=tuple(sorted(by_state.get(TaskState.BLOCKED, ()))),
            abandoned=tuple(sorted(by_state.get(TaskState.ABANDONED, ()))),
            attempts=dict(self._attempts),
            states=dict(self._states),
            cancelled=self._cancelled,
            deadlocked=deadlocked,
        )

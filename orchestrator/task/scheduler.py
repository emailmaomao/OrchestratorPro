"""The scheduler — a pure function from graph and state to a decision.

``ORCHESTRATOR_PRO_SPEC`` §4.1 is explicit about this module's shape: *"The
scheduler is a pure function of graph + current state → set of tasks to start.
It has no I/O, which makes it exhaustively testable."*

So it does none. No clock, no database, no processes, no randomness. Given the
same graph and the same state it returns the same decision, every time
(NFR-1.1, NFR-1.2). Everything that actually *happens* — starting attempts,
sleeping between retries, writing events — belongs to the dispatcher above it.

Three invariants it exists to guarantee, each of which has a property test over
randomly generated graphs:

* **No task starts before its dependencies have succeeded** (FR-2.2).
* **The concurrency cap is never exceeded** (FR-2.1).
* **A task whose dependency failed terminally is blocked, never attempted**
  (FR-2.3).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from orchestrator.core.events import OrchestratorError, TaskId
from orchestrator.task.graph import TaskGraph
from orchestrator.task.model import TaskState

__all__ = [
    "SchedulerError",
    "SchedulingDecision",
    "SchedulerState",
    "is_complete",
    "next_ready",
]


class SchedulerError(OrchestratorError):
    """The scheduler was asked to reason about an incoherent state."""

    code = "scheduler"
    retryable = False


#: States from which a task may become runnable.
_STARTABLE = frozenset({TaskState.PENDING, TaskState.READY, TaskState.RETRYING})

#: States meaning a task will never succeed, so its dependents cannot proceed.
_DEAD = frozenset({TaskState.BLOCKED, TaskState.ABANDONED})

#: States meaning a task is occupying a concurrency slot right now.
_OCCUPYING = frozenset({TaskState.RUNNING, TaskState.GATING})


@dataclass(frozen=True, slots=True)
class SchedulerState:
    """A snapshot of where every task stands.

    Immutable on purpose: the scheduler must not be able to mutate the caller's
    state, and a snapshot can be compared, logged, and replayed.
    """

    states: Mapping[TaskId, TaskState] = field(default_factory=dict)
    attempts: Mapping[TaskId, int] = field(default_factory=dict)

    def state_of(self, task_id: TaskId) -> TaskState:
        """Return a task's state, defaulting to :attr:`TaskState.PENDING`."""
        return self.states.get(task_id, TaskState.PENDING)

    def attempts_of(self, task_id: TaskId) -> int:
        """Return how many attempts a task has already had."""
        return self.attempts.get(task_id, 0)

    @property
    def occupied_slots(self) -> int:
        """How many tasks are currently holding a concurrency slot."""
        return sum(1 for state in self.states.values() if state in _OCCUPYING)

    def running(self) -> tuple[TaskId, ...]:
        """Return the tasks currently occupying a slot, in identifier order."""
        return tuple(
            sorted(t for t, state in self.states.items() if state in _OCCUPYING)
        )

    def with_state(self, task_id: TaskId, state: TaskState) -> SchedulerState:
        """Return a copy with one task's state replaced."""
        return SchedulerState(
            states={**self.states, task_id: state}, attempts=dict(self.attempts)
        )


@dataclass(frozen=True, slots=True)
class SchedulingDecision:
    """What the scheduler concluded from one look at the state.

    Attributes:
        start: Tasks to start now, in the order they should be started.
        block: Tasks that can never run because a dependency failed terminally.
        ready: Tasks whose dependencies are satisfied but which have no slot.
        waiting: Tasks still waiting on an unfinished dependency.
        complete: Whether nothing further can happen without external input.
    """

    start: tuple[TaskId, ...] = ()
    block: tuple[TaskId, ...] = ()
    ready: tuple[TaskId, ...] = ()
    waiting: tuple[TaskId, ...] = ()
    complete: bool = False

    @property
    def has_work(self) -> bool:
        """Whether this decision asks for anything to happen."""
        return bool(self.start or self.block)


def _dependency_verdict(
    graph: TaskGraph, state: SchedulerState, task_id: TaskId
) -> str:
    """Classify a task's dependencies as ``satisfied``, ``dead``, or ``waiting``."""
    verdict = "satisfied"
    for dependency in graph.dependencies(task_id):
        dependency_state = state.state_of(dependency)
        if dependency_state in _DEAD:
            return "dead"
        if dependency_state is TaskState.FAILED:
            # Terminal only once no attempts remain; otherwise it will retry.
            if state.attempts_of(dependency) >= graph.get(dependency).max_attempts:
                return "dead"
            verdict = "waiting"
        elif dependency_state is not TaskState.SUCCEEDED:
            verdict = "waiting"
    return verdict


def next_ready(
    graph: TaskGraph,
    state: SchedulerState,
    *,
    max_concurrency: int,
    label_limits: Mapping[str, int] | None = None,
) -> SchedulingDecision:
    """Decide what to start next.

    Args:
        graph: The validated task graph.
        state: Where every task currently stands.
        max_concurrency: The global cap on simultaneously running tasks.
        label_limits: Optional per-label caps, applied on top of the global one.

    Returns:
        The decision. ``start`` is ordered by how much downstream work each task
        unblocks, then by identifier — deterministic for a given input.

    Raises:
        SchedulerError: If ``max_concurrency`` is not positive, or if the state
            refers to a task that is not in the graph.
    """
    if max_concurrency < 1:
        raise SchedulerError(
            f"max_concurrency must be at least 1, got {max_concurrency}",
            detail={"max_concurrency": max_concurrency},
        )
    unknown = sorted(str(t) for t in state.states if t not in graph)
    if unknown:
        raise SchedulerError(
            f"state refers to tasks that are not in the graph: {', '.join(unknown)}",
            detail={"unknown": unknown},
        )

    limits = dict(label_limits or {})
    label_usage: dict[str, int] = {}
    for task_id in state.running():
        for label in graph.get(task_id).labels:
            label_usage[label] = label_usage.get(label, 0) + 1

    available = max_concurrency - state.occupied_slots

    blocked: list[TaskId] = []
    candidates: list[TaskId] = []
    waiting: list[TaskId] = []

    for task_id in graph.task_ids:
        current = state.state_of(task_id)
        if current not in _STARTABLE:
            continue

        verdict = _dependency_verdict(graph, state, task_id)
        if verdict == "dead":
            blocked.append(task_id)
        elif verdict == "waiting":
            waiting.append(task_id)
        elif state.attempts_of(task_id) >= graph.get(task_id).max_attempts:
            # Ready, but out of attempts: it can never run again.
            blocked.append(task_id)
        else:
            candidates.append(task_id)

    # Highest unblock weight first, then identifier, so ordering is total.
    candidates.sort(key=lambda t: (-graph.unblock_weight(t), str(t)))

    start: list[TaskId] = []
    for task_id in candidates:
        if len(start) >= available:
            break
        labels = graph.get(task_id).labels
        if any(
            label in limits and label_usage.get(label, 0) >= limits[label]
            for label in labels
        ):
            continue
        start.append(task_id)
        for label in labels:
            label_usage[label] = label_usage.get(label, 0) + 1

    deferred = tuple(t for t in candidates if t not in set(start))

    return SchedulingDecision(
        start=tuple(start),
        block=tuple(blocked),
        ready=deferred,
        waiting=tuple(waiting),
        complete=not start
        and not blocked
        and not deferred
        and state.occupied_slots == 0,
    )


def is_complete(graph: TaskGraph, state: SchedulerState) -> bool:
    """Whether every task has reached a state from which nothing more happens.

    Args:
        graph: The task graph.
        state: Current state.

    Returns:
        ``True`` when no task is running and none could be started.
    """
    if state.occupied_slots:
        return False
    for task_id in graph.task_ids:
        current = state.state_of(task_id)
        if current.is_terminal:
            continue
        if current is TaskState.FAILED:
            if state.attempts_of(task_id) < graph.get(task_id).max_attempts:
                return False
            continue
        return False
    return True

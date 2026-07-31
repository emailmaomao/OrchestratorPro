"""The task domain model and its state machine.

A :class:`Task` is the smallest independently-executable unit of work: an
identifier, a prompt, its dependencies, and the gates its output must clear.

This module is deliberately **pure**. It performs no I/O, spawns no processes,
and knows nothing about Git worktrees, models, or the event log. That purity is
what will let the M3 scheduler be exhaustively tested as a function of graph and
state (NFR-1.1), and it is why the module sits in layer 2 with no dependency on
layer 1.

It also does not import :mod:`orchestrator.agent`, and must not: per
``docs/020_ARCHITECTURE`` §1, the task model and the agent runtime are siblings
that never reference each other. The workflow engine translates between them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from orchestrator.core.events import (
    Budget,
    DomainValidationError,
    StateTransitionError,
    TaskId,
)

__all__ = [
    "GateKind",
    "GateSpec",
    "Task",
    "TaskState",
]


class TaskState(StrEnum):
    """The lifecycle of a single task within a run.

    The transition graph, from ``ORCHESTRATOR_PRO_SPEC`` §4.1::

        pending ──▶ ready ──▶ running ──▶ gating ──▶ succeeded
                      ▲          │           │
                      │          ▼           ▼
                      └──── retrying ◀── failed ──▶ abandoned
                                           │
                                      blocked (dependency failed)

    Terminal states are :attr:`SUCCEEDED`, :attr:`BLOCKED`, and
    :attr:`ABANDONED`. :attr:`FAILED` is *not* terminal — a task with attempts
    remaining moves on to :attr:`RETRYING`.
    """

    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    GATING = "gating"
    RETRYING = "retrying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    ABANDONED = "abandoned"

    @property
    def is_terminal(self) -> bool:
        """Whether no further transition is possible from this state."""
        return self in _TERMINAL_STATES

    def can_transition_to(self, target: TaskState) -> bool:
        """Whether moving directly to ``target`` is legal.

        Args:
            target: The proposed next state.

        Returns:
            ``True`` if the transition is permitted.
        """
        return target in _ALLOWED_TRANSITIONS[self]

    def assert_transition(self, target: TaskState, *, task_id: TaskId | None = None) -> None:
        """Raise unless moving to ``target`` is legal.

        Illegal transitions raise rather than being silently coerced, per
        NFR-1.3: a state machine that quietly repairs itself hides the bug that
        drove it off the rails.

        Args:
            target: The proposed next state.
            task_id: Task being transitioned, included in the error detail.

        Raises:
            StateTransitionError: If the transition is not permitted.
        """
        if not self.can_transition_to(target):
            allowed = sorted(s.value for s in _ALLOWED_TRANSITIONS[self])
            raise StateTransitionError(
                f"cannot move task from {self.value!r} to {target.value!r}; "
                f"allowed: {allowed or ['(terminal)']}",
                detail={
                    "task_id": str(task_id) if task_id is not None else None,
                    "from": self.value,
                    "to": target.value,
                    "allowed": allowed,
                },
            )


_TERMINAL_STATES: Final = frozenset(
    {TaskState.SUCCEEDED, TaskState.BLOCKED, TaskState.ABANDONED}
)

#: The complete transition table. Every state has an entry, including terminal
#: states, which map to the empty set — an omission would read as "unconstrained".
_ALLOWED_TRANSITIONS: Final[Mapping[TaskState, frozenset[TaskState]]] = {
    TaskState.PENDING: frozenset(
        {TaskState.READY, TaskState.BLOCKED, TaskState.ABANDONED}
    ),
    TaskState.READY: frozenset({TaskState.RUNNING, TaskState.ABANDONED}),
    TaskState.RUNNING: frozenset(
        {TaskState.GATING, TaskState.FAILED, TaskState.ABANDONED}
    ),
    TaskState.GATING: frozenset({TaskState.SUCCEEDED, TaskState.FAILED}),
    TaskState.FAILED: frozenset({TaskState.RETRYING, TaskState.ABANDONED}),
    TaskState.RETRYING: frozenset({TaskState.READY, TaskState.ABANDONED}),
    TaskState.SUCCEEDED: frozenset(),
    TaskState.BLOCKED: frozenset(),
    TaskState.ABANDONED: frozenset(),
}


class GateKind(StrEnum):
    """The category of verification a gate performs."""

    TEST = "test"
    LINT = "lint"
    TYPECHECK = "typecheck"
    HUMAN = "human"


@dataclass(frozen=True, slots=True)
class GateSpec:
    """A check an attempt's work must clear before it is accepted.

    Attributes:
        name: Unique label within the task, used in reports and events.
        kind: What sort of verification this is.
        required: When ``False``, a failure is recorded but does not block
            acceptance. Advisory gates surface signal without halting a run.
        config: Kind-specific settings, resolved by the gate runner in M5.
    """

    name: str
    kind: GateKind = GateKind.TEST
    required: bool = True
    config: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise DomainValidationError("gate.name must not be empty")


@dataclass(frozen=True, slots=True)
class Task:
    """One unit of work within a run.

    A task is immutable. Its *state* lives outside it, in the run's event log
    and the scheduler's working set, so that the same task definition can be
    replayed, retried, or inspected without carrying mutable history.

    Attributes:
        id: Stable identifier, sortable by creation time.
        title: Short human-readable summary, shown in the dashboard.
        prompt: The instruction handed to an agent.
        depends_on: Tasks that must succeed before this one may start.
        gates: Checks the resulting work must clear.
        max_attempts: How many times this task may be tried before it is
            abandoned. Must be at least 1.
        budget: The per-attempt allowance.
        labels: Free-form tags, used for per-label concurrency caps and for
            routing tasks to particular roles.
    """

    id: TaskId
    title: str
    prompt: str
    budget: Budget
    depends_on: tuple[TaskId, ...] = ()
    gates: tuple[GateSpec, ...] = ()
    max_attempts: int = 3
    labels: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise DomainValidationError(
                "task.title must not be empty", detail={"task_id": str(self.id)}
            )
        if not self.prompt.strip():
            raise DomainValidationError(
                "task.prompt must not be empty", detail={"task_id": str(self.id)}
            )
        if self.max_attempts < 1:
            raise DomainValidationError(
                f"task.max_attempts must be at least 1, got {self.max_attempts}",
                detail={"task_id": str(self.id), "max_attempts": self.max_attempts},
            )
        if self.id in self.depends_on:
            raise DomainValidationError(
                "task must not depend on itself",
                detail={"task_id": str(self.id)},
            )
        if len(set(self.depends_on)) != len(self.depends_on):
            raise DomainValidationError(
                "task.depends_on must not contain duplicates",
                detail={"task_id": str(self.id)},
            )
        gate_names = [gate.name for gate in self.gates]
        if len(set(gate_names)) != len(gate_names):
            raise DomainValidationError(
                "task.gates must have unique names",
                detail={"task_id": str(self.id), "names": gate_names},
            )

    @classmethod
    def create(
        cls,
        *,
        title: str,
        prompt: str,
        budget: Budget,
        depends_on: Iterable[TaskId] = (),
        gates: Iterable[GateSpec] = (),
        max_attempts: int = 3,
        labels: Iterable[str] = (),
    ) -> Task:
        """Create a task, minting a fresh identifier.

        Args:
            title: Short human-readable summary.
            prompt: The instruction handed to an agent.
            budget: Per-attempt allowance.
            depends_on: Identifiers of prerequisite tasks.
            gates: Checks the work must clear.
            max_attempts: Maximum number of attempts.
            labels: Free-form tags.

        Returns:
            The validated task.

        Raises:
            DomainValidationError: If any field is invalid.
        """
        return cls(
            id=TaskId.generate(),
            title=title,
            prompt=prompt,
            budget=budget,
            depends_on=tuple(depends_on),
            gates=tuple(gates),
            max_attempts=max_attempts,
            labels=frozenset(labels),
        )

    @property
    def required_gates(self) -> tuple[GateSpec, ...]:
        """Gates whose failure blocks acceptance."""
        return tuple(gate for gate in self.gates if gate.required)

    @property
    def is_root(self) -> bool:
        """Whether this task has no prerequisites and may start immediately."""
        return not self.depends_on

    def has_label(self, label: str) -> bool:
        """Whether ``label`` is attached to this task."""
        return label in self.labels

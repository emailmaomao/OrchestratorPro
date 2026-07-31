"""The agent lifecycle state machine.

An attempt moves through exactly five states::

    idle ──▶ running ──▶ completed
              ▲   │
              │   ▼
         waiting_tool ──▶ failed

:attr:`AgentState.WAITING_TOOL` exists as a distinct state rather than an
internal detail of the loop because it is the one place an agent is *blocked on
us*. When a run stalls, the difference between "the model is thinking" and "a
tool has not returned" is the first thing an operator needs, and a lifecycle
that collapses them cannot answer it.

Transitions are enumerated and enforced. An illegal one raises rather than being
silently coerced (NFR-1.3): a state machine that quietly repairs itself hides
the bug that drove it off the rails.

The machine records its own history with a monotonic clock, so an attempt can
report where its wall-clock time actually went — which is usually not where the
operator assumes.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from orchestrator.core.events import OrchestratorError, StateTransitionError

__all__ = ["AgentLifecycle", "AgentState", "StateChange"]


class AgentState(StrEnum):
    """Where an agent attempt currently stands."""

    IDLE = "idle"
    RUNNING = "running"
    WAITING_TOOL = "waiting_tool"
    COMPLETED = "completed"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        """Whether no further transition is possible."""
        return self in _TERMINAL

    @property
    def is_active(self) -> bool:
        """Whether the attempt is doing work or waiting on us to do some."""
        return self in (AgentState.RUNNING, AgentState.WAITING_TOOL)

    def can_transition_to(self, target: AgentState) -> bool:
        """Whether moving directly to ``target`` is legal."""
        return target in _ALLOWED[self]


_TERMINAL: Final = frozenset({AgentState.COMPLETED, AgentState.FAILED})

#: The complete transition table. Terminal states map to the empty set rather
#: than being omitted — an omission would read as "unconstrained".
_ALLOWED: Final[Mapping[AgentState, frozenset[AgentState]]] = {
    AgentState.IDLE: frozenset({AgentState.RUNNING, AgentState.FAILED}),
    AgentState.RUNNING: frozenset(
        {AgentState.WAITING_TOOL, AgentState.COMPLETED, AgentState.FAILED}
    ),
    AgentState.WAITING_TOOL: frozenset(
        {AgentState.RUNNING, AgentState.COMPLETED, AgentState.FAILED}
    ),
    AgentState.COMPLETED: frozenset(),
    AgentState.FAILED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class StateChange:
    """One recorded transition."""

    source: AgentState
    target: AgentState
    at: float
    reason: str = ""

    def __str__(self) -> str:
        arrow = f"{self.source.value} -> {self.target.value}"
        return f"{arrow} ({self.reason})" if self.reason else arrow


class AgentLifecycle:
    """Tracks and enforces one attempt's state transitions."""

    __slots__ = ("_clock", "_entered_at", "_history", "_observer", "_state", "_time_in")

    def __init__(
        self,
        *,
        clock: Callable[[], float] | None = None,
        observer: Callable[[StateChange], None] | None = None,
    ) -> None:
        """Create a lifecycle in :attr:`AgentState.IDLE`.

        Args:
            clock: Monotonic time source. Injected so the recorded durations are
                testable without sleeping.
            observer: Called after each accepted transition, for event emission.
        """
        self._clock = clock or time.monotonic
        self._observer = observer
        self._state = AgentState.IDLE
        self._history: list[StateChange] = []
        self._entered_at = self._clock()
        self._time_in: dict[AgentState, float] = {}

    @property
    def state(self) -> AgentState:
        """The current state."""
        return self._state

    @property
    def history(self) -> tuple[StateChange, ...]:
        """Every transition so far, in order."""
        return tuple(self._history)

    @property
    def is_terminal(self) -> bool:
        """Whether the attempt has finished."""
        return self._state.is_terminal

    def time_in(self, state: AgentState) -> float:
        """Return the seconds spent in ``state``, including the current stay."""
        elapsed = self._time_in.get(state, 0.0)
        if state is self._state:
            elapsed += self._clock() - self._entered_at
        return elapsed

    def breakdown(self) -> Mapping[str, float]:
        """Return the time spent in each state, for reporting.

        Usually the surprising number here is :attr:`AgentState.WAITING_TOOL`.
        """
        states = {*self._time_in, self._state}
        return {state.value: self.time_in(state) for state in states}

    def can(self, target: AgentState) -> bool:
        """Whether the machine may move to ``target`` right now."""
        return self._state.can_transition_to(target)

    def transition_to(self, target: AgentState, *, reason: str = "") -> StateChange:
        """Move to ``target``.

        Args:
            target: The state to move to.
            reason: Why, recorded on the change and surfaced in errors.

        Returns:
            The recorded change.

        Raises:
            StateTransitionError: If the transition is not permitted.
        """
        if not self._state.can_transition_to(target):
            allowed = sorted(s.value for s in _ALLOWED[self._state])
            raise StateTransitionError(
                f"an agent cannot move from {self._state.value!r} to "
                f"{target.value!r}; allowed: {allowed or ['(terminal)']}",
                detail={
                    "from": self._state.value,
                    "to": target.value,
                    "allowed": allowed,
                    "reason": reason,
                },
            )

        now = self._clock()
        self._time_in[self._state] = (
            self._time_in.get(self._state, 0.0) + now - self._entered_at
        )
        change = StateChange(source=self._state, target=target, at=now, reason=reason)
        self._state = target
        self._entered_at = now
        self._history.append(change)

        if self._observer is not None:
            self._observer(change)
        return change

    # ------------------------------------------------------- named shortcuts

    def start(self, *, reason: str = "attempt started") -> StateChange:
        """Move from idle to running."""
        return self.transition_to(AgentState.RUNNING, reason=reason)

    def await_tools(self, *, reason: str = "dispatching tool calls") -> StateChange:
        """Move to waiting-on-tools."""
        return self.transition_to(AgentState.WAITING_TOOL, reason=reason)

    def resume(self, *, reason: str = "tool results returned") -> StateChange:
        """Return to running after tools have answered."""
        return self.transition_to(AgentState.RUNNING, reason=reason)

    def complete(self, *, reason: str = "attempt finished") -> StateChange:
        """Finish successfully."""
        return self.transition_to(AgentState.COMPLETED, reason=reason)

    def fail(self, reason: str) -> StateChange:
        """Finish unsuccessfully.

        Args:
            reason: Why the attempt failed. Required — a failure with no stated
                cause is not worth recording.

        Raises:
            OrchestratorError: If ``reason`` is blank.
        """
        if not reason.strip():
            raise OrchestratorError("a failure must state its reason")
        return self.transition_to(AgentState.FAILED, reason=reason)

    def fail_quietly(self, reason: str) -> StateChange | None:
        """Fail if the machine is still active, otherwise do nothing.

        For cleanup paths that must not raise over an attempt which already
        reached a terminal state by another route.
        """
        if self._state.is_terminal:
            return None
        return self.fail(reason)

    def path(self) -> Sequence[str]:
        """Return the states visited, as a readable trail."""
        if not self._history:
            return (self._state.value,)
        return (
            self._history[0].source.value,
            *(change.target.value for change in self._history),
        )


@dataclass(frozen=True, slots=True)
class LifecycleSummary:
    """A serializable snapshot of one attempt's lifecycle."""

    state: AgentState
    path: tuple[str, ...]
    breakdown: Mapping[str, float] = field(default_factory=dict)

    @classmethod
    def of(cls, lifecycle: AgentLifecycle) -> LifecycleSummary:
        """Capture a lifecycle's current shape."""
        return cls(
            state=lifecycle.state,
            path=tuple(lifecycle.path()),
            breakdown=dict(lifecycle.breakdown()),
        )

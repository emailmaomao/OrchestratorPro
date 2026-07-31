"""Progress tracking and event emission.

Two things an operator needs from a long run, and one thing the system needs
from itself:

:class:`ProgressTracker`
    What fraction is done, what is running right now, and what is stuck. Derived
    from task states rather than counted separately, so it cannot drift from
    reality.
:class:`EventEmitter`
    Turns dispatcher and engine activity into :class:`~orchestrator.core.events.Event`
    objects and hands them to a sink. The sink decides what durability means —
    an in-memory list in a test, the M2 :class:`~orchestrator.core.run_store.RunStore`
    in production.

**Emission is best-effort by design.** A sink that raises must not take a run
down with it; observability is not worth losing work over. Failures are counted
and reported, so a silently broken sink is still visible.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from orchestrator.core.events import (
    AttemptId,
    Event,
    EventType,
    RunId,
    TaskId,
)
from orchestrator.task.model import TaskState

__all__ = [
    "EventEmitter",
    "ProgressSnapshot",
    "ProgressTracker",
    "StepProgress",
]

#: States that mean a task will make no further progress.
_TERMINAL = frozenset(
    {TaskState.SUCCEEDED, TaskState.BLOCKED, TaskState.ABANDONED}
)

#: States that mean a task is occupying a slot right now.
_ACTIVE = frozenset({TaskState.RUNNING, TaskState.GATING})


@dataclass(frozen=True, slots=True)
class StepProgress:
    """One step's progress, keyed by the name an operator recognizes."""

    name: str
    state: TaskState
    attempts: int = 0
    max_attempts: int = 1

    @property
    def is_done(self) -> bool:
        """Whether the step has reached a terminal state."""
        return self.state in _TERMINAL

    @property
    def is_active(self) -> bool:
        """Whether the step is running right now."""
        return self.state in _ACTIVE


@dataclass(frozen=True, slots=True)
class ProgressSnapshot:
    """The state of a whole run at one instant."""

    total: int = 0
    succeeded: int = 0
    failed: int = 0
    blocked: int = 0
    running: int = 0
    pending: int = 0
    attempts: int = 0
    steps: tuple[StepProgress, ...] = ()

    @property
    def finished(self) -> int:
        """How many steps have reached a terminal state."""
        return self.succeeded + self.failed + self.blocked

    @property
    def percent(self) -> float:
        """Fraction finished, as a percentage. Zero steps is zero percent."""
        return 0.0 if not self.total else 100.0 * self.finished / self.total

    @property
    def complete(self) -> bool:
        """Whether every step has finished."""
        return self.total > 0 and self.finished == self.total

    @property
    def healthy(self) -> bool:
        """Whether nothing has failed or been blocked so far."""
        return self.failed == 0 and self.blocked == 0

    def active_names(self) -> tuple[str, ...]:
        """The steps running right now, in name order."""
        return tuple(sorted(step.name for step in self.steps if step.is_active))

    def summary(self) -> str:
        """A one-line account, for a log line or a status command."""
        parts = [f"{self.finished}/{self.total} steps ({self.percent:.0f}%)"]
        if self.running:
            parts.append(f"{self.running} running")
        if self.failed:
            parts.append(f"{self.failed} failed")
        if self.blocked:
            parts.append(f"{self.blocked} blocked")
        return ", ".join(parts)


class ProgressTracker:
    """Derives run progress from task states.

    Holds no counters of its own: every number is computed from the states it is
    given, so the report cannot drift out of step with the run it describes.
    """

    __slots__ = ("_attempts", "_names", "_observers", "_states", "_max_attempts")

    def __init__(
        self,
        names_by_id: Mapping[TaskId, str],
        *,
        max_attempts: Mapping[TaskId, int] | None = None,
    ) -> None:
        """Create the tracker.

        Args:
            names_by_id: Task identifier to the step name an operator reads.
            max_attempts: Per-task attempt allowance, for reporting.
        """
        self._names = dict(names_by_id)
        self._max_attempts = dict(max_attempts or {})
        self._states: dict[TaskId, TaskState] = dict.fromkeys(
            self._names, TaskState.PENDING
        )
        self._attempts: dict[TaskId, int] = dict.fromkeys(self._names, 0)
        self._observers: list[Callable[[ProgressSnapshot], None]] = []

    def subscribe(self, observer: Callable[[ProgressSnapshot], None]) -> None:
        """Register a callback invoked after every update."""
        self._observers.append(observer)

    def update(
        self,
        states: Mapping[TaskId, TaskState],
        attempts: Mapping[TaskId, int] | None = None,
    ) -> ProgressSnapshot:
        """Record the latest states and return the resulting snapshot."""
        self._states.update(states)
        if attempts:
            self._attempts.update(attempts)
        snapshot = self.snapshot()
        for observer in self._observers:
            observer(snapshot)
        return snapshot

    def snapshot(self) -> ProgressSnapshot:
        """Compute the current snapshot."""
        steps = tuple(
            StepProgress(
                name=self._names.get(task_id, str(task_id)),
                state=state,
                attempts=self._attempts.get(task_id, 0),
                max_attempts=self._max_attempts.get(task_id, 1),
            )
            for task_id, state in sorted(
                self._states.items(), key=lambda pair: self._names.get(pair[0], "")
            )
        )
        return ProgressSnapshot(
            total=len(steps),
            succeeded=sum(1 for s in steps if s.state is TaskState.SUCCEEDED),
            failed=sum(
                1 for s in steps if s.state in (TaskState.FAILED, TaskState.ABANDONED)
            ),
            blocked=sum(1 for s in steps if s.state is TaskState.BLOCKED),
            running=sum(1 for s in steps if s.is_active),
            pending=sum(
                1
                for s in steps
                if s.state in (TaskState.PENDING, TaskState.READY, TaskState.RETRYING)
            ),
            attempts=sum(s.attempts for s in steps),
            steps=steps,
        )


class EventEmitter:
    """Emits run events to a sink, without letting the sink break the run."""

    __slots__ = ("_count", "_failures", "_open_attempts", "_run_id", "_sink")

    def __init__(
        self, run_id: RunId, sink: Callable[[Event], None] | None = None
    ) -> None:
        """Create the emitter.

        Args:
            run_id: Stamped onto every event.
            sink: Receives each event. ``None`` discards them, which is a valid
                configuration for a run nobody is watching.
        """
        self._run_id = run_id
        self._sink = sink
        self._count = 0
        self._failures = 0
        # The open attempt per task, learned from the dispatcher's own
        # attempt.started / attempt.finished events as they pass through. The
        # step executor knows its attempt *number* but not the AttemptId the
        # dispatcher minted; this is how a gate verdict is attributed to the
        # right attempt without widening the executor protocol.
        self._open_attempts: dict[TaskId, AttemptId] = {}

    @property
    def run_id(self) -> RunId:
        """The run these events belong to."""
        return self._run_id

    @property
    def emitted(self) -> int:
        """How many events were accepted by the sink."""
        return self._count

    @property
    def failures(self) -> int:
        """How many events the sink rejected.

        Non-zero means observability is degraded — worth reporting, not worth
        failing a run over.
        """
        return self._failures

    def emit(self, event: Event) -> bool:
        """Send one event to the sink.

        Returns:
            Whether the sink accepted it.
        """
        if event.task_id is not None:
            if event.type is EventType.ATTEMPT_STARTED and event.attempt_id is not None:
                self._open_attempts[event.task_id] = event.attempt_id
            elif event.type is EventType.ATTEMPT_FINISHED:
                self._open_attempts.pop(event.task_id, None)
        if self._sink is None:
            return False
        try:
            self._sink(event)
        except Exception:  # noqa: BLE001 - telemetry must not break the run
            self._failures += 1
            return False
        self._count += 1
        return True

    def emit_all(self, events: Iterable[Event]) -> int:
        """Send several events, returning how many were accepted."""
        return sum(1 for event in events if self.emit(event))

    def record(
        self,
        event_type: EventType,
        *,
        task_id: TaskId | None = None,
        attempt_id: AttemptId | None = None,
        **payload: object,
    ) -> Event:
        """Build an event, emit it, and return it."""
        event = Event.new(
            event_type,
            run_id=self._run_id,
            task_id=task_id,
            attempt_id=attempt_id,
            payload=payload,
        )
        self.emit(event)
        return event

    # ------------------------------------------------------- run lifecycle

    def run_created(self, *, goal: str, repo_path: str, workflow: str) -> Event:
        """Record the run's creation, carrying what replay will need."""
        return self.record(
            EventType.RUN_CREATED, goal=goal, repo_path=repo_path, workflow=workflow
        )

    def task_created(
        self,
        task_id: TaskId,
        *,
        step: str,
        title: str,
        prompt: str,
        depends_on: Sequence[TaskId] = (),
        max_attempts: int = 1,
    ) -> Event:
        """Declare a task's existence.

        The step name travels in the payload: recovery needs to map persisted
        identifiers back onto a definition, and only the name is stable across
        runs.
        """
        return self.record(
            EventType.TASK_CREATED,
            task_id=task_id,
            step=step,
            title=title,
            prompt=prompt,
            depends_on=[str(d) for d in depends_on],
            max_attempts=max_attempts,
        )

    def run_started(self) -> Event:
        """Record that execution began."""
        return self.record(EventType.RUN_STARTED)

    def run_finished(self, *, outcome: str) -> Event:
        """Record that the run reached a terminal state."""
        return self.record(EventType.RUN_FINISHED, outcome=outcome)

    def run_cancelled(self, *, reason: str) -> Event:
        """Record that the run was cancelled."""
        return self.record(EventType.RUN_CANCELLED, reason=reason)

    def gate_evaluated(
        self,
        task_id: TaskId,
        attempt_id: AttemptId | None,
        *,
        gate: str,
        verdict: str,
        **detail: object,
    ) -> Event:
        """Record a gate's verdict, attributed to an attempt.

        The projection *requires* an attempt id on this event — a verdict that
        belongs to no attempt is structurally impossible, and replay refuses it
        rather than guessing. So ``None`` here resolves to the task's open
        attempt (learned from the dispatcher's events passing through this same
        emitter), and only an emitter operating outside any dispatcher — a
        bare unit test — falls back to minting one. Emitting ``None`` outright
        would poison the log: the append succeeds, and every later replay of
        the run raises.
        """
        resolved = (
            attempt_id
            or self._open_attempts.get(task_id)
            or AttemptId.generate()
        )
        return self.record(
            EventType.GATE_EVALUATED,
            task_id=task_id,
            attempt_id=resolved,
            gate=gate,
            verdict=verdict,
            detail=dict(detail),
        )

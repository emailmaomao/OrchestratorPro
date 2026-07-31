"""Recovery: resuming a run from what was persisted.

The event log is authoritative (``ORCHESTRATOR_PRO_SPEC`` §5), so recovery is
replay, not guesswork. This module turns a replayed
:class:`~orchestrator.core.records.RunState` into a :class:`RecoveryPlan`: the
identifiers behind each step name, the state each task reached, and how many
attempts each had.

Two properties make it work:

* **Step names are in the log.** ``task.created`` carries the workflow's own
  name for the task, so a definition can be bound to identifiers that were
  minted by an earlier process (FR-5.5).
* **Drift is reported, not assumed away.** :meth:`WorkflowRecovery.reconcile`
  compares the plan against what is actually on disk. A worktree that vanished
  while the log says its task succeeded is exactly the kind of inconsistency
  that produces a confidently wrong resume, so it is surfaced (FR-5.6).

A task that was mid-flight when the process died is reset to pending: its
attempt cannot be resumed — the agent's context is gone — but the task itself
can be tried again, and its earlier attempt is still in the log.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.core.events import OrchestratorError, RunId, TaskId
from orchestrator.core.records import RunState, RunStatus
from orchestrator.core.run_store import RunStore
from orchestrator.task.model import TaskState
from orchestrator.workflow.definition import WorkflowDefinition, WorkflowPlan

__all__ = [
    "ReconcileReport",
    "RecoveryError",
    "RecoveryPlan",
    "WorkflowRecovery",
]

#: States that cannot survive a process restart: whatever was in flight is gone.
_INTERRUPTED = frozenset(
    {TaskState.RUNNING, TaskState.GATING, TaskState.READY, TaskState.RETRYING}
)

#: States a task reaches by failing on its own terms rather than being cut off.
_SPENT = frozenset({TaskState.FAILED, TaskState.ABANDONED})


class RecoveryError(OrchestratorError):
    """A run could not be recovered from its persisted state."""

    code = "workflow_recovery"
    retryable = False


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    """What a resumed run should start from."""

    run_id: RunId
    goal: str
    workflow: str
    ids_by_name: Mapping[str, TaskId]
    states: Mapping[TaskId, TaskState]
    attempts: Mapping[TaskId, int]
    interrupted: tuple[TaskId, ...] = ()
    already_finished: bool = False

    @property
    def completed_steps(self) -> tuple[str, ...]:
        """Step names that already succeeded, in name order."""
        names = {task_id: name for name, task_id in self.ids_by_name.items()}
        return tuple(
            sorted(
                names[task_id]
                for task_id, state in self.states.items()
                if state is TaskState.SUCCEEDED and task_id in names
            )
        )

    @property
    def remaining_steps(self) -> tuple[str, ...]:
        """Step names still to do, in name order."""
        done = set(self.completed_steps)
        return tuple(sorted(name for name in self.ids_by_name if name not in done))

    @property
    def is_resumable(self) -> bool:
        """Whether there is anything left to do."""
        return not self.already_finished and bool(self.remaining_steps)

    def summary(self) -> str:
        """A one-line account, for a log line or an operator prompt."""
        if self.already_finished:
            return f"run {self.run_id} already finished; nothing to resume"
        return (
            f"run {self.run_id}: {len(self.completed_steps)} step(s) already done, "
            f"{len(self.remaining_steps)} remaining, "
            f"{len(self.interrupted)} interrupted mid-flight"
        )


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """Differences between a recovery plan and what is on disk."""

    missing_workspaces: tuple[str, ...] = ()
    unexpected_workspaces: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def consistent(self) -> bool:
        """Whether the persisted state and the filesystem agree."""
        return not (self.missing_workspaces or self.unexpected_workspaces)

    def describe(self) -> str:
        """A human-readable account of any drift."""
        if self.consistent:
            return "persisted state and the filesystem agree"
        parts: list[str] = []
        if self.missing_workspaces:
            parts.append(
                f"{len(self.missing_workspaces)} expected worktree(s) are gone"
            )
        if self.unexpected_workspaces:
            parts.append(
                f"{len(self.unexpected_workspaces)} worktree(s) are not in the log"
            )
        return "; ".join(parts)


class WorkflowRecovery:
    """Builds resume plans from persisted state."""

    __slots__ = ("_store",)

    def __init__(self, store: RunStore) -> None:
        """Bind recovery to a run store."""
        self._store = store

    @property
    def store(self) -> RunStore:
        """The underlying store."""
        return self._store

    def plan(self, run_id: RunId, *, retry_failed: bool = False) -> RecoveryPlan:
        """Replay a run's log and work out what remains.

        Args:
            run_id: The run to recover.
            retry_failed: Also put tasks that ran out of attempts back into play.
                Off by default: a task that failed on its own terms failed for a
                reason, and retrying it automatically would loop.

        Returns:
            The plan.

        Raises:
            RecoveryError: If the log holds no record of the run.
        """
        state = self._store.replay(run_id)
        if state.event_count == 0:
            raise RecoveryError(
                f"there is no event log for run {run_id}; nothing to recover",
                detail={"run_id": str(run_id)},
            )
        return self.plan_from(state, retry_failed=retry_failed)

    def plan_from(
        self, state: RunState, *, retry_failed: bool = False
    ) -> RecoveryPlan:
        """Build a plan from an already-replayed state."""
        ids_by_name: dict[str, TaskId] = {}
        states: dict[TaskId, TaskState] = {}
        attempts: dict[TaskId, int] = {}
        interrupted: list[TaskId] = []
        open_attempts = _unfinished_attempts(state)

        for task_id, task in state.tasks.items():
            name = self._step_name(task_id, title=task.title)
            ids_by_name[name] = task_id
            # An attempt that never reached a finish event produced no outcome,
            # so it did not consume the task's allowance. Charging for it would
            # make a single-attempt step unresumable after any crash.
            attempts[task_id] = max(
                0, task.attempts_made - open_attempts.get(task_id, 0)
            )

            recorded = _as_task_state(task.state)
            if recorded in _INTERRUPTED:
                # The attempt's context died with the process. The task can be
                # tried again; the attempt that was running cannot be resumed.
                interrupted.append(task_id)
                states[task_id] = TaskState.PENDING
            elif retry_failed and recorded in _SPENT:
                # The operator asked for another try, which means a fresh
                # allowance: putting it back as PENDING with no attempts left
                # would abandon it again before it ran.
                states[task_id] = TaskState.PENDING
                attempts[task_id] = 0
            else:
                states[task_id] = recorded

        return RecoveryPlan(
            run_id=state.run_id,
            goal=state.goal,
            workflow=state.repo_path,
            ids_by_name=ids_by_name,
            states=states,
            attempts=attempts,
            interrupted=tuple(interrupted),
            already_finished=state.status is RunStatus.FINISHED,
        )

    @staticmethod
    def _step_name(task_id: TaskId, *, title: str) -> str:
        """Recover a task's workflow step name.

        The engine writes the step's **name** into the ``task.created`` event's
        ``title`` field precisely so this is exact — the display title travels
        separately. A log with no title at all falls back to the identifier,
        which will not bind to a definition, and :meth:`bind` says so plainly
        rather than guessing.
        """
        return title or str(task_id)

    def bind(self, definition: WorkflowDefinition, plan: RecoveryPlan) -> WorkflowPlan:
        """Bind a definition onto a recovered run's identifiers.

        Args:
            definition: The workflow being resumed. Must describe the same steps
                the run was started with.
            plan: The recovery plan.

        Returns:
            A compiled plan using the persisted identifiers.

        Raises:
            RecoveryError: If the definition and the persisted run disagree
                about which steps exist. Resuming a changed workflow onto an old
                run would silently mis-attribute completed work.
        """
        expected = set(definition.step_names())
        persisted = set(plan.ids_by_name)

        added = sorted(expected - persisted)
        removed = sorted(persisted - expected)
        if added or removed:
            raise RecoveryError(
                "the workflow definition no longer matches the persisted run: "
                f"added {added or '[]'}, removed {removed or '[]'}. Start a new "
                "run rather than resuming a changed workflow.",
                detail={"added": added, "removed": removed, "run_id": str(plan.run_id)},
            )
        return definition.bind(plan.ids_by_name)

    def reconcile(
        self,
        plan: RecoveryPlan,
        *,
        expected_workspaces: Mapping[str, Path] | None = None,
        found_workspaces: Sequence[Path] = (),
    ) -> ReconcileReport:
        """Compare a plan against what is actually on disk (FR-5.6).

        Args:
            plan: The recovery plan.
            expected_workspaces: Step name to the worktree the log implies.
            found_workspaces: Worktrees actually present.

        Returns:
            A report naming any drift. Drift is surfaced rather than repaired:
            deleting a worktree the operator was mid-way through inspecting
            would be worse than telling them about it.
        """
        expected = dict(expected_workspaces or {})
        found = {path.resolve() for path in found_workspaces}

        missing = tuple(
            sorted(name for name, path in expected.items() if path.resolve() not in found)
        )
        unexpected = tuple(
            sorted(
                str(path)
                for path in found
                if path not in {p.resolve() for p in expected.values()}
            )
        )

        notes: list[str] = []
        if plan.interrupted:
            notes.append(
                f"{len(plan.interrupted)} task(s) were mid-flight when the run "
                "stopped and will be attempted again"
            )
        if missing:
            notes.append(
                "a worktree named in the log is gone; that task's partial work "
                "cannot be inspected"
            )

        return ReconcileReport(
            missing_workspaces=missing,
            unexpected_workspaces=unexpected,
            notes=tuple(notes),
        )


def _unfinished_attempts(state: RunState) -> dict[TaskId, int]:
    """Count each task's attempts that were opened but never closed."""
    open_by_task: dict[TaskId, int] = {}
    for attempt in state.attempts.values():
        if attempt.finished_at is None:
            open_by_task[attempt.task_id] = open_by_task.get(attempt.task_id, 0) + 1
    return open_by_task


def _as_task_state(value: str) -> TaskState:
    """Convert a persisted state string back into a :class:`TaskState`.

    An unrecognized value means the log was written by a build that knew a state
    this one does not. Treating it as pending is the conservative reading: the
    task gets attempted again rather than being assumed complete.
    """
    try:
        return TaskState(value)
    except ValueError:
        return TaskState.PENDING

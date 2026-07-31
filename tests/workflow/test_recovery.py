"""Tests for recovering a run from persisted state."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.core.events import AttemptId, Event, EventType, RunId, TaskId
from orchestrator.core.records import RunState, RunStatus, TaskProjection
from orchestrator.core.run_store import RunStore
from orchestrator.task.model import TaskState
from orchestrator.workflow.progress import EventEmitter
from orchestrator.workflow.recovery import (
    ReconcileReport,
    RecoveryError,
    RecoveryPlan,
    WorkflowRecovery,
)

from tests.workflow.conftest import step, workflow


def persist(store: RunStore, *, states: dict[str, TaskState], finished: bool = False) -> RunId:
    """Write a run's log so it can be replayed, and return its identifier."""
    run_id = RunId.generate()
    emitter = EventEmitter(run_id, store.record)
    emitter.run_created(goal="a goal", repo_path="/repo", workflow="wf")

    ids = {name: TaskId.generate() for name in states}
    for name, task_id in ids.items():
        emitter.task_created(task_id, step=name, title=name, prompt=f"do {name}")
    emitter.run_started()

    for name, state in states.items():
        _advance(store, run_id, ids[name], state)

    if finished:
        emitter.run_finished(outcome="succeeded")
    return run_id


def _advance(store: RunStore, run_id: RunId, task_id: TaskId, state: TaskState) -> None:
    """Drive one task to a given state through the events that reach it."""
    if state is TaskState.PENDING:
        return

    store.record(Event.new(EventType.TASK_READY, run_id=run_id, task_id=task_id))
    store.record(
        Event.new(
            EventType.TASK_STARTED, run_id=run_id, task_id=task_id, payload={"attempt": 1}
        )
    )
    attempt_id = AttemptId.generate()
    store.record(
        Event.new(
            EventType.ATTEMPT_STARTED,
            run_id=run_id,
            task_id=task_id,
            attempt_id=attempt_id,
            payload={"number": 1},
        )
    )
    if state is not TaskState.RUNNING:
        store.record(
            Event.new(
                EventType.ATTEMPT_FINISHED,
                run_id=run_id,
                task_id=task_id,
                attempt_id=attempt_id,
                payload={"status": state.value},
            )
        )

    terminal = {
        TaskState.SUCCEEDED: EventType.TASK_SUCCEEDED,
        TaskState.FAILED: EventType.TASK_FAILED,
        TaskState.ABANDONED: EventType.TASK_ABANDONED,
        TaskState.BLOCKED: EventType.TASK_BLOCKED,
    }.get(state)
    if terminal is not None:
        store.record(Event.new(terminal, run_id=run_id, task_id=task_id))


class TestPlan:
    """A plan is replayed from the log, never guessed."""

    def test_an_unknown_run_is_refused(self, store: RunStore) -> None:
        with pytest.raises(RecoveryError, match="no event log"):
            WorkflowRecovery(store).plan(RunId.generate())

    def test_completed_and_remaining_are_separated(self, store: RunStore) -> None:
        run_id = persist(
            store, states={"a": TaskState.SUCCEEDED, "b": TaskState.PENDING}
        )
        plan = WorkflowRecovery(store).plan(run_id)

        assert plan.completed_steps == ("a",)
        assert plan.remaining_steps == ("b",)
        assert plan.is_resumable

    def test_the_goal_survives_replay(self, store: RunStore) -> None:
        run_id = persist(store, states={"a": TaskState.PENDING})
        assert WorkflowRecovery(store).plan(run_id).goal == "a goal"

    def test_identifiers_are_recovered_by_step_name(self, store: RunStore) -> None:
        run_id = persist(store, states={"alpha": TaskState.PENDING})
        plan = WorkflowRecovery(store).plan(run_id)
        assert set(plan.ids_by_name) == {"alpha"}

    def test_attempt_counts_survive(self, store: RunStore) -> None:
        run_id = persist(store, states={"a": TaskState.SUCCEEDED})
        plan = WorkflowRecovery(store).plan(run_id)
        assert list(plan.attempts.values()) == [1]

    def test_an_in_flight_task_is_reset_to_pending(self, store: RunStore) -> None:
        """The attempt's context died with the process; the task can be retried."""
        run_id = persist(store, states={"a": TaskState.RUNNING})
        plan = WorkflowRecovery(store).plan(run_id)

        assert plan.states[plan.ids_by_name["a"]] is TaskState.PENDING
        assert plan.interrupted == (plan.ids_by_name["a"],)

    def test_a_failed_task_keeps_its_state(self, store: RunStore) -> None:
        run_id = persist(store, states={"a": TaskState.FAILED})
        plan = WorkflowRecovery(store).plan(run_id)
        assert plan.states[plan.ids_by_name["a"]] is not TaskState.SUCCEEDED

    def test_a_finished_run_is_not_resumable(self, store: RunStore) -> None:
        run_id = persist(store, states={"a": TaskState.SUCCEEDED}, finished=True)
        plan = WorkflowRecovery(store).plan(run_id)

        assert plan.already_finished
        assert not plan.is_resumable
        assert "nothing to resume" in plan.summary()

    def test_a_fully_done_run_is_not_resumable_either(self, store: RunStore) -> None:
        run_id = persist(store, states={"a": TaskState.SUCCEEDED})
        assert not WorkflowRecovery(store).plan(run_id).is_resumable

    def test_the_summary_counts_both_sides(self, store: RunStore) -> None:
        run_id = persist(
            store, states={"a": TaskState.SUCCEEDED, "b": TaskState.PENDING}
        )
        summary = WorkflowRecovery(store).plan(run_id).summary()
        assert "1 step(s) already done" in summary
        assert "1 remaining" in summary

    def test_an_unrecognized_state_is_read_conservatively(self) -> None:
        """A log from a newer build must not read as 'already done'."""
        run_id = RunId.generate()
        task_id = TaskId.generate()
        state = RunState(
            run_id=run_id,
            goal="g",
            repo_path="/r",
            status=RunStatus.RUNNING,
            tasks={
                task_id: TaskProjection(
                    id=task_id, run_id=run_id, title="a", state="quantum"
                )
            },
            event_count=3,
        )
        plan = WorkflowRecovery(None).plan_from(state)  # type: ignore[arg-type]
        assert plan.states[task_id] is TaskState.PENDING


class TestBind:
    """A definition is only bound onto a run it actually describes."""

    def test_binding_reuses_the_persisted_identifiers(self, store: RunStore) -> None:
        run_id = persist(
            store, states={"a": TaskState.SUCCEEDED, "b": TaskState.PENDING}
        )
        recovery = WorkflowRecovery(store)
        recovered = recovery.plan(run_id)
        definition = workflow(step("a"), step("b", depends_on=["a"]))

        bound = recovery.bind(definition, recovered)

        assert bound.task_id("a") == recovered.ids_by_name["a"]
        assert bound.graph.dependencies(bound.task_id("b")) == (bound.task_id("a"),)

    def test_an_added_step_is_refused(self, store: RunStore) -> None:
        """Resuming a changed workflow would mis-attribute completed work."""
        run_id = persist(store, states={"a": TaskState.SUCCEEDED})
        recovery = WorkflowRecovery(store)
        recovered = recovery.plan(run_id)

        with pytest.raises(RecoveryError, match="no longer matches"):
            recovery.bind(workflow(step("a"), step("c")), recovered)

    def test_a_removed_step_is_refused(self, store: RunStore) -> None:
        run_id = persist(
            store, states={"a": TaskState.SUCCEEDED, "b": TaskState.PENDING}
        )
        recovery = WorkflowRecovery(store)
        recovered = recovery.plan(run_id)

        with pytest.raises(RecoveryError, match="removed"):
            recovery.bind(workflow(step("a")), recovered)

    def test_the_error_names_the_difference(self, store: RunStore) -> None:
        run_id = persist(store, states={"a": TaskState.PENDING})
        recovery = WorkflowRecovery(store)
        recovered = recovery.plan(run_id)

        with pytest.raises(RecoveryError) as caught:
            recovery.bind(workflow(step("b")), recovered)

        assert caught.value.detail["added"] == ["b"]
        assert caught.value.detail["removed"] == ["a"]


class TestReconcile:
    """Drift between the log and the disk is reported, not repaired."""

    def _plan(self) -> RecoveryPlan:
        return RecoveryPlan(
            run_id=RunId.generate(),
            goal="g",
            workflow="wf",
            ids_by_name={},
            states={},
            attempts={},
        )

    def test_agreement_is_reported_as_consistent(self, tmp_dir: Path) -> None:
        report = WorkflowRecovery(None).reconcile(  # type: ignore[arg-type]
            self._plan(),
            expected_workspaces={"a": tmp_dir},
            found_workspaces=[tmp_dir],
        )
        assert report.consistent
        assert "agree" in report.describe()

    def test_a_vanished_worktree_is_named(self, tmp_dir: Path) -> None:
        report = WorkflowRecovery(None).reconcile(  # type: ignore[arg-type]
            self._plan(),
            expected_workspaces={"a": tmp_dir / "gone"},
            found_workspaces=[],
        )
        assert report.missing_workspaces == ("a",)
        assert not report.consistent
        assert "gone" in report.describe()

    def test_an_unexpected_worktree_is_named(self, tmp_dir: Path) -> None:
        stray = tmp_dir / "stray"
        stray.mkdir()
        report = WorkflowRecovery(None).reconcile(  # type: ignore[arg-type]
            self._plan(), expected_workspaces={}, found_workspaces=[stray]
        )
        assert len(report.unexpected_workspaces) == 1
        assert not report.consistent

    def test_interrupted_tasks_are_noted(self) -> None:
        plan = RecoveryPlan(
            run_id=RunId.generate(),
            goal="g",
            workflow="wf",
            ids_by_name={},
            states={},
            attempts={},
            interrupted=(TaskId.generate(),),
        )
        report = WorkflowRecovery(None).reconcile(plan)  # type: ignore[arg-type]
        assert any("mid-flight" in note for note in report.notes)

    def test_nothing_expected_and_nothing_found_is_consistent(self) -> None:
        report = WorkflowRecovery(None).reconcile(self._plan())  # type: ignore[arg-type]
        assert report.consistent
        assert report.notes == ()

    def test_an_empty_report_is_consistent(self) -> None:
        assert ReconcileReport().consistent

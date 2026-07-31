"""Tests for progress tracking and event emission."""

from __future__ import annotations

import pytest

from orchestrator.core.events import Event, EventType, RunId, TaskId
from orchestrator.task.model import TaskState
from orchestrator.workflow.progress import EventEmitter, ProgressTracker


@pytest.fixture
def ids() -> dict[TaskId, str]:
    """Three tasks with readable names."""
    return {TaskId.generate(): name for name in ("alpha", "beta", "gamma")}


class TestProgressTracker:
    """Every number is derived, so it cannot drift from reality."""

    def test_a_fresh_run_is_all_pending(self, ids: dict[TaskId, str]) -> None:
        snapshot = ProgressTracker(ids).snapshot()
        assert snapshot.total == 3
        assert snapshot.pending == 3
        assert snapshot.percent == 0.0
        assert not snapshot.complete

    def test_progress_advances_with_state(self, ids: dict[TaskId, str]) -> None:
        tracker = ProgressTracker(ids)
        first = next(iter(ids))
        snapshot = tracker.update({first: TaskState.SUCCEEDED})

        assert snapshot.succeeded == 1
        assert snapshot.percent == pytest.approx(100 / 3)

    def test_a_finished_run_is_complete(self, ids: dict[TaskId, str]) -> None:
        tracker = ProgressTracker(ids)
        snapshot = tracker.update(dict.fromkeys(ids, TaskState.SUCCEEDED))
        assert snapshot.complete
        assert snapshot.percent == 100.0
        assert snapshot.healthy

    def test_failures_and_blocks_are_counted_separately(
        self, ids: dict[TaskId, str]
    ) -> None:
        keys = list(ids)
        tracker = ProgressTracker(ids)
        snapshot = tracker.update(
            {
                keys[0]: TaskState.SUCCEEDED,
                keys[1]: TaskState.ABANDONED,
                keys[2]: TaskState.BLOCKED,
            }
        )
        assert snapshot.succeeded == 1
        assert snapshot.failed == 1
        assert snapshot.blocked == 1
        assert snapshot.complete
        assert not snapshot.healthy

    def test_running_steps_are_reported_by_name(self, ids: dict[TaskId, str]) -> None:
        keys = list(ids)
        tracker = ProgressTracker(ids)
        tracker.update({keys[0]: TaskState.RUNNING, keys[1]: TaskState.GATING})
        snapshot = tracker.snapshot()

        assert snapshot.running == 2
        assert len(snapshot.active_names()) == 2
        assert all(name in ids.values() for name in snapshot.active_names())

    def test_attempts_are_totalled(self, ids: dict[TaskId, str]) -> None:
        tracker = ProgressTracker(ids)
        snapshot = tracker.update({}, dict.fromkeys(ids, 2))
        assert snapshot.attempts == 6

    def test_observers_are_notified(self, ids: dict[TaskId, str]) -> None:
        seen: list[float] = []
        tracker = ProgressTracker(ids)
        tracker.subscribe(lambda snapshot: seen.append(snapshot.percent))
        tracker.update({next(iter(ids)): TaskState.SUCCEEDED})
        assert len(seen) == 1

    def test_an_empty_run_is_zero_percent_not_complete(self) -> None:
        snapshot = ProgressTracker({}).snapshot()
        assert snapshot.percent == 0.0
        assert not snapshot.complete

    def test_the_summary_reads_naturally(self, ids: dict[TaskId, str]) -> None:
        keys = list(ids)
        tracker = ProgressTracker(ids)
        snapshot = tracker.update(
            {keys[0]: TaskState.SUCCEEDED, keys[1]: TaskState.RUNNING}
        )
        summary = snapshot.summary()
        assert "1/3" in summary
        assert "1 running" in summary

    def test_steps_are_reported_in_name_order(self, ids: dict[TaskId, str]) -> None:
        names = [s.name for s in ProgressTracker(ids).snapshot().steps]
        assert names == sorted(names)


class TestEventEmitter:
    """Events reach the sink, and a broken sink does not break the run."""

    def test_events_reach_the_sink(self) -> None:
        captured: list[Event] = []
        emitter = EventEmitter(RunId.generate(), captured.append)
        emitter.run_created(goal="g", repo_path="/r", workflow="wf")

        assert len(captured) == 1
        assert captured[0].type is EventType.RUN_CREATED
        assert captured[0].payload["goal"] == "g"
        assert emitter.emitted == 1

    def test_the_run_id_is_stamped_on_everything(self) -> None:
        captured: list[Event] = []
        run_id = RunId.generate()
        emitter = EventEmitter(run_id, captured.append)
        emitter.run_started()
        emitter.run_finished(outcome="succeeded")

        assert all(event.run_id == run_id for event in captured)

    def test_task_created_carries_the_step_name(self) -> None:
        """Recovery binds on this; only the name is stable across runs."""
        captured: list[Event] = []
        emitter = EventEmitter(RunId.generate(), captured.append)
        task_id = TaskId.generate()
        emitter.task_created(
            task_id, step="build", title="build", prompt="p", max_attempts=2
        )

        payload = captured[0].payload
        assert payload["step"] == "build"
        assert payload["title"] == "build"
        assert captured[0].task_id == task_id

    def test_no_sink_discards_quietly(self) -> None:
        emitter = EventEmitter(RunId.generate(), None)
        assert emitter.emit(Event.new(EventType.RUN_STARTED)) is False
        assert emitter.emitted == 0

    def test_a_raising_sink_does_not_break_the_run(self) -> None:
        """Observability is not worth losing work over."""

        def broken(event: Event) -> None:
            raise RuntimeError("sink is down")

        emitter = EventEmitter(RunId.generate(), broken)
        assert emitter.emit(Event.new(EventType.RUN_STARTED)) is False
        assert emitter.failures == 1
        assert emitter.emitted == 0

    def test_sink_failures_are_visible(self) -> None:
        """A silently broken sink would be worse than a loud one."""

        def broken(event: Event) -> None:
            raise RuntimeError("down")

        emitter = EventEmitter(RunId.generate(), broken)
        for _ in range(3):
            emitter.emit(Event.new(EventType.RUN_STARTED))
        assert emitter.failures == 3

    def test_emit_all_counts_accepted(self) -> None:
        captured: list[Event] = []
        emitter = EventEmitter(RunId.generate(), captured.append)
        events = [Event.new(EventType.TOOL_CALLED) for _ in range(4)]
        assert emitter.emit_all(events) == 4

    def test_gate_verdicts_are_recorded(self) -> None:
        captured: list[Event] = []
        emitter = EventEmitter(RunId.generate(), captured.append)
        emitter.gate_evaluated(
            TaskId.generate(), None, gate="unit", verdict="failed", required=True
        )

        event = captured[0]
        assert event.type is EventType.GATE_EVALUATED
        assert event.payload["gate"] == "unit"
        assert event.payload["verdict"] == "failed"

    def test_events_are_serializable(self) -> None:
        captured: list[Event] = []
        emitter = EventEmitter(RunId.generate(), captured.append)
        emitter.run_created(goal="g", repo_path="/r", workflow="wf")
        emitter.task_created(TaskId.generate(), step="s", title="s", prompt="p")
        emitter.run_finished(outcome="succeeded")

        for event in captured:
            assert Event.from_json(event.to_json()) == event

    def test_cancellation_is_its_own_event(self) -> None:
        captured: list[Event] = []
        emitter = EventEmitter(RunId.generate(), captured.append)
        emitter.run_cancelled(reason="operator asked")
        assert captured[0].type is EventType.RUN_CANCELLED

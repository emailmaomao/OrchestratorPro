"""Tests for the dispatcher — the part that actually runs a graph."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from orchestrator.core.events import Event, EventType, RunId
from orchestrator.task.dispatcher import (
    AttemptOutcome,
    DispatcherError,
    TaskDispatcher,
)
from orchestrator.task.graph import TaskGraph
from orchestrator.task.model import Task, TaskState
from orchestrator.task.retry import NO_BACKOFF, BackoffStrategy, RetryPolicy

from tests.task.conftest import (
    RecordingExecutor,
    SleepRecorder,
    chain,
    diamond,
    fan_out,
    make_task,
    run,
)


def dispatcher(
    tasks: list[Task],
    executor: RecordingExecutor,
    **kwargs: object,
) -> TaskDispatcher:
    """Build a dispatcher over ``tasks`` with test-friendly defaults."""
    kwargs.setdefault("policy", NO_BACKOFF)
    return TaskDispatcher(TaskGraph(tasks), executor, **kwargs)  # type: ignore[arg-type]


class TestHappyPath:
    """A graph whose attempts all succeed."""

    def test_a_single_task_runs_once(self) -> None:
        task = make_task()
        executor = RecordingExecutor()
        report = run(dispatcher([task], executor).run())

        assert report.succeeded == (task.id,)
        assert report.complete
        assert report.attempts[task.id] == 1
        assert executor.attempts_for(task.id) == 1

    def test_a_chain_runs_in_dependency_order(self) -> None:
        tasks = chain(4)
        executor = RecordingExecutor()
        report = run(dispatcher(tasks, executor).run())

        assert report.complete
        assert executor.order == [t.id for t in tasks]

    def test_a_diamond_completes(self) -> None:
        tasks = diamond()
        executor = RecordingExecutor()
        report = run(dispatcher(tasks, executor).run())

        assert set(report.succeeded) == {t.id for t in tasks}
        assert executor.order[0] == tasks[0].id
        assert executor.order[-1] == tasks[3].id

    def test_every_task_is_attempted_exactly_once(self) -> None:
        tasks = fan_out(5)
        executor = RecordingExecutor()
        run(dispatcher(tasks, executor).run())
        assert all(executor.attempts_for(t.id) == 1 for t in tasks)

    def test_an_empty_graph_completes_immediately(self) -> None:
        executor = RecordingExecutor()
        report = run(dispatcher([], executor).run())
        assert report.complete
        assert executor.calls == []


class TestParallelism:
    """Independent work runs concurrently, within the cap."""

    def test_independent_tasks_overlap(self) -> None:
        tasks = [make_task(f"t{i}") for i in range(4)]
        executor = RecordingExecutor(delay_s=0.01)
        run(dispatcher(tasks, executor, max_concurrency=4).run())
        assert executor.peak_concurrent > 1

    def test_the_cap_is_respected(self) -> None:
        """FR-2.1 observed end to end, not just in the scheduler."""
        tasks = [make_task(f"t{i}") for i in range(8)]
        executor = RecordingExecutor(delay_s=0.01)
        run(dispatcher(tasks, executor, max_concurrency=3).run())
        assert executor.peak_concurrent <= 3

    def test_a_cap_of_one_serializes(self) -> None:
        tasks = [make_task(f"t{i}") for i in range(4)]
        executor = RecordingExecutor(delay_s=0.005)
        run(dispatcher(tasks, executor, max_concurrency=1).run())
        assert executor.peak_concurrent == 1

    def test_a_chain_cannot_overlap_however_wide_the_cap(self) -> None:
        executor = RecordingExecutor(delay_s=0.005)
        run(dispatcher(chain(4), executor, max_concurrency=10).run())
        assert executor.peak_concurrent == 1

    def test_label_limits_throttle_within_the_global_cap(self) -> None:
        tasks = [make_task(f"t{i}", labels=("db",)) for i in range(5)]
        executor = RecordingExecutor(delay_s=0.01)
        run(
            dispatcher(
                tasks, executor, max_concurrency=5, label_limits={"db": 2}
            ).run()
        )
        assert executor.peak_concurrent <= 2

    def test_the_backlog_shows_slot_bound_work(self) -> None:
        tasks = [make_task(f"t{i}") for i in range(6)]
        executor = RecordingExecutor(delay_s=0.01)
        instance = dispatcher(tasks, executor, max_concurrency=2)
        run(instance.run())
        # Everything finished, so nothing should remain queued.
        assert instance.backlog == ()

    def test_a_non_positive_cap_is_refused(self) -> None:
        with pytest.raises(DispatcherError, match="at least 1"):
            dispatcher([make_task()], RecordingExecutor(), max_concurrency=0)


class TestFailureAndRetry:
    """Failures retry when the policy allows, and stop when it does not."""

    def test_a_retryable_failure_is_retried_then_succeeds(self) -> None:
        task = make_task(max_attempts=3)
        executor = RecordingExecutor(
            {
                task.id: [
                    AttemptOutcome.failure("flaky", retryable=True),
                    AttemptOutcome.success(),
                ]
            }
        )
        report = run(dispatcher([task], executor).run())

        assert report.succeeded == (task.id,)
        assert report.attempts[task.id] == 2
        assert executor.attempts_for(task.id) == 2

    def test_retries_stop_at_the_task_allowance(self) -> None:
        task = make_task(max_attempts=2)
        executor = RecordingExecutor(default=AttemptOutcome.failure("nope", retryable=True))
        report = run(dispatcher([task], executor).run())

        assert report.abandoned == (task.id,)
        assert executor.attempts_for(task.id) == 2

    def test_a_non_retryable_failure_is_not_retried(self) -> None:
        """Retrying would burn budget reproducing the identical error."""
        task = make_task(max_attempts=5)
        executor = RecordingExecutor(
            default=AttemptOutcome.failure("invalid_request", retryable=False)
        )
        report = run(dispatcher([task], executor).run())

        assert report.abandoned == (task.id,)
        assert executor.attempts_for(task.id) == 1

    def test_an_executor_crash_becomes_a_failed_outcome(self) -> None:
        task = make_task(max_attempts=3)

        class Exploding(RecordingExecutor):
            async def __call__(self, task: Task, attempt: int) -> AttemptOutcome:
                self.calls.append((task.id, attempt))
                raise RuntimeError("executor bug")

        executor = Exploding()
        report = run(dispatcher([task], executor).run())

        assert report.abandoned == (task.id,)
        # A crashed executor is a bug, not a blip: it is not retried.
        assert executor.attempts_for(task.id) == 1

    def test_backoff_delays_follow_the_policy(self) -> None:
        task = make_task(max_attempts=3)
        executor = RecordingExecutor(
            default=AttemptOutcome.failure("flaky", retryable=True)
        )
        sleeper = SleepRecorder()
        policy = RetryPolicy(
            strategy=BackoffStrategy.LINEAR, base_delay_s=2.0, jitter=0.0
        )
        run(
            TaskDispatcher(
                TaskGraph([task]), executor, policy=policy, sleep=sleeper
            ).run()
        )
        # Three attempts means two backoffs, growing linearly.
        assert sleeper.delays == [2.0, 4.0]
        assert executor.attempts_for(task.id) == 3

    def test_no_backoff_never_sleeps(self) -> None:
        task = make_task(max_attempts=3)
        executor = RecordingExecutor(
            default=AttemptOutcome.failure("flaky", retryable=True)
        )
        sleeper = SleepRecorder()
        run(dispatcher([task], executor, sleep=sleeper).run())
        assert sleeper.delays == []

    def test_backoff_does_not_hold_a_concurrency_slot(self) -> None:
        """A sleeping retry that occupied a slot would halve throughput."""
        flaky = make_task("flaky", max_attempts=2)
        others = [make_task(f"t{i}") for i in range(3)]
        executor = RecordingExecutor(
            {flaky.id: [AttemptOutcome.failure("f", retryable=True), AttemptOutcome.success()]}
        )
        sleeper = SleepRecorder()
        policy = RetryPolicy(
            strategy=BackoffStrategy.FIXED, base_delay_s=5.0, jitter=0.0
        )
        report = run(
            TaskDispatcher(
                TaskGraph([flaky, *others]),
                executor,
                max_concurrency=2,
                policy=policy,
                sleep=sleeper,
            ).run()
        )
        assert report.complete
        assert sleeper.delays == [5.0]


class TestBlocking:
    """A dead dependency blocks everything downstream."""

    def test_a_failed_dependency_blocks_its_dependent(self) -> None:
        tasks = chain(2, max_attempts=1)
        executor = RecordingExecutor(
            {tasks[0].id: [AttemptOutcome.failure("boom", retryable=False)]}
        )
        report = run(dispatcher(tasks, executor).run())

        assert report.abandoned == (tasks[0].id,)
        assert report.blocked == (tasks[1].id,)
        assert executor.attempts_for(tasks[1].id) == 0

    def test_blocking_propagates_down_a_chain(self) -> None:
        tasks = chain(4, max_attempts=1)
        executor = RecordingExecutor(
            {tasks[0].id: [AttemptOutcome.failure("boom", retryable=False)]}
        )
        report = run(dispatcher(tasks, executor).run())

        assert report.abandoned == (tasks[0].id,)
        assert set(report.blocked) == {t.id for t in tasks[1:]}
        # Only the head ran, and it ran once, as attempt number 1.
        assert executor.calls == [(tasks[0].id, 1)]

    def test_an_unaffected_branch_still_completes(self) -> None:
        """FR-2.3 / NFR-2.3: one failure must not abort the whole run."""
        root = make_task("root", max_attempts=1)
        doomed = make_task("doomed", depends_on=(root.id,), max_attempts=1)
        blocked = make_task("blocked", depends_on=(doomed.id,), max_attempts=1)
        healthy = make_task("healthy", depends_on=(root.id,), max_attempts=1)

        executor = RecordingExecutor(
            {doomed.id: [AttemptOutcome.failure("boom", retryable=False)]}
        )
        report = run(dispatcher([root, doomed, blocked, healthy], executor).run())

        assert set(report.succeeded) == {root.id, healthy.id}
        assert report.abandoned == (doomed.id,)
        assert report.blocked == (blocked.id,)
        assert not report.complete
        assert report.unfinished == tuple(sorted({doomed.id, blocked.id}))

    def test_a_diamond_join_blocks_if_either_side_dies(self) -> None:
        root, left, right, join = diamond(max_attempts=1)
        executor = RecordingExecutor(
            {left.id: [AttemptOutcome.failure("boom", retryable=False)]}
        )
        report = run(dispatcher([root, left, right, join], executor).run())

        assert right.id in report.succeeded
        assert report.blocked == (join.id,)


class TestCancellation:
    """FR-2.9: stop starting new work, let in-flight work finish."""

    def test_cancelling_before_the_run_starts_nothing(self) -> None:
        tasks = [make_task(f"t{i}") for i in range(3)]
        executor = RecordingExecutor()
        instance = dispatcher(tasks, executor)
        instance.request_cancel()

        report = run(instance.run())
        assert report.cancelled
        assert executor.calls == []
        assert report.succeeded == ()

    def test_cancellation_is_reported(self) -> None:
        instance = dispatcher([make_task()], RecordingExecutor())
        instance.request_cancel()
        assert run(instance.run()).cancelled
        assert instance.cancelled


class TestStateAndReporting:
    """The dispatcher's own view of the world stays coherent."""

    def test_states_start_pending(self) -> None:
        tasks = chain(2)
        instance = dispatcher(tasks, RecordingExecutor())
        assert all(state is TaskState.PENDING for state in instance.states.values())

    def test_final_states_match_the_report(self) -> None:
        tasks = chain(2, max_attempts=1)
        executor = RecordingExecutor(
            {tasks[0].id: [AttemptOutcome.failure("boom", retryable=False)]}
        )
        instance = dispatcher(tasks, executor)
        report = run(instance.run())

        assert report.states[tasks[0].id] is TaskState.ABANDONED
        assert report.states[tasks[1].id] is TaskState.BLOCKED

    def test_total_attempts_sums_correctly(self) -> None:
        tasks = chain(3)
        report = run(dispatcher(tasks, RecordingExecutor()).run())
        assert report.total_attempts == 3

    def test_a_task_is_never_dispatched_twice_for_one_attempt(self) -> None:
        """The slot must be claimed before awaiting, or the task runs twice."""
        tasks = [make_task(f"t{i}") for i in range(5)]
        executor = RecordingExecutor(delay_s=0.01)
        run(dispatcher(tasks, executor, max_concurrency=2).run())
        assert len(executor.calls) == len(set(executor.calls))
        assert all(executor.attempts_for(t.id) == 1 for t in tasks)


class TestEvents:
    """Integration with the M2 event model, without touching the store."""

    def test_events_are_emitted_for_the_lifecycle(self) -> None:
        task = make_task()
        collected: list[Event] = []
        run_id = RunId.generate()

        run(
            TaskDispatcher(
                TaskGraph([task]),
                RecordingExecutor(),
                policy=NO_BACKOFF,
                run_id=run_id,
                event_sink=collected.append,
            ).run()
        )

        kinds = [event.type for event in collected]
        assert EventType.TASK_READY in kinds
        assert EventType.TASK_STARTED in kinds
        assert EventType.TASK_SUCCEEDED in kinds
        assert all(event.run_id == run_id for event in collected)
        assert all(event.task_id == task.id for event in collected)

    def test_failure_and_block_events_are_emitted(self) -> None:
        tasks = chain(2, max_attempts=1)
        collected: list[Event] = []
        executor = RecordingExecutor(
            {tasks[0].id: [AttemptOutcome.failure("boom", retryable=False)]}
        )
        run(
            TaskDispatcher(
                TaskGraph(tasks),
                executor,
                policy=NO_BACKOFF,
                event_sink=collected.append,
            ).run()
        )

        kinds = [event.type for event in collected]
        assert EventType.TASK_FAILED in kinds
        assert EventType.TASK_ABANDONED in kinds
        assert EventType.TASK_BLOCKED in kinds

    def test_a_crash_message_reaches_the_failed_event(self) -> None:
        """The one fact needed to diagnose an executor crash must be recorded.

        The dispatcher converts a crash into ``executor_error`` with the
        exception message in the outcome's detail — but until the detail was
        carried onto the ``task.failed`` event, the log said only that an
        executor crashed, never why. That is exactly how a merge-serialization
        race stayed undiagnosable from the API.
        """
        task = make_task(max_attempts=1)
        collected: list[Event] = []

        class Exploding(RecordingExecutor):
            async def __call__(self, task: Task, attempt: int) -> AttemptOutcome:
                raise RuntimeError("the integration branch already exists")

        run(
            TaskDispatcher(
                TaskGraph([task]),
                Exploding(),
                policy=NO_BACKOFF,
                event_sink=collected.append,
            ).run()
        )

        failed = [e for e in collected if e.type is EventType.TASK_FAILED]
        assert failed, [e.type for e in collected]
        detail = failed[0].payload["detail"]
        assert detail["message"] == "the integration branch already exists"

    def test_attempt_finished_carries_usage(self) -> None:
        """The executor's usage must survive into the event payload.

        Before OP-004 this payload carried only ``status``; the projection
        read tokens with a default of zero, so every served attempt replayed
        as free. The dispatcher stays ignorant of what the keys mean — it just
        refuses to lose them.
        """
        task = make_task()
        collected: list[Event] = []
        executor = RecordingExecutor(
            default=AttemptOutcome.success(
                usage={
                    "tokens_in": 120,
                    "tokens_out": 34,
                    "cost_usd": None,
                    "tokens_estimated": True,
                }
            )
        )
        run(
            TaskDispatcher(
                TaskGraph([task]),
                executor,
                policy=NO_BACKOFF,
                event_sink=collected.append,
            ).run()
        )

        finished = [e for e in collected if e.type is EventType.ATTEMPT_FINISHED]
        assert finished, [e.type for e in collected]
        payload = finished[0].payload
        assert payload["tokens_in"] == 120
        assert payload["tokens_out"] == 34
        assert payload["tokens_estimated"] is True

    def test_events_are_serializable(self) -> None:
        """Anything emitted must be appendable to the durable log."""
        collected: list[Event] = []
        run(
            TaskDispatcher(
                TaskGraph([make_task()]),
                RecordingExecutor(),
                policy=NO_BACKOFF,
                run_id=RunId.generate(),
                event_sink=collected.append,
            ).run()
        )
        for event in collected:
            assert Event.from_json(event.to_json()) == event

    def test_no_sink_means_no_error(self) -> None:
        report = run(dispatcher([make_task()], RecordingExecutor()).run())
        assert report.complete


class TestOutcomeHelpers:
    """The neutral outcome type."""

    def test_success_helper(self) -> None:
        outcome = AttemptOutcome.success(files=2)
        assert outcome.ok
        assert outcome.detail["files"] == 2

    def test_failure_helper(self) -> None:
        outcome = AttemptOutcome.failure("timeout", retryable=True, seconds=30)
        assert not outcome.ok
        assert outcome.error_code == "timeout"
        assert outcome.retryable
        assert outcome.detail["seconds"] == 30


def test_the_task_package_never_imports_the_agent_or_provider_packages() -> None:
    """docs/020 §1: task and agent are siblings that never reference each other."""
    import orchestrator.task as package

    root = Path(package.__path__[0])
    offenders: list[str] = []

    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name.startswith(("orchestrator.agent", "orchestrator.provider")):
                    offenders.append(f"{path.name}: {name}")

    assert offenders == [], f"forbidden imports in the task package: {offenders}"

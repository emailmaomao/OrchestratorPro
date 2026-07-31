"""Tests for the workflow engine: execution, timeout, cancellation, resume."""

from __future__ import annotations

import asyncio

import pytest

from orchestrator.core.events import AttemptId, Event, EventType, RunId
from orchestrator.core.records import RunStatus
from orchestrator.core.run_store import RunStore
from orchestrator.task.model import TaskState
from orchestrator.task.retry import RetryPolicy
from orchestrator.workflow.definition import StepDidWork, WorkflowDefinition
from orchestrator.workflow.engine import (
    EngineConfig,
    WorkflowEngine,
    WorkflowEngineError,
    WorkflowReport,
)

from tests.workflow.conftest import ScriptedExecutor, failed, run, step, succeeded, workflow


def execute(
    definition: WorkflowDefinition,
    *,
    outcomes: dict[str, object] | None = None,
    store: RunStore | None = None,
    config: EngineConfig | None = None,
    delay_s: float = 0.0,
    run_id: RunId | None = None,
) -> tuple[WorkflowReport, ScriptedExecutor]:
    """Run a definition against a scripted executor."""
    scripted = ScriptedExecutor(
        definition.compile(), outcomes=outcomes, delay_s=delay_s  # type: ignore[arg-type]
    )
    engine = WorkflowEngine(config=config or EngineConfig(), store=store)
    report = run(engine.run(definition, scripted.factory(), run_id=run_id))
    return report, scripted


def no_wait() -> EngineConfig:
    """A config whose retries do not really wait."""
    return EngineConfig(policy=RetryPolicy(base_delay_s=0.0, max_delay_s=0.0))


class TestConfig:
    """A configuration that cannot work is refused where it is written."""

    def test_a_non_positive_concurrency_is_refused(self) -> None:
        with pytest.raises(WorkflowEngineError, match="at least 1"):
            EngineConfig(max_concurrency=0)

    def test_a_non_positive_timeout_is_refused(self) -> None:
        with pytest.raises(WorkflowEngineError, match="must be positive"):
            EngineConfig(run_timeout_s=0)

    def test_the_defaults_are_usable(self) -> None:
        config = EngineConfig()
        assert config.max_concurrency is None
        assert config.run_timeout_s is None
        assert config.auto_finish


class TestExecution:
    """The DAG runs, in order, to a report in the operator's own step names."""

    def test_a_single_step_runs(self) -> None:
        report, scripted = execute(workflow(step("build")))

        assert report.complete
        assert report.outcome == "succeeded"
        assert scripted.executed == ["build"]

    def test_dependencies_are_honoured(self) -> None:
        definition = workflow(
            step("a"), step("b", depends_on=["a"]), step("c", depends_on=["b"])
        )
        _, scripted = execute(definition)
        assert scripted.executed == ["a", "b", "c"]

    def test_the_report_uses_step_names(self) -> None:
        report, _ = execute(workflow(step("alpha"), step("beta")))
        assert report.succeeded_steps == ("alpha", "beta")
        assert report.unfinished_steps == ()

    def test_progress_reaches_one_hundred_percent(self) -> None:
        report, _ = execute(workflow(step("a"), step("b")))
        assert report.progress.percent == 100.0
        assert report.progress.complete

    def test_a_failing_step_leaves_the_run_incomplete(self) -> None:
        report, _ = execute(
            workflow(step("a")), outcomes={"a": failed("boom")}, config=no_wait()
        )

        assert not report.complete
        assert report.outcome == "failed"
        assert report.unfinished_steps == ("a",)

    def test_a_dependent_of_a_failed_step_is_blocked(self) -> None:
        definition = workflow(step("a"), step("b", depends_on=["a"]))
        report, scripted = execute(
            definition, outcomes={"a": failed("boom")}, config=no_wait()
        )

        assert scripted.executed == ["a"]
        assert set(report.unfinished_steps) == {"a", "b"}

    def test_the_summary_names_the_workflow_and_outcome(self) -> None:
        report, _ = execute(workflow(step("a"), name="release"))
        assert report.summary().startswith("release: succeeded")

    def test_a_run_identifier_can_be_supplied(self) -> None:
        run_id = RunId.generate()
        report, _ = execute(workflow(step("a")), run_id=run_id)
        assert report.run_id == run_id


class TestParallelism:
    """Independent steps run together, under the configured cap."""

    def test_independent_steps_run_concurrently(self) -> None:
        definition = workflow(step("a"), step("b"), step("c"), max_concurrency=3)
        _, scripted = execute(definition, delay_s=0.01)
        assert scripted.peak_concurrent == 3

    def test_the_definition_cap_is_honoured(self) -> None:
        definition = workflow(step("a"), step("b"), step("c"), max_concurrency=2)
        _, scripted = execute(definition, delay_s=0.01)
        assert scripted.peak_concurrent <= 2

    def test_the_engine_cap_overrides_the_definition(self) -> None:
        definition = workflow(step("a"), step("b"), step("c"), max_concurrency=3)
        _, scripted = execute(
            definition, delay_s=0.01, config=EngineConfig(max_concurrency=1)
        )
        assert scripted.peak_concurrent == 1

    def test_a_diamond_completes(self) -> None:
        definition = workflow(
            step("root"),
            step("left", depends_on=["root"]),
            step("right", depends_on=["root"]),
            step("join", depends_on=["left", "right"]),
            max_concurrency=2,
        )
        report, scripted = execute(definition, delay_s=0.005)

        assert report.complete
        assert scripted.executed[0] == "root"
        assert scripted.executed[-1] == "join"


class TestRetry:
    """Attempts are spent according to the step's own allowance."""

    def test_a_retryable_failure_is_tried_again(self) -> None:
        definition = workflow(step("a", max_attempts=3))
        scripted = ScriptedExecutor(definition.compile(), {})
        calls: list[int] = []

        async def flaky(task: object, attempt: int) -> object:
            calls.append(attempt)
            return succeeded() if attempt == 2 else failed("flaky", retryable=True)

        engine = WorkflowEngine(config=no_wait())
        report = run(engine.run(definition, lambda plan, rid, em: flaky))

        assert report.complete
        assert calls == [1, 2]

    def test_a_non_retryable_failure_is_not_retried(self) -> None:
        definition = workflow(step("a", max_attempts=3))
        report, scripted = execute(
            definition, outcomes={"a": failed("fatal")}, config=no_wait()
        )

        assert len(scripted.calls) == 1
        assert not report.complete

    def test_attempts_stop_at_the_step_allowance(self) -> None:
        definition = workflow(step("a", max_attempts=2))
        _, scripted = execute(
            definition,
            outcomes={"a": failed("flaky", retryable=True)},
            config=no_wait(),
        )
        assert [attempt for _, attempt in scripted.calls] == [1, 2]

    def test_attempts_are_counted_in_the_progress_report(self) -> None:
        definition = workflow(step("a", max_attempts=2))
        report, _ = execute(
            definition,
            outcomes={"a": failed("flaky", retryable=True)},
            config=no_wait(),
        )
        assert report.progress.attempts == 2


class TestTimeoutAndCancellation:
    """A run can be stopped, and what happened up to that point is reported."""

    def test_a_run_level_timeout_stops_the_run(self) -> None:
        definition = workflow(step("a"), step("b", depends_on=["a"]))
        report, _ = execute(
            definition, delay_s=0.5, config=EngineConfig(run_timeout_s=0.05)
        )

        assert report.timed_out
        assert report.outcome == "timed_out"
        assert not report.complete

    def test_a_timeout_still_reports_what_had_happened(self) -> None:
        definition = workflow(step("a"))
        report, _ = execute(
            definition, delay_s=0.5, config=EngineConfig(run_timeout_s=0.05)
        )
        assert report.progress.total == 1

    def test_a_generous_timeout_does_not_fire(self) -> None:
        report, _ = execute(
            workflow(step("a")), config=EngineConfig(run_timeout_s=30.0)
        )
        assert not report.timed_out
        assert report.complete

    def test_cancellation_before_the_run_starts_nothing(self) -> None:
        definition = workflow(step("a"), step("b"))
        scripted = ScriptedExecutor(definition.compile())
        engine = WorkflowEngine()
        engine.request_cancel()

        report = run(engine.run(definition, scripted.factory()))

        assert engine.cancelled
        assert report.dispatch.cancelled
        assert report.outcome == "cancelled"
        assert scripted.calls == []

    def test_cancellation_mid_run_lets_in_flight_work_finish(self) -> None:
        """FR-2.9: stop starting new work, do not kill what is running."""
        definition = workflow(
            step("a"), step("b", depends_on=["a"]), step("c", depends_on=["b"])
        )
        engine = WorkflowEngine()
        started: list[str] = []

        def factory(plan: object, run_id: object, emitter: object) -> object:
            async def cancelling(task: object, attempt: int) -> object:
                name = plan.step_name(task.id)  # type: ignore[attr-defined]
                started.append(name)
                if name == "a":
                    engine.request_cancel()
                return succeeded()

            return cancelling

        report = run(engine.run(definition, factory))

        assert started == ["a"]
        assert report.outcome == "cancelled"
        assert not report.complete

    def test_a_cancelled_run_is_not_recorded_as_finished(self, store: RunStore) -> None:
        """Cancel, fix something, carry on is the most ordinary recovery there is."""
        definition = workflow(step("a"), step("b", depends_on=["a"]))
        engine = WorkflowEngine(store=store)
        engine.request_cancel()

        report = run(engine.run(definition, ScriptedExecutor(definition.compile()).factory()))
        kinds = [event.type for event in store.events.read_run(report.run_id)]

        assert EventType.RUN_FINISHED not in kinds
        assert store.replay(report.run_id).status is RunStatus.CANCELLED
        assert not engine.recovery().plan(report.run_id).already_finished

    def test_a_cancelled_run_is_recorded_as_cancelled(self, store: RunStore) -> None:
        definition = workflow(step("a"))
        engine = WorkflowEngine(store=store)
        engine.request_cancel()
        scripted = ScriptedExecutor(definition.compile())

        report = run(engine.run(definition, scripted.factory()))
        kinds = [event.type for event in store.events.read_run(report.run_id)]

        assert EventType.RUN_CANCELLED in kinds


class TestConditionalBranches:
    """A pruned branch is not a failed one."""

    def test_a_pruned_branch_does_not_fail_the_run(self) -> None:
        definition = workflow(
            step("a"),
            step("b", depends_on=["a"], condition=StepDidWork("a")),
        )
        outcomes = {"b": succeeded(skipped=True, condition="a did work")}
        report, _ = execute(definition, outcomes=outcomes)

        assert report.complete
        assert report.succeeded_steps == ("a", "b")


class TestPersistence:
    """The log is written as the run happens."""

    def test_a_run_is_recorded_end_to_end(self, store: RunStore) -> None:
        definition = workflow(step("a"), step("b", depends_on=["a"]))
        scripted = ScriptedExecutor(definition.compile())
        engine = WorkflowEngine(store=store)

        report = run(engine.run(definition, scripted.factory()))
        state = store.replay(report.run_id)

        assert state.status is RunStatus.FINISHED
        assert state.goal == "a goal"
        assert len(state.tasks) == 2
        assert report.events_emitted > 0
        assert report.event_failures == 0

    def test_the_lifecycle_events_are_all_there(self, store: RunStore) -> None:
        definition = workflow(step("a"))
        scripted = ScriptedExecutor(definition.compile())
        report = run(WorkflowEngine(store=store).run(definition, scripted.factory()))
        kinds = {event.type for event in store.events.read_run(report.run_id)}

        assert {
            EventType.RUN_CREATED,
            EventType.TASK_CREATED,
            EventType.RUN_STARTED,
            EventType.TASK_STARTED,
            EventType.ATTEMPT_STARTED,
            EventType.ATTEMPT_FINISHED,
            EventType.TASK_SUCCEEDED,
            EventType.RUN_FINISHED,
        } <= kinds

    def test_step_names_travel_with_the_task(self, store: RunStore) -> None:
        """Recovery binds on these, so they must be in the log."""
        definition = workflow(step("alpha"))
        scripted = ScriptedExecutor(definition.compile())
        report = run(WorkflowEngine(store=store).run(definition, scripted.factory()))

        created = [
            event
            for event in store.events.read_run(report.run_id)
            if event.type is EventType.TASK_CREATED
        ]
        assert created[0].payload["step"] == "alpha"
        assert created[0].payload["title"] == "alpha"

    def test_the_materialized_view_agrees_with_the_log(self, store: RunStore) -> None:
        definition = workflow(step("a"), step("b", depends_on=["a"]))
        scripted = ScriptedExecutor(definition.compile())
        report = run(WorkflowEngine(store=store).run(definition, scripted.factory()))

        assert store.verify(report.run_id)[0]

    def test_a_run_without_a_store_still_executes(self) -> None:
        """Persistence is what recovery needs, not what execution needs."""
        report, _ = execute(workflow(step("a")))
        assert report.complete
        assert report.events_emitted == 0

    def test_auto_finish_can_be_turned_off(self, store: RunStore) -> None:
        definition = workflow(step("a"))
        scripted = ScriptedExecutor(definition.compile())
        engine = WorkflowEngine(config=EngineConfig(auto_finish=False), store=store)

        report = run(engine.run(definition, scripted.factory()))
        kinds = {event.type for event in store.events.read_run(report.run_id)}

        assert EventType.RUN_FINISHED not in kinds


class TestInspect:
    """A persisted run can be examined without running anything."""

    def test_inspect_reports_what_remains(self, store: RunStore) -> None:
        definition = workflow(step("a"), step("b", depends_on=["a"]))
        report = run(
            WorkflowEngine(
                config=no_wait(), store=store
            ).run(
                definition,
                ScriptedExecutor(
                    definition.compile(), {"b": failed("boom")}
                ).factory(),
            )
        )

        summary = WorkflowEngine(store=store).inspect(report.run_id)

        assert summary["completed"] == ["a"]
        assert summary["remaining"] == ["b"]

    def test_a_finished_run_is_reported_as_not_resumable(
        self, store: RunStore
    ) -> None:
        definition = workflow(step("a"))
        report = run(
            WorkflowEngine(store=store).run(
                definition, ScriptedExecutor(definition.compile()).factory()
            )
        )
        assert WorkflowEngine(store=store).inspect(report.run_id)["resumable"] is False

    def test_inspect_without_a_store_is_refused(self) -> None:
        with pytest.raises(WorkflowEngineError, match="needs a run store"):
            WorkflowEngine().inspect(RunId.generate())


class TestResume:
    """A resumed run continues; it does not start over."""

    def _interrupted(self, store: RunStore) -> tuple[WorkflowDefinition, RunId]:
        """Start a two-step workflow and stop it after the first step.

        Cancellation stands in for the process going away: ``a`` is in the log
        as succeeded, ``b`` was never dispatched.
        """
        definition = workflow(step("a"), step("b", depends_on=["a"]))
        engine = WorkflowEngine(config=EngineConfig(auto_finish=False), store=store)

        def factory(plan: object, run_id: object, emitter: object) -> object:
            async def stop_after_first(task: object, attempt: int) -> object:
                engine.request_cancel()
                return succeeded()

            return stop_after_first

        report = run(engine.run(definition, factory))
        assert report.dispatch.cancelled
        return definition, report.run_id

    def test_completed_work_is_not_redone(self, store: RunStore) -> None:
        definition, run_id = self._interrupted(store)
        resumed = ScriptedExecutor(definition.compile())
        engine = WorkflowEngine(config=no_wait(), store=store)

        report = run(engine.resume(definition, resumed.factory(), run_id=run_id))

        assert resumed.executed == ["b"]
        assert report.resumed
        assert report.complete

    def test_the_resumed_run_keeps_its_identifier(self, store: RunStore) -> None:
        definition, run_id = self._interrupted(store)
        resumed = ScriptedExecutor(definition.compile())
        engine = WorkflowEngine(config=no_wait(), store=store)

        report = run(engine.resume(definition, resumed.factory(), run_id=run_id))
        assert report.run_id == run_id

    def test_the_resumed_run_reuses_the_persisted_task_ids(
        self, store: RunStore
    ) -> None:
        """A fresh identifier would orphan the completed work in the log."""
        definition, run_id = self._interrupted(store)
        before = {
            event.task_id
            for event in store.events.read_run(run_id)
            if event.type is EventType.TASK_CREATED
        }
        engine = WorkflowEngine(config=no_wait(), store=store)
        resumed = ScriptedExecutor(definition.compile())

        report = run(engine.resume(definition, resumed.factory(), run_id=run_id))

        assert set(report.plan.names_by_id) == before

    def test_resuming_emits_no_duplicate_task_creation(self, store: RunStore) -> None:
        definition, run_id = self._interrupted(store)
        engine = WorkflowEngine(config=no_wait(), store=store)
        resumed = ScriptedExecutor(definition.compile())

        run(engine.resume(definition, resumed.factory(), run_id=run_id))
        created = [
            event
            for event in store.events.read_run(run_id)
            if event.type is EventType.TASK_CREATED
        ]
        assert len(created) == 2

    def test_the_completed_step_is_never_started_again(self, store: RunStore) -> None:
        definition, run_id = self._interrupted(store)
        engine = WorkflowEngine(config=no_wait(), store=store)
        resumed = ScriptedExecutor(definition.compile())

        run(engine.resume(definition, resumed.factory(), run_id=run_id))
        starts = [
            event
            for event in store.events.read_run(run_id)
            if event.type is EventType.TASK_STARTED
        ]
        by_task = [event.task_id for event in starts]
        assert len(set(by_task)) == 2

    def test_the_replayed_state_stays_consistent(self, store: RunStore) -> None:
        definition, run_id = self._interrupted(store)
        engine = WorkflowEngine(config=no_wait(), store=store)
        run(engine.resume(definition, ScriptedExecutor(definition.compile()).factory(), run_id=run_id))

        state = store.replay(run_id)
        assert all(
            task.state == TaskState.SUCCEEDED.value for task in state.tasks.values()
        )
        assert store.verify(run_id)[0]

    def test_a_finished_run_cannot_be_resumed(self, store: RunStore) -> None:
        definition = workflow(step("a"))
        scripted = ScriptedExecutor(definition.compile())
        engine = WorkflowEngine(store=store)
        report = run(engine.run(definition, scripted.factory()))

        with pytest.raises(WorkflowEngineError, match="nothing to resume"):
            run(engine.resume(definition, scripted.factory(), run_id=report.run_id))

    def test_an_attempt_cut_off_mid_flight_is_tried_again(
        self, store: RunStore
    ) -> None:
        """The interrupted attempt produced no outcome, so it is not charged."""
        definition, run_id = self._interrupted(store)
        recovered = WorkflowEngine(store=store).recovery().plan(run_id)
        task_id = recovered.ids_by_name["b"]

        # What a crash between dispatch and outcome leaves in the log.
        store.record(Event.new(EventType.TASK_READY, run_id=run_id, task_id=task_id))
        store.record(
            Event.new(
                EventType.TASK_STARTED,
                run_id=run_id,
                task_id=task_id,
                payload={"attempt": 1},
            )
        )
        store.record(
            Event.new(
                EventType.ATTEMPT_STARTED,
                run_id=run_id,
                task_id=task_id,
                attempt_id=AttemptId.generate(),
                payload={"number": 1},
            )
        )

        after = WorkflowEngine(store=store).recovery().plan(run_id)
        assert after.interrupted == (task_id,)
        assert after.attempts[task_id] == 0

        resumed = ScriptedExecutor(definition.compile())
        report = run(
            WorkflowEngine(config=no_wait(), store=store).resume(
                definition, resumed.factory(), run_id=run_id
            )
        )

        assert resumed.executed == ["b"]
        assert report.complete

    def test_a_failed_step_is_not_retried_by_default(self, store: RunStore) -> None:
        """Resuming is for work that was cut off, not work that failed."""
        definition, run_id = self._failed(store)
        resumed = ScriptedExecutor(definition.compile())

        report = run(
            WorkflowEngine(config=no_wait(), store=store).resume(
                definition, resumed.factory(), run_id=run_id
            )
        )

        assert resumed.executed == []
        assert not report.complete

    def test_a_failed_step_can_be_retried_on_request(self, store: RunStore) -> None:
        definition, run_id = self._failed(store)
        resumed = ScriptedExecutor(definition.compile())

        report = run(
            WorkflowEngine(config=no_wait(), store=store).resume(
                definition, resumed.factory(), run_id=run_id, retry_failed=True
            )
        )

        assert resumed.executed == ["b"]
        assert report.complete

    def _failed(self, store: RunStore) -> tuple[WorkflowDefinition, RunId]:
        """Run a workflow whose second step fails for good."""
        definition = workflow(step("a"), step("b", depends_on=["a"]))
        scripted = ScriptedExecutor(definition.compile(), {"b": failed("boom")})
        engine = WorkflowEngine(
            config=EngineConfig(auto_finish=False, policy=no_wait().policy), store=store
        )
        report = run(engine.run(definition, scripted.factory()))
        return definition, report.run_id

    def test_conditions_see_the_earlier_run(self, store: RunStore) -> None:
        """A resumed branch must not be pruned on an empty history."""
        definition, run_id = self._interrupted(store)
        seeded: list[tuple[str, ...]] = []

        class Recording(ScriptedExecutor):
            def seed_completed(self, steps: object) -> None:
                seeded.append(tuple(steps))  # type: ignore[arg-type]

        resumed = Recording(definition.compile())
        run(
            WorkflowEngine(config=no_wait(), store=store).resume(
                definition, resumed.factory(), run_id=run_id
            )
        )

        assert seeded == [("a",)]

    def test_resuming_without_a_store_is_refused(self) -> None:
        with pytest.raises(WorkflowEngineError, match="without a run store"):
            run(
                WorkflowEngine().resume(
                    workflow(step("a")),
                    ScriptedExecutor(workflow(step("a")).compile()).factory(),
                    run_id=RunId.generate(),
                )
            )

    def test_an_unknown_run_cannot_be_resumed(self, store: RunStore) -> None:
        from orchestrator.workflow.recovery import RecoveryError

        definition = workflow(step("a"))
        engine = WorkflowEngine(store=store)
        with pytest.raises(RecoveryError):
            run(
                engine.resume(
                    definition,
                    ScriptedExecutor(definition.compile()).factory(),
                    run_id=RunId.generate(),
                )
            )


class TestConcurrentRuns:
    """Two runs in one process do not see each other."""

    def test_two_runs_are_independent(self, store: RunStore) -> None:
        definition = workflow(step("a"), step("b"))

        async def both() -> tuple[object, object]:
            first = ScriptedExecutor(definition.compile(), delay_s=0.01)
            second = ScriptedExecutor(definition.compile(), delay_s=0.01)
            return await asyncio.gather(
                WorkflowEngine(store=store).run(definition, first.factory()),
                WorkflowEngine(store=store).run(definition, second.factory()),
            )

        one, two = run(both())

        assert one.run_id != two.run_id  # type: ignore[attr-defined]
        assert one.complete and two.complete  # type: ignore[attr-defined]

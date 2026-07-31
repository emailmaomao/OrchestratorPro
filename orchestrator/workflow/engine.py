"""The workflow engine.

The layer that finally connects everything: a definition is compiled into a task
graph, the graph is executed by the M4 dispatcher, each step is performed by the
M7 agent runtime through the executor, and every state change is persisted to
the M2 event log.

The engine deliberately owns very little of that. DAG ordering, parallelism, and
retry-with-backoff already live in
:class:`~orchestrator.task.dispatcher.TaskDispatcher`; re-implementing them here
would give the system two schedulers that could disagree. What the engine adds
is the part that spans a whole run:

* compiling and validating definitions, and binding them on resume;
* persistence — the events that make a run replayable;
* a **run-level timeout**, distinct from the per-attempt budgets;
* cancellation that reaches both the dispatcher and the tools;
* progress, reported in the step names an operator wrote.

**Resume does not redo work.** A recovered run starts with completed tasks
already in their terminal states, so the scheduler never yields them: no
worktree, no model call, no duplicate events (FR-5.5).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from orchestrator.core.events import Event, OrchestratorError, RunId
from orchestrator.core.run_store import RunStore
from orchestrator.task.dispatcher import DispatchReport, TaskDispatcher, TaskExecutor
from orchestrator.task.model import TaskState
from orchestrator.task.retry import RetryPolicy
from orchestrator.workflow.definition import WorkflowDefinition, WorkflowPlan
from orchestrator.workflow.progress import EventEmitter, ProgressSnapshot, ProgressTracker
from orchestrator.workflow.recovery import RecoveryPlan, WorkflowRecovery

__all__ = ["EngineConfig", "WorkflowEngine", "WorkflowEngineError", "WorkflowReport"]


class WorkflowEngineError(OrchestratorError):
    """The engine could not run or resume a workflow."""

    code = "workflow_engine"
    retryable = False


@dataclass(frozen=True, slots=True)
class EngineConfig:
    """How a run is executed.

    Attributes:
        repo_path: Recorded on the run, for reports and recovery.
        max_concurrency: Overrides the definition's own cap when set.
        run_timeout_s: Wall-clock ceiling for the whole run, distinct from the
            per-attempt budgets. A run can consist entirely of attempts that
            each finish inside their budget and still take a week.
        policy: Retry and backoff. The task's ``max_attempts`` still governs
            *how many* tries; this governs *how* they are spaced.
        auto_finish: Emit ``run.finished`` when the dispatcher returns.
    """

    repo_path: str = ""
    max_concurrency: int | None = None
    run_timeout_s: float | None = None
    policy: RetryPolicy = field(default_factory=RetryPolicy)
    auto_finish: bool = True

    def __post_init__(self) -> None:
        if self.max_concurrency is not None and self.max_concurrency < 1:
            raise WorkflowEngineError(
                f"max_concurrency must be at least 1, got {self.max_concurrency}"
            )
        if self.run_timeout_s is not None and self.run_timeout_s <= 0:
            raise WorkflowEngineError(
                f"run_timeout_s must be positive, got {self.run_timeout_s}"
            )


@dataclass(frozen=True, slots=True)
class WorkflowReport:
    """What a run produced."""

    run_id: RunId
    workflow: str
    dispatch: DispatchReport
    progress: ProgressSnapshot
    plan: WorkflowPlan
    resumed: bool = False
    timed_out: bool = False
    events_emitted: int = 0
    event_failures: int = 0

    @property
    def succeeded_steps(self) -> tuple[str, ...]:
        """Step names that succeeded, in name order."""
        return tuple(sorted(self.plan.step_name(t) for t in self.dispatch.succeeded))

    @property
    def unfinished_steps(self) -> tuple[str, ...]:
        """Step names that did not succeed, in name order."""
        return tuple(sorted(self.plan.step_name(t) for t in self.dispatch.unfinished))

    @property
    def complete(self) -> bool:
        """Whether every step succeeded and the run was not cut short."""
        return (
            self.dispatch.complete
            and not self.timed_out
            and not self.dispatch.cancelled
        )

    @property
    def outcome(self) -> str:
        """A single word for the run's disposition."""
        if self.timed_out:
            return "timed_out"
        if self.dispatch.cancelled:
            return "cancelled"
        if self.dispatch.deadlocked:
            return "deadlocked"
        return "succeeded" if self.complete else "failed"

    def summary(self) -> str:
        """A one-line account of the run."""
        return (
            f"{self.workflow}: {self.outcome} — {self.progress.summary()}"
            f"{' (resumed)' if self.resumed else ''}"
        )


class WorkflowEngine:
    """Runs and resumes workflows."""

    __slots__ = ("_cancelled", "_config", "_dispatcher", "_store")

    def __init__(
        self, *, config: EngineConfig | None = None, store: RunStore | None = None
    ) -> None:
        """Create the engine.

        Args:
            config: How runs are executed.
            store: Durable event log. ``None`` runs without persistence, which
                is valid for a preview but forfeits recovery.
        """
        self._config = config or EngineConfig()
        self._store = store
        self._cancelled = False
        self._dispatcher: TaskDispatcher | None = None

    @property
    def config(self) -> EngineConfig:
        """The engine's configuration."""
        return self._config

    @property
    def store(self) -> RunStore | None:
        """The event store, if persistence is configured."""
        return self._store

    @property
    def cancelled(self) -> bool:
        """Whether cancellation has been requested."""
        return self._cancelled

    def request_cancel(self) -> None:
        """Stop starting new work; let in-flight attempts finish (FR-2.9)."""
        self._cancelled = True
        if self._dispatcher is not None:
            self._dispatcher.request_cancel()

    # ------------------------------------------------------------------ run

    async def run(
        self,
        definition: WorkflowDefinition,
        executor_factory: Callable[[WorkflowPlan, RunId, EventEmitter], TaskExecutor],
        *,
        run_id: RunId | None = None,
    ) -> WorkflowReport:
        """Compile a definition and execute it.

        Args:
            definition: The workflow to run.
            executor_factory: Builds the step executor once the plan and run
                identifier exist. Taking a factory rather than an executor is
                what keeps the engine free of any agent or provider knowledge.
            run_id: Reuse an existing identifier. One is minted otherwise.

        Returns:
            The report.
        """
        plan = definition.compile()
        resolved_run_id = run_id or RunId.generate()
        emitter = self._emitter_for(resolved_run_id)

        emitter.run_created(
            goal=definition.goal,
            repo_path=self._config.repo_path,
            workflow=definition.name,
        )
        for step in definition.steps:
            task_id = plan.task_id(step.name)
            emitter.task_created(
                task_id,
                step=step.name,
                # The *name* travels as the title so recovery can bind exactly;
                # the display title is carried separately.
                title=step.name,
                prompt=step.prompt,
                depends_on=[plan.task_id(d) for d in step.depends_on],
                max_attempts=step.max_attempts,
            )

        return await self._execute(
            plan, resolved_run_id, emitter, executor_factory, resumed=False
        )

    async def resume(
        self,
        definition: WorkflowDefinition,
        executor_factory: Callable[[WorkflowPlan, RunId, EventEmitter], TaskExecutor],
        *,
        run_id: RunId,
        retry_failed: bool = False,
    ) -> WorkflowReport:
        """Resume a persisted run without redoing completed work (FR-5.5).

        Args:
            definition: The workflow being resumed. Must describe the same steps
                the run started with.
            executor_factory: Builds the step executor.
            run_id: The run to resume.
            retry_failed: Also give steps that ran out of attempts a fresh
                allowance. Off by default — resuming is for work that was cut
                off, not for work that failed on its own terms.

        Returns:
            The report.

        Raises:
            WorkflowEngineError: If no store is configured, or the run has
                already finished.
        """
        if self._store is None:
            raise WorkflowEngineError(
                "cannot resume without a run store: recovery replays the event log",
                detail={"run_id": str(run_id)},
            )

        recovery = WorkflowRecovery(self._store)
        recovered = recovery.plan(run_id, retry_failed=retry_failed)
        if recovered.already_finished:
            raise WorkflowEngineError(
                f"run {run_id} already finished; there is nothing to resume",
                detail={"run_id": str(run_id)},
            )

        plan = recovery.bind(definition, recovered)
        emitter = self._emitter_for(run_id)
        return await self._execute(
            plan,
            run_id,
            emitter,
            executor_factory,
            resumed=True,
            recovered=recovered,
        )

    # ------------------------------------------------------------ internals

    def _emitter_for(self, run_id: RunId) -> EventEmitter:
        """Build an emitter wired to the store, if there is one."""
        sink: Callable[[Event], None] | None = None
        if self._store is not None:
            store = self._store
            sink = store.record
        return EventEmitter(run_id, sink)

    async def _execute(
        self,
        plan: WorkflowPlan,
        run_id: RunId,
        emitter: EventEmitter,
        executor_factory: Callable[[WorkflowPlan, RunId, EventEmitter], TaskExecutor],
        *,
        resumed: bool,
        recovered: RecoveryPlan | None = None,
    ) -> WorkflowReport:
        """Drive the dispatcher and assemble the report."""
        definition = plan.definition
        executor = executor_factory(plan, run_id, emitter)

        if recovered is not None:
            # Conditions are evaluated against what has already succeeded. An
            # executor that starts with an empty history would prune a branch
            # whose premise was satisfied by the earlier process.
            seed = getattr(executor, "seed_completed", None)
            if callable(seed):
                seed(recovered.completed_steps)

        tracker = ProgressTracker(
            plan.names_by_id,
            max_attempts={
                plan.task_id(step.name): step.max_attempts for step in definition.steps
            },
        )

        dispatcher = TaskDispatcher(
            plan.graph,
            executor,
            max_concurrency=self._config.max_concurrency or definition.max_concurrency,
            policy=self._config.policy,
            label_limits=definition.label_limits,
            run_id=run_id,
            event_sink=emitter.emit,
            initial_states=recovered.states if recovered else None,
            initial_attempts=recovered.attempts if recovered else None,
        )
        self._dispatcher = dispatcher
        if self._cancelled:
            dispatcher.request_cancel()

        emitter.run_started()
        timed_out = False
        try:
            report = await self._with_timeout(dispatcher)
        except TimeoutError:
            timed_out = True
            dispatcher.request_cancel()
            report = dispatcher.report_now()

        tracker.update(report.states, report.attempts)
        progress = tracker.snapshot()

        outcome = self._outcome_of(report, timed_out=timed_out)
        if self._config.auto_finish:
            if outcome == "cancelled":
                # A cancelled run has stopped, but it is not *finished*: work
                # remains and an operator may well resume it. Recording it as
                # finished would close the door on the most ordinary recovery
                # there is — cancel, fix something, carry on.
                emitter.run_cancelled(reason="cancellation was requested")
            else:
                emitter.run_finished(outcome=outcome)

        self._dispatcher = None
        return WorkflowReport(
            run_id=run_id,
            workflow=definition.name,
            dispatch=report,
            progress=progress,
            plan=plan,
            resumed=resumed,
            timed_out=timed_out,
            events_emitted=emitter.emitted,
            event_failures=emitter.failures,
        )

    async def _with_timeout(self, dispatcher: TaskDispatcher) -> DispatchReport:
        """Run the dispatcher under the run-level timeout, if one is set."""
        if self._config.run_timeout_s is None:
            return await dispatcher.run()
        return await asyncio.wait_for(
            dispatcher.run(), timeout=self._config.run_timeout_s
        )

    @staticmethod
    def _outcome_of(report: DispatchReport, *, timed_out: bool) -> str:
        """Reduce a dispatch report to a single word."""
        if timed_out:
            return "timed_out"
        if report.cancelled:
            return "cancelled"
        if report.deadlocked:
            return "deadlocked"
        return "succeeded" if report.complete else "failed"

    # -------------------------------------------------------------- helpers

    def recovery(self) -> WorkflowRecovery:
        """Return a recovery helper bound to this engine's store.

        Raises:
            WorkflowEngineError: If no store is configured.
        """
        if self._store is None:
            raise WorkflowEngineError(
                "recovery needs a run store: there is nothing to replay"
            )
        return WorkflowRecovery(self._store)

    def inspect(self, run_id: RunId) -> Mapping[str, Any]:
        """Return a summary of a persisted run, without executing anything."""
        recovered = self.recovery().plan(run_id)
        return {
            "run_id": str(run_id),
            "goal": recovered.goal,
            "completed": list(recovered.completed_steps),
            "remaining": list(recovered.remaining_steps),
            "interrupted": [str(t) for t in recovered.interrupted],
            "resumable": recovered.is_resumable,
            "summary": recovered.summary(),
        }


def states_by_step(
    plan: WorkflowPlan, states: Mapping[Any, TaskState]
) -> dict[str, TaskState]:
    """Re-key a state mapping into the step names an operator wrote."""
    return plan.states_by_name(states)  # type: ignore[arg-type]

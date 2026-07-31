"""Tests for the step executor: conditions, agent, gates, and feedback."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.agent.runtime import AgentRuntime, RuntimeConfig
from orchestrator.agent.tools import default_registry
from orchestrator.core.events import Event, EventType, RunId
from orchestrator.task.model import GateKind, GateSpec, Task
from orchestrator.test_runner.base import SuiteSpec
from orchestrator.test_runner.execution import ProcessResult, ScriptedRunner
from orchestrator.test_runner.runner import TestRunner as GateRunner
from orchestrator.workflow.definition import (
    StepDefinition,
    StepDidWork,
    StepSkipped,
    WorkflowDefinition,
)
from orchestrator.workflow.executor import ExecutionServices, StepExecutor
from orchestrator.workflow.progress import EventEmitter

from tests.agent.conftest import FakeProvider, finish_turn, refusal_turn, tool_turn
from tests.test_runner.conftest import FAILING_OUTPUT, PASSING_OUTPUT
from tests.workflow.conftest import agent_runtime, run, step, workflow


def build(
    definition: WorkflowDefinition,
    responses: list[object],
    *,
    services: ExecutionServices,
    gate_specs: dict[str, SuiteSpec] | None = None,
    emitter: EventEmitter | None = None,
) -> tuple[StepExecutor, object]:
    """Compile a definition and wrap it in an executor over a scripted model."""
    plan = definition.compile()
    executor = StepExecutor(
        agent_runtime(responses),
        plan,
        run_id=RunId.generate(),
        services=services,
        emitter=emitter,
        gate_specs=gate_specs,
    )
    return executor, plan


def task_of(plan: object, name: str) -> Task:
    """The compiled task behind a step name."""
    return plan.graph.get(plan.task_id(name))  # type: ignore[attr-defined]


def gated(
    scratch: Path, results: list[ProcessResult], **gate_kwargs: object
) -> tuple[StepExecutor, object]:
    """An executor for a single gated step, over a scripted process runner."""
    definition = workflow(
        step("build", gates=(GateSpec(name="unit", **gate_kwargs),), max_attempts=2)
    )
    runner = GateRunner(process_runner=ScriptedRunner(results))
    return build(
        definition,
        [finish_turn("built")],
        services=ExecutionServices(fallback_root=scratch, gates=runner),
        gate_specs={"unit": SuiteSpec(name="unit")},
    )


class TestServicesValidation:
    """A misconfigured executor is refused where it is built."""

    def test_commits_need_a_workspace(self) -> None:
        with pytest.raises(ValueError, match="needs a workspace manager"):
            ExecutionServices(commits=object(), fallback_root=Path("."))  # type: ignore[arg-type]

    def test_something_must_say_where_to_run(self) -> None:
        with pytest.raises(ValueError, match="fallback_root"):
            ExecutionServices()

    def test_a_plain_directory_is_enough(self, scratch: Path) -> None:
        assert ExecutionServices(fallback_root=scratch).workspaces is None


class TestAgentExecution:
    """The agent runs, and its result is translated into an outcome."""

    def test_a_successful_step_returns_success(
        self, services: ExecutionServices
    ) -> None:
        executor, plan = build(
            workflow(step("build")), [finish_turn("built it")], services=services
        )
        outcome = run(executor(task_of(plan, "build"), 1))

        assert outcome.ok
        assert outcome.detail["summary"] == "built it"

    def test_the_agent_works_inside_the_run_directory(
        self, services: ExecutionServices, scratch: Path
    ) -> None:
        executor, plan = build(
            workflow(step("build")),
            [
                tool_turn(("c1", "write_file", {"path": "out.txt", "content": "hi"})),
                finish_turn("wrote out.txt"),
            ],
            services=services,
        )
        outcome = run(executor(task_of(plan, "build"), 1))

        assert outcome.ok
        assert (scratch / "out.txt").read_text(encoding="utf-8") == "hi"
        assert outcome.detail["changed_files"] == ["out.txt"]

    def test_a_refusal_is_a_non_retryable_failure(
        self, services: ExecutionServices
    ) -> None:
        executor, plan = build(
            workflow(step("build")), [refusal_turn()], services=services
        )
        outcome = run(executor(task_of(plan, "build"), 1))

        assert not outcome.ok
        assert outcome.error_code == "refused"
        assert not outcome.retryable

    def test_a_successful_step_is_remembered_for_conditions(
        self, services: ExecutionServices
    ) -> None:
        executor, plan = build(
            workflow(step("build")), [finish_turn("ok")], services=services
        )
        run(executor(task_of(plan, "build"), 1))
        assert executor.completed_steps == frozenset({"build"})

    def test_feedback_accumulates_for_the_next_attempt(
        self, services: ExecutionServices
    ) -> None:
        """FR-2.5: attempt n+1 is told why attempt n failed."""
        executor, plan = build(
            workflow(step("build", max_attempts=2)),
            [refusal_turn(), finish_turn("ok")],
            services=services,
        )
        task = task_of(plan, "build")
        run(executor(task, 1))

        feedback = executor.feedback_for(task.id)
        assert feedback and "attempt 1" in feedback[0]

    def test_an_empty_summary_is_not_recorded_as_feedback(
        self, services: ExecutionServices
    ) -> None:
        """Blank feedback would spend context saying nothing."""
        executor, plan = build(
            workflow(step("build")), [finish_turn("ok")], services=services
        )
        task = task_of(plan, "build")
        run(executor(task, 1))
        assert executor.feedback_for(task.id) == ()


class TestConditions:
    """A false condition prunes a branch without failing it."""

    def _branch(self) -> WorkflowDefinition:
        return workflow(
            step("head"),
            step("tail", depends_on=["head"], condition=StepDidWork("head")),
        )

    def test_a_false_condition_skips_the_step(
        self, services: ExecutionServices
    ) -> None:
        executor, plan = build(self._branch(), [], services=services)
        outcome = run(executor(task_of(plan, "tail"), 1))

        assert outcome.ok
        assert outcome.detail["skipped"] is True
        assert outcome.detail["condition"] == "head did work"
        assert executor.skipped_steps == frozenset({"tail"})

    def test_a_skipped_step_never_calls_the_model(
        self, services: ExecutionServices
    ) -> None:
        provider = FakeProvider([finish_turn("should not run")])
        plan = self._branch().compile()
        executor = StepExecutor(
            AgentRuntime(
                provider,
                config=RuntimeConfig(model="fake-model"),
                registry=default_registry(),
            ),
            plan,
            run_id=RunId.generate(),
            services=services,
        )
        run(executor(task_of(plan, "tail"), 1))

        assert provider.requests == []

    def test_a_true_condition_runs_the_step(
        self, services: ExecutionServices
    ) -> None:
        executor, plan = build(
            self._branch(),
            [finish_turn("head"), finish_turn("tail")],
            services=services,
        )
        run(executor(task_of(plan, "head"), 1))
        outcome = run(executor(task_of(plan, "tail"), 1))

        assert outcome.ok
        assert "skipped" not in outcome.detail

    def test_a_skip_propagates_down_the_branch(
        self, services: ExecutionServices
    ) -> None:
        """A step behind a pruned one has no premise left either."""
        definition = workflow(
            step("head"),
            step("mid", depends_on=["head"], condition=StepDidWork("head")),
            step("tail", depends_on=["mid"], condition=StepDidWork("mid")),
        )
        executor, plan = build(definition, [], services=services)

        run(executor(task_of(plan, "mid"), 1))
        outcome = run(executor(task_of(plan, "tail"), 1))

        assert outcome.detail["skipped"] is True
        assert executor.skipped_steps == frozenset({"mid", "tail"})

    def test_the_complement_condition_fires_on_a_skip(
        self, services: ExecutionServices
    ) -> None:
        definition = workflow(
            step("head"),
            step("main", depends_on=["head"], condition=StepDidWork("head")),
            step("alt", depends_on=["main"], condition=StepSkipped("main")),
        )
        executor, plan = build(definition, [finish_turn("alt ran")], services=services)

        run(executor(task_of(plan, "main"), 1))
        outcome = run(executor(task_of(plan, "alt"), 1))

        assert outcome.ok
        assert "skipped" not in outcome.detail

    def test_seeding_restores_condition_history_on_resume(
        self, services: ExecutionServices
    ) -> None:
        """Otherwise a resumed run prunes a branch whose premise was satisfied."""
        executor, plan = build(self._branch(), [finish_turn("tail")], services=services)
        executor.seed_completed(["head"])

        outcome = run(executor(task_of(plan, "tail"), 1))

        assert outcome.ok
        assert "skipped" not in outcome.detail


class TestGates:
    """The agent reports; gates decide."""

    def test_a_passing_gate_lets_the_step_succeed(self, scratch: Path) -> None:
        executor, plan = gated(
            scratch, [ProcessResult(exit_code=0, stdout=PASSING_OUTPUT)]
        )
        outcome = run(executor(task_of(plan, "build"), 1))

        assert outcome.ok
        assert outcome.detail["gates"] == ["passed"]

    def test_a_failing_gate_fails_a_successful_agent_run(self, scratch: Path) -> None:
        executor, plan = gated(
            scratch, [ProcessResult(exit_code=1, stdout=FAILING_OUTPUT)]
        )
        outcome = run(executor(task_of(plan, "build"), 1))

        assert not outcome.ok
        assert outcome.error_code == "gate_failed"
        assert outcome.retryable
        assert "tests/test_a.py::test_two" in outcome.detail["failing"]

    def test_a_failing_gate_is_not_a_completed_step(self, scratch: Path) -> None:
        executor, plan = gated(
            scratch, [ProcessResult(exit_code=1, stdout=FAILING_OUTPUT)]
        )
        run(executor(task_of(plan, "build"), 1))
        assert executor.completed_steps == frozenset()

    def test_gate_failure_names_the_failing_tests_in_the_feedback(
        self, scratch: Path
    ) -> None:
        """FR-4.3: 'tests failed' alone is not actionable."""
        executor, plan = gated(
            scratch, [ProcessResult(exit_code=1, stdout=FAILING_OUTPUT)]
        )
        task = task_of(plan, "build")
        run(executor(task, 1))

        assert "test_two" in executor.feedback_for(task.id)[0]

    def test_a_broken_harness_says_so_in_the_feedback(self, scratch: Path) -> None:
        """FR-4.4: an attempt told 'tests failed' will start editing tests."""
        executor, plan = gated(
            scratch, [ProcessResult(exit_code=4, stderr="usage error")]
        )
        task = task_of(plan, "build")
        outcome = run(executor(task, 1))

        assert outcome.error_code == "gate_errored"
        assert outcome.retryable
        assert "Do not modify tests" in executor.feedback_for(task.id)[0]

    def test_a_timed_out_gate_is_not_a_code_failure(self, scratch: Path) -> None:
        executor, plan = gated(scratch, [ProcessResult(exit_code=-1, timed_out=True)])
        outcome = run(executor(task_of(plan, "build"), 1))
        assert outcome.error_code == "gate_timed_out"

    def test_a_required_gate_with_no_suite_does_not_pass(self, scratch: Path) -> None:
        """It verified nothing, so it must not read as green."""
        definition = workflow(step("build", gates=(GateSpec(name="unit"),)))
        executor, plan = build(
            definition,
            [finish_turn("built")],
            services=ExecutionServices(
                fallback_root=scratch,
                gates=GateRunner(process_runner=ScriptedRunner([])),
            ),
            gate_specs={},
        )
        outcome = run(executor(task_of(plan, "build"), 1))

        assert not outcome.ok
        assert outcome.error_code == "gate_skipped"

    def test_an_advisory_gate_with_no_suite_is_genuinely_optional(
        self, scratch: Path
    ) -> None:
        definition = workflow(
            step("build", gates=(GateSpec(name="lint", required=False),))
        )
        executor, plan = build(
            definition,
            [finish_turn("built")],
            services=ExecutionServices(
                fallback_root=scratch,
                gates=GateRunner(process_runner=ScriptedRunner([])),
            ),
            gate_specs={},
        )
        assert run(executor(task_of(plan, "build"), 1)).ok

    def test_an_advisory_gate_never_blocks(self, scratch: Path) -> None:
        executor, plan = gated(
            scratch,
            [ProcessResult(exit_code=1, stdout=FAILING_OUTPUT)],
            kind=GateKind.LINT,
            required=False,
        )
        assert run(executor(task_of(plan, "build"), 1)).ok

    def test_an_advisory_verdict_keeps_its_real_outcome(self, scratch: Path) -> None:
        """Reporting it as a pass without a trace would hide the signal."""
        captured: list[Event] = []
        definition = workflow(
            step("build", gates=(GateSpec(name="lint", required=False),))
        )
        executor, plan = build(
            definition,
            [finish_turn("built")],
            services=ExecutionServices(
                fallback_root=scratch,
                gates=GateRunner(
                    process_runner=ScriptedRunner(
                        [ProcessResult(exit_code=1, stdout=FAILING_OUTPUT)]
                    )
                ),
            ),
            gate_specs={"lint": SuiteSpec(name="lint")},
            emitter=EventEmitter(RunId.generate(), captured.append),
        )
        run(executor(task_of(plan, "build"), 1))

        assert captured[0].payload["verdict"] == "failed"

    def test_gate_verdicts_are_emitted(self, scratch: Path) -> None:
        captured: list[Event] = []
        definition = workflow(step("build", gates=(GateSpec(name="unit"),)))
        executor, plan = build(
            definition,
            [finish_turn("built")],
            services=ExecutionServices(
                fallback_root=scratch,
                gates=GateRunner(
                    process_runner=ScriptedRunner(
                        [ProcessResult(exit_code=0, stdout=PASSING_OUTPUT)]
                    )
                ),
            ),
            gate_specs={"unit": SuiteSpec(name="unit")},
            emitter=EventEmitter(RunId.generate(), captured.append),
        )
        task = task_of(plan, "build")
        run(executor(task, 1))

        assert len(captured) == 1
        assert captured[0].type is EventType.GATE_EVALUATED
        assert captured[0].task_id == task.id
        assert captured[0].payload["gate"] == "unit"

    def test_without_a_gate_runner_nothing_is_verified(
        self, services: ExecutionServices
    ) -> None:
        """A dry run must not look like a verified one."""
        executor, plan = build(
            workflow(step("build", gates=(GateSpec(name="unit"),))),
            [finish_turn("built")],
            services=services,
        )
        outcome = run(executor(task_of(plan, "build"), 1))

        assert outcome.ok
        assert outcome.detail["gates"] == []

    def test_gates_run_in_the_attempt_directory(self, scratch: Path) -> None:
        runner = ScriptedRunner([ProcessResult(exit_code=0, stdout=PASSING_OUTPUT)])
        definition = workflow(step("build", gates=(GateSpec(name="unit"),)))
        executor, plan = build(
            definition,
            [finish_turn("built")],
            services=ExecutionServices(
                fallback_root=scratch, gates=GateRunner(process_runner=runner)
            ),
            gate_specs={"unit": SuiteSpec(name="unit")},
        )
        run(executor(task_of(plan, "build"), 1))

        assert runner.runs[0].cwd == scratch


class TestWithoutGit:
    """Nothing is committed or merged when there is no repository."""

    def test_a_run_without_git_reports_no_commit(
        self, services: ExecutionServices
    ) -> None:
        executor, plan = build(
            workflow(step("build")), [finish_turn("built")], services=services
        )
        outcome = run(executor(task_of(plan, "build"), 1))

        assert outcome.detail["committed"] is False
        assert outcome.detail["merged"] is False

    def test_no_workspace_is_recorded(self, services: ExecutionServices) -> None:
        executor, plan = build(
            workflow(step("build")), [finish_turn("built")], services=services
        )
        run(executor(task_of(plan, "build"), 1))
        assert executor.workspaces == {}


class TestMergeSerialization:
    """Attempts run in parallel; merges do not (docs/020 §4, rule 1).

    The rule was documented in M0 and unimplemented until two concurrent
    passing attempts raced inside ``ensure_integration_branch`` — both saw the
    integration branch missing, both ran ``git branch``, and the loser crashed
    the executor. The overlap detector below fails deterministically if the
    merge stage is ever entered by two attempts at once, so the invariant can
    never quietly un-happen again.
    """

    def test_concurrent_merges_never_overlap(self, scratch: Path) -> None:
        """Two parallel attempts must enter the merge stage one at a time."""
        import asyncio

        from orchestrator.git_manager.merge import MergeResult, MergeStatus
        from orchestrator.git_manager.workspace import Workspace

        class FakeWorkspaces:
            """Hands each attempt its own directory, no git involved."""

            def __init__(self, root: Path) -> None:
                self.root = root
                self.start_points: list[str] = []

            async def create(self, *, run_id, task_id, attempt, start_point="HEAD"):
                """Return a fresh directory dressed as a workspace."""
                self.start_points.append(start_point)
                path = self.root / f"{task_id}-{attempt}"
                path.mkdir(parents=True, exist_ok=True)
                return Workspace(
                    path=path,
                    branch=f"branch-{task_id}",
                    run_id=str(run_id),
                    task_id=str(task_id),
                    attempt=attempt,
                )

        class OverlapDetectingMerges:
            """Records how many attempts hold the merge stage at once."""

            def __init__(self) -> None:
                self.active = 0
                self.max_active = 0
                self.calls = 0

            async def ensure_integration_branch(self, run_id, **_):
                """Attempts branch from here, so the executor asks for it."""
                return f"orchestrator/{run_id}/integration"

            async def integrate_attempt(self, *, run_id, source, **_):
                """Count concurrent holders of the merge stage."""
                self.calls += 1
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                # Hold the stage open across a scheduling point, so an
                # unserialized second merge would demonstrably overlap.
                await asyncio.sleep(0.02)
                self.active -= 1
                return MergeResult(
                    status=MergeStatus.MERGED, source=source, target="integration"
                )

        merges = OverlapDetectingMerges()
        services = ExecutionServices(
            workspaces=FakeWorkspaces(scratch),  # type: ignore[arg-type]
            merges=merges,  # type: ignore[arg-type]
        )
        executor, plan = build(
            workflow(step("left"), step("right")),
            [finish_turn("a"), finish_turn("b")],
            services=services,
        )
        left = task_of(plan, "left")
        right = task_of(plan, "right")

        async def both():
            return await asyncio.gather(executor(left, 1), executor(right, 1))

        results = run(both())

        assert all(outcome.ok for outcome in results), results
        assert merges.calls == 2
        assert merges.max_active == 1, (
            "two attempts held the merge stage at once — docs/020 §4 rule 1 "
            "(merges are serialized) has regressed"
        )


class TestTranscripts:
    """FR-5.3: an attempt's transcript lands as JSONL on disk.

    The path was documented in ``docs/020`` §5 from the start; nothing wrote it
    until ``ExecutionServices.transcripts`` existed. The executor owns the
    routing because one runtime serves every concurrent attempt and only the
    executor knows which run, task, and attempt an entry belongs to.
    """

    def test_an_attempt_writes_its_transcript(
        self, scratch: Path, tmp_dir: Path
    ) -> None:
        """One run, one task, one attempt → one JSONL file with real entries."""
        import json as jsonlib

        root = tmp_dir / "transcripts"
        services = ExecutionServices(fallback_root=scratch, transcripts=root)
        executor, plan = build(
            workflow(step("build")), [finish_turn("built")], services=services
        )
        outcome = run(executor(task_of(plan, "build"), 1))
        assert outcome.ok

        files = list(root.rglob("*.jsonl"))
        assert len(files) == 1, files
        assert files[0].name == "1.jsonl"
        # <root>/<run>/<task>/<attempt>.jsonl — the documented layout.
        task_dir, run_dir = files[0].parent, files[0].parent.parent
        assert task_dir.name.startswith("task_")
        assert run_dir.parent == root

        lines = [
            jsonlib.loads(line)
            for line in files[0].read_text(encoding="utf-8").splitlines()
        ]
        assert lines, "the transcript is empty"
        assert {"index", "kind", "content", "detail"} <= set(lines[0])

    def test_no_root_means_no_file(self, scratch: Path, tmp_dir: Path) -> None:
        """Without a configured root nothing is written anywhere."""
        services = ExecutionServices(fallback_root=scratch)
        executor, plan = build(
            workflow(step("build")), [finish_turn("built")], services=services
        )
        run(executor(task_of(plan, "build"), 1))
        assert list(tmp_dir.rglob("*.jsonl")) == []


class TestNoOpGuard:
    """An attempt that changed nothing must not pass as success.

    The first real harness run against another repository passed both of its
    gates having produced no code at all: the agent was blocked, said so,
    exited zero, and the gate then verified the *unchanged* baseline, which
    was green. A gate over unmodified code proves nothing about work that
    never happened.
    """

    def test_a_no_op_fails_an_ordinary_step(self, services: ExecutionServices) -> None:
        """The default: changing nothing is not doing the task."""
        executor, plan = build(
            workflow(step("build", expects_changes=True)),
            [finish_turn("I could not edit anything")],
            services=services,
        )
        outcome = run(executor(task_of(plan, "build"), 1))

        assert not outcome.ok
        assert outcome.error_code == "no_op"
        assert outcome.retryable, "a blocked agent deserves one more try, told why"

    def test_the_retry_is_told_it_changed_nothing(
        self, services: ExecutionServices
    ) -> None:
        """FR-2.5: the next attempt must know what went wrong."""
        executor, plan = build(
            workflow(step("build", expects_changes=True)),
            [finish_turn("done")],
            services=services,
        )
        task = task_of(plan, "build")
        run(executor(task, 1))

        feedback = " ".join(executor.feedback_for(task.id))
        assert "changed no files" in feedback
        assert "do not report success without work" in feedback

    def test_a_step_that_expects_no_changes_still_passes(
        self, services: ExecutionServices
    ) -> None:
        """A verification step legitimately produces no diff."""
        definition = workflow(
            StepDefinition(
                name="verify",
                prompt="Read the code and report; change nothing.",
                expects_changes=False,
            )
        )
        executor, plan = build(
            definition, [finish_turn("checked; all consistent")], services=services
        )
        outcome = run(executor(task_of(plan, "verify"), 1))

        assert outcome.ok, outcome
        assert outcome.detail.get("summary") == "checked; all consistent"


class TestConflictReporting:
    """A merge that cannot land fails the attempt, and says why.

    `_prepare` branching from the integration branch means a sequential
    conflict usually cannot arise — but parallel steps can still collide, and
    when they do the verdict must be a structured failure naming the files
    (FR-3.5), not a bool nobody reads (PROJECT_STATUS D-2d).
    """

    def _conflicting(self, scratch: Path, paths: tuple[str, ...]):
        """An executor whose merge always conflicts on ``paths``."""
        from orchestrator.git_manager.merge import MergeResult, MergeStatus
        from orchestrator.git_manager.workspace import Workspace

        class Workspaces:
            def __init__(self, root: Path) -> None:
                self.root = root

            async def create(self, *, run_id, task_id, attempt, start_point="HEAD"):
                """A directory dressed as a worktree."""
                path = self.root / f"{task_id}-{attempt}"
                path.mkdir(parents=True, exist_ok=True)
                return Workspace(
                    path=path, branch=f"b-{task_id}", run_id=str(run_id),
                    task_id=str(task_id), attempt=attempt,
                )

        class Conflicting:
            async def ensure_integration_branch(self, run_id, **_):
                """The branch attempts are cut from."""
                return f"orchestrator/{run_id}/integration"

            async def integrate_attempt(self, *, run_id, source, **_):
                """Always conflict, on the given paths."""
                return MergeResult(
                    status=MergeStatus.CONFLICT, source=source,
                    target="integration", conflicted_paths=paths,
                )

        return ExecutionServices(
            workspaces=Workspaces(scratch),  # type: ignore[arg-type]
            merges=Conflicting(),  # type: ignore[arg-type]
        )

    def test_a_conflict_fails_the_attempt(self, scratch: Path) -> None:
        """Not a successful attempt with a quiet flag: a failure."""
        services = self._conflicting(scratch, ("src/ui/main_window.py",))
        executor, plan = build(
            workflow(step("build")), [finish_turn("edited")], services=services
        )
        outcome = run(executor(task_of(plan, "build"), 1))

        assert not outcome.ok
        assert outcome.error_code == "merge_conflict"
        assert outcome.retryable, "the retry rebases onto integration; let it try"
        assert outcome.detail["conflicted_paths"] == ["src/ui/main_window.py"]
        assert outcome.detail["merge_status"] == "conflict"

    def test_the_conflicted_files_reach_the_next_attempt(
        self, scratch: Path
    ) -> None:
        """FR-2.5: the retry is told which files collided, and what to do."""
        services = self._conflicting(scratch, ("a.py", "b.py"))
        executor, plan = build(
            workflow(step("build")), [finish_turn("edited")], services=services
        )
        task = task_of(plan, "build")
        run(executor(task, 1))

        feedback = " ".join(executor.feedback_for(task.id))
        assert "a.py" in feedback and "b.py" in feedback
        assert "could not be merged" in feedback
        assert "integration branch" in feedback

    def test_a_conflicted_step_is_not_recorded_as_succeeded(
        self, scratch: Path
    ) -> None:
        """It must not satisfy a later step's did_work condition."""
        services = self._conflicting(scratch, ("x.py",))
        executor, plan = build(
            workflow(step("build")), [finish_turn("edited")], services=services
        )
        run(executor(task_of(plan, "build"), 1))

        assert "build" not in executor.completed_steps

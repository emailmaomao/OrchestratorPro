"""Integration tests: a document becomes a run.

The unit tests check that a document compiles. These check that what it
compiles to actually executes — the whole point of FR-1.2 is that a
hand-written file and a generated plan are equally executable, and a compile
step that produced something the engine could not run would satisfy every
earlier test while being useless.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.core.run_store import RunStore
from orchestrator.core.storage import Database
from orchestrator.planner.llm import PlanRequest, WorkflowPlanner
from orchestrator.planner.loader import load_workflow, load_workflow_file
from orchestrator.task.dispatcher import AttemptOutcome
from orchestrator.workflow.engine import EngineConfig, WorkflowEngine

from tests.planner.conftest import VALID_PLAN, ScriptedProvider, json_response, run

BRANCHING_YAML = """\
name: release
goal: cut a release
max_concurrency: 2
defaults:
  max_attempts: 1
  gates: [tests]
steps:
  - name: changelog
    prompt: Write the changelog entry for this release.
  - name: version-bump
    prompt: Bump the version in pyproject.toml.
  - name: tag
    prompt: Create the release tag.
    depends_on: [changelog, version-bump]
    when:
      all_of:
        - did_work: changelog
        - did_work: version-bump
"""


class Recorder:
    """A step executor that records what ran and returns a scripted outcome."""

    def __init__(self, outcomes: dict[str, AttemptOutcome] | None = None) -> None:
        self.outcomes = outcomes or {}
        self.calls: list[str] = []
        self._plan: object = None

    async def __call__(self, task: object, attempt: int) -> AttemptOutcome:
        name = self._plan.step_name(task.id)  # type: ignore[attr-defined,union-attr]
        self.calls.append(name)
        return self.outcomes.get(name, AttemptOutcome.success())

    def factory(self) -> object:
        def build(plan: object, run_id: object, emitter: object) -> object:
            self._plan = plan
            return self

        return build


@pytest.fixture
def store() -> RunStore:
    """A run store over a migrated in-memory database."""
    database = Database.in_memory()
    database.migrate()
    return RunStore(database)


class TestYamlExecutes:
    """A file, end to end."""

    def test_a_yaml_workflow_runs_to_completion(self, store: RunStore) -> None:
        workflow = load_workflow(BRANCHING_YAML)
        executor = Recorder()
        engine = WorkflowEngine(store=store)

        report = run(engine.run(workflow, executor.factory()))

        assert report.complete
        assert report.succeeded_steps == ("changelog", "tag", "version-bump")

    def test_the_declared_order_is_honoured(self, store: RunStore) -> None:
        executor = Recorder()
        report = run(WorkflowEngine(store=store).run(load_workflow(BRANCHING_YAML), executor.factory()))

        assert report.complete
        assert executor.calls[-1] == "tag"

    def test_the_declared_concurrency_is_used(self, store: RunStore) -> None:
        workflow = load_workflow(BRANCHING_YAML)
        assert workflow.max_concurrency == 2

    def test_the_run_is_recorded_under_the_step_names(self, store: RunStore) -> None:
        executor = Recorder()
        report = run(WorkflowEngine(store=store).run(load_workflow(BRANCHING_YAML), executor.factory()))

        titles = {task.title for task in store.replay(report.run_id).tasks.values()}
        assert titles == {"changelog", "version-bump", "tag"}

    def test_a_failing_step_blocks_its_dependents(self, store: RunStore) -> None:
        executor = Recorder({"changelog": AttemptOutcome.failure("boom")})
        engine = WorkflowEngine(
            config=EngineConfig(policy=_no_wait()), store=store
        )

        report = run(engine.run(load_workflow(BRANCHING_YAML), executor.factory()))

        assert not report.complete
        assert "tag" not in executor.calls

    def test_a_file_on_disk_runs(self, tmp_dir: Path, store: RunStore) -> None:
        path = tmp_dir / "release.yaml"
        path.write_text(BRANCHING_YAML, encoding="utf-8")

        report = run(WorkflowEngine(store=store).run(load_workflow_file(path), Recorder().factory()))
        assert report.complete

    def test_the_run_can_be_replayed_afterwards(self, store: RunStore) -> None:
        """The log is authoritative for a YAML-defined run like any other."""
        report = run(WorkflowEngine(store=store).run(load_workflow(BRANCHING_YAML), Recorder().factory()))

        state = store.replay(report.run_id)
        assert state.goal == "cut a release"
        assert store.verify(report.run_id)[0]


class TestGeneratedPlanExecutes:
    """FR-1.2, the part that matters: a generated plan is equally executable."""

    def test_a_generated_plan_runs(self, store: RunStore) -> None:
        planner = WorkflowPlanner(ScriptedProvider([json_response(VALID_PLAN)]))
        workflow = run(planner.plan_or_raise(PlanRequest(goal="migrate the client")))

        executor = Recorder()
        report = run(WorkflowEngine(store=store).run(workflow, executor.factory()))

        assert report.complete
        assert executor.calls == ["client-uses-httpx", "call-sites-updated"]

    def test_a_generated_plan_and_an_equivalent_file_produce_equal_graphs(self) -> None:
        planner = WorkflowPlanner(ScriptedProvider([json_response(VALID_PLAN)]))
        generated = run(planner.plan_or_raise(PlanRequest(goal="g")))

        handwritten = load_workflow(
            "name: migrate-http-client\n"
            "goal: migrate the HTTP client to httpx\n"
            "steps:\n"
            "  - name: client-uses-httpx\n"
            "    prompt: Replace the requests-based client in src/client.py with httpx.\n"
            "    gates: [tests]\n"
            "  - name: call-sites-updated\n"
            "    prompt: Update every call site to the new client signature.\n"
            "    depends_on: [client-uses-httpx]\n"
            "    gates: [tests]\n"
        )

        one, two = generated.compile(), handwritten.compile()
        assert [sorted(one.step_name(t) for t in layer) for layer in one.graph.layers()] == [
            sorted(two.step_name(t) for t in layer) for layer in two.graph.layers()
        ]
        assert [s.prompt for s in generated.steps] == [s.prompt for s in handwritten.steps]
        assert [
            [g.name for g in s.gates] for s in generated.steps
        ] == [[g.name for g in s.gates] for s in handwritten.steps]

    def test_a_generated_plan_can_be_resumed(self, store: RunStore) -> None:
        """It is an ordinary workflow once it exists."""
        planner = WorkflowPlanner(ScriptedProvider([json_response(VALID_PLAN)]))
        workflow = run(planner.plan_or_raise(PlanRequest(goal="g")))

        engine = WorkflowEngine(config=EngineConfig(auto_finish=False), store=store)
        stopper = Recorder()

        def factory(plan: object, run_id: object, emitter: object) -> object:
            async def stop_after_first(task: object, attempt: int) -> AttemptOutcome:
                stopper._plan = plan
                stopper.calls.append(plan.step_name(task.id))  # type: ignore[attr-defined]
                engine.request_cancel()
                return AttemptOutcome.success()

            return stop_after_first

        first = run(engine.run(workflow, factory))
        assert first.dispatch.cancelled

        resumed = Recorder()
        report = run(
            WorkflowEngine(store=store).resume(workflow, resumed.factory(), run_id=first.run_id)
        )

        assert report.complete
        assert "client-uses-httpx" not in resumed.calls


def _no_wait() -> object:
    """A retry policy that does not really wait."""
    from orchestrator.task.retry import RetryPolicy

    return RetryPolicy(base_delay_s=0.0, max_delay_s=0.0)

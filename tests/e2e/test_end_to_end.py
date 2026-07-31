"""End-to-end tests: whole paths, not units.

Each of these would pass if every unit test were deleted and the system did not
work, or fail if the units all passed and the wiring did not. That is the point
of them.

The Docker and live-provider tests skip when their prerequisites are absent
rather than failing: a laptop with no Docker daemon and no model endpoint
should still be able to run the suite green, and a test that fails for the
absence of an optional tool is a test people learn to ignore.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.api.state import AppState
from orchestrator.core.events import Event, EventType, RunId
from orchestrator.core.run_store import RunStore
from orchestrator.core.storage import Database
from orchestrator.planner.loader import load_workflow
from orchestrator.task.dispatcher import AttemptOutcome
from orchestrator.workflow.approval import ApprovalDecision, ApprovalService
from orchestrator.workflow.engine import EngineConfig, WorkflowEngine

from tests.e2e.conftest import (
    HERMES_BASE_URL,
    HERMES_MODEL,
    OPENAI_BASE_URL,
    OPENAI_MODEL,
    PIPELINE_YAML,
    UrllibTransport,
    docker,
    make_repository,
    requires_docker,
    requires_git,
    requires_hermes,
    requires_openai,
    run,
)


class Executor:
    """A step executor that records what ran, with optional delay and failures."""

    def __init__(
        self,
        outcomes: dict[str, AttemptOutcome] | None = None,
        *,
        delay_s: float = 0.0,
    ) -> None:
        self.outcomes = outcomes or {}
        self.delay_s = delay_s
        self.calls: list[str] = []
        self.concurrent = 0
        self.peak = 0
        self._plan: object = None

    async def __call__(self, task: object, attempt: int) -> AttemptOutcome:
        import asyncio

        name = self._plan.step_name(task.id)  # type: ignore[attr-defined,union-attr]
        self.calls.append(name)
        self.concurrent += 1
        self.peak = max(self.peak, self.concurrent)
        try:
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
            return self.outcomes.get(name, AttemptOutcome.success(step=name))
        finally:
            self.concurrent -= 1

    def factory(self) -> object:
        def build(plan: object, run_id: object, emitter: object) -> object:
            self._plan = plan
            return self

        return build


class TestCompleteWorkflowExecution:
    """A YAML file becomes a finished, recorded, replayable run."""

    def test_a_pipeline_runs_to_completion(self, durable_store: RunStore) -> None:
        workflow = load_workflow(PIPELINE_YAML)
        executor = Executor()

        report = run(WorkflowEngine(store=durable_store).run(workflow, executor.factory()))

        assert report.complete
        assert set(report.succeeded_steps) == {"design", "implement", "document", "verify"}

    def test_the_declared_order_is_honoured(self, durable_store: RunStore) -> None:
        executor = Executor()
        run(WorkflowEngine(store=durable_store).run(load_workflow(PIPELINE_YAML), executor.factory()))

        assert executor.calls[0] == "design"
        assert executor.calls[-1] == "verify"

    def test_the_whole_run_is_in_the_log(self, durable_store: RunStore) -> None:
        report = run(
            WorkflowEngine(store=durable_store).run(load_workflow(PIPELINE_YAML), Executor().factory())
        )
        kinds = {event.type for event in durable_store.events.read_run(report.run_id)}

        assert {
            EventType.RUN_CREATED,
            EventType.TASK_CREATED,
            EventType.TASK_STARTED,
            EventType.ATTEMPT_STARTED,
            EventType.ATTEMPT_FINISHED,
            EventType.TASK_SUCCEEDED,
            EventType.RUN_FINISHED,
        } <= kinds

    def test_the_materialized_view_agrees_with_the_log(self, durable_store: RunStore) -> None:
        report = run(
            WorkflowEngine(store=durable_store).run(load_workflow(PIPELINE_YAML), Executor().factory())
        )
        ok, differences = durable_store.verify(report.run_id)

        assert ok, differences

    def test_the_run_is_visible_through_the_api(self, durable_store: RunStore) -> None:
        """The same run, read the way an operator would read it."""
        report = run(
            WorkflowEngine(store=durable_store).run(load_workflow(PIPELINE_YAML), Executor().factory())
        )

        with TestClient(create_app(state=AppState(store=durable_store))) as client:
            status = client.get(f"/runs/{report.run_id}/status").json()
            tasks = client.get(f"/runs/{report.run_id}/tasks").json()

        assert status["complete"] is True
        assert status["healthy"] is True
        assert {task["title"] for task in tasks} == {
            "design",
            "implement",
            "document",
            "verify",
        }

    def test_a_pruned_branch_does_not_fail_the_run(self, durable_store: RunStore) -> None:
        """`verify` is conditional; skipping it is not a failure."""
        executor = Executor({"implement": AttemptOutcome.success(skipped=True)})
        report = run(
            WorkflowEngine(store=durable_store).run(load_workflow(PIPELINE_YAML), executor.factory())
        )

        assert report.complete


class TestRestartAndRecovery:
    """A process that dies mid-run does not lose the run."""

    def test_a_cancelled_run_resumes_without_redoing_work(
        self, durable_store: RunStore
    ) -> None:
        workflow = load_workflow(PIPELINE_YAML)
        engine = WorkflowEngine(config=EngineConfig(auto_finish=False), store=durable_store)
        first = Executor()

        def stopping(plan: object, run_id: object, emitter: object) -> object:
            first._plan = plan

            async def once(task: object, attempt: int) -> AttemptOutcome:
                first.calls.append(plan.step_name(task.id))  # type: ignore[attr-defined]
                engine.request_cancel()
                return AttemptOutcome.success()

            return once

        interrupted = run(engine.run(workflow, stopping))
        assert interrupted.dispatch.cancelled
        assert first.calls == ["design"]

        second = Executor()
        resumed = run(
            WorkflowEngine(store=durable_store).resume(
                workflow, second.factory(), run_id=interrupted.run_id
            )
        )

        assert resumed.complete
        assert "design" not in second.calls

    def test_the_state_survives_reopening_the_database(self, tmp_dir: Path) -> None:
        """The real crash-safety question: does it come back off disk?"""
        path = tmp_dir / "runs.db"
        first = Database(path)
        first.migrate()
        store = RunStore(first)

        report = run(
            WorkflowEngine(store=store).run(load_workflow(PIPELINE_YAML), Executor().factory())
        )
        first.close()

        second = Database(path)
        second.migrate()
        try:
            state = RunStore(second).replay(report.run_id)
            assert state.goal == "add the greeting endpoint and prove it works"
            assert len(state.tasks) == 4
        finally:
            second.close()

    def test_an_interrupted_attempt_is_not_charged(self, durable_store: RunStore) -> None:
        """A single-attempt step must remain resumable after a crash."""
        workflow = load_workflow(PIPELINE_YAML)
        engine = WorkflowEngine(config=EngineConfig(auto_finish=False), store=durable_store)

        def stopping(plan: object, run_id: object, emitter: object) -> object:
            async def once(task: object, attempt: int) -> AttemptOutcome:
                engine.request_cancel()
                return AttemptOutcome.success()

            return once

        first = run(engine.run(workflow, stopping))
        recovered = WorkflowEngine(store=durable_store).recovery().plan(first.run_id)

        assert not recovered.already_finished
        assert recovered.remaining_steps


class TestMultiAgent:
    """Several steps in flight at once."""

    def test_independent_steps_run_together(self, durable_store: RunStore) -> None:
        """`implement` and `document` both depend only on `design`."""
        executor = Executor(delay_s=0.05)
        report = run(
            WorkflowEngine(store=durable_store).run(load_workflow(PIPELINE_YAML), executor.factory())
        )

        assert report.complete
        assert executor.peak >= 2

    def test_the_concurrency_cap_is_respected(self, durable_store: RunStore) -> None:
        workflow = load_workflow(
            "name: wide\ngoal: g\nmax_concurrency: 2\nsteps:\n"
            + "".join(f"  - {{name: s{i}, prompt: p}}\n" for i in range(6))
        )
        executor = Executor(delay_s=0.03)

        run(WorkflowEngine(store=durable_store).run(workflow, executor.factory()))
        assert executor.peak <= 2

    def test_every_agent_gets_its_own_task(self, durable_store: RunStore) -> None:
        workflow = load_workflow(
            "name: wide\ngoal: g\nmax_concurrency: 4\nsteps:\n"
            + "".join(f"  - {{name: s{i}, prompt: p}}\n" for i in range(8))
        )
        executor = Executor()

        report = run(WorkflowEngine(store=durable_store).run(workflow, executor.factory()))

        assert report.complete
        assert sorted(executor.calls) == sorted(f"s{i}" for i in range(8))
        assert len(set(executor.calls)) == 8


class TestApprovalFlow:
    """A run that stops for a person, and what a reviewer sees."""

    def test_a_task_can_be_held_and_released(self, durable_store: RunStore) -> None:
        report = run(
            WorkflowEngine(store=durable_store).run(load_workflow(PIPELINE_YAML), Executor().factory())
        )
        approvals = ApprovalService(durable_store)
        task_id = next(iter(durable_store.replay(report.run_id).tasks))

        approvals.request(report.run_id, task_id, reason="touches the schema")
        assert len(approvals.queue()) == 1

        approvals.resolve(
            report.run_id, task_id, ApprovalDecision.APPROVED, actor="alice", note="fine"
        )
        assert approvals.queue() == ()

    def test_a_reviewer_sees_the_attempt_history(self, durable_store: RunStore) -> None:
        report = run(
            WorkflowEngine(store=durable_store).run(load_workflow(PIPELINE_YAML), Executor().factory())
        )
        task_id = next(iter(durable_store.replay(report.run_id).tasks))

        history = ApprovalService(durable_store).history(report.run_id, task_id)
        assert history.attempts
        assert history.attempts[0].status == "succeeded"

    def test_the_queue_is_visible_through_the_api(self, durable_store: RunStore) -> None:
        report = run(
            WorkflowEngine(store=durable_store).run(load_workflow(PIPELINE_YAML), Executor().factory())
        )
        task_id = next(iter(durable_store.replay(report.run_id).tasks))

        with TestClient(create_app(state=AppState(store=durable_store))) as client:
            created = client.post(
                f"/runs/{report.run_id}/tasks/{task_id}/approval",
                json={"reason": "needs a look"},
            )
            assert created.status_code == 201

            queue = client.get("/approvals").json()
            assert len(queue) == 1

            resolved = client.post(
                f"/runs/{report.run_id}/tasks/{task_id}/approval/resolve",
                json={"decision": "approved", "note": "looks right"},
            )
            assert resolved.status_code == 200
            assert client.get("/approvals").json() == []

    def test_a_transcript_is_readable_through_the_api(
        self, durable_store: RunStore
    ) -> None:
        report = run(
            WorkflowEngine(store=durable_store).run(load_workflow(PIPELINE_YAML), Executor().factory())
        )
        task_id = next(iter(durable_store.replay(report.run_id).tasks))

        with TestClient(create_app(state=AppState(store=durable_store))) as client:
            transcript = client.get(
                f"/runs/{report.run_id}/tasks/{task_id}/transcript"
            ).json()

        assert transcript["entries"]
        assert any(entry["type"] == "attempt.started" for entry in transcript["entries"])


class TestGitIsReal:
    """Against an actual repository, not a mock."""

    @requires_git
    def test_a_repository_is_initialized_and_read(self, tmp_dir: Path) -> None:
        from orchestrator.git_manager.repo import GitRepository

        root = make_repository(tmp_dir / "repo")
        repository = GitRepository(root)

        assert run(repository.is_repository())

    @requires_git
    def test_a_branch_diff_is_produced(self, tmp_dir: Path) -> None:
        """The diff viewer's data source, against a real branch."""
        from orchestrator.git_manager.repo import GitRepository

        root = make_repository(tmp_dir / "repo")
        subprocess.run(["git", "checkout", "-b", "feature"], cwd=root, check=True, capture_output=True)
        (root / "src" / "app.py").write_text(
            "def main() -> int:\n    return 1\n", encoding="utf-8"
        )
        subprocess.run(["git", "commit", "-am", "change"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "checkout", "main"], cwd=root, check=True, capture_output=True)

        patch = run(GitRepository(root).diff_branch("feature"))

        assert "src/app.py" in patch
        assert "+    return 1" in patch

    @requires_git
    def test_an_unchanged_branch_diffs_to_nothing(self, tmp_dir: Path) -> None:
        from orchestrator.git_manager.repo import GitRepository

        root = make_repository(tmp_dir / "repo")
        subprocess.run(["git", "branch", "quiet"], cwd=root, check=True, capture_output=True)

        assert run(GitRepository(root).diff_branch("quiet")) == ""


class TestLoad:
    """Enough volume to catch an accidental quadratic.

    The thresholds are loose on purpose. This is here to catch an order-of-
    magnitude regression — a lost index, an fsync per row, an O(n²) replay —
    not to police a shared runner's variance.
    """

    @pytest.mark.slow
    def test_a_large_log_replays_quickly(self, durable_store: RunStore) -> None:
        run_id = RunId.generate()
        durable_store.record(
            Event.new(
                EventType.RUN_CREATED, run_id=run_id, payload={"goal": "load", "repo_path": "/r"}
            )
        )
        events = [
            Event.new(EventType.TOOL_CALLED, run_id=run_id, payload={"n": index})
            for index in range(5000)
        ]
        durable_store.record_all(events)

        started = time.perf_counter()
        state = durable_store.replay(run_id)
        elapsed = time.perf_counter() - started

        assert state.event_count == 5001
        assert elapsed < 10.0, f"replaying 5001 events took {elapsed:.1f}s"

    @pytest.mark.slow
    def test_many_runs_stay_listable(self, durable_store: RunStore) -> None:
        for index in range(100):
            run_id = RunId.generate()
            durable_store.record(
                Event.new(
                    EventType.RUN_CREATED,
                    run_id=run_id,
                    payload={"goal": f"run {index}", "repo_path": "/r"},
                )
            )

        started = time.perf_counter()
        ids = durable_store.run_ids()
        assert len(ids) == 100
        assert time.perf_counter() - started < 5.0

    @pytest.mark.slow
    def test_a_wide_workflow_completes(self, durable_store: RunStore) -> None:
        workflow = load_workflow(
            "name: load\ngoal: g\nmax_concurrency: 8\nsteps:\n"
            + "".join(f"  - {{name: s{i}, prompt: p}}\n" for i in range(50))
        )
        executor = Executor()

        started = time.perf_counter()
        report = run(WorkflowEngine(store=durable_store).run(workflow, executor.factory()))
        elapsed = time.perf_counter() - started

        assert report.complete
        assert len(report.succeeded_steps) == 50
        assert elapsed < 60.0, f"50 steps took {elapsed:.1f}s"

    @pytest.mark.slow
    def test_the_api_serves_a_busy_database(self, durable_store: RunStore) -> None:
        for index in range(50):
            run_id = RunId.generate()
            durable_store.record(
                Event.new(
                    EventType.RUN_CREATED,
                    run_id=run_id,
                    payload={"goal": f"run {index}", "repo_path": "/r"},
                )
            )

        with TestClient(create_app(state=AppState(store=durable_store))) as client:
            started = time.perf_counter()
            for _ in range(20):
                assert client.get("/runs?limit=10").status_code == 200
            elapsed = time.perf_counter() - started

        assert elapsed < 20.0, f"20 list calls took {elapsed:.1f}s"


class TestLiveProviders:
    """Against a real endpoint, when one is configured.

    Skipped by default. There is no endpoint in CI and no credential in this
    repository, and a test that silently passes by not running is worse than
    one that says why it did not.
    """

    @requires_openai
    @pytest.mark.live
    def test_an_openai_compatible_endpoint_completes(self) -> None:
        from orchestrator.provider.base import CompletionRequest, Message, Role, TextBlock
        from orchestrator.provider.openai_compat import (
            OpenAICompatConfig,
            OpenAICompatProvider,
        )

        provider = OpenAICompatProvider(
            OpenAICompatConfig(model=OPENAI_MODEL, base_url=OPENAI_BASE_URL),
            transport=UrllibTransport(f"{OPENAI_BASE_URL.rstrip('/')}/chat/completions"),
        )
        response = run(
            provider.complete(
                CompletionRequest(
                    model=OPENAI_MODEL,
                    messages=(
                        Message(role=Role.USER, content=(TextBlock("Reply with the word OK."),)),
                    ),
                    max_output_tokens=32,
                )
            )
        )

        assert response.text
        assert response.usage.output_tokens >= 0

    @requires_openai
    @pytest.mark.live
    def test_the_planner_works_against_a_real_endpoint(self) -> None:
        """FR-1.1 against something that is actually a model."""
        from orchestrator.planner.llm import PlanRequest, WorkflowPlanner
        from orchestrator.provider.openai_compat import (
            OpenAICompatConfig,
            OpenAICompatProvider,
        )

        provider = OpenAICompatProvider(
            OpenAICompatConfig(model=OPENAI_MODEL, base_url=OPENAI_BASE_URL),
            transport=UrllibTransport(f"{OPENAI_BASE_URL.rstrip('/')}/chat/completions"),
        )
        result = run(
            WorkflowPlanner(provider, max_attempts=3).plan(
                PlanRequest(
                    goal="Add a health endpoint to the HTTP service and test it.",
                    repo_summary="Python, src/ layout, pytest.",
                    max_steps=4,
                    model=OPENAI_MODEL,
                )
            )
        )

        assert result.ok, result.summary()
        assert result.workflow is not None
        assert result.workflow.compile().graph

    @requires_hermes
    @pytest.mark.live
    def test_a_hermes_endpoint_completes(self) -> None:
        from orchestrator.provider.base import CompletionRequest, Message, Role, TextBlock
        from orchestrator.provider.hermes import HermesConfig, HermesProvider

        provider = HermesProvider(
            HermesConfig(model=HERMES_MODEL, base_url=HERMES_BASE_URL),
            transport=UrllibTransport(f"{HERMES_BASE_URL.rstrip('/')}/chat/completions"),
        )
        response = run(
            provider.complete(
                CompletionRequest(
                    model=HERMES_MODEL,
                    messages=(
                        Message(role=Role.USER, content=(TextBlock("Reply with the word OK."),)),
                    ),
                    max_output_tokens=32,
                )
            )
        )

        assert response.text


class TestDockerDeployment:
    """The image, actually built and actually run."""

    @requires_docker
    @pytest.mark.slow
    def test_the_image_builds_and_serves(self, tmp_dir: Path) -> None:
        """Slow — a real build — but it is the only way to know the image works."""
        root = Path(__file__).resolve().parents[2]
        tag = "orchestratorpro:e2e-test"

        build = subprocess.run(
            [docker or "docker", "build", "-t", tag, "."],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1800,
        )
        assert build.returncode == 0, build.stderr[-4000:]

        container = ""
        try:
            started = subprocess.run(
                [
                    docker or "docker",
                    "run",
                    "-d",
                    "-p",
                    "18765:8765",
                    # The same environment docker-compose.yml supplies. A
                    # non-loopback bind refuses to start without all of it,
                    # which is the point — see the test below.
                    *("-e", "ORCHESTRATORPRO__API__HOST=0.0.0.0"),
                    *("-e", "ORCHESTRATORPRO__API__AUTH_TOKEN_ENV=ORCHESTRATORPRO_TOKEN"),
                    *("-e", "ORCHESTRATORPRO_TOKEN=a-token-for-the-test"),
                    *("-e", "ORCHESTRATORPRO__SECURITY__ALLOWED_HOSTS=localhost,127.0.0.1"),
                    tag,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
            assert started.returncode == 0, started.stderr
            container = started.stdout.strip()

            import urllib.error
            import urllib.request

            body = None
            for _ in range(60):
                try:
                    with urllib.request.urlopen(
                        "http://127.0.0.1:18765/health", timeout=3
                    ) as response:
                        body = json.loads(response.read().decode("utf-8"))
                        break
                except (urllib.error.URLError, OSError, TimeoutError):
                    time.sleep(1)

            assert body is not None, "the container never answered /health"
            assert body["status"] == "ok"
            assert body["version"]
        finally:
            if container:
                subprocess.run(
                    [docker or "docker", "rm", "-f", container],
                    capture_output=True,
                    timeout=120,
                )


    @requires_docker
    @pytest.mark.slow
    def test_the_container_refuses_an_unsafe_bind(self) -> None:
        """The hardening, exercised where it matters: at container start.

        A public bind with no token, no allowed hosts, and no rate limit must
        make the process exit rather than serve. This test exists because the
        first version of the Docker test failed for exactly this reason, and
        the refusal was correct.
        """
        root = Path(__file__).resolve().parents[2]
        tag = "orchestratorpro:e2e-test"

        build = subprocess.run(
            [docker or "docker", "build", "-t", tag, "."],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1800,
        )
        assert build.returncode == 0, build.stderr[-2000:]

        result = subprocess.run(
            [
                docker or "docker",
                "run",
                "--rm",
                "-e",
                "ORCHESTRATORPRO__API__HOST=0.0.0.0",
                tag,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )

        assert result.returncode == 3, "an unsafe bind should exit 3"
        assert "auth_token_env" in (result.stdout + result.stderr)

    @requires_docker
    def test_the_dockerfile_is_syntactically_valid(self) -> None:
        """Cheap, and catches a broken Dockerfile without a full build."""
        root = Path(__file__).resolve().parents[2]
        result = subprocess.run(
            [docker or "docker", "build", "--check", "."],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        # `--check` exists on modern buildx; an old client reports usage, which
        # is not a Dockerfile problem and must not fail the suite.
        if "unknown flag" in (result.stderr or "").lower():
            pytest.skip("this docker client has no `build --check`")
        assert result.returncode == 0, result.stdout + result.stderr

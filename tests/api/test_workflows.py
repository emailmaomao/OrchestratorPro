"""Tests for workflow registration, execution, cancellation, and resume."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from orchestrator.core.events import EventType, RunId
from orchestrator.task.dispatcher import AttemptOutcome

from tests.api.conftest import (
    LINEAR_WORKFLOW,
    RecordingExecutor,
    make_app,
    register,
    start,
    wait_for_run,
)


class TestRegister:
    """Registering a definition."""

    def test_a_workflow_is_registered(self, client: TestClient) -> None:
        response = client.post("/workflows", json=dict(LINEAR_WORKFLOW))

        assert response.status_code == 201
        body = response.json()
        assert body["name"] == "ship"
        assert len(body["steps"]) == 3

    def test_the_compiled_shape_is_reported(self, client: TestClient) -> None:
        """An operator should be able to see the graph before running it."""
        body = client.post("/workflows", json=dict(LINEAR_WORKFLOW)).json()

        assert body["layers"] == [["design"], ["build"], ["verify"]]
        assert body["depth"] == 3
        assert body["max_width"] == 1

    def test_a_parallel_workflow_reports_its_width(self, client: TestClient) -> None:
        body = client.post(
            "/workflows",
            json={
                "name": "fan",
                "goal": "g",
                "steps": [
                    {"name": "a", "prompt": "p"},
                    {"name": "b", "prompt": "p"},
                    {"name": "c", "prompt": "p"},
                ],
            },
        ).json()

        assert body["max_width"] == 3
        assert body["depth"] == 1

    def test_gates_survive_registration(self, client: TestClient) -> None:
        body = client.post(
            "/workflows",
            json={
                "name": "gated",
                "goal": "g",
                "steps": [{"name": "a", "prompt": "p", "gates": ["unit"]}],
            },
        ).json()

        assert body["steps"][0]["gates"] == ["unit"]

    def test_a_duplicate_step_name_is_refused(self, client: TestClient) -> None:
        response = client.post(
            "/workflows",
            json={
                "name": "dupe",
                "goal": "g",
                "steps": [
                    {"name": "a", "prompt": "p"},
                    {"name": "a", "prompt": "p"},
                ],
            },
        )
        assert response.status_code == 400

    def test_a_cycle_is_refused(self, client: TestClient) -> None:
        """Only detectable at compile time, which is why registration compiles."""
        response = client.post(
            "/workflows",
            json={
                "name": "cyclic",
                "goal": "g",
                "steps": [
                    {"name": "a", "prompt": "p", "depends_on": ["b"]},
                    {"name": "b", "prompt": "p", "depends_on": ["a"]},
                ],
            },
        )
        assert response.status_code == 400

    def test_a_workflow_with_no_steps_is_refused(self, client: TestClient) -> None:
        response = client.post("/workflows", json={"name": "empty", "goal": "g", "steps": []})
        assert response.status_code == 422

    def test_re_registering_replaces(self, client: TestClient) -> None:
        register(client)
        client.post(
            "/workflows",
            json={"name": "ship", "goal": "changed", "steps": [{"name": "only", "prompt": "p"}]},
        )
        assert client.get("/workflows/ship").json()["goal"] == "changed"


class TestListAndDelete:
    """The registry."""

    def test_an_empty_registry_lists_nothing(self, client: TestClient) -> None:
        assert client.get("/workflows").json() == []

    def test_workflows_are_listed_by_name(self, client: TestClient) -> None:
        register(client)
        client.post(
            "/workflows",
            json={"name": "alpha", "goal": "g", "steps": [{"name": "a", "prompt": "p"}]},
        )
        assert [w["name"] for w in client.get("/workflows").json()] == ["alpha", "ship"]

    def test_an_unknown_workflow_is_a_404(self, client: TestClient) -> None:
        response = client.get("/workflows/ghost")

        assert response.status_code == 404
        assert "ghost" in response.json()["error"]["message"]

    def test_deleting_unregisters(self, client: TestClient) -> None:
        register(client)
        assert client.delete("/workflows/ship").status_code == 204
        assert client.get("/workflows/ship").status_code == 404

    def test_deleting_an_unknown_workflow_is_a_404(self, client: TestClient) -> None:
        assert client.delete("/workflows/ghost").status_code == 404


class TestExecution:
    """Running a workflow."""

    def test_a_run_starts_and_finishes(
        self, client: TestClient, executor: RecordingExecutor
    ) -> None:
        register(client)
        run_id = start(client)
        final = wait_for_run(client, run_id)

        assert final["complete"] is True
        assert final["succeeded"] == 3
        assert executor.calls == ["design", "build", "verify"]

    def test_the_response_arrives_before_the_work_does(self, client: TestClient) -> None:
        """202 means accepted, not finished."""
        register(client)
        response = client.post("/workflows/ship/runs", json={})

        assert response.status_code == 202
        assert response.json()["active"] is True
        assert response.json()["complete"] is False

    def test_the_run_is_addressable_immediately(self, client: TestClient) -> None:
        register(client)
        run_id = start(client)

        assert client.get(f"/runs/{run_id}").status_code == 200
        wait_for_run(client, run_id)

    def test_the_run_appears_in_the_active_list(self, client: TestClient) -> None:
        executor = RecordingExecutor(delay_s=0.05)
        with TestClient(make_app(executor=executor)) as client_:
            register(client_)
            run_id = start(client_)
            active = client_.get("/runs?active=true").json()

            assert [run["id"] for run in active] == [run_id]
            wait_for_run(client_, run_id)

    def test_a_failing_step_leaves_the_run_incomplete(self) -> None:
        executor = RecordingExecutor({"build": AttemptOutcome.failure("boom")})
        with TestClient(make_app(executor=executor)) as client:
            register(client)
            run_id = start(client)
            final = wait_for_run(client, run_id)

        assert final["healthy"] is False
        assert final["failed"] >= 1

    def test_the_whole_run_is_in_the_log(
        self, client: TestClient, state_of: Any
    ) -> None:
        register(client)
        run_id = start(client)
        wait_for_run(client, run_id)

        state = state_of(client)
        kinds = {e.type for e in state.store.events.read_run(RunId(run_id))}
        assert EventType.RUN_CREATED in kinds
        assert EventType.TASK_SUCCEEDED in kinds
        assert EventType.RUN_FINISHED in kinds

    def test_the_tasks_carry_the_step_names(self, client: TestClient) -> None:
        register(client)
        run_id = start(client)
        wait_for_run(client, run_id)

        titles = {task["title"] for task in client.get(f"/runs/{run_id}/tasks").json()}
        assert titles == {"design", "build", "verify"}

    def test_a_concurrency_override_is_accepted(self, client: TestClient) -> None:
        register(client)
        run_id = start(client, max_concurrency=1)
        assert wait_for_run(client, run_id)["complete"] is True

    def test_an_absurd_concurrency_is_refused(self, client: TestClient) -> None:
        register(client)
        response = client.post("/workflows/ship/runs", json={"max_concurrency": 0})
        assert response.status_code == 422

    def test_starting_an_unknown_workflow_is_a_404(self, client: TestClient) -> None:
        assert client.post("/workflows/ghost/runs", json={}).status_code == 404

    def test_the_run_timeout_is_honoured(self) -> None:
        executor = RecordingExecutor(delay_s=5.0)
        with TestClient(make_app(executor=executor)) as client:
            register(client)
            run_id = start(client, run_timeout_s=0.05)
            final = wait_for_run(client, run_id)

        assert final["complete"] is False


class TestCancellation:
    """Stopping a run that is under way."""

    def test_a_running_run_can_be_cancelled(self) -> None:
        executor = RecordingExecutor(delay_s=0.05)
        with TestClient(make_app(executor=executor)) as client:
            register(client)
            run_id = start(client)
            response = client.post(f"/runs/{run_id}/cancel")

            assert response.status_code == 200
            final = wait_for_run(client, run_id)

        assert final["complete"] is False
        assert len(executor.calls) < 3

    def test_cancellation_is_recorded(self, state_of: Any) -> None:
        executor = RecordingExecutor(delay_s=0.05)
        with TestClient(make_app(executor=executor)) as client:
            register(client)
            run_id = start(client)
            client.post(f"/runs/{run_id}/cancel")
            wait_for_run(client, run_id)

            state = state_of(client)
            kinds = {e.type for e in state.store.events.read_run(RunId(run_id))}

        assert EventType.RUN_CANCELLED in kinds

    def test_cancelling_twice_is_a_conflict_the_second_time(self) -> None:
        executor = RecordingExecutor(delay_s=0.05)
        with TestClient(make_app(executor=executor)) as client:
            register(client)
            run_id = start(client)
            assert client.post(f"/runs/{run_id}/cancel").status_code == 200
            wait_for_run(client, run_id)
            assert client.post(f"/runs/{run_id}/cancel").status_code == 409


class TestResume:
    """Continuing an interrupted run."""

    def _interrupted(self, client: TestClient) -> str:
        """Start a run, cancel it after the first step, and return its id."""
        register(client)
        run_id = start(client)
        client.post(f"/runs/{run_id}/cancel")
        wait_for_run(client, run_id)
        return run_id

    def test_a_cancelled_run_can_be_resumed(self) -> None:
        first = RecordingExecutor(delay_s=0.02)
        app = make_app(executor=first)
        with TestClient(app) as client:
            run_id = self._interrupted(client)
            done_before = client.get(f"/runs/{run_id}/status").json()["succeeded"]

            second = RecordingExecutor()
            app.state.orchestrator.executor_factory = second.factory()
            response = client.post(f"/runs/{run_id}/resume?workflow=ship")

            assert response.status_code == 202
            final = wait_for_run(client, run_id)

        assert final["succeeded"] >= done_before
        assert "design" not in second.calls

    def test_resuming_requires_a_workflow(self, client: TestClient) -> None:
        run_id = self._interrupted(client)
        assert client.post(f"/runs/{run_id}/resume").status_code == 422

    def test_resuming_an_unknown_run_is_a_404(self, client: TestClient) -> None:
        response = client.post(f"/runs/{RunId.generate()}/resume?workflow=ship")
        assert response.status_code == 404

    def test_resuming_with_an_unknown_workflow_is_a_404(self, client: TestClient) -> None:
        run_id = self._interrupted(client)
        response = client.post(f"/runs/{run_id}/resume?workflow=ghost")
        assert response.status_code == 404

    def test_resuming_a_finished_run_is_refused(self, client: TestClient) -> None:
        register(client)
        run_id = start(client)
        wait_for_run(client, run_id)

        response = client.post(f"/runs/{run_id}/resume?workflow=ship")
        assert response.status_code == 409
        assert "nothing to resume" in response.json()["error"]["message"]

    def test_resuming_without_a_backend_is_a_503(self, bare_client: TestClient) -> None:
        register(bare_client)
        response = bare_client.post(f"/runs/{RunId.generate()}/resume?workflow=ship")
        assert response.status_code in (404, 503)


class TestShutdown:
    """Winding the server down."""

    def test_in_flight_runs_are_stopped_on_shutdown(self) -> None:
        """A lingering task would outlive the application that owns its store."""
        executor = RecordingExecutor(delay_s=0.2)
        app = make_app(executor=executor)
        with TestClient(app) as client:
            register(client)
            start(client)

        state = app.state.orchestrator
        assert all(job.done for job in state.runs.values())

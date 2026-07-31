"""Tests for the build and agent endpoints."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from orchestrator.core.config import Effort, OrchestratorConfig, ProviderConfig, RoleConfig

from tests.api.conftest import (
    GCC_OUTPUT,
    failing_process,
    make_app,
    ok_process,
    scripted_runner,
    wait_for_build,
    write_project,
)


def build_client(tmp_dir: Path, *results: object, confined: bool = False) -> tuple[TestClient, Path]:
    """A client whose build commands are scripted, over a real project tree."""
    project = write_project(tmp_dir / "project")
    app = make_app(
        process_runner=scripted_runner(*results),  # type: ignore[arg-type]
        workspace_root=tmp_dir if confined else None,
    )
    return TestClient(app), project


class TestAnalyze:
    """Scanning a project."""

    def test_a_project_is_analyzed(self, tmp_dir: Path) -> None:
        client, project = build_client(tmp_dir)
        with client:
            body = client.post("/builds/analyze", json={"path": str(project)}).json()

        assert body["kind"] == "python"
        assert sorted(u["name"] for u in body["units"]) == ["app", "core"]
        assert body["source_count"] >= 4

    def test_inferred_dependencies_are_reported(self, tmp_dir: Path) -> None:
        client, project = build_client(tmp_dir)
        with client:
            body = client.post("/builds/analyze", json={"path": str(project)}).json()

        units = {u["name"]: u for u in body["units"]}
        assert units["app"]["depends_on"] == ["core"]
        assert units["core"]["dependents"] == ["app"]

    def test_a_manifest_is_honoured(self, tmp_dir: Path) -> None:
        client, project = build_client(tmp_dir)
        with client:
            body = client.post(
                "/builds/analyze",
                json={
                    "path": str(project),
                    "manifest": [
                        {"name": "everything", "command": "make", "sources": ["app", "core"]}
                    ],
                },
            ).json()

        assert [u["name"] for u in body["units"]] == ["everything"]

    def test_a_missing_directory_is_a_400(self, tmp_dir: Path) -> None:
        client, _ = build_client(tmp_dir)
        with client:
            response = client.post(
                "/builds/analyze", json={"path": str(tmp_dir / "absent")}
            )

        assert response.status_code == 400
        assert response.json()["error"]["code"]

    def test_an_empty_path_is_refused(self, tmp_dir: Path) -> None:
        client, _ = build_client(tmp_dir)
        with client:
            assert client.post("/builds/analyze", json={"path": ""}).status_code == 422

    def test_a_manifest_path_that_escapes_is_refused(self, tmp_dir: Path) -> None:
        """Manifest paths come over HTTP, so they are a trust boundary."""
        client, project = build_client(tmp_dir)
        with client:
            response = client.post(
                "/builds/analyze",
                json={
                    "path": str(project),
                    "manifest": [
                        {"name": "bad", "command": "make", "sources": ["../../etc"]}
                    ],
                },
            )

        assert response.status_code == 400


class TestPathConfinement:
    """A path in a request body is as untrusted as one from a model."""

    def test_a_path_outside_the_workspace_is_refused(self, tmp_dir: Path) -> None:
        client, _ = build_client(tmp_dir, confined=True)
        with client:
            response = client.post("/builds/analyze", json={"path": "/etc"})

        assert response.status_code == 400
        assert "workspace root" in response.json()["error"]["message"]

    def test_traversal_is_refused(self, tmp_dir: Path) -> None:
        client, _ = build_client(tmp_dir, confined=True)
        with client:
            response = client.post(
                "/builds/analyze", json={"path": "../../../../etc/passwd"}
            )

        assert response.status_code == 400

    def test_a_path_inside_the_workspace_is_accepted(self, tmp_dir: Path) -> None:
        client, project = build_client(tmp_dir, confined=True)
        with client:
            response = client.post("/builds/analyze", json={"path": str(project)})

        assert response.status_code == 200

    def test_a_repo_path_is_confined_too(self, tmp_dir: Path) -> None:
        client, _ = build_client(tmp_dir, confined=True)
        with client:
            response = client.post(
                "/runs", json={"goal": "g", "repo_path": "../../elsewhere"}
            )

        assert response.status_code == 400


class TestPlan:
    """Planning without running."""

    def test_a_cold_plan_rebuilds_everything(self, tmp_dir: Path) -> None:
        client, project = build_client(tmp_dir)
        with client:
            body = client.post("/builds/plan", json={"path": str(project)}).json()

        assert sorted(u["name"] for u in body["units"]) == ["app", "core"]
        assert body["empty"] is False

    def test_every_unit_says_why(self, tmp_dir: Path) -> None:
        client, project = build_client(tmp_dir)
        with client:
            body = client.post("/builds/plan", json={"path": str(project)}).json()

        assert all(u["reason"] for u in body["units"])
        assert all(u["why"] for u in body["units"])

    def test_the_waves_are_reported(self, tmp_dir: Path) -> None:
        client, project = build_client(tmp_dir)
        with client:
            body = client.post("/builds/plan", json={"path": str(project)}).json()

        assert body["layers"] == [["core"], ["app"]]
        assert body["max_parallel"] == 1

    def test_changed_paths_narrow_the_plan(self, tmp_dir: Path) -> None:
        client, project = build_client(tmp_dir, ok_process(), ok_process())
        with client:
            build_id = client.post("/builds", json={"path": str(project)}).json()["id"]
            wait_for_build(client, build_id)

            body = client.post(
                "/builds/plan",
                json={"path": str(project), "changed_paths": ["app/main.py"]},
            ).json()

        assert [u["name"] for u in body["units"]] == ["app"]
        assert body["cached"] == ["core"]

    def test_a_change_upstream_rebuilds_downstream(self, tmp_dir: Path) -> None:
        """The rule an incremental build most often gets wrong."""
        client, project = build_client(tmp_dir, ok_process(), ok_process())
        with client:
            build_id = client.post("/builds", json={"path": str(project)}).json()["id"]
            wait_for_build(client, build_id)

            body = client.post(
                "/builds/plan",
                json={"path": str(project), "changed_paths": ["core/util.py"]},
            ).json()

        assert sorted(u["name"] for u in body["units"]) == ["app", "core"]

    def test_the_cache_can_be_ignored(self, tmp_dir: Path) -> None:
        client, project = build_client(tmp_dir, ok_process(), ok_process())
        with client:
            build_id = client.post("/builds", json={"path": str(project)}).json()["id"]
            wait_for_build(client, build_id)

            body = client.post(
                "/builds/plan", json={"path": str(project), "use_cache": False}
            ).json()

        assert body["empty"] is False

    def test_forcing_a_unit_is_accepted(self, tmp_dir: Path) -> None:
        client, project = build_client(tmp_dir)
        with client:
            body = client.post(
                "/builds/plan", json={"path": str(project), "force": ["core"]}
            ).json()

        reasons = {u["name"]: u["reason"] for u in body["units"]}
        assert reasons["core"] == "forced"

    def test_forcing_an_unknown_unit_is_a_400(self, tmp_dir: Path) -> None:
        client, project = build_client(tmp_dir)
        with client:
            response = client.post(
                "/builds/plan", json={"path": str(project), "force": ["ghost"]}
            )

        assert response.status_code == 400


class TestExecute:
    """Running a build."""

    def test_a_build_starts_and_succeeds(self, tmp_dir: Path) -> None:
        client, project = build_client(tmp_dir, ok_process(), ok_process())
        with client:
            response = client.post("/builds", json={"path": str(project)})
            assert response.status_code == 202
            assert response.json()["status"] == "running"

            final = wait_for_build(client, response.json()["id"])

        assert final["status"] == "succeeded"
        assert sorted(final["rebuilt"]) == ["app", "core"]

    def test_a_failing_build_reports_its_diagnostics(self, tmp_dir: Path) -> None:
        client, project = build_client(tmp_dir, failing_process(GCC_OUTPUT))
        with client:
            build_id = client.post("/builds", json={"path": str(project)}).json()["id"]
            final = wait_for_build(client, build_id)

        assert final["status"] == "failed"
        assert final["ok"] is False
        units = {u["unit"]: u for u in final["units"]}
        assert units["core"]["diagnostics"][0]["file"] == "src/main.c"
        assert units["core"]["diagnostics"][0]["line"] == 12

    def test_a_broken_tool_is_reported_as_errored(self, tmp_dir: Path) -> None:
        """FR-4.4: a missing compiler is not a failing program."""
        from orchestrator.test_runner.execution import ProcessResult

        client, project = build_client(tmp_dir, ProcessResult(exit_code=127))
        with client:
            build_id = client.post("/builds", json={"path": str(project)}).json()["id"]
            final = wait_for_build(client, build_id)

        assert final["status"] == "errored"
        assert final["harness_problems"] == ["core"]

    def test_the_feedback_is_actionable(self, tmp_dir: Path) -> None:
        client, project = build_client(tmp_dir, failing_process(GCC_OUTPUT))
        with client:
            build_id = client.post("/builds", json={"path": str(project)}).json()["id"]
            final = wait_for_build(client, build_id)

        assert "src/main.c:12:5" in final["feedback"]

    def test_a_second_build_is_a_no_op(self, tmp_dir: Path) -> None:
        client, project = build_client(tmp_dir, ok_process(), ok_process())
        with client:
            first = client.post("/builds", json={"path": str(project)}).json()["id"]
            wait_for_build(client, first)

            second = client.post("/builds", json={"path": str(project)}).json()["id"]
            final = wait_for_build(client, second)

        assert final["rebuilt"] == []
        assert sorted(final["cached"]) == ["app", "core"]

    def test_builds_are_listed(self, tmp_dir: Path) -> None:
        client, project = build_client(tmp_dir, ok_process(), ok_process())
        with client:
            build_id = client.post("/builds", json={"path": str(project)}).json()["id"]
            wait_for_build(client, build_id)

            listed = client.get("/builds").json()

        assert [b["id"] for b in listed] == [build_id]

    def test_an_unknown_build_is_a_404(self, tmp_dir: Path) -> None:
        client, _ = build_client(tmp_dir)
        with client:
            assert client.get("/builds/nope").status_code == 404

    def test_the_cache_can_be_cleared(self, tmp_dir: Path) -> None:
        client, project = build_client(tmp_dir, ok_process(), ok_process())
        with client:
            build_id = client.post("/builds", json={"path": str(project)}).json()["id"]
            wait_for_build(client, build_id)

            assert client.delete("/builds/cache").json()["cleared"] == 2
            plan = client.post("/builds/plan", json={"path": str(project)}).json()

        assert plan["empty"] is False


class TestAgents:
    """What the agent layer is configured to do."""

    def test_roles_are_listed(self, client: TestClient) -> None:
        body = client.get("/agents/roles").json()
        roles = {entry["role"] for entry in body}

        assert {"worker", "planner", "reviewer"} <= roles

    def test_role_settings_are_resolved(self) -> None:
        config = OrchestratorConfig(
            providers={"anthropic": ProviderConfig(model="claude-opus-5")},
            roles={"planner": RoleConfig(effort=Effort.MAX)},
        )
        with TestClient(make_app(config=config)) as client:
            roles = {r["role"]: r for r in client.get("/agents/roles").json()}

        assert roles["planner"]["effort"] == "max"
        assert roles["worker"]["effort"] == "xhigh"
        assert roles["planner"]["model"] == "claude-opus-5"

    def test_budgets_are_reported(self, client: TestClient) -> None:
        entry = client.get("/agents/roles").json()[0]

        assert entry["budget_seconds"] > 0
        assert entry["budget_tokens"] > 0
        assert entry["budget_tool_calls"] > 0

    def test_no_vendor_appears_in_the_role_listing(self, client: TestClient) -> None:
        """The control plane describes settings, not a particular backend."""
        text = client.get("/agents/roles").text.lower()
        assert "anthropic" not in text

    def test_tools_are_listed_in_name_order(self, client: TestClient) -> None:
        names = [tool["name"] for tool in client.get("/agents/tools").json()]

        assert names == sorted(names)
        assert "write_file" in names

    def test_each_tool_carries_its_schema(self, client: TestClient) -> None:
        tools = {tool["name"]: tool for tool in client.get("/agents/tools").json()}

        assert tools["write_file"]["schema"]["type"] == "object"
        assert tools["write_file"]["description"]


class TestPromptPreview:
    """Rendering a prompt without calling a model."""

    def test_a_prompt_is_rendered(self, client: TestClient) -> None:
        body = client.post(
            "/agents/prompt", json={"title": "Add a greeting", "prompt": "make it nice"}
        ).json()

        assert body["role"] == "worker"
        assert any("Add a greeting" in block for block in body["blocks"])
        assert body["fingerprint"]

    def test_identical_inputs_fingerprint_identically(self, client: TestClient) -> None:
        """If this ever fails, prompt caching is silently not happening."""
        payload = {"title": "t", "prompt": "p"}
        one = client.post("/agents/prompt", json=payload).json()["fingerprint"]
        two = client.post("/agents/prompt", json=payload).json()["fingerprint"]

        assert one == two

    def test_a_different_prompt_fingerprints_differently(self, client: TestClient) -> None:
        one = client.post("/agents/prompt", json={"title": "t", "prompt": "a"}).json()
        two = client.post("/agents/prompt", json={"title": "t", "prompt": "b"}).json()

        assert one["fingerprint"] != two["fingerprint"]

    def test_the_role_changes_the_prompt(self, client: TestClient) -> None:
        worker = client.post(
            "/agents/prompt", json={"title": "t", "prompt": "p", "role": "worker"}
        ).json()
        reviewer = client.post(
            "/agents/prompt", json={"title": "t", "prompt": "p", "role": "reviewer"}
        ).json()

        assert worker["blocks"][0] != reviewer["blocks"][0]

    def test_feedback_reaches_the_opening_turn(self, client: TestClient) -> None:
        body = client.post(
            "/agents/prompt",
            json={"title": "t", "prompt": "p", "feedback": ["attempt 1: tests failed"]},
        ).json()

        assert any("attempt 1" in message for message in body["messages"])

    def test_feedback_stays_out_of_the_cached_prefix(self, client: TestClient) -> None:
        """It changes every attempt; in the prefix it would invalidate the cache."""
        payload = {"title": "t", "prompt": "p"}
        clean = client.post("/agents/prompt", json=payload).json()
        retried = client.post(
            "/agents/prompt", json={**payload, "feedback": ["attempt 1: failed"]}
        ).json()

        assert clean["fingerprint"] == retried["fingerprint"]
        assert not any("attempt 1" in block for block in retried["blocks"])

    def test_an_unknown_role_is_a_404(self, client: TestClient) -> None:
        response = client.post(
            "/agents/prompt", json={"title": "t", "prompt": "p", "role": "wizard"}
        )

        assert response.status_code == 404
        assert "wizard" in response.json()["error"]["message"]

    def test_no_identifier_leaks_into_the_prefix(self, client: TestClient) -> None:
        """A run identifier in a cached prefix invalidates it every time."""
        body = client.post("/agents/prompt", json={"title": "t", "prompt": "p"}).json()

        assert not any("task_" in block for block in body["blocks"])

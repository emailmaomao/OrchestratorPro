"""Tests for health, configuration, OpenAPI, and the error envelope."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from orchestrator.api.app import OPENAPI_TAGS, create_app
from orchestrator.api.state import API_VERSION, AppState, ApiError, Conflict, NotFound, NotSupported, error_body, status_code_for
from orchestrator.core.config import ConfigError, Effort, OrchestratorConfig, ProviderConfig, RoleConfig
from orchestrator.core.events import DomainValidationError, OrchestratorError

from tests.api.conftest import make_app, new_run


class TestHealth:
    """The endpoint a supervisor polls."""

    def test_a_healthy_server_says_ok(self, client: TestClient) -> None:
        body = client.get("/health").json()

        assert body["status"] == "ok"
        assert body["version"] == API_VERSION
        assert body["uptime_s"] >= 0

    def test_it_reports_the_database_it_is_using(self, client: TestClient) -> None:
        body = client.get("/health").json()

        assert body["database"] == ":memory:"
        assert body["schema_version"] >= 1

    def test_it_counts_runs(self, client: TestClient) -> None:
        assert client.get("/health").json()["runs"] == 0
        new_run(client)
        assert client.get("/health").json()["runs"] == 1

    def test_it_says_whether_it_can_execute(
        self, client: TestClient, bare_client: TestClient
    ) -> None:
        """A server that cannot run anything should say so before being asked."""
        assert client.get("/health").json()["execution_available"] is True
        assert bare_client.get("/health").json()["execution_available"] is False


class TestConfig:
    """The effective configuration, as resolved."""

    def test_defaults_are_reported(self, client: TestClient) -> None:
        body = client.get("/config").json()

        assert body["run"]["max_concurrency"] == 4
        assert body["api"]["host"] == "127.0.0.1"
        assert body["agent"]["adapter"] == "tool_loop"

    def test_provider_settings_are_reported(self) -> None:
        config = OrchestratorConfig(
            providers={"anthropic": ProviderConfig(model="claude-opus-5")}
        )
        with TestClient(make_app(config=config)) as client:
            body = client.get("/config").json()

        assert body["providers"]["anthropic"]["model"] == "claude-opus-5"
        assert body["providers"]["anthropic"]["thinking"] == "adaptive"

    def test_role_overrides_are_reported(self) -> None:
        config = OrchestratorConfig(roles={"planner": RoleConfig(effort=Effort.MAX)})
        with TestClient(make_app(config=config)) as client:
            body = client.get("/config").json()

        assert body["roles"]["planner"]["effort"] == "max"

    def test_no_credential_is_ever_present(self, client: TestClient) -> None:
        """Config rejects secrets at load time, so there is nothing to leak."""
        text = client.get("/config").text.lower()

        assert "api_key" not in text
        assert "token" not in text or "auth_token_env" in text

    def test_the_token_env_var_name_is_published_not_its_value(self) -> None:
        """The name is a pointer; the value is never read here."""
        from orchestrator.core.config import ApiConfig

        config = OrchestratorConfig(api=ApiConfig(auth_token_env="ORCH_TOKEN"))
        with TestClient(make_app(config=config)) as client:
            assert client.get("/config").json()["api"]["auth_token_env"] == "ORCH_TOKEN"


class TestOpenAPI:
    """The generated document is part of the deliverable."""

    def test_the_schema_is_served(self, client: TestClient) -> None:
        response = client.get("/openapi.json")

        assert response.status_code == 200
        assert response.json()["openapi"].startswith("3.")

    def test_it_carries_the_application_identity(self, client: TestClient) -> None:
        info = client.get("/openapi.json").json()["info"]

        assert info["title"] == "OrchestratorPro"
        assert info["version"] == API_VERSION
        assert "append-only event log" in info["description"]

    def test_every_endpoint_is_documented(self, client: TestClient) -> None:
        paths = client.get("/openapi.json").json()["paths"]

        for expected in (
            "/health",
            "/config",
            "/runs",
            "/runs/{run_id}",
            "/runs/{run_id}/tasks",
            "/workflows",
            "/workflows/{name}/runs",
            "/agents/roles",
            "/agents/tools",
            "/builds",
            "/builds/plan",
            "/events",
        ):
            assert expected in paths, f"{expected} is missing from the schema"

    def test_every_operation_has_a_summary(self, client: TestClient) -> None:
        """An endpoint with no summary is an endpoint nobody can find."""
        paths = client.get("/openapi.json").json()["paths"]
        missing = [
            f"{method.upper()} {path}"
            for path, operations in paths.items()
            for method, operation in operations.items()
            if not operation.get("summary")
        ]
        assert missing == []

    def test_every_operation_is_tagged(self, client: TestClient) -> None:
        paths = client.get("/openapi.json").json()["paths"]
        known = {tag["name"] for tag in OPENAPI_TAGS}
        for path, operations in paths.items():
            for method, operation in operations.items():
                assert set(operation.get("tags", [])) <= known, f"{method} {path}"

    def test_the_tags_are_described(self, client: TestClient) -> None:
        tags = {tag["name"]: tag for tag in client.get("/openapi.json").json()["tags"]}
        assert set(tags) == {t["name"] for t in OPENAPI_TAGS}
        assert all(tag["description"] for tag in tags.values())

    def test_the_docs_page_is_served(self, client: TestClient) -> None:
        assert client.get("/docs").status_code == 200

    def test_response_models_are_declared(self, client: TestClient) -> None:
        """A schema with untyped responses documents nothing useful."""
        operation = client.get("/openapi.json").json()["paths"]["/health"]["get"]
        content = operation["responses"]["200"]["content"]["application/json"]
        assert "$ref" in content["schema"]


class TestErrorEnvelope:
    """Every failure leaves through the same door."""

    def test_an_unknown_run_is_a_404_in_the_envelope(self, client: TestClient) -> None:
        from orchestrator.core.events import RunId

        response = client.get(f"/runs/{RunId.generate()}")

        assert response.status_code == 404
        error = response.json()["error"]
        assert error["code"] == "not_found"
        assert error["retryable"] is False
        assert error["message"]

    def test_a_malformed_identifier_is_a_400_not_a_404(self, client: TestClient) -> None:
        """It is not that the run is missing; the request could never name one."""
        response = client.get("/runs/not-an-identifier")

        assert response.status_code == 400
        assert response.json()["error"]["code"]

    def test_a_malformed_body_is_a_422_in_the_envelope(self, client: TestClient) -> None:
        response = client.post("/runs", json={})

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_request"

    def test_validation_detail_names_the_field(self, client: TestClient) -> None:
        response = client.post("/runs", json={"goal": ""})
        errors = response.json()["error"]["detail"]["errors"]

        assert any("goal" in error["location"] for error in errors)

    def test_an_unknown_field_is_refused(self, client: TestClient) -> None:
        """A client that sends max_attemps should be told, not defaulted."""
        run_id = new_run(client)
        response = client.post(
            f"/runs/{run_id}/tasks",
            json={"title": "t", "prompt": "p", "max_attemps": 9},
        )
        assert response.status_code == 422

    def test_validation_errors_do_not_echo_the_body(self, client: TestClient) -> None:
        """Reflecting a rejected body is a way to serve content nobody accepted."""
        response = client.post("/runs", json={"goal": "<script>alert(1)</script>x" * 400})
        assert "<script>" not in response.text

    def test_a_domain_rule_violation_is_a_400(self, client: TestClient) -> None:
        response = client.post(
            "/workflows",
            json={
                "name": "wf",
                "goal": "g",
                "steps": [{"name": "a", "prompt": "p", "depends_on": ["ghost"]}],
            },
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"]

    def test_execution_without_a_backend_is_a_503(self, bare_client: TestClient) -> None:
        """The request was fine; the server is not equipped. That is not a 500."""
        bare_client.post(
            "/workflows",
            json={"name": "wf", "goal": "g", "steps": [{"name": "a", "prompt": "p"}]},
        )
        response = bare_client.post("/workflows/wf/runs", json={})

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "not_supported"
        assert response.json()["error"]["retryable"] is True


class TestStatusMapping:
    """Which exception becomes which status."""

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (NotFound("gone"), 404),
            (Conflict("no"), 409),
            (NotSupported("nope"), 503),
            (ApiError("bad"), 400),
            (DomainValidationError("bad"), 400),
            (ConfigError("bad"), 400),
            (OrchestratorError("boom"), 500),
            (RuntimeError("boom"), 500),
        ],
    )
    def test_mapping(self, error: Exception, expected: int) -> None:
        assert status_code_for(error) == expected

    def test_the_body_carries_the_stable_code(self) -> None:
        body = error_body(NotFound("gone", detail={"id": "x"}))

        assert body["code"] == "not_found"
        assert body["detail"] == {"id": "x"}

    def test_a_foreign_exception_is_rendered_safely(self) -> None:
        body = error_body(RuntimeError("boom"))

        assert body["code"] == "internal"
        assert body["retryable"] is False


class TestApplicationAssembly:
    """How the application is composed."""

    def test_two_applications_do_not_share_state(self) -> None:
        """One process, two servers, two databases — no globals."""
        with TestClient(make_app()) as one, TestClient(make_app()) as two:
            new_run(one)
            assert one.get("/health").json()["runs"] == 1
            assert two.get("/health").json()["runs"] == 0

    def test_the_state_is_reachable_on_the_app(self) -> None:
        app = make_app()
        assert isinstance(app.state.orchestrator, AppState)

    def test_a_supplied_workspace_root_is_kept(self, tmp_dir: Path) -> None:
        app = create_app(workspace_root=tmp_dir)
        assert app.state.orchestrator.workspace_root == tmp_dir

    def test_shutdown_releases_subscribers(self) -> None:
        app = make_app()
        state: AppState = app.state.orchestrator
        with TestClient(app) as client:
            client.get("/health")
        assert state.broker.subscribers == 0

    def test_a_default_application_needs_no_arguments(self) -> None:
        with TestClient(create_app()) as client:
            assert client.get("/health").status_code == 200


def test_only_the_api_layer_imports_a_web_framework() -> None:
    """Everything below L4 stays on the standard library.

    Not tidiness: the scheduler, the agent runtime, and the builder are meant to
    be usable from a script or a test with nothing installed. One convenient
    ``from pydantic import`` in a domain module quietly ends that.

    ``api`` and ``dashboard`` are the web layers themselves, and ``ops`` hardens
    them; all three are exempt by definition. ``cli.py`` is the entry point that
    composes them, so it is exempt too — and it is the only file at the package
    root that is.
    """
    import ast

    import orchestrator

    root = Path(orchestrator.__path__[0])
    web_layers = {"api", "dashboard", "ops"}
    exempt_files = {"cli.py"}
    forbidden = ("fastapi", "starlette", "uvicorn", "pydantic", "httpx")
    offenders: list[str] = []

    for path in sorted(root.rglob("*.py")):
        if path.parent.name in web_layers or path.name in exempt_files:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            offenders.extend(
                f"{path.relative_to(root)}: {name}"
                for name in names
                if name.split(".")[0] in forbidden
            )

    assert offenders == [], f"web dependencies leaked below the API layer: {offenders}"

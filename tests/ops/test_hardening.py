"""Tests for security hardening and request logging."""

from __future__ import annotations

import json
import logging
from io import StringIO

import pytest
from fastapi.testclient import TestClient

from orchestrator.core.config import (
    ApiConfig,
    ConfigError,
    OrchestratorConfig,
    RunConfig,
    SecurityConfig,
)
from orchestrator.core.logging import configure_logging
from orchestrator.ops.hardening import (
    REQUEST_ID_HEADER,
    SECURITY_HEADERS,
    verify_deployment,
)

from tests.ops.conftest import hardened_app


class TestSecurityHeaders:
    """What every response carries."""

    def test_the_headers_are_present(self, client: TestClient) -> None:
        headers = client.get("/health").headers

        for name in SECURITY_HEADERS:
            assert name in headers, f"{name} is missing"

    def test_framing_is_refused(self, client: TestClient) -> None:
        """A control plane that can be framed can be clickjacked."""
        headers = client.get("/health").headers

        assert headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in headers["content-security-policy"]

    def test_content_sniffing_is_refused(self, client: TestClient) -> None:
        assert client.get("/health").headers["x-content-type-options"] == "nosniff"

    def test_the_policy_allows_no_remote_origin(self, client: TestClient) -> None:
        policy = client.get("/health").headers["content-security-policy"]

        assert "default-src 'self'" in policy
        assert "object-src 'none'" in policy
        assert "unsafe-inline" not in policy

    def test_they_are_on_errors_too(self, client: TestClient) -> None:
        """A 404 is served to the same browser as a 200."""
        response = client.get("/nope")

        assert response.status_code == 404
        assert response.headers["x-frame-options"] == "DENY"

    def test_they_can_be_turned_off(self) -> None:
        with TestClient(hardened_app(SecurityConfig(security_headers=False))) as client:
            assert "content-security-policy" not in client.get("/health").headers

    def test_the_dashboard_still_loads_under_the_policy(self) -> None:
        """A policy that breaks the UI it protects is a policy that gets removed."""
        from orchestrator.api.state import AppState
        from orchestrator.dashboard.app import create_app as create_dashboard
        from orchestrator.ops.hardening import harden

        app = harden(create_dashboard(state=AppState.in_memory()), SecurityConfig())
        with TestClient(app) as client:
            shell = client.get("/ui/")
            module = client.get("/ui/assets/js/app.js")

        assert shell.status_code == 200
        assert module.status_code == 200
        # Nothing inline, so the policy needs no exemption.
        assert "<script>" not in shell.text


class TestBodyLimit:
    """How much a client may send."""

    def test_a_normal_body_passes(self, client: TestClient) -> None:
        assert client.post("/runs", json={"goal": "small"}).status_code == 201

    def test_an_oversized_body_is_refused(self) -> None:
        with TestClient(hardened_app(SecurityConfig(max_body_bytes=2048))) as client:
            response = client.post("/runs", json={"goal": "x" * 4000})

        assert response.status_code == 413
        assert response.json()["error"]["code"] == "body_too_large"

    def test_the_refusal_names_the_limit(self) -> None:
        with TestClient(hardened_app(SecurityConfig(max_body_bytes=2048))) as client:
            body = client.post("/runs", json={"goal": "x" * 4000}).json()

        assert body["error"]["detail"]["limit"] == 2048

    def test_it_is_refused_before_the_handler_sees_it(self) -> None:
        """Rejecting after buffering defeats the purpose of a limit."""
        with TestClient(hardened_app(SecurityConfig(max_body_bytes=2048))) as client:
            client.post("/runs", json={"goal": "x" * 4000})
            assert client.get("/runs").json() == []

    def test_a_body_at_the_limit_passes(self) -> None:
        with TestClient(hardened_app(SecurityConfig(max_body_bytes=4096))) as client:
            assert client.post("/runs", json={"goal": "x" * 500}).status_code == 201

    def test_a_get_is_unaffected(self) -> None:
        with TestClient(hardened_app(SecurityConfig(max_body_bytes=1024))) as client:
            assert client.get("/health").status_code == 200


class TestTrustedHosts:
    """Which Host headers the server answers to."""

    def test_an_unlisted_host_is_refused(self) -> None:
        app = hardened_app(SecurityConfig(allowed_hosts=("orchestrator.internal",)))
        with TestClient(app, base_url="http://elsewhere.example") as client:
            assert client.get("/health").status_code == 400

    def test_a_listed_host_is_served(self) -> None:
        app = hardened_app(SecurityConfig(allowed_hosts=("orchestrator.internal",)))
        with TestClient(app, base_url="http://orchestrator.internal") as client:
            assert client.get("/health").status_code == 200

    def test_no_list_means_any_host(self, client: TestClient) -> None:
        """Only tolerable on loopback, which verify_deployment enforces."""
        assert client.get("/health").status_code == 200


class TestCors:
    """Who may call from a browser."""

    def test_cross_origin_is_refused_by_default(self, client: TestClient) -> None:
        response = client.get("/health", headers={"origin": "http://evil.example"})

        assert "access-control-allow-origin" not in response.headers

    def test_a_named_origin_is_allowed(self) -> None:
        app = hardened_app(SecurityConfig(cors_origins=("http://tools.internal",)))
        with TestClient(app) as client:
            response = client.get("/health", headers={"origin": "http://tools.internal"})

        assert response.headers["access-control-allow-origin"] == "http://tools.internal"

    def test_an_unnamed_origin_is_still_refused(self) -> None:
        app = hardened_app(SecurityConfig(cors_origins=("http://tools.internal",)))
        with TestClient(app) as client:
            response = client.get("/health", headers={"origin": "http://evil.example"})

        assert response.headers.get("access-control-allow-origin") != "http://evil.example"

    def test_a_wildcard_origin_is_refused_in_configuration(self) -> None:
        """On an unauthenticated API it lets any page drive the agents."""
        with pytest.raises(ConfigError, match="must not contain"):
            SecurityConfig(cors_origins=("*",))


class TestRequestContext:
    """Correlation and the access log."""

    def test_every_response_carries_a_request_id(self, client: TestClient) -> None:
        assert client.get("/health").headers[REQUEST_ID_HEADER]

    def test_two_requests_get_different_ids(self, client: TestClient) -> None:
        first = client.get("/health").headers[REQUEST_ID_HEADER]
        second = client.get("/health").headers[REQUEST_ID_HEADER]

        assert first != second

    def test_a_supplied_id_is_echoed(self, client: TestClient) -> None:
        """So a request can be traced across a proxy that already tagged it."""
        response = client.get("/health", headers={REQUEST_ID_HEADER: "trace-1234"})

        assert response.headers[REQUEST_ID_HEADER] == "trace-1234"

    def test_a_supplied_id_is_bounded(self, client: TestClient) -> None:
        """An unbounded header would put anything a caller likes in the log."""
        response = client.get("/health", headers={REQUEST_ID_HEADER: "x" * 500})

        assert len(response.headers[REQUEST_ID_HEADER]) <= 64

    def test_requests_are_logged_as_json(self) -> None:
        stream = StringIO()
        configure_logging(level=logging.INFO, stream=stream, json_output=True)
        try:
            with TestClient(hardened_app()) as client:
                client.get("/runs")
        finally:
            configure_logging(level=logging.WARNING, stream=StringIO())

        lines = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
        requests = [line for line in lines if line.get("message") == "request"]

        assert requests, "no request was logged"
        assert requests[0]["path"] == "/runs"
        assert requests[0]["status"] == 200
        assert requests[0]["duration_ms"] >= 0

    def test_the_log_line_carries_the_request_id(self) -> None:
        stream = StringIO()
        configure_logging(level=logging.INFO, stream=stream, json_output=True)
        try:
            with TestClient(hardened_app()) as client:
                client.get("/runs", headers={REQUEST_ID_HEADER: "trace-abc"})
        finally:
            configure_logging(level=logging.WARNING, stream=StringIO())

        lines = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
        requests = [line for line in lines if line.get("message") == "request"]

        assert requests[0]["request_id"] == "trace-abc"

    def test_health_checks_are_not_logged(self) -> None:
        """A log that is 90% health checks is a log nobody reads."""
        stream = StringIO()
        configure_logging(level=logging.INFO, stream=stream, json_output=True)
        try:
            with TestClient(hardened_app()) as client:
                client.get("/health")
        finally:
            configure_logging(level=logging.WARNING, stream=StringIO())

        lines = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
        assert not [line for line in lines if line.get("path") == "/health"]

    def test_logging_can_be_turned_off(self) -> None:
        stream = StringIO()
        configure_logging(level=logging.INFO, stream=stream, json_output=True)
        try:
            with TestClient(hardened_app(SecurityConfig(request_log=False))) as client:
                client.get("/runs")
        finally:
            configure_logging(level=logging.WARNING, stream=StringIO())

        lines = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
        assert not [line for line in lines if line.get("message") == "request"]

    def test_a_failed_request_is_still_logged(self) -> None:
        stream = StringIO()
        configure_logging(level=logging.INFO, stream=stream, json_output=True)
        try:
            with TestClient(hardened_app()) as client:
                client.get("/runs/not-an-id")
        finally:
            configure_logging(level=logging.WARNING, stream=StringIO())

        lines = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
        requests = [line for line in lines if line.get("message") == "request"]

        assert requests and requests[0]["status"] == 400


class TestVerifyDeployment:
    """The checks a deployment gets before it starts."""

    def test_a_loopback_default_is_clean(self) -> None:
        assert verify_deployment(OrchestratorConfig(), env={}) == ()

    def test_a_public_bind_without_a_token_is_refused(self) -> None:
        config = OrchestratorConfig(api=ApiConfig(host="0.0.0.0"))

        with pytest.raises(ConfigError, match="auth_token_env"):
            verify_deployment(config, env={})

    def test_a_public_bind_without_allowed_hosts_is_refused(self) -> None:
        """A server answering any Host answers a rebinding attack."""
        config = OrchestratorConfig(api=ApiConfig(host="0.0.0.0", auth_token_env="TOK"))

        with pytest.raises(ConfigError, match="allowed_hosts"):
            verify_deployment(config, env={"TOK": "x"})

    def test_a_public_bind_without_rate_limiting_is_refused(self) -> None:
        config = OrchestratorConfig(
            api=ApiConfig(host="0.0.0.0", auth_token_env="TOK"),
            security=SecurityConfig(allowed_hosts=("host",), rate_limit_per_minute=0),
        )

        with pytest.raises(ConfigError, match="rate limiting"):
            verify_deployment(config, env={"TOK": "x"})

    def test_a_fully_configured_public_bind_is_accepted(self) -> None:
        config = OrchestratorConfig(
            api=ApiConfig(host="0.0.0.0", auth_token_env="TOK"),
            security=SecurityConfig(allowed_hosts=("host",)),
        )

        assert verify_deployment(config, env={"TOK": "x"}) == ()

    def test_auto_approve_is_a_high_warning(self) -> None:
        config = OrchestratorConfig(run=RunConfig(auto_approve=True))
        warnings = verify_deployment(config, env={})

        assert warnings[0].code == "auto_approve"
        assert warnings[0].severity == "high"

    def test_cors_is_a_high_warning(self) -> None:
        config = OrchestratorConfig(
            security=SecurityConfig(cors_origins=("http://tools.internal",))
        )
        assert verify_deployment(config, env={})[0].code == "cors_enabled"

    def test_turning_off_a_control_warns(self) -> None:
        config = OrchestratorConfig(
            security=SecurityConfig(
                security_headers=False, rate_limit_per_minute=0, request_log=False
            )
        )
        codes = {warning.code for warning in verify_deployment(config, env={})}

        assert codes == {"headers_disabled", "no_rate_limit", "no_request_log"}

    def test_warnings_are_ordered_by_severity(self) -> None:
        config = OrchestratorConfig(
            run=RunConfig(auto_approve=True),
            security=SecurityConfig(security_headers=False),
        )
        warnings = verify_deployment(config, env={})

        assert warnings[0].severity == "high"

    def test_a_warning_reads_as_a_sentence(self) -> None:
        config = OrchestratorConfig(run=RunConfig(auto_approve=True))
        assert "auto_approve" in str(verify_deployment(config, env={})[0])


class TestSecurityConfig:
    """The settings themselves."""

    def test_the_defaults_are_the_safe_ones(self) -> None:
        settings = SecurityConfig()

        assert settings.cors_origins == ()
        assert settings.security_headers is True
        assert settings.rate_limiting is True
        assert settings.request_log is True

    def test_an_absurd_body_limit_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="at least 1024"):
            SecurityConfig(max_body_bytes=10)

    def test_a_negative_rate_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="must not be negative"):
            SecurityConfig(rate_limit_per_minute=-1)

    def test_rate_limiting_can_be_turned_off(self) -> None:
        assert SecurityConfig(rate_limit_per_minute=0).rate_limiting is False

    def test_a_zero_burst_with_limiting_on_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="rate_limit_burst"):
            SecurityConfig(rate_limit_per_minute=60, rate_limit_burst=0)

    def test_hosts_parse_from_a_comma_separated_string(self) -> None:
        """Because an environment variable cannot hold a list."""
        config = OrchestratorConfig.from_mapping(
            {"security": {"allowed_hosts": "a.example, b.example"}}, env={}
        )
        assert config.security.allowed_hosts == ("a.example", "b.example")

    def test_hosts_parse_from_a_list(self) -> None:
        config = OrchestratorConfig.from_mapping(
            {"security": {"allowed_hosts": ["a.example"]}}, env={}
        )
        assert config.security.allowed_hosts == ("a.example",)

    def test_an_unknown_key_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="unknown key"):
            OrchestratorConfig.from_mapping({"security": {"ratelimit": 5}}, env={})

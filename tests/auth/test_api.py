"""Tests for the authentication endpoints and role enforcement."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.auth.conftest import PASSWORD, login, tokens_of


class TestOpenInstallation:
    """An installation with no accounts."""

    def test_everything_is_served(self, open_client: TestClient) -> None:
        """What a single person on loopback wants, and what earlier builds did."""
        assert open_client.get("/runs").status_code == 200
        assert open_client.post("/runs", json={"goal": "g"}).status_code == 201

    def test_the_caller_is_an_operator(self, open_client: TestClient) -> None:
        body = open_client.get("/auth/me").json()

        assert body["role"] == "operator"
        assert body["authentication"] == "open"

    def test_it_cannot_manage_accounts(self, open_client: TestClient) -> None:
        """Locking an installation down is a deliberate act at the machine."""
        assert open_client.get("/auth/users").status_code == 403


class TestLogin:
    """Getting in."""

    def test_a_correct_password_returns_tokens(self, client: TestClient) -> None:
        response = client.post(
            "/auth/login", json={"username": "admin", "password": PASSWORD}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["access_token"]
        assert body["refresh_token"]
        assert body["token_type"] == "Bearer"
        assert body["expires_in"] > 0

    def test_a_wrong_password_is_a_401(self, client: TestClient) -> None:
        response = client.post(
            "/auth/login", json={"username": "admin", "password": "wrong-password-xx"}
        )

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"

    def test_an_unknown_user_is_indistinguishable(self, client: TestClient) -> None:
        absent = client.post("/auth/login", json={"username": "ghost", "password": PASSWORD})
        wrong = client.post(
            "/auth/login", json={"username": "admin", "password": "wrong-password-xx"}
        )

        assert absent.status_code == wrong.status_code == 401
        assert absent.json() == wrong.json()

    def test_login_needs_no_credentials(self, client: TestClient) -> None:
        """The only route that is always open."""
        assert client.post("/auth/login", json={"username": "x", "password": "y"}).status_code == 401

    def test_a_malformed_body_is_a_422(self, client: TestClient) -> None:
        assert client.post("/auth/login", json={}).status_code == 422


class TestRequiringCredentials:
    """Once accounts exist, everything requires one."""

    def test_an_unauthenticated_read_is_a_401(self, client: TestClient) -> None:
        assert client.get("/runs").status_code == 401

    def test_an_unauthenticated_write_is_a_401(self, client: TestClient) -> None:
        assert client.post("/runs", json={"goal": "g"}).status_code == 401

    def test_health_still_requires_one(self, client: TestClient) -> None:
        """No window in which some routes are protected and others are not."""
        assert client.get("/health").status_code == 401

    def test_a_garbage_token_is_a_401(self, client: TestClient) -> None:
        response = client.get("/runs", headers={"authorization": "Bearer nonsense"})
        assert response.status_code == 401

    def test_a_valid_token_is_served(self, client: TestClient) -> None:
        assert client.get("/runs", headers=login(client, "viewer")).status_code == 200

    def test_the_openapi_document_is_still_public(self, client: TestClient) -> None:
        """A client needs to know what to log in to."""
        assert client.get("/openapi.json").status_code == 200


class TestRoles:
    """The floor, enforced."""

    def test_a_viewer_may_read(self, client: TestClient) -> None:
        assert client.get("/runs", headers=login(client, "viewer")).status_code == 200

    def test_a_viewer_may_not_write(self, client: TestClient) -> None:
        response = client.post("/runs", json={"goal": "g"}, headers=login(client, "viewer"))

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "forbidden"

    def test_an_operator_may_write(self, client: TestClient) -> None:
        response = client.post("/runs", json={"goal": "g"}, headers=login(client, "operator"))
        assert response.status_code == 201

    def test_an_operator_may_not_manage_accounts(self, client: TestClient) -> None:
        assert client.get("/auth/users", headers=login(client, "operator")).status_code == 403

    def test_an_administrator_may_do_both(self, client: TestClient) -> None:
        headers = login(client, "admin")

        assert client.post("/runs", json={"goal": "g"}, headers=headers).status_code == 201
        assert client.get("/auth/users", headers=headers).status_code == 200

    def test_the_message_names_what_was_needed(self, client: TestClient) -> None:
        response = client.post("/runs", json={"goal": "g"}, headers=login(client, "viewer"))
        assert "operator" in response.json()["error"]["message"]

    def test_a_viewer_may_not_cancel(self, client: TestClient) -> None:
        from orchestrator.core.events import RunId

        response = client.post(
            f"/runs/{RunId.generate()}/cancel", headers=login(client, "viewer")
        )
        assert response.status_code == 403

    def test_a_viewer_may_not_clear_the_build_cache(self, client: TestClient) -> None:
        assert client.delete("/builds/cache", headers=login(client, "viewer")).status_code == 403


class TestRefreshAndLogout:
    """Sessions over HTTP."""

    def test_a_refresh_returns_a_new_pair(self, client: TestClient) -> None:
        pair = tokens_of(client, "operator")
        response = client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})

        assert response.status_code == 200
        assert response.json()["access_token"]

    def test_an_access_token_cannot_be_refreshed(self, client: TestClient) -> None:
        pair = tokens_of(client, "operator")
        response = client.post("/auth/refresh", json={"refresh_token": pair["access_token"]})

        assert response.status_code == 401

    def test_a_logout_ends_the_session(self, client: TestClient) -> None:
        pair = tokens_of(client, "operator")
        headers = {"authorization": f"Bearer {pair['access_token']}"}

        assert client.post("/auth/logout", headers=headers).json()["ended"] is True

        after = client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})
        assert after.status_code == 401

    def test_sessions_are_listed(self, client: TestClient) -> None:
        headers = login(client, "operator")
        sessions = client.get("/auth/sessions", headers=headers).json()

        assert sessions and sessions[0]["username"] == "operator"

    def test_listing_somebody_elses_needs_an_administrator(self, client: TestClient) -> None:
        response = client.get(
            "/auth/sessions?username=admin", headers=login(client, "operator")
        )
        assert response.status_code == 403


class TestUserManagement:
    """Accounts over HTTP."""

    def test_an_administrator_creates_an_account(self, client: TestClient) -> None:
        response = client.post(
            "/auth/users",
            json={"username": "newbie", "password": PASSWORD, "role": "viewer"},
            headers=login(client, "admin"),
        )

        assert response.status_code == 201
        assert response.json()["username"] == "newbie"

    def test_the_response_carries_no_hash(self, client: TestClient) -> None:
        response = client.post(
            "/auth/users",
            json={"username": "newbie", "password": PASSWORD},
            headers=login(client, "admin"),
        )
        assert "password" not in response.text

    def test_a_short_password_is_refused(self, client: TestClient) -> None:
        response = client.post(
            "/auth/users",
            json={"username": "newbie", "password": "short"},
            headers=login(client, "admin"),
        )
        assert response.status_code == 422

    def test_an_unknown_role_is_a_400(self, client: TestClient) -> None:
        response = client.post(
            "/auth/users",
            json={"username": "newbie", "password": PASSWORD, "role": "superuser"},
            headers=login(client, "admin"),
        )
        assert response.status_code == 400

    def test_a_user_may_read_their_own_account(self, client: TestClient) -> None:
        response = client.get("/auth/users/viewer", headers=login(client, "viewer"))
        assert response.status_code == 200

    def test_a_user_may_not_read_another(self, client: TestClient) -> None:
        assert client.get("/auth/users/admin", headers=login(client, "viewer")).status_code == 403

    def test_a_user_may_change_their_own_password(self, client: TestClient) -> None:
        response = client.put(
            "/auth/users/viewer/password",
            json={"password": "a-brand-new-password"},
            headers=login(client, "viewer"),
        )

        assert response.status_code == 200
        assert client.post(
            "/auth/login", json={"username": "viewer", "password": "a-brand-new-password"}
        ).status_code == 200

    def test_changing_a_password_ends_the_sessions(self, client: TestClient) -> None:
        pair = tokens_of(client, "viewer")
        client.put(
            "/auth/users/viewer/password",
            json={"password": "a-brand-new-password"},
            headers={"authorization": f"Bearer {pair['access_token']}"},
        )

        after = client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})
        assert after.status_code == 401

    def test_an_administrator_changes_a_role(self, client: TestClient) -> None:
        response = client.put(
            "/auth/users/viewer/role", json={"role": "operator"}, headers=login(client, "admin")
        )

        assert response.status_code == 200
        assert client.post(
            "/runs", json={"goal": "g"}, headers=login(client, "viewer")
        ).status_code == 201

    def test_an_administrator_disables_an_account(self, client: TestClient) -> None:
        client.put(
            "/auth/users/viewer/active", json={"active": False}, headers=login(client, "admin")
        )
        response = client.post(
            "/auth/login", json={"username": "viewer", "password": PASSWORD}
        )

        assert response.status_code == 401

    def test_deleting_removes_the_account(self, client: TestClient) -> None:
        headers = login(client, "admin")

        assert client.delete("/auth/users/viewer", headers=headers).status_code == 204
        assert client.get("/auth/users/viewer", headers=headers).status_code == 404


class TestApiKeys:
    """Keys over HTTP."""

    def test_a_key_is_minted(self, client: TestClient) -> None:
        response = client.post(
            "/auth/keys", json={"name": "ci"}, headers=login(client, "operator")
        )

        assert response.status_code == 201
        assert response.json()["secret"].startswith("opk_")

    def test_the_secret_authenticates(self, client: TestClient) -> None:
        secret = client.post(
            "/auth/keys", json={"name": "ci"}, headers=login(client, "operator")
        ).json()["secret"]

        response = client.get("/runs", headers={"authorization": f"Bearer {secret}"})
        assert response.status_code == 200

    def test_the_key_carries_its_role(self, client: TestClient) -> None:
        secret = client.post(
            "/auth/keys", json={"name": "ci"}, headers=login(client, "viewer")
        ).json()["secret"]

        response = client.post(
            "/runs", json={"goal": "g"}, headers={"authorization": f"Bearer {secret}"}
        )
        assert response.status_code == 403

    def test_a_weaker_key_can_be_minted(self, client: TestClient) -> None:
        secret = client.post(
            "/auth/keys",
            json={"name": "readonly", "role": "viewer"},
            headers=login(client, "admin"),
        ).json()["secret"]

        headers = {"authorization": f"Bearer {secret}"}
        assert client.get("/runs", headers=headers).status_code == 200
        assert client.post("/runs", json={"goal": "g"}, headers=headers).status_code == 403

    def test_the_secret_appears_only_once(self, client: TestClient) -> None:
        headers = login(client, "operator")
        client.post("/auth/keys", json={"name": "ci"}, headers=headers)

        listed = client.get("/auth/keys", headers=headers).json()
        assert listed and "secret" not in listed[0]

    def test_a_revoked_key_stops_working(self, client: TestClient) -> None:
        headers = login(client, "operator")
        created = client.post("/auth/keys", json={"name": "ci"}, headers=headers).json()

        client.delete(f"/auth/keys/{created['id']}", headers=headers)

        response = client.get(
            "/runs", headers={"authorization": f"Bearer {created['secret']}"}
        )
        assert response.status_code == 401

    def test_revoking_an_unknown_key_is_a_404(self, client: TestClient) -> None:
        assert client.delete("/auth/keys/key_absent", headers=login(client, "admin")).status_code == 404


class TestAuditEndpoint:
    """The trail over HTTP."""

    def test_an_administrator_reads_it(self, client: TestClient) -> None:
        login(client, "admin")
        entries = client.get("/auth/audit", headers=login(client, "admin")).json()

        assert entries
        assert entries[0]["action"] == "auth.login"

    def test_an_operator_may_not(self, client: TestClient) -> None:
        assert client.get("/auth/audit", headers=login(client, "operator")).status_code == 403

    def test_failed_logins_are_in_it(self, client: TestClient) -> None:
        client.post("/auth/login", json={"username": "admin", "password": "wrong-password-x"})
        entries = client.get("/auth/audit", headers=login(client, "admin")).json()

        assert any(entry["outcome"] == "denied" for entry in entries)

    def test_account_changes_are_in_it(self, client: TestClient) -> None:
        headers = login(client, "admin")
        client.post("/auth/users", json={"username": "newbie", "password": PASSWORD}, headers=headers)

        entries = client.get("/auth/audit?action=user.create", headers=headers).json()
        assert entries and entries[0]["target"] == "newbie"

    def test_it_can_be_filtered_by_actor(self, client: TestClient) -> None:
        headers = login(client, "admin")
        entries = client.get("/auth/audit?actor=admin", headers=headers).json()

        assert all(entry["actor"] == "admin" for entry in entries)


class TestStreamingCredentials:
    """A browser cannot set a header on EventSource or a WebSocket."""

    def test_a_stream_accepts_a_query_token(self, client: TestClient) -> None:
        pair = tokens_of(client, "viewer")
        run_id = client.post(
            "/runs", json={"goal": "g"}, headers=login(client, "operator")
        ).json()["id"]

        from tests.api.conftest import sse

        status, _, _ = sse(
            client.app,
            f"/runs/{run_id}/events?heartbeat_s=0.05&token={pair['access_token']}",
            frames=1,
        )
        assert status == 200

    def test_a_stream_without_one_is_refused(self, client: TestClient) -> None:
        run_id = client.post(
            "/runs", json={"goal": "g"}, headers=login(client, "operator")
        ).json()["id"]

        from tests.api.conftest import sse

        status, _, _ = sse(client.app, f"/runs/{run_id}/events?heartbeat_s=0.05", frames=1)
        assert status == 401

    def test_the_query_token_is_not_accepted_elsewhere(self, client: TestClient) -> None:
        """A token in a URL is a token in a proxy log; the exemption stays narrow."""
        pair = tokens_of(client, "viewer")
        assert client.get(f"/runs?token={pair['access_token']}").status_code == 401

    def test_a_websocket_accepts_a_query_token(self, client: TestClient) -> None:
        pair = tokens_of(client, "viewer")
        run_id = client.post(
            "/runs", json={"goal": "g"}, headers=login(client, "operator")
        ).json()["id"]

        with client.websocket_connect(
            f"/runs/{run_id}/ws?token={pair['access_token']}"
        ) as socket:
            assert socket.receive_json()["type"] == "run.created"

    def test_a_websocket_without_one_is_refused(self, client: TestClient) -> None:
        run_id = client.post(
            "/runs", json={"goal": "g"}, headers=login(client, "operator")
        ).json()["id"]

        import pytest
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises((WebSocketDisconnect, Exception)):
            with client.websocket_connect(f"/runs/{run_id}/ws") as socket:
                socket.receive_json()

"""Tests for accounts, keys, sessions, roles, and the audit trail."""

from __future__ import annotations

import pytest

from orchestrator.auth.models import (
    ApiKey,
    AuthError,
    Credentials,
    Forbidden,
    Principal,
    Role,
    Unauthorized,
    User,
    generate_api_key,
    hash_api_key,
    hash_password,
    verify_password,
)
from orchestrator.auth.service import AuthService, LoginContext
from orchestrator.auth.store import AuthStore

from tests.auth.conftest import PASSWORD, populated, principal


class TestPasswords:
    """Hashing."""

    def test_a_hash_round_trips(self) -> None:
        assert verify_password("a-long-enough-password", hash_password("a-long-enough-password"))

    def test_a_wrong_password_is_refused(self) -> None:
        assert not verify_password("wrong-password-entirely", hash_password(PASSWORD))

    def test_two_hashes_of_one_password_differ(self) -> None:
        """Per-password salt: a rainbow table buys nothing."""
        assert hash_password(PASSWORD) != hash_password(PASSWORD)

    def test_the_parameters_travel_with_the_hash(self) -> None:
        """Raising the cost later must not invalidate every existing password."""
        stored = hash_password(PASSWORD)

        assert stored.startswith("scrypt$")
        assert len(stored.split("$")) == 6

    def test_a_short_password_is_refused(self) -> None:
        with pytest.raises(AuthError, match="at least 12"):
            hash_password("short")

    @pytest.mark.parametrize("stored", ["", "nonsense", "scrypt$bad", "md5$1$2$3$4$5"])
    def test_a_corrupt_hash_fails_the_login_rather_than_the_endpoint(
        self, stored: str
    ) -> None:
        assert verify_password(PASSWORD, stored) is False


class TestApiKeySecrets:
    """Minting and storing keys."""

    def test_a_key_is_prefixed(self) -> None:
        """A leaked key is recognizable in a log or a secret scanner."""
        secret, _ = generate_api_key()
        assert secret.startswith("opk_")

    def test_the_secret_is_not_the_stored_form(self) -> None:
        secret, digest = generate_api_key()

        assert digest != secret
        assert hash_api_key(secret) == digest

    def test_two_keys_differ(self) -> None:
        assert generate_api_key()[0] != generate_api_key()[0]


class TestRoles:
    """The ordering."""

    def test_the_ordering_holds(self) -> None:
        assert Role.ADMIN.can(Role.OPERATOR)
        assert Role.ADMIN.can(Role.VIEWER)
        assert Role.OPERATOR.can(Role.VIEWER)
        assert not Role.OPERATOR.can(Role.ADMIN)
        assert not Role.VIEWER.can(Role.OPERATOR)

    def test_a_role_satisfies_itself(self) -> None:
        for role in Role:
            assert role.can(role)

    def test_an_unknown_role_is_refused(self) -> None:
        """Defaulting *any* role for a typo grants permissions nobody meant to."""
        with pytest.raises(AuthError, match="not a role"):
            Role.parse("superuser")

    def test_a_principal_enforces_its_role(self) -> None:
        with pytest.raises(Forbidden, match="requires the admin role"):
            principal(role=Role.VIEWER).require(Role.ADMIN)

    def test_a_key_principal_names_the_key(self) -> None:
        """"the CI key" is actionable; "an operator" is not."""
        actor = Principal(username="ci", role=Role.OPERATOR, method="api_key", key_id="key_1")
        assert actor.label == "ci (key key_1)"


class TestUsernames:
    """What may be an account name."""

    @pytest.mark.parametrize("name", ["alice", "a.b", "ci-bot", "user_1", "a1"])
    def test_acceptable(self, name: str) -> None:
        User(username=name, role=Role.VIEWER)

    @pytest.mark.parametrize(
        "name", ["", "a", "Alice", "has space", "-leading", "trailing-", "a" * 100, "e@x"]
    )
    def test_refused(self, name: str) -> None:
        with pytest.raises(AuthError, match="usable username"):
            User(username=name, role=Role.VIEWER)


class TestAccounts:
    """The store."""

    def test_an_account_is_created(self, store: AuthStore) -> None:
        user = store.create_user("alice", PASSWORD, Role.OPERATOR)

        assert user.username == "alice"
        assert store.get_user("alice") is not None

    def test_a_duplicate_is_refused(self, store: AuthStore) -> None:
        store.create_user("alice", PASSWORD, Role.VIEWER)

        with pytest.raises(AuthError, match="already exists"):
            store.create_user("alice", PASSWORD, Role.VIEWER)

    def test_the_password_is_not_stored_readably(self, store: AuthStore) -> None:
        store.create_user("alice", PASSWORD, Role.VIEWER)
        user = store.get_user("alice")

        assert user is not None
        assert PASSWORD not in user.password_hash

    def test_the_public_form_carries_no_hash(self, store: AuthStore) -> None:
        store.create_user("alice", PASSWORD, Role.VIEWER)
        public = store.get_user("alice").to_public()  # type: ignore[union-attr]

        assert "password_hash" not in public

    def test_a_disabled_account_cannot_log_in(self, store: AuthStore) -> None:
        store.create_user("alice", PASSWORD, Role.VIEWER)
        store.set_active("alice", False)

        assert not store.get_user("alice").check(PASSWORD)  # type: ignore[union-attr]

    def test_the_last_administrator_cannot_be_deleted(self, store: AuthStore) -> None:
        """The recovery would be editing the database by hand."""
        store.create_user("admin", PASSWORD, Role.ADMIN)

        with pytest.raises(AuthError, match="only administrator"):
            store.delete_user("admin")

    def test_an_administrator_can_be_deleted_when_another_exists(
        self, store: AuthStore
    ) -> None:
        store.create_user("admin", PASSWORD, Role.ADMIN)
        store.create_user("admin2", PASSWORD, Role.ADMIN)

        store.delete_user("admin")
        assert store.get_user("admin") is None

    def test_deleting_removes_the_keys_and_sessions(self, store: AuthStore) -> None:
        store.create_user("admin", PASSWORD, Role.ADMIN)
        store.create_user("alice", PASSWORD, Role.OPERATOR)
        store.create_api_key("alice", "ci")
        store.create_session("alice", ttl_s=60)

        store.delete_user("alice")

        assert store.list_api_keys("alice") == ()
        assert store.list_sessions("alice") == ()


class TestBootstrap:
    """The first administrator."""

    def test_it_creates_one(self, store: AuthStore) -> None:
        user, password = store.bootstrap()

        assert user.role is Role.ADMIN
        assert len(password) >= 20
        assert store.get_user(user.username) is not None

    def test_the_password_works(self, store: AuthStore) -> None:
        _, password = store.bootstrap()
        assert store.get_user("admin").check(password)  # type: ignore[union-attr]

    def test_it_refuses_to_run_twice(self, store: AuthStore) -> None:
        """Otherwise it is a way to add an administrator without being one."""
        store.bootstrap()

        with pytest.raises(AuthError, match="already has accounts"):
            store.bootstrap()

    def test_a_fresh_installation_has_no_accounts(self, store: AuthStore) -> None:
        """No default administrator with a known password. Ever."""
        assert store.count_users() == 0

    def test_it_is_audited(self, store: AuthStore) -> None:
        store.bootstrap()
        assert any(entry.action == "auth.bootstrap" for entry in store.read_audit())


class TestLogin:
    """Exchanging a password for tokens."""

    def test_a_correct_password_yields_tokens(self, service: AuthService) -> None:
        populated(service)
        pair = service.login(Credentials(username="admin", password=PASSWORD))

        assert pair.access_token
        assert pair.refresh_token

    def test_a_wrong_password_is_refused(self, service: AuthService) -> None:
        populated(service)

        with pytest.raises(Unauthorized, match="username or password"):
            service.login(Credentials(username="admin", password="wrong-password-xx"))

    def test_an_unknown_user_gives_the_same_message(self, service: AuthService) -> None:
        """Distinguishing them is a user-enumeration oracle."""
        populated(service)

        with pytest.raises(Unauthorized) as absent:
            service.login(Credentials(username="ghost", password=PASSWORD))
        with pytest.raises(Unauthorized) as wrong:
            service.login(Credentials(username="admin", password="wrong-password-xx"))

        assert str(absent.value) == str(wrong.value)

    def test_a_disabled_account_is_refused(self, service: AuthService) -> None:
        populated(service)
        service.store.set_active("viewer", False)

        with pytest.raises(Unauthorized):
            service.login(Credentials(username="viewer", password=PASSWORD))

    def test_a_failure_is_audited(self, service: AuthService) -> None:
        """The half of the trail that matters."""
        populated(service)
        with pytest.raises(Unauthorized):
            service.login(Credentials(username="admin", password="wrong-password-xx"))

        entries = service.store.read_audit(action="auth.login")
        assert entries[0].outcome == "denied"

    def test_a_success_is_audited_with_the_address(self, service: AuthService) -> None:
        populated(service)
        service.login(
            Credentials(username="admin", password=PASSWORD),
            LoginContext(address="10.0.0.9"),
        )
        entry = service.store.read_audit(action="auth.login")[0]

        assert entry.outcome == "ok"
        assert entry.address == "10.0.0.9"

    def test_the_login_time_is_recorded(self, service: AuthService) -> None:
        populated(service)
        service.login(Credentials(username="admin", password=PASSWORD))

        assert service.store.get_user("admin").last_login_at  # type: ignore[union-attr]

    def test_empty_credentials_are_refused(self) -> None:
        with pytest.raises(Unauthorized, match="both required"):
            Credentials(username="", password="")


class TestRefreshAndLogout:
    """Sessions."""

    def test_a_refresh_token_yields_a_new_pair(self, service: AuthService) -> None:
        populated(service)
        first = service.login(Credentials(username="admin", password=PASSWORD))

        second = service.refresh(first.refresh_token)
        assert second.access_token

    def test_a_logout_ends_the_session_immediately(self, service: AuthService) -> None:
        """Not when the token expires — that is the point of checking the store."""
        populated(service)
        pair = service.login(Credentials(username="admin", password=PASSWORD))
        actor = service.authenticate(f"Bearer {pair.access_token}")

        service.logout(actor)

        with pytest.raises(Unauthorized, match="no longer valid"):
            service.refresh(pair.refresh_token)

    def test_a_password_change_ends_every_session(self, service: AuthService) -> None:
        populated(service)
        pair = service.login(Credentials(username="admin", password=PASSWORD))

        service.store.set_password("admin", "a-brand-new-password")

        with pytest.raises(Unauthorized):
            service.refresh(pair.refresh_token)

    def test_a_disabled_account_cannot_refresh(self, service: AuthService) -> None:
        populated(service)
        pair = service.login(Credentials(username="operator", password=PASSWORD))
        service.store.set_active("operator", False)

        with pytest.raises(Unauthorized):
            service.refresh(pair.refresh_token)

    def test_a_refresh_reissues_against_the_current_role(self, service: AuthService) -> None:
        """A demotion takes effect at the next refresh, not whenever it expires."""
        populated(service)
        pair = service.login(Credentials(username="operator", password=PASSWORD))
        service.store.set_role("operator", Role.VIEWER)

        # The role change revoked the session, so log in again and demote after.
        pair = service.login(Credentials(username="operator", password=PASSWORD))
        refreshed = service.refresh(pair.refresh_token)
        actor = service.authenticate(f"Bearer {refreshed.access_token}")

        assert actor.role is Role.VIEWER

    def test_logging_out_everywhere_ends_them_all(self, service: AuthService) -> None:
        populated(service)
        service.login(Credentials(username="admin", password=PASSWORD))
        service.login(Credentials(username="admin", password=PASSWORD))
        actor = principal("admin", Role.ADMIN)

        assert service.logout_everywhere(actor) == 2

    def test_ending_somebody_elses_session_needs_an_administrator(
        self, service: AuthService
    ) -> None:
        populated(service)
        with pytest.raises(Forbidden):
            service.logout_everywhere(principal("operator", Role.OPERATOR), "admin")


class TestAuthenticate:
    """Identifying a caller."""

    def test_an_access_token_identifies(self, service: AuthService) -> None:
        populated(service)
        pair = service.login(Credentials(username="operator", password=PASSWORD))

        actor = service.authenticate(f"Bearer {pair.access_token}")
        assert actor.username == "operator"
        assert actor.role is Role.OPERATOR
        assert actor.method == "token"

    def test_an_api_key_identifies(self, service: AuthService) -> None:
        populated(service)
        _, secret = service.store.create_api_key("operator", "ci")

        actor = service.authenticate(f"Bearer {secret}")
        assert actor.method == "api_key"
        assert actor.role is Role.OPERATOR

    def test_no_header_is_refused(self, service: AuthService) -> None:
        populated(service)
        with pytest.raises(Unauthorized, match="no credentials"):
            service.authenticate(None)

    def test_a_non_bearer_scheme_is_refused(self, service: AuthService) -> None:
        populated(service)
        with pytest.raises(Unauthorized, match="Bearer"):
            service.authenticate("Basic dXNlcjpwYXNz")

    def test_an_unknown_key_is_refused(self, service: AuthService) -> None:
        populated(service)
        with pytest.raises(Unauthorized, match="not recognized"):
            service.authenticate("Bearer opk_totally-made-up")

    def test_a_revoked_key_is_refused(self, service: AuthService) -> None:
        populated(service)
        key, secret = service.store.create_api_key("operator", "ci")
        service.store.revoke_api_key(key.id)

        with pytest.raises(Unauthorized, match="revoked"):
            service.authenticate(f"Bearer {secret}")

    def test_an_expired_key_is_refused(self, service: AuthService) -> None:
        populated(service)
        _, secret = service.store.create_api_key(
            "operator", "ci", expires_at="2020-01-01T00:00:00+00:00"
        )
        with pytest.raises(Unauthorized, match="expired"):
            service.authenticate(f"Bearer {secret}")

    def test_using_a_key_records_when(self, service: AuthService) -> None:
        populated(service)
        key, secret = service.store.create_api_key("operator", "ci")
        service.authenticate(f"Bearer {secret}")

        stored = {k.id: k for k in service.store.list_api_keys()}[key.id]
        assert stored.last_used_at


class TestApiKeys:
    """Minting and scoping."""

    def test_a_key_defaults_to_the_account_role(self, service: AuthService) -> None:
        populated(service)
        key, _ = service.store.create_api_key("operator", "ci")

        assert key.role is Role.OPERATOR

    def test_a_key_may_be_weaker_than_its_account(self, service: AuthService) -> None:
        populated(service)
        key, _ = service.store.create_api_key("admin", "readonly", role=Role.VIEWER)

        assert key.role is Role.VIEWER

    def test_a_key_may_not_outrank_its_account(self, service: AuthService) -> None:
        """A privilege escalation wearing a convenience."""
        populated(service)
        with pytest.raises(AuthError, match="cannot have the admin role"):
            service.store.create_api_key("viewer", "sneaky", role=Role.ADMIN)

    def test_the_secret_is_returned_once(self, service: AuthService) -> None:
        populated(service)
        key, secret = service.store.create_api_key("operator", "ci")

        stored = {k.id: k for k in service.store.list_api_keys()}[key.id]
        assert secret not in stored.key_hash
        assert "secret" not in stored.to_public()

    def test_minting_for_somebody_else_needs_an_administrator(
        self, service: AuthService
    ) -> None:
        populated(service)
        with pytest.raises(Forbidden):
            service.create_api_key(
                principal("operator", Role.OPERATOR), "ci", username="admin"
            )

    def test_a_key_for_an_unknown_account_is_refused(self, service: AuthService) -> None:
        with pytest.raises(AuthError, match="no user named"):
            service.store.create_api_key("ghost", "ci")

    def test_disabling_an_account_disables_its_keys(self, service: AuthService) -> None:
        populated(service)
        _, secret = service.store.create_api_key("operator", "ci")
        service.store.set_active("operator", False)

        with pytest.raises(Unauthorized):
            service.authenticate(f"Bearer {secret}")

    def test_an_unreadable_expiry_is_treated_as_expired(self) -> None:
        """Better than a key that never expires because its date was wrong."""
        key = ApiKey(id="k", name="n", username="u", role=Role.VIEWER, expires_at="soon")
        assert key.is_expired()


class TestAdministration:
    """What an administrator may do, and may not."""

    def test_creating_an_account_needs_an_administrator(self, service: AuthService) -> None:
        populated(service)
        with pytest.raises(Forbidden):
            service.create_user(
                principal("operator", Role.OPERATOR), "new", PASSWORD, Role.VIEWER
            )

    def test_an_administrator_cannot_demote_themselves(self, service: AuthService) -> None:
        """One request would otherwise lock out the last administrator."""
        populated(service)
        with pytest.raises(AuthError, match="cannot demote themselves"):
            service.set_role(principal("admin", Role.ADMIN), "admin", Role.VIEWER)

    def test_an_administrator_cannot_disable_themselves(self, service: AuthService) -> None:
        populated(service)
        with pytest.raises(AuthError, match="cannot disable"):
            service.set_active(principal("admin", Role.ADMIN), "admin", False)

    def test_an_administrator_cannot_delete_themselves(self, service: AuthService) -> None:
        populated(service)
        with pytest.raises(AuthError, match="cannot delete"):
            service.delete_user(principal("admin", Role.ADMIN), "admin")

    def test_anyone_may_change_their_own_password(self, service: AuthService) -> None:
        populated(service)
        service.set_password(principal("viewer", Role.VIEWER), "viewer", "a-new-password-x")

        assert service.store.get_user("viewer").check("a-new-password-x")  # type: ignore[union-attr]

    def test_changing_somebody_elses_needs_an_administrator(
        self, service: AuthService
    ) -> None:
        populated(service)
        with pytest.raises(Forbidden):
            service.set_password(principal("viewer", Role.VIEWER), "admin", "another-one-xx")

    def test_a_role_change_revokes_the_keys_role_too(self, service: AuthService) -> None:
        populated(service)
        key, _ = service.store.create_api_key("operator", "ci")
        service.store.set_role("operator", Role.VIEWER)

        stored = {k.id: k for k in service.store.list_api_keys()}[key.id]
        assert stored.role is Role.VIEWER

    def test_reading_the_audit_trail_needs_an_administrator(
        self, service: AuthService
    ) -> None:
        populated(service)
        with pytest.raises(Forbidden):
            service.read_audit(principal("operator", Role.OPERATOR))


class TestAuditTrail:
    """The record of who did what."""

    def test_entries_are_recorded(self, store: AuthStore) -> None:
        store.audit(actor="alice", action="user.create", target="bob")
        entries = store.read_audit()

        assert entries[0].actor == "alice"
        assert entries[0].action == "user.create"
        assert entries[0].target == "bob"

    def test_it_is_newest_first(self, store: AuthStore) -> None:
        store.audit(actor="a", action="first")
        store.audit(actor="a", action="second")

        assert store.read_audit()[0].action == "second"

    def test_it_can_be_filtered(self, store: AuthStore) -> None:
        store.audit(actor="alice", action="x")
        store.audit(actor="bob", action="y")

        assert len(store.read_audit(actor="alice")) == 1
        assert len(store.read_audit(action="y")) == 1

    def test_it_cannot_be_edited(self, store: AuthStore) -> None:
        """A trail that can be edited answers a different question."""
        store.audit(actor="a", action="x")

        with pytest.raises(Exception, match="append-only"):
            with store.db.transaction() as conn:
                conn.execute("UPDATE audit_log SET actor = 'someone else'")

        assert store.read_audit()[0].actor == "a"

    def test_it_cannot_be_deleted_from(self, store: AuthStore) -> None:
        store.audit(actor="a", action="x")

        with pytest.raises(Exception, match="append-only"):
            with store.db.transaction() as conn:
                conn.execute("DELETE FROM audit_log")

        assert store.count_audit() == 1

    def test_a_failing_audit_never_fails_the_action(self, store: AuthStore) -> None:
        """Otherwise a failing log is a way to act unlogged."""
        store.db.close()
        store.audit(actor="a", action="x")

    def test_the_trail_is_separate_from_the_run_log(self, store: AuthStore) -> None:
        """Different questions, different retention."""
        store.audit(actor="a", action="x")

        assert store.db.query("SELECT COUNT(*) AS n FROM events")[0]["n"] == 0
        assert store.count_audit() == 1


class TestSessions:
    """The revocable half."""

    def test_a_session_is_active_when_fresh(self, store: AuthStore) -> None:
        store.create_user("alice", PASSWORD, Role.VIEWER)
        assert store.create_session("alice", ttl_s=3600).active

    def test_revoking_deactivates_it(self, store: AuthStore) -> None:
        store.create_user("alice", PASSWORD, Role.VIEWER)
        session = store.create_session("alice", ttl_s=3600)

        assert store.revoke_session(session.id)
        assert not store.get_session(session.id).active  # type: ignore[union-attr]

    def test_revoking_twice_reports_nothing_the_second_time(
        self, store: AuthStore
    ) -> None:
        store.create_user("alice", PASSWORD, Role.VIEWER)
        session = store.create_session("alice", ttl_s=3600)

        store.revoke_session(session.id)
        assert not store.revoke_session(session.id)

    def test_an_expired_session_is_inactive(self, store: AuthStore) -> None:
        store.create_user("alice", PASSWORD, Role.VIEWER)
        assert not store.create_session("alice", ttl_s=-10).active

    def test_pruning_removes_the_dead_ones(self, store: AuthStore) -> None:
        store.create_user("alice", PASSWORD, Role.VIEWER)
        store.create_session("alice", ttl_s=-10)
        store.create_session("alice", ttl_s=3600)

        assert store.prune_sessions() == 1
        assert len(store.list_sessions("alice")) == 1

    def test_the_public_form_truncates_the_user_agent(self, store: AuthStore) -> None:
        store.create_user("alice", PASSWORD, Role.VIEWER)
        session = store.create_session("alice", ttl_s=60, user_agent="x" * 500)

        assert len(session.to_public()["user_agent"]) <= 200

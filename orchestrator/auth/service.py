"""The authentication service: login, refresh, logout, and who-is-this.

One object ties the store and the token service together and owns the decisions
that involve both. Everything above it — the API, the CLI — asks this and does
not reason about hashes, sessions, or claims.

Two behaviours worth stating, because both are the kind of thing that is
skipped and then written into an incident report:

* **A failed login says nothing about why.** Wrong password, unknown user, and
  disabled account all produce the same message and take the same time. An
  endpoint that distinguishes them is a user-enumeration oracle.
* **Every attempt is audited, including the failures.** A successful login
  nobody can correlate is half a trail; a failed one is the half that matters.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from orchestrator.auth.models import (
    ApiKey,
    AuthError,
    Credentials,
    Forbidden,
    Principal,
    Role,
    Unauthorized,
    User,
)
from orchestrator.auth.store import AuthStore
from orchestrator.auth.tokens import REFRESH_TTL_S, TokenPair, TokenService
from orchestrator.core.logging import get_logger

__all__ = ["AuthService", "LoginContext"]

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class LoginContext:
    """Where a login came from, for the session record and the audit trail."""

    address: str = ""
    user_agent: str = ""


class AuthService:
    """Everything the layers above need to know about identity."""

    __slots__ = ("_store", "_tokens")

    def __init__(self, store: AuthStore, tokens: TokenService) -> None:
        """Bind the service to a store and a token issuer."""
        self._store = store
        self._tokens = tokens

    @property
    def store(self) -> AuthStore:
        """The underlying store."""
        return self._store

    @property
    def tokens(self) -> TokenService:
        """The token issuer."""
        return self._tokens

    @property
    def has_accounts(self) -> bool:
        """Whether this installation has been set up."""
        return self._store.count_users() > 0

    # ---------------------------------------------------------------- login

    def login(self, credentials: Credentials, context: LoginContext | None = None) -> TokenPair:
        """Exchange a username and password for tokens.

        Raises:
            Unauthorized: If the credentials are not accepted. The message does
                not distinguish an unknown user from a wrong password.
        """
        where = context or LoginContext()
        user = self._store.get_user(credentials.username)

        # Hash even when the user is absent, so the two paths cost the same.
        if user is None:
            User(username="unknown-account", role=Role.VIEWER).check(credentials.password)
            self._audit_login(credentials.username, where, ok=False, reason="no such user")
            raise Unauthorized("the username or password is not correct")

        if not user.check(credentials.password):
            self._audit_login(
                credentials.username,
                where,
                ok=False,
                reason="disabled" if not user.active else "bad password",
            )
            raise Unauthorized("the username or password is not correct")

        session = self._store.create_session(
            user.username,
            ttl_s=REFRESH_TTL_S,
            user_agent=where.user_agent,
            address=where.address,
        )
        self._store.record_login(user.username)
        self._audit_login(user.username, where, ok=True, role=user.role.value)

        return self._tokens.issue_pair(user.username, user.role, session_id=session.id)

    def refresh(self, refresh_token: str, context: LoginContext | None = None) -> TokenPair:
        """Exchange a refresh token for a new pair.

        The session is checked against the store, which is what makes a logout
        take effect immediately rather than when the token happens to expire.

        Raises:
            Unauthorized: If the token or its session is not usable.
        """
        where = context or LoginContext()
        claims = self._tokens.verify(refresh_token, kind="refresh")
        username = str(claims["sub"])
        session_id = str(claims.get("sid", ""))

        session = self._store.get_session(session_id) if session_id else None
        if session is None or not session.active or session.username != username:
            self._store.audit(
                actor=username,
                action="auth.refresh",
                outcome="denied",
                address=where.address,
                detail="the session is not active",
            )
            raise Unauthorized("this session is no longer valid; log in again")

        user = self._store.get_user(username)
        if user is None or not user.active:
            self._store.audit(
                actor=username,
                action="auth.refresh",
                outcome="denied",
                address=where.address,
                detail="the account is not usable",
            )
            raise Unauthorized("this account can no longer sign in")

        # Reissued against the account's *current* role, so a demotion takes
        # effect at the next refresh rather than whenever the token expires.
        self._store.audit(
            actor=username,
            action="auth.refresh",
            role=user.role.value,
            address=where.address,
            target=session.id,
        )
        return self._tokens.issue_pair(user.username, user.role, session_id=session.id)

    def logout(self, principal: Principal, *, session_id: str = "") -> bool:
        """End a session.

        Args:
            principal: Who is logging out.
            session_id: The session to end. Defaults to the one in use.

        Returns:
            Whether a session was ended.
        """
        target = session_id or principal.session_id
        if not target:
            return False

        session = self._store.get_session(target)
        if session is None:
            return False
        if session.username != principal.username and not principal.role.can(Role.ADMIN):
            raise Forbidden("only an administrator may end somebody else's session")

        revoked = self._store.revoke_session(target)
        self._store.audit(
            actor=principal.label,
            action="auth.logout",
            role=principal.role.value,
            target=target,
            outcome="ok" if revoked else "noop",
        )
        return revoked

    def logout_everywhere(self, principal: Principal, username: str = "") -> int:
        """End every session for an account."""
        target = username or principal.username
        if target != principal.username:
            principal.require(Role.ADMIN)

        count = self._store.revoke_sessions_of(target)
        self._store.audit(
            actor=principal.label,
            action="auth.logout_all",
            role=principal.role.value,
            target=target,
            detail=f"{count} session(s)",
        )
        return count

    # ------------------------------------------------------- authentication

    def authenticate(self, header: str | None, *, address: str = "") -> Principal:
        """Identify the caller from an ``Authorization`` header.

        Accepts ``Bearer <jwt>`` and ``Bearer <api key>``; a key is recognized
        by its prefix, so one header serves both and a client does not have to
        know which kind of credential it holds.

        Raises:
            Unauthorized: If there is no usable credential.
        """
        if not header:
            raise Unauthorized("this request carries no credentials")

        scheme, _, value = header.partition(" ")
        token = value.strip()
        if scheme.lower() not in ("bearer", "token") or not token:
            raise Unauthorized("the Authorization header must be 'Bearer <token>'")

        from orchestrator.auth.models import API_KEY_PREFIX

        if token.startswith(API_KEY_PREFIX):
            return self._authenticate_key(token, address=address)
        return self._authenticate_token(token)

    def _authenticate_token(self, token: str) -> Principal:
        """Identify a caller from an access token."""
        claims = self._tokens.verify(token, kind="access")
        return Principal(
            username=str(claims["sub"]),
            role=self._tokens.role_of(claims),
            method="token",
            session_id=str(claims.get("sid", "")),
            claims=claims,
        )

    def _authenticate_key(self, secret: str, *, address: str = "") -> Principal:
        """Identify a caller from an API key."""
        key = self._store.find_api_key(secret)
        if key is None:
            self._store.audit(
                actor="unknown",
                action="auth.key",
                outcome="denied",
                address=address,
                detail="unrecognized key",
            )
            raise Unauthorized("this API key is not recognized")
        if not key.usable:
            self._store.audit(
                actor=key.username,
                action="auth.key",
                outcome="denied",
                target=key.id,
                address=address,
                detail="expired" if key.is_expired() else "revoked",
            )
            raise Unauthorized(
                "this API key has expired" if key.is_expired() else "this API key was revoked"
            )

        user = self._store.get_user(key.username)
        if user is None or not user.active:
            raise Unauthorized("the account this key belongs to is not active")

        self._store.touch_api_key(key.id)
        return Principal(
            username=key.username, role=key.role, method="api_key", key_id=key.id
        )

    # --------------------------------------------------------------- admin

    def create_user(
        self,
        principal: Principal,
        username: str,
        password: str,
        role: Role,
        *,
        display_name: str = "",
    ) -> User:
        """Create an account. Administrators only."""
        principal.require(Role.ADMIN)
        user = self._store.create_user(username, password, role, display_name=display_name)
        self._store.audit(
            actor=principal.label,
            action="user.create",
            role=principal.role.value,
            target=username,
            detail=f"role={role.value}",
        )
        return user

    def set_role(self, principal: Principal, username: str, role: Role) -> None:
        """Change an account's role. Administrators only."""
        principal.require(Role.ADMIN)
        if username == principal.username and role is not Role.ADMIN:
            # Otherwise the last administrator can lock themselves out with one
            # request, and the recovery is editing the database by hand.
            raise AuthError("an administrator cannot demote themselves")
        self._store.set_role(username, role)
        self._store.audit(
            actor=principal.label,
            action="user.set_role",
            role=principal.role.value,
            target=username,
            detail=f"role={role.value}",
        )

    def set_password(self, principal: Principal, username: str, password: str) -> None:
        """Change a password. Anyone may change their own; admins, anyone's."""
        if username != principal.username:
            principal.require(Role.ADMIN)
        self._store.set_password(username, password)
        self._store.audit(
            actor=principal.label,
            action="user.set_password",
            role=principal.role.value,
            target=username,
        )

    def set_active(self, principal: Principal, username: str, active: bool) -> None:
        """Enable or disable an account. Administrators only."""
        principal.require(Role.ADMIN)
        if username == principal.username and not active:
            raise AuthError("an administrator cannot disable their own account")
        self._store.set_active(username, active)
        self._store.audit(
            actor=principal.label,
            action="user.set_active" if active else "user.disable",
            role=principal.role.value,
            target=username,
        )

    def delete_user(self, principal: Principal, username: str) -> None:
        """Remove an account. Administrators only."""
        principal.require(Role.ADMIN)
        if username == principal.username:
            raise AuthError("an administrator cannot delete their own account")
        self._store.delete_user(username)
        self._store.audit(
            actor=principal.label,
            action="user.delete",
            role=principal.role.value,
            target=username,
        )

    def create_api_key(
        self,
        principal: Principal,
        name: str,
        *,
        username: str = "",
        role: Role | None = None,
        expires_at: str = "",
    ) -> tuple[ApiKey, str]:
        """Mint a key. Anyone may mint their own; admins, anyone's."""
        owner = username or principal.username
        if owner != principal.username:
            principal.require(Role.ADMIN)

        key, secret = self._store.create_api_key(
            owner, name, role=role, expires_at=expires_at
        )
        self._store.audit(
            actor=principal.label,
            action="key.create",
            role=principal.role.value,
            target=key.id,
            detail=f"for={owner} role={key.role.value}",
        )
        return key, secret

    def revoke_api_key(self, principal: Principal, key_id: str) -> bool:
        """Revoke a key. Anyone may revoke their own; admins, anyone's."""
        keys = {key.id: key for key in self._store.list_api_keys()}
        key = keys.get(key_id)
        if key is None:
            return False
        if key.username != principal.username:
            principal.require(Role.ADMIN)

        revoked = self._store.revoke_api_key(key_id)
        self._store.audit(
            actor=principal.label,
            action="key.revoke",
            role=principal.role.value,
            target=key_id,
            outcome="ok" if revoked else "noop",
        )
        return revoked

    def read_audit(self, principal: Principal, **query: Any) -> tuple[Any, ...]:
        """Read the audit trail. Administrators only."""
        principal.require(Role.ADMIN)
        return self._store.read_audit(**query)

    # ------------------------------------------------------------- internals

    def _audit_login(
        self,
        username: str,
        context: LoginContext,
        *,
        ok: bool,
        reason: str = "",
        role: str = "",
    ) -> None:
        """Record a login attempt, successful or not."""
        self._store.audit(
            actor=username,
            action="auth.login",
            role=role,
            outcome="ok" if ok else "denied",
            address=context.address,
            detail=reason,
        )
        if not ok:
            _log.warning("login refused", username=username, reason=reason)


def now_iso() -> str:
    """The current instant, in the form the store uses."""
    return datetime.now(UTC).isoformat()

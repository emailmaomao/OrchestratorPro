"""Login, logout, refresh, users, keys, and the audit trail.

Kept in its own router so the authentication surface is readable in one file
and so an embedder can decline to mount it — an installation fronted by an
identity provider wants the rest of the API and none of this.

Every response about an account or a key goes through ``to_public``: a password
hash or a key digest that reaches a JSON body is a secret that reaches a log,
a proxy cache, and a browser's history.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from orchestrator.api.security import (
    Admin,
    CurrentPrincipal,
    login_context,
)
from orchestrator.api.state import Conflict, NotFound, NotSupported
from orchestrator.auth.models import AuthError, Credentials, Role

__all__ = ["auth_router"]

auth_router = APIRouter(prefix="/auth", tags=["auth"])


class _Strict(BaseModel):
    """Request bodies reject what they do not understand."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class LoginRequest(_Strict):
    """A username and password."""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=512)


class RefreshRequest(_Strict):
    """A refresh token."""

    refresh_token: str = Field(min_length=1, max_length=4096)


class CreateUserRequest(_Strict):
    """A new account."""

    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=12, max_length=512)
    role: str = Field(default="viewer")
    display_name: str = Field(default="", max_length=120)


class PasswordRequest(_Strict):
    """A replacement password."""

    password: str = Field(min_length=12, max_length=512)


class RoleRequest(_Strict):
    """A replacement role."""

    role: str


class ActiveRequest(_Strict):
    """Whether an account may sign in."""

    active: bool


class CreateKeyRequest(_Strict):
    """A new API key."""

    name: str = Field(min_length=1, max_length=120)
    username: str = Field(default="", max_length=64)
    role: str = Field(default="")
    expires_at: str = Field(default="", max_length=64)


def _service(request: Request) -> Any:
    """Return the auth service, or refuse.

    Raises:
        NotSupported: If the application was built without one.
    """
    state = getattr(request.app.state, "orchestrator", None)
    service = getattr(state, "auth", None) if state else None
    if service is None:
        raise NotSupported(
            "this server was built without authentication; there are no accounts "
            "to manage"
        )
    return service


@auth_router.post("/login", summary="Exchange a password for tokens")
async def login(request: Request, body: LoginRequest) -> dict[str, Any]:
    """Log in.

    A failure says only that the username or password is not correct — not
    which, and not whether the account exists.
    """
    service = _service(request)
    pair = service.login(
        Credentials(username=body.username, password=body.password),
        login_context(request),
    )
    return pair.to_public()


@auth_router.post("/refresh", summary="Exchange a refresh token for a new pair")
async def refresh(request: Request, body: RefreshRequest) -> dict[str, Any]:
    """Refresh a session.

    The session is checked against the store, so a logout takes effect here
    immediately rather than when the token happens to expire.
    """
    service = _service(request)
    return service.refresh(body.refresh_token, login_context(request)).to_public()


@auth_router.post("/logout", summary="End the current session")
async def logout(
    request: Request,
    principal: CurrentPrincipal,
    session_id: Annotated[str, Query(description="Another session to end.")] = "",
) -> dict[str, Any]:
    """Log out of this session, or another one belonging to the same account."""
    service = _service(request)
    return {"ended": service.logout(principal, session_id=session_id)}


@auth_router.post("/logout-all", summary="End every session for an account")
async def logout_all(
    request: Request,
    principal: CurrentPrincipal,
    username: Annotated[str, Query()] = "",
) -> dict[str, Any]:
    """End every session. Ending somebody else's requires an administrator."""
    service = _service(request)
    return {"ended": service.logout_everywhere(principal, username)}


@auth_router.get("/me", summary="Who this request is from")
async def whoami(request: Request, principal: CurrentPrincipal) -> dict[str, Any]:
    """Report the calling principal, and how it was established."""
    service = getattr(getattr(request.app.state, "orchestrator", None), "auth", None)
    payload = principal.to_public()
    payload["authentication"] = "open" if service is None or not service.has_accounts else "required"
    return payload


@auth_router.get("/sessions", summary="Sessions for an account")
async def list_sessions(
    request: Request,
    principal: CurrentPrincipal,
    username: Annotated[str, Query()] = "",
) -> list[dict[str, Any]]:
    """List sessions. Listing somebody else's requires an administrator."""
    service = _service(request)
    target = username or principal.username
    if target != principal.username:
        principal.require(Role.ADMIN)
    return [session.to_public() for session in service.store.list_sessions(target)]


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #


@auth_router.get("/users", summary="Every account")
async def list_users(request: Request, principal: Admin) -> list[dict[str, Any]]:
    """List accounts. Administrators only."""
    return [user.to_public() for user in _service(request).store.list_users()]


@auth_router.post("/users", status_code=201, summary="Create an account")
async def create_user(
    request: Request, principal: Admin, body: CreateUserRequest
) -> dict[str, Any]:
    """Create an account. Administrators only."""
    service = _service(request)
    user = service.create_user(
        principal,
        body.username,
        body.password,
        Role.parse(body.role),
        display_name=body.display_name,
    )
    return user.to_public()


@auth_router.get("/users/{username}", summary="One account")
async def get_user(
    request: Request, principal: CurrentPrincipal, username: str
) -> dict[str, Any]:
    """Return an account. Reading somebody else's requires an administrator."""
    if username != principal.username:
        principal.require(Role.ADMIN)
    user = _service(request).store.get_user(username)
    if user is None:
        raise NotFound(f"there is no user named {username!r}")
    return user.to_public()


@auth_router.put("/users/{username}/password", summary="Change a password")
async def set_password(
    request: Request, principal: CurrentPrincipal, username: str, body: PasswordRequest
) -> dict[str, Any]:
    """Change a password, which revokes every session for that account."""
    _service(request).set_password(principal, username, body.password)
    return {"username": username, "sessions_revoked": True}


@auth_router.put("/users/{username}/role", summary="Change a role")
async def set_role(
    request: Request, principal: Admin, username: str, body: RoleRequest
) -> dict[str, Any]:
    """Change a role. Administrators only, and never their own."""
    _service(request).set_role(principal, username, Role.parse(body.role))
    return {"username": username, "role": body.role}


@auth_router.put("/users/{username}/active", summary="Enable or disable an account")
async def set_active(
    request: Request, principal: Admin, username: str, body: ActiveRequest
) -> dict[str, Any]:
    """Enable or disable an account. Administrators only."""
    _service(request).set_active(principal, username, body.active)
    return {"username": username, "active": body.active}


@auth_router.delete("/users/{username}", status_code=204, summary="Delete an account")
async def delete_user(request: Request, principal: Admin, username: str) -> None:
    """Delete an account. Administrators only, and never the last one."""
    _service(request).delete_user(principal, username)


# --------------------------------------------------------------------------- #
# API keys
# --------------------------------------------------------------------------- #


@auth_router.get("/keys", summary="API keys")
async def list_keys(
    request: Request,
    principal: CurrentPrincipal,
    username: Annotated[str, Query()] = "",
) -> list[dict[str, Any]]:
    """List keys. Listing somebody else's requires an administrator."""
    service = _service(request)
    if username and username != principal.username:
        principal.require(Role.ADMIN)
    target = username or (None if principal.role.can(Role.ADMIN) else principal.username)
    return [key.to_public() for key in service.store.list_api_keys(target)]


@auth_router.post("/keys", status_code=201, summary="Mint an API key")
async def create_key(
    request: Request, principal: CurrentPrincipal, body: CreateKeyRequest
) -> dict[str, Any]:
    """Mint a key.

    The secret is in this response and nowhere else, ever. It is not stored in
    a form anything can present, so a lost key is replaced rather than recovered.
    """
    service = _service(request)
    key, secret = service.create_api_key(
        principal,
        body.name,
        username=body.username,
        role=Role.parse(body.role) if body.role else None,
        expires_at=body.expires_at,
    )
    payload = key.to_public()
    payload["secret"] = secret
    payload["note"] = "this secret is shown once and cannot be retrieved again"
    return payload


@auth_router.delete("/keys/{key_id}", summary="Revoke an API key")
async def revoke_key(
    request: Request, principal: CurrentPrincipal, key_id: str
) -> dict[str, Any]:
    """Revoke a key."""
    service = _service(request)
    if not service.revoke_api_key(principal, key_id):
        raise NotFound(f"there is no API key with id {key_id!r}")
    return {"id": key_id, "revoked": True}


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #


@auth_router.get("/audit", summary="The audit trail")
async def read_audit(
    request: Request,
    principal: Admin,
    limit: Annotated[int, Query(ge=1, le=5000)] = 200,
    actor: Annotated[str, Query()] = "",
    action: Annotated[str, Query()] = "",
    since: Annotated[str, Query(description="ISO timestamp lower bound.")] = "",
) -> list[dict[str, Any]]:
    """Read the audit trail, newest first. Administrators only.

    The trail is append-only at the database level: there is no endpoint that
    edits it, and there is no SQL that could.
    """
    service = _service(request)
    entries = service.read_audit(
        principal, limit=limit, actor=actor, action=action, since=since
    )
    return [entry.to_public() for entry in entries]


def _unused() -> None:  # pragma: no cover
    """Keeps the Conflict and AuthError imports honest for future handlers."""
    raise Conflict("unused") from AuthError("unused")

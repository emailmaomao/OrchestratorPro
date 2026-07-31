"""Authentication and authorization for the control plane.

The dependencies here are what every route uses to find out who is calling and
whether they may. Two decisions shape them:

* **Authentication is optional per installation, not per route.** An
  installation with no accounts serves everything as a built-in operator, which
  is what a single person on loopback wants and what every earlier build did.
  The moment an account exists, every route requires a credential — there is no
  window in which some routes are protected and others are not.
* **The requirement is a floor, not a list.** A route declares the least role
  that may reach it and the ordering does the rest. Enumerating permissions per
  route drifts the first time somebody adds an endpoint and forgets the table.

The login endpoints are the only ones that are always open, and they are rate
limited like everything else.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends
from starlette.requests import HTTPConnection

from orchestrator.auth.models import Principal, Role, Unauthorized
from orchestrator.auth.service import AuthService, LoginContext

__all__ = [
    "ANONYMOUS",
    "CurrentPrincipal",
    "login_context",
    "principal_of",
    "require",
    "requires_admin",
    "requires_operator",
    "requires_viewer",
]

#: Who a request is from when the installation has no accounts.
#:
#: An operator rather than an administrator: an unconfigured installation
#: should be able to run workflows, and should not be able to create the
#: accounts that would lock it down. Setting those up is a deliberate act,
#: through the CLI, by somebody with access to the machine.
ANONYMOUS = Principal(username="anonymous", role=Role.OPERATOR, method="open")


#: Paths where the credential may travel as a query parameter.
#:
#: A browser cannot set a header on an ``EventSource`` or a ``WebSocket``. The
#: alternative to this narrow exemption is a dashboard that cannot watch a run.
#: It stays narrow because a token in a URL is a token in a proxy log.
_QUERY_TOKEN_PATHS = ("/events", "/ws")


def _service(connection: HTTPConnection) -> AuthService | None:
    """Return the application's auth service, if it has one."""
    state = getattr(connection.app.state, "orchestrator", None)
    return getattr(state, "auth", None) if state else None


def _credential(connection: HTTPConnection) -> str | None:
    """Find the credential a connection presents.

    Prefers the header. Falls back to a ``token`` query parameter, and only on
    the streaming paths, where no header can be set.
    """
    header = connection.headers.get("authorization")
    if header:
        return header

    path = connection.url.path
    if any(path.endswith(marker) for marker in _QUERY_TOKEN_PATHS):
        token = connection.query_params.get("token", "")
        if token:
            return f"Bearer {token}"
    return None


def login_context(request: HTTPConnection) -> LoginContext:
    """Describe where a request came from, for the session and the audit trail."""
    client = request.client
    return LoginContext(
        address=client.host if client else "",
        user_agent=request.headers.get("user-agent", ""),
    )


def principal_of(request: HTTPConnection) -> Principal:
    """Identify the caller.

    Returns:
        The principal. :data:`ANONYMOUS` when the installation has no accounts.

    Raises:
        Unauthorized: If accounts exist and no usable credential was presented.
    """
    service = _service(request)
    if service is None or not service.has_accounts:
        return ANONYMOUS

    client = request.client
    principal = service.authenticate(
        _credential(request), address=client.host if client else ""
    )
    # Stashed so a route that audits does not have to resolve it twice.
    request.state.principal = principal
    return principal


CurrentPrincipal = Annotated[Principal, Depends(principal_of)]


def require(role: Role) -> Callable[[HTTPConnection], Principal]:
    """Build a dependency that admits only a role and above.

    Args:
        role: The least role that may reach the route.

    Returns:
        A dependency yielding the principal.
    """

    def dependency(request: HTTPConnection) -> Principal:
        principal = principal_of(request)
        principal.require(role)
        return principal

    return dependency


#: Read-only routes.
requires_viewer = require(Role.VIEWER)

#: Routes that start, stop, or change work.
requires_operator = require(Role.OPERATOR)

#: Routes that manage accounts, keys, or the audit trail.
requires_admin = require(Role.ADMIN)

Viewer = Annotated[Principal, Depends(requires_viewer)]
Operator = Annotated[Principal, Depends(requires_operator)]
Admin = Annotated[Principal, Depends(requires_admin)]


def audit(
    request: HTTPConnection, action: str, *, target: str = "", detail: str = ""
) -> None:
    """Record an action against the calling principal.

    A no-op when the installation has no accounts: there is nobody to attribute
    it to, and an audit trail full of ``anonymous`` is a trail that teaches
    people to ignore it.
    """
    service = _service(request)
    if service is None or not service.has_accounts:
        return

    principal = getattr(request.state, "principal", None) or ANONYMOUS
    client = request.client
    service.store.audit(
        actor=principal.label,
        action=action,
        role=principal.role.value,
        target=target,
        detail=detail,
        address=client.host if client else "",
    )


def unauthorized_headers() -> dict[str, str]:
    """The headers a 401 should carry, so a client knows what to present."""
    return {"www-authenticate": 'Bearer realm="orchestratorpro"'}


def is_open(request: HTTPConnection) -> bool:
    """Whether this installation is serving without authentication."""
    service = _service(request)
    return service is None or not service.has_accounts


def _reject() -> None:  # pragma: no cover - documentation of intent
    """Never called; kept so the import of Unauthorized is not mistaken for dead."""
    raise Unauthorized("credentials are required")

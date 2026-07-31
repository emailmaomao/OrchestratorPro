"""Security hardening and structured request logging for the control plane.

This build ships **no authentication**. That is a decision, not an oversight,
and it makes every other control here load-bearing: an unauthenticated service
that is also unbounded, unlogged, and reachable from anywhere is not a service,
it is an incident waiting for someone to notice the port.

So the defaults fail closed:

* A non-loopback bind is refused unless the deployment has named its hosts.
* A request body larger than the configured ceiling is rejected before it is
  read, not after it has been buffered.
* Cross-origin requests are refused by default. The dashboard is served from
  this same origin and needs no exemption; anything that does need one is a
  page on somebody else's site driving this operator's agents.
* Every response carries the headers that stop a browser from being creative
  with content it was given.

The request log is structured, one JSON line per request, with a correlation
identifier that also goes back to the client. When something goes wrong at
three in the morning, "which request was that" should be a lookup and not an
inference.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from orchestrator.core.config import ConfigError, OrchestratorConfig, SecurityConfig
from orchestrator.core.logging import bind_context, get_logger

__all__ = [
    "SECURITY_HEADERS",
    "BodyLimitMiddleware",
    "RequestContextMiddleware",
    "SecurityHeadersMiddleware",
    "harden",
    "verify_deployment",
]

_log = get_logger(__name__)

#: Response headers applied to everything this server returns.
#:
#: The API answers JSON and the dashboard is a static page with no inline
#: script, so the policy can be strict without breaking anything. ``frame-ancestors``
#: matters more than it looks: a control plane that can be framed is a control
#: plane that can be clickjacked into cancelling somebody's run.
SECURITY_HEADERS: dict[str, str] = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
    "permissions-policy": "geolocation=(), microphone=(), camera=(), payment=()",
    "content-security-policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "font-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    ),
}

#: The header carrying the correlation identifier, in both directions.
REQUEST_ID_HEADER = "x-request-id"

#: Paths never logged per-request. A dashboard polls health every five seconds,
#: and a log where 90% of the lines are ``GET /health 200`` is a log nobody reads.
_QUIET_PATHS = frozenset({"/health"})


@dataclass(frozen=True, slots=True)
class DeploymentWarning:
    """Something about this deployment that an operator should know."""

    code: str
    message: str
    severity: str = "warning"

    def __str__(self) -> str:
        return f"[{self.severity}] {self.code}: {self.message}"


def verify_deployment(
    config: OrchestratorConfig, *, env: dict[str, str] | None = None
) -> tuple[DeploymentWarning, ...]:
    """Check a configuration for the ways a deployment usually goes wrong.

    Returns warnings rather than raising, except where the combination is
    genuinely unsafe — those raise, because a warning printed into a container
    log at boot is a warning nobody reads.

    Args:
        config: The resolved configuration.
        env: Environment used to resolve token references.

    Returns:
        Warnings, most severe first.

    Raises:
        ConfigError: If the deployment would expose an unauthenticated service.
    """
    import os

    environment = os.environ if env is None else env
    warnings: list[DeploymentWarning] = []
    security = config.security

    if not config.api.is_local:
        # NFR-3.4, enforced rather than advised.
        config.api.validate_binding(environment)
        if not security.allowed_hosts:
            raise ConfigError(
                f"api.host = {config.api.host!r} is not loopback, so "
                "security.allowed_hosts must name the hostnames this server "
                "answers to; an empty list accepts any Host header",
                detail={"host": config.api.host},
            )
        if not security.rate_limiting:
            raise ConfigError(
                "a non-loopback bind requires rate limiting; set "
                "security.rate_limit_per_minute above zero",
                detail={"host": config.api.host},
            )

    if security.cors_origins:
        warnings.append(
            DeploymentWarning(
                "cors_enabled",
                f"{len(security.cors_origins)} cross-origin caller(s) are "
                "allowed; this build has no authentication, so any page on "
                "those origins can drive this server",
                severity="high",
            )
        )
    if not security.security_headers:
        warnings.append(
            DeploymentWarning("headers_disabled", "security headers are turned off")
        )
    if not security.rate_limiting:
        warnings.append(
            DeploymentWarning("no_rate_limit", "requests are not rate limited")
        )
    if config.run.auto_approve:
        warnings.append(
            DeploymentWarning(
                "auto_approve",
                "generated plans execute without human approval",
                severity="high",
            )
        )
    if not security.request_log:
        warnings.append(
            DeploymentWarning("no_request_log", "per-request logging is turned off")
        )

    order = {"high": 0, "warning": 1, "info": 2}
    return tuple(sorted(warnings, key=lambda w: (order.get(w.severity, 3), w.code)))


class SecurityHeadersMiddleware:
    """Adds the hardening headers to every response."""

    def __init__(self, app: ASGIApp, *, headers: dict[str, str] | None = None) -> None:
        """Wrap ``app``, sending ``headers`` on everything it returns."""
        self.app = app
        self._headers = [
            (key.encode("latin-1"), value.encode("latin-1"))
            for key, value in (headers or SECURITY_HEADERS).items()
        ]

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        """Pass the request through, decorating the response start."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: dict) -> None:
            if message["type"] == "http.response.start":
                existing = {key.lower() for key, _ in message.get("headers", [])}
                message["headers"] = [
                    *message.get("headers", []),
                    *[(key, value) for key, value in self._headers if key not in existing],
                ]
            await send(message)

        await self.app(scope, receive, send_with_headers)


class BodyLimitMiddleware:
    """Rejects request bodies above a ceiling.

    Checked against ``content-length`` first, so an oversized upload is refused
    before a byte of it is read, and then against what actually arrives, because
    a chunked request does not declare its length and a client that lies about
    ``content-length`` should not be believed.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        """Wrap ``app``, refusing bodies larger than ``max_bytes``."""
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        """Enforce the ceiling around the request."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {key.decode("latin-1").lower(): value for key, value in scope.get("headers", [])}
        declared = headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > self.max_bytes:
                    await self._refuse(send, int(declared))
                    return
            except ValueError:
                pass

        seen = 0
        too_large = False

        async def counting_receive() -> dict:
            nonlocal seen, too_large
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    too_large = True
                    return {"type": "http.disconnect"}
            return message

        await self.app(scope, counting_receive, send)
        if too_large:  # pragma: no cover - the app has already replied
            _log.warning("request body exceeded the limit", limit=self.max_bytes)

    async def _refuse(self, send: Callable, size: int) -> None:
        """Reply 413 in the API's own error envelope."""
        response = JSONResponse(
            status_code=413,
            content={
                "error": {
                    "code": "body_too_large",
                    "message": (
                        f"the request body is {size} bytes; this server accepts "
                        f"at most {self.max_bytes}"
                    ),
                    "retryable": False,
                    "detail": {"limit": self.max_bytes, "size": size},
                }
            },
        )
        await response({"type": "http"}, _no_receive, send)


class RequestContextMiddleware:
    """Assigns a correlation identifier and logs one line per request."""

    def __init__(self, app: ASGIApp, *, log_requests: bool = True) -> None:
        """Wrap ``app``, optionally emitting an access log."""
        self.app = app
        self.log_requests = log_requests

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        """Time the request, tag it, and record what happened."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {key.decode("latin-1").lower(): value for key, value in scope.get("headers", [])}
        supplied = headers.get(REQUEST_ID_HEADER)
        request_id = (
            supplied.decode("latin-1")[:64] if supplied else uuid.uuid4().hex[:16]
        )
        started = time.perf_counter()
        status = 500

        async def send_with_id(message: dict) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
                message["headers"] = [
                    *message.get("headers", []),
                    (REQUEST_ID_HEADER.encode("latin-1"), request_id.encode("latin-1")),
                ]
            await send(message)

        with bind_context(request_id=request_id):
            try:
                await self.app(scope, receive, send_with_id)
            finally:
                if self.log_requests and scope.get("path") not in _QUIET_PATHS:
                    _log.info(
                        "request",
                        method=scope.get("method", ""),
                        path=scope.get("path", ""),
                        status=status,
                        duration_ms=round((time.perf_counter() - started) * 1000, 2),
                        client=(scope.get("client") or ("", 0))[0],
                    )


async def _no_receive() -> dict:  # pragma: no cover - required by the signature
    """A receive channel for a response that reads no body."""
    return {"type": "http.request", "body": b"", "more_body": False}


def harden(
    app: FastAPI,
    security: SecurityConfig | None = None,
    *,
    allowed_hosts: Iterable[str] | None = None,
) -> FastAPI:
    """Apply the hardening stack to an application.

    Order matters. Starlette makes the *last* middleware added the outermost
    one, so these are registered inside-out to produce this stack:

    1. **Security headers** — outermost, so every response carries them,
       including the ones produced by the layers below rather than by a handler.
       A 429 with no ``x-frame-options`` is still a page a browser will render.
    2. **Request context** — so a refused request is logged with its identifier.
       A rate-limited caller is exactly the one worth having in the log.
    3. **Trusted hosts** — reject a request for a host we do not serve before
       spending anything else on it.
    4. **CORS** — preflight is answered before the budget is charged.
    5. **Rate limit** — then the body limit, then routing.

    Args:
        app: The application to harden.
        security: The policy. Defaults are used when omitted.
        allowed_hosts: Overrides ``security.allowed_hosts``.

    Returns:
        The same application.
    """
    settings = security or SecurityConfig()
    hosts = tuple(allowed_hosts) if allowed_hosts is not None else settings.allowed_hosts

    app.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_body_bytes)

    if settings.rate_limiting:
        from orchestrator.ops.ratelimit import RateLimitMiddleware

        app.add_middleware(
            RateLimitMiddleware,
            per_minute=settings.rate_limit_per_minute,
            burst=settings.rate_limit_burst,
        )

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_methods=["GET", "POST", "PATCH", "DELETE"],
            allow_headers=["content-type", REQUEST_ID_HEADER],
            allow_credentials=False,
            max_age=600,
        )

    if hosts:
        from starlette.middleware.trustedhost import TrustedHostMiddleware

        app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(hosts))

    app.add_middleware(RequestContextMiddleware, log_requests=settings.request_log)

    if settings.security_headers:
        app.add_middleware(SecurityHeadersMiddleware)

    return app


async def _healthy(request: Request) -> Response:  # pragma: no cover - reference
    """A trivially healthy response, used in documentation examples."""
    return JSONResponse({"status": "ok"})


AsyncHandler = Callable[[Request], Awaitable[Response]]

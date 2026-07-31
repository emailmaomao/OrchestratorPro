"""The FastAPI application: assembly, error handling, and OpenAPI.

``create_app`` is the composition root. It takes the things the control plane
cannot invent for itself — a store, a configuration, an execution backend — and
returns an ASGI application. Nothing here reaches for a global: two applications
can run in one process with different databases, which is what makes the test
suite able to build a fresh server per test without cleanup rituals.

**No authentication.** This milestone deliberately ships none, and the server
refuses to serve a non-loopback bind without one rather than pretending the
question does not arise (NFR-3.4). ``serve()`` enforces that; mounting the app
behind your own front door is your business.

Errors leave through one door. Every domain exception is rendered as
``{"error": {"code", "message", "retryable", "detail"}}`` with a status chosen
by :func:`~orchestrator.api.state.status_code_for`, so a client can branch on a
stable code instead of parsing prose.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from orchestrator.api.auth_routes import auth_router
from orchestrator.api.routes import build_router
from orchestrator.api.security import requires_viewer
from orchestrator.api.state import API_VERSION, AppState, error_body, status_code_for
from orchestrator.core.config import ConfigError, OrchestratorConfig
from orchestrator.core.events import OrchestratorError

__all__ = ["OPENAPI_TAGS", "create_app", "serve"]

_log = logging.getLogger(__name__)

#: Tag metadata, so the generated OpenAPI groups endpoints the way an operator
#: thinks about them rather than the way the router happens to be ordered.
OPENAPI_TAGS: list[dict[str, str]] = [
    {
        "name": "system",
        "description": "Liveness and the effective configuration.",
    },
    {
        "name": "runs",
        "description": (
            "Runs are the unit of work. Every read replays the event log, which "
            "is authoritative; every write appends to it."
        ),
    },
    {
        "name": "tasks",
        "description": (
            "Tasks within a run. The log is append-only: amendments are recorded "
            "as fresh declarations and a retired task is abandoned, not erased."
        ),
    },
    {
        "name": "workflows",
        "description": "Register workflow definitions and execute them.",
    },
    {
        "name": "agents",
        "description": (
            "How agents are configured, what tools they may call, and what a "
            "prompt would look like — without calling a model."
        ),
    },
    {
        "name": "builds",
        "description": "Analyze a project, plan an incremental build, run it.",
    },
    {
        "name": "approvals",
        "description": (
            "Human review gates, attempt history, transcripts, and diffs. An "
            "approval is an event in the same append-only log as everything "
            "else, so it replays and cannot be quietly changed."
        ),
    },
    {
        "name": "auth",
        "description": (
            "Logging in, accounts, API keys, and the audit trail. An "
            "installation with no accounts serves every other route as a "
            "built-in operator; the moment one exists, all of them require a "
            "credential."
        ),
    },
    {
        "name": "events",
        "description": (
            "Live event streams over SSE or WebSocket. A subscription is opened "
            "before the log is replayed, so nothing is lost in the seam."
        ),
    },
]

_DESCRIPTION = """\
The OrchestratorPro control plane.

Every run is reconstructed from an append-only event log, so a crash at any
point leaves recoverable state and every answer this API gives can be replayed.

**This build ships no authentication.** It binds to loopback by default and
refuses a non-loopback bind unless an auth token is configured (NFR-3.4).
"""


def create_app(
    *,
    state: AppState | None = None,
    config: OrchestratorConfig | None = None,
    executor_factory: Callable[..., Any] | None = None,
    workspace_root: Path | None = None,
    title: str = "OrchestratorPro",
    include_auth: bool = True,
) -> FastAPI:
    """Build the ASGI application.

    Args:
        state: A prepared application state. One over an in-memory database is
            created when omitted, which is useful for a preview and useless for
            anything you want to survive a restart.
        config: The resolved configuration. Ignored when ``state`` carries one.
        executor_factory: Builds the executor a run's steps are performed by.
            Without it the server records and reports but refuses to execute,
            which is what keeps this layer free of provider knowledge.
        workspace_root: Confines every client-supplied path. Strongly advised:
            a path in a request body is exactly as untrusted as one from a model.
        title: Application title, shown in the OpenAPI document.
        include_auth: Whether to mount the login and account routes. An
            installation fronted by an identity provider wants the rest of the
            API and none of them.

    Returns:
        The application. Its state is on ``app.state.orchestrator``.
    """
    resolved = state or AppState.in_memory(config=config or OrchestratorConfig())
    if config is not None and state is not None:
        resolved.config = config
    if executor_factory is not None:
        resolved.executor_factory = executor_factory
    if workspace_root is not None:
        resolved.workspace_root = workspace_root

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Hold the state for the application's lifetime and wind it down."""
        yield
        await resolved.shutdown()

    app = FastAPI(
        title=title,
        version=API_VERSION,
        description=_DESCRIPTION,
        openapi_tags=OPENAPI_TAGS,
        lifespan=lifespan,
        responses={
            400: {"description": "The request was malformed or violated a domain rule."},
            401: {"description": "No usable credential was presented."},
            403: {"description": "The caller may not do this."},
            404: {"description": "The addressed thing does not exist."},
            409: {"description": "The request contradicts the current state."},
            503: {"description": "The server is not configured to do this."},
        },
    )
    app.state.orchestrator = resolved
    # Every route below this line requires at least a viewer once the
    # installation has accounts. A floor rather than a per-route table:
    # a table drifts the first time somebody adds an endpoint.
    app.include_router(build_router(), dependencies=[Depends(requires_viewer)])
    if include_auth:
        app.include_router(auth_router)
    _install_error_handlers(app)
    return app


def _install_error_handlers(app: FastAPI) -> None:
    """Render every failure through one envelope."""

    @app.exception_handler(OrchestratorError)
    async def _domain_error(request: Request, exc: OrchestratorError) -> JSONResponse:
        status = status_code_for(exc)
        if status >= 500:
            # A 5xx is our bug, not the client's, and is the one case worth a
            # stack trace in the server log.
            _log.exception("unhandled domain error serving %s", request.url.path)
        return JSONResponse(status_code=status, content={"error": error_body(exc)})

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "invalid_request",
                    "message": "the request body or parameters did not validate",
                    "retryable": False,
                    "detail": {"errors": _sanitize(exc.errors())},
                }
            },
        )

    @app.exception_handler(ValueError)
    async def _value_error(request: Request, exc: ValueError) -> JSONResponse:
        # Domain constructors that predate the typed hierarchy still raise these;
        # rendering them as 500s would blame the server for a bad request.
        return JSONResponse(
            status_code=400, content={"error": error_body(exc)}
        )


def _sanitize(errors: Any) -> list[dict[str, Any]]:
    """Render validation errors as plain JSON.

    Pydantic attaches the offending input and, for some error types, an
    exception object. Neither is reliably serializable, and echoing a rejected
    body back to its sender is a way to reflect content the server never
    accepted.
    """
    return [
        {
            "location": [str(part) for part in error.get("loc", ())],
            "message": str(error.get("msg", "")),
            "type": str(error.get("type", "")),
        }
        for error in errors
    ]


def serve(
    app: FastAPI,
    *,
    config: OrchestratorConfig | None = None,
    env: Any = None,
) -> None:  # pragma: no cover - exercised by running it
    """Run the application under Uvicorn.

    Refuses a non-loopback bind without a configured auth token (NFR-3.4). This
    build has no authentication, so that check is the only thing standing
    between a convenience default and an unauthenticated service on a shared
    network.

    Raises:
        ConfigError: If the configured bind is not safe to serve.
    """
    import os

    import uvicorn

    settings = (config or app.state.orchestrator.config).api
    settings.validate_binding(os.environ if env is None else env)
    if not settings.is_local:
        raise ConfigError(
            f"api.host = {settings.host!r} is not loopback, and this build ships "
            "no authentication; put it behind a proxy that authenticates, or "
            "bind to localhost",
            detail={"host": settings.host},
        )
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")

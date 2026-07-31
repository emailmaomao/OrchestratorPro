"""The dashboard backend.

Deliberately almost nothing. It serves static files and one HTML document; every
fact the UI displays is fetched by the browser from the M10 API. There is no
server-side rendering, no read model, and no import of
:mod:`orchestrator.core` or :mod:`orchestrator.api.state` — a test enforces that
last part.

That constraint is not architectural neatness. A dashboard that reaches past the
API grows a second, subtly different view of what a run is, and the first time
the two disagree nobody can say which is right. Keeping the UI on the same
endpoints anyone else would use means the API stays honest, because its only
serious consumer is exercising it.

Two consequences worth stating:

* **No build step.** Plain ES modules, plain CSS, no bundler, no transpiler, no
  package manager. Open the directory in a browser and it works.
* **No network beyond this origin.** Nothing is loaded from a CDN. An operator
  running this on a machine with no internet access gets the whole UI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

__all__ = [
    "DASHBOARD_PREFIX",
    "STATIC_ROOT",
    "create_app",
    "index_html",
    "mount_dashboard",
]

#: Where the dashboard is mounted unless a caller says otherwise.
DASHBOARD_PREFIX = "/ui"

#: The directory holding every asset the browser will ask for.
STATIC_ROOT = Path(__file__).parent / "static"

#: Replaced with the mount prefix when the document is served, so that every
#: relative asset and API path resolves from any depth of client-side route.
_BASE_PLACEHOLDER = "__BASE__"


def index_html(prefix: str = DASHBOARD_PREFIX) -> str:
    """Return the shell document, with its ``<base>`` pointed at ``prefix``.

    Args:
        prefix: Where the dashboard is mounted.

    Returns:
        The HTML.

    Raises:
        FileNotFoundError: If the static assets are missing, which means a
            broken installation rather than a runtime condition worth handling.
    """
    document = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    base = prefix if prefix.endswith("/") else f"{prefix}/"
    return document.replace(_BASE_PLACEHOLDER, base)


def mount_dashboard(
    app: FastAPI, *, prefix: str = DASHBOARD_PREFIX, redirect_root: bool = True
) -> FastAPI:
    """Mount the dashboard onto an existing application.

    Args:
        app: The application serving the API. The dashboard is added to it
            rather than run beside it, so the browser needs no CORS
            configuration and no second port.
        prefix: Where to mount. Client-side routes live below it.
        redirect_root: Whether ``/`` should redirect to the dashboard. Off if
            something else owns the root.

    Returns:
        The same application, for chaining.
    """
    trimmed = prefix.rstrip("/") or ""
    app.mount(
        f"{trimmed}/assets",
        StaticFiles(directory=STATIC_ROOT / "assets"),
        name="dashboard-assets",
    )

    document = index_html(trimmed or "/")

    async def shell() -> HTMLResponse:
        """Serve the shell document."""
        return HTMLResponse(
            document, headers={"cache-control": "no-store"}
        )

    # Every client-side route serves the same document: the browser's history
    # API owns the path below the prefix, and a deep link that 404s would make
    # the address bar useless for sharing a view of a run.
    app.get(trimmed or "/", include_in_schema=False)(shell)
    app.get(f"{trimmed}/{{path:path}}", include_in_schema=False)(shell)

    if redirect_root and trimmed:

        @app.get("/", include_in_schema=False)
        async def root() -> RedirectResponse:
            """Send a bare visit to the dashboard."""
            return RedirectResponse(f"{trimmed}/", status_code=307)

    return app


def create_app(*, prefix: str = DASHBOARD_PREFIX, **api_kwargs: Any) -> FastAPI:
    """Build an application serving the API and the dashboard together.

    Args:
        prefix: Where to mount the dashboard.
        **api_kwargs: Passed through to
            :func:`orchestrator.api.app.create_app` — the store, the execution
            backend, the workspace root.

    Returns:
        The application.
    """
    from orchestrator.api.app import create_app as create_api_app

    return mount_dashboard(create_api_app(**api_kwargs), prefix=prefix)

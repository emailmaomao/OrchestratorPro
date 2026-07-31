"""Tests for the dashboard backend and the shape of what it serves.

Two kinds of test live here. The first exercises the server: the shell renders,
assets are served with the right types, deep links resolve, and the API is
untouched by the mount. The second inspects the assets as text — the import
graph resolves, nothing reaches for a CDN, nothing builds markup from a string,
and every endpoint the client calls exists in the API's own document.

The second kind is not a substitute for opening a browser. It is the set of
mistakes that would otherwise be found by opening a browser at exactly the
wrong moment.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from orchestrator.api.app import create_app as create_api_app
from orchestrator.dashboard.app import (
    DASHBOARD_PREFIX,
    STATIC_ROOT,
    create_app,
    index_html,
    mount_dashboard,
)

JS_ROOT = STATIC_ROOT / "assets" / "js"


@pytest.fixture
def client() -> TestClient:
    """A client for the API with the dashboard mounted."""
    with TestClient(create_app()) as test_client:
        yield test_client


def js_modules() -> list[Path]:
    """Every shipped JavaScript module."""
    return sorted(JS_ROOT.rglob("*.js"))


def source_of(module: Path) -> str:
    """Read one module."""
    return module.read_text(encoding="utf-8")


def code_of(module: Path) -> str:
    """Read one module with its comments removed.

    A token check should read code, not prose. A module that documents the rule
    it obeys must not fail the test for mentioning it.
    """
    from tests.dashboard.test_frontend_units import strip_comments

    return strip_comments(source_of(module))


class TestShell:
    """The document the browser loads first."""

    def test_the_dashboard_is_served(self, client: TestClient) -> None:
        response = client.get(f"{DASHBOARD_PREFIX}/")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert "OrchestratorPro" in response.text

    def test_the_root_redirects_to_it(self, client: TestClient) -> None:
        response = client.get("/", follow_redirects=False)

        assert response.status_code == 307
        # With the trailing slash, so the shell's relative <base> resolves.
        assert response.headers["location"] == f"{DASHBOARD_PREFIX}/"

    def test_a_deep_link_serves_the_same_document(self, client: TestClient) -> None:
        """The history API owns the path; a 404 would make links unshareable."""
        deep = client.get(f"{DASHBOARD_PREFIX}/runs/run_01ABC")

        assert deep.status_code == 200
        assert "assets/js/app.js" in deep.text

    def test_the_base_is_pointed_at_the_mount(self, client: TestClient) -> None:
        assert '<base href="/ui/"' in client.get(f"{DASHBOARD_PREFIX}/").text

    def test_the_shell_is_not_cached(self, client: TestClient) -> None:
        """A stale shell after an upgrade loads modules that no longer match."""
        assert client.get(f"{DASHBOARD_PREFIX}/").headers["cache-control"] == "no-store"

    def test_it_can_be_mounted_somewhere_else(self) -> None:
        app = mount_dashboard(create_api_app(), prefix="/console")
        with TestClient(app) as client:
            response = client.get("/console/")

        assert response.status_code == 200
        assert '<base href="/console/"' in response.text

    def test_the_root_redirect_can_be_declined(self) -> None:
        app = mount_dashboard(create_api_app(), prefix="/ui", redirect_root=False)
        with TestClient(app) as client:
            assert client.get("/", follow_redirects=False).status_code == 404

    def test_the_placeholder_never_reaches_the_browser(self, client: TestClient) -> None:
        assert "__BASE__" not in client.get(f"{DASHBOARD_PREFIX}/").text

    def test_it_says_what_to_do_without_javascript(self, client: TestClient) -> None:
        """A blank page is a bad answer; the API is right there."""
        body = client.get(f"{DASHBOARD_PREFIX}/").text

        assert "<noscript>" in body
        assert "/openapi.json" in body


class TestAssets:
    """What the browser fetches next."""

    def test_the_stylesheet_is_served(self, client: TestClient) -> None:
        response = client.get(f"{DASHBOARD_PREFIX}/assets/css/main.css")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/css")

    def test_the_entry_module_is_served_as_javascript(self, client: TestClient) -> None:
        """A wrong MIME type makes a browser refuse an ES module outright."""
        response = client.get(f"{DASHBOARD_PREFIX}/assets/js/app.js")

        assert response.status_code == 200
        assert "javascript" in response.headers["content-type"]

    def test_every_page_module_is_served(self, client: TestClient) -> None:
        for module in js_modules():
            relative = module.relative_to(STATIC_ROOT).as_posix()
            response = client.get(f"{DASHBOARD_PREFIX}/{relative}")
            assert response.status_code == 200, relative

    def test_a_missing_asset_is_a_404(self, client: TestClient) -> None:
        assert client.get(f"{DASHBOARD_PREFIX}/assets/js/ghost.js").status_code == 404

    def test_assets_cannot_be_used_to_escape_the_directory(
        self, client: TestClient
    ) -> None:
        response = client.get(f"{DASHBOARD_PREFIX}/assets/../../../etc/passwd")
        assert response.status_code in (200, 404)
        assert "root:" not in response.text


class TestTheApiIsUnharmed:
    """Mounting a UI must not change the thing it is a UI for."""

    def test_the_api_still_answers(self, client: TestClient) -> None:
        assert client.get("/health").status_code == 200
        assert client.get("/runs").json() == []

    def test_the_schema_still_generates(self, client: TestClient) -> None:
        assert client.get("/openapi.json").json()["info"]["title"] == "OrchestratorPro"

    def test_the_dashboard_is_absent_from_the_schema(self, client: TestClient) -> None:
        """It is a client of the API, not part of its contract."""
        paths = client.get("/openapi.json").json()["paths"]

        assert not any(path.startswith(DASHBOARD_PREFIX) for path in paths)

    def test_the_api_can_be_served_without_the_dashboard(self) -> None:
        with TestClient(create_api_app()) as client:
            assert client.get("/health").status_code == 200
            assert client.get(f"{DASHBOARD_PREFIX}/").status_code == 404


class TestIndexHtml:
    """The document, as a function."""

    def test_the_base_is_substituted(self) -> None:
        assert '<base href="/somewhere/"' in index_html("/somewhere")

    def test_a_trailing_slash_is_not_doubled(self) -> None:
        assert '<base href="/somewhere/"' in index_html("/somewhere/")

    def test_it_references_the_entry_module(self) -> None:
        assert 'src="assets/js/app.js"' in index_html()


class TestNoServerSideKnowledge:
    """The dashboard must not grow a second idea of what a run is."""

    def test_it_imports_nothing_below_the_api(self) -> None:
        forbidden = (
            "orchestrator.core",
            "orchestrator.task",
            "orchestrator.agent",
            "orchestrator.workflow",
            "orchestrator.builder",
            "orchestrator.git_manager",
            "orchestrator.test_runner",
            "orchestrator.provider",
        )
        offenders: list[str] = []

        for path in sorted(Path(__file__).parents[2].glob("orchestrator/dashboard/*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                offenders.extend(
                    f"{path.name}: {name}" for name in names if name.startswith(forbidden)
                )

        assert offenders == [], f"the dashboard reached past the API: {offenders}"

    def test_the_backend_renders_nothing_from_domain_data(self) -> None:
        """No template, no serialization — one file, read and served."""
        source = (Path(__file__).parents[2] / "orchestrator/dashboard/app.py").read_text(
            encoding="utf-8"
        )

        assert "jinja" not in source.lower()
        assert "RunStore" not in source
        assert "AppState" not in source


class TestFrontendStructure:
    """Properties of the shipped modules, checked as text."""

    def test_every_import_resolves(self) -> None:
        """A broken import is a blank page and a console message nobody sees."""
        pattern = re.compile(r"""^\s*import\s.*?from\s+['"](\..*?)['"]""", re.MULTILINE)
        missing: list[str] = []

        for module in js_modules():
            for target in pattern.findall(source_of(module)):
                resolved = (module.parent / target).resolve()
                if not resolved.is_file():
                    missing.append(f"{module.name} -> {target}")

        assert missing == [], f"unresolvable imports: {missing}"

    def test_nothing_is_loaded_from_another_origin(self) -> None:
        """Local-first: the UI works on a machine with no internet access."""
        offenders: list[str] = []
        for path in [*js_modules(), STATIC_ROOT / "index.html", STATIC_ROOT / "assets/css/main.css"]:
            text = source_of(path)
            for needle in ("//cdn.", "https://unpkg", "https://cdnjs", "googleapis.com"):
                if needle in text:
                    offenders.append(f"{path.name}: {needle}")

        assert offenders == [], f"external resources: {offenders}"

    def test_no_module_builds_markup_from_a_string(self) -> None:
        """Goals, prompts, and diagnostics are agent output. They are text."""
        offenders: list[str] = []
        for module in js_modules():
            text = code_of(module)
            for needle in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
                if needle in text:
                    offenders.append(f"{module.name}: {needle}")

        assert offenders == [], f"markup built from strings: {offenders}"

    def test_no_module_uses_eval(self) -> None:
        offenders = [
            module.name
            for module in js_modules()
            if re.search(r"\beval\s*\(|new\s+Function\s*\(", source_of(module))
        ]
        assert offenders == []

    def test_every_page_module_exports_a_page(self) -> None:
        for module in sorted((JS_ROOT / "pages").glob("*.js")):
            text = source_of(module)
            assert re.search(r"export const \w+Page = \{", text), module.name
            assert "title:" in text, f"{module.name} has no title"
            assert "render(" in text, f"{module.name} has no render"

    def test_every_route_points_at_a_module_that_exists(self) -> None:
        app_source = source_of(JS_ROOT / "app.js")
        pages = re.findall(r"page:\s*(\w+)", app_source)
        assert pages, "the route table is empty"

        imported = set(re.findall(r"import \{([^}]*)\} from '\./pages/", app_source))
        names = {name.strip() for group in imported for name in group.split(",")}

        assert set(pages) <= names, f"routes referencing unimported pages: {set(pages) - names}"

    def test_the_navigation_matches_the_route_table(self) -> None:
        """A nav link with no route is a dead end an operator will find first."""
        document = source_of(STATIC_ROOT / "index.html")
        nav = set(re.findall(r'data-route="([\w-]+)"', document))
        sections = set(re.findall(r"section:\s*'([\w-]+)'", source_of(JS_ROOT / "app.js")))

        assert nav <= sections, f"nav links with no route: {sorted(nav - sections)}"

    def test_every_module_carries_a_file_comment(self) -> None:
        """These are read by whoever inherits them; a bare file is a bad start."""
        undocumented = [
            module.name for module in js_modules() if not source_of(module).startswith("/**")
        ]
        assert undocumented == []


class TestClientMatchesTheApi:
    """The client and the server agree about what exists."""

    def _paths(self, client: TestClient) -> set[str]:
        return set(client.get("/openapi.json").json()["paths"])

    def test_every_endpoint_the_client_calls_exists(self, client: TestClient) -> None:
        """A typo in a path is a 404 at runtime and nothing at review time."""
        source = source_of(JS_ROOT / "api.js")
        declared = self._paths(client)

        called = set(re.findall(r"""request\(\s*['"`]([^'"`$]+)['"`]""", source))
        called |= {
            re.sub(r"\$\{[^}]+\}", "{param}", match)
            for match in re.findall(r"request\(\s*`([^`]+)`", source)
        }

        # FastAPI serves its own document but does not list it in `paths`.
        well_known = {"/openapi.json", "/docs"}

        missing: list[str] = []
        for path in called:
            if path in well_known:
                continue
            template = re.sub(r"\{param\}", "{x}", path.split("?")[0])
            if not _matches_any(template, declared):
                missing.append(path)

        assert missing == [], f"client calls endpoints the API does not have: {missing}"

    def test_the_streaming_paths_exist(self, client: TestClient) -> None:
        declared = self._paths(client)

        assert "/events" in declared
        assert "/runs/{run_id}/events" in declared

    def test_the_log_endpoint_exists(self, client: TestClient) -> None:
        """The dashboard reads history through it rather than holding a stream."""
        assert "/runs/{run_id}/log" in self._paths(client)


def _matches_any(template: str, declared: set[str]) -> bool:
    """Whether a client path template corresponds to a declared API path."""
    normalized = re.sub(r"\{[^}]+\}", "{}", template.rstrip("/")) or "/"
    for path in declared:
        if re.sub(r"\{[^}]+\}", "{}", path.rstrip("/")) == normalized:
            return True
    return False

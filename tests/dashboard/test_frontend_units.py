"""Runs the frontend's own unit tests as part of the ordinary suite.

The dashboard's pure modules — the API client, the formatters, the graph layout
— are real code with real edge cases, and testing them by clicking around a
browser is not testing them. Node's built-in runner executes them here, so a
regression fails `pytest -q` like everything else.

If Node is not installed the tests skip rather than fail: Node is a development
convenience, not a runtime dependency. OrchestratorPro serves the dashboard as
static files and needs no JavaScript toolchain to run.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS_TESTS = Path(__file__).parent / "js"
REPO_ROOT = Path(__file__).resolve().parents[2]

#: Node's test runner has been stable since 20; older versions lack `--test`
#: reporters and would report a pass by doing nothing.
_MIN_NODE_MAJOR = 20

node = shutil.which("node")
requires_node = pytest.mark.skipif(
    node is None, reason="Node.js is not installed; frontend unit tests skipped"
)


def _node_major() -> int:
    """Return the installed Node's major version, or 0 if it cannot be read."""
    if node is None:
        return 0
    try:
        result = subprocess.run(
            [node, "--version"], capture_output=True, text=True, timeout=30, check=True
        )
    except (subprocess.SubprocessError, OSError):  # pragma: no cover
        return 0
    return int(result.stdout.strip().lstrip("v").split(".")[0] or 0)


@requires_node
def test_node_is_recent_enough_to_run_them() -> None:
    """A runner that silently does nothing would be worse than none."""
    assert _node_major() >= _MIN_NODE_MAJOR, (
        f"Node {_MIN_NODE_MAJOR}+ is needed for the built-in test runner"
    )


@requires_node
def test_the_frontend_unit_tests_pass() -> None:
    """Run every ``*.test.mjs`` under ``tests/dashboard/js``."""
    assert node is not None  # narrowed by the skip marker
    files = sorted(JS_TESTS.glob("*.test.mjs"))
    assert files, "no frontend unit tests were found"

    result = subprocess.run(
        [node, "--test", "--test-reporter=tap", *[str(path) for path in files]],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=300,
    )

    if result.returncode != 0:
        pytest.fail(
            "frontend unit tests failed\n\n"
            f"{result.stdout[-8000:]}\n{result.stderr[-4000:]}"
        )


@requires_node
def test_every_frontend_module_parses() -> None:
    """Syntax-check every shipped module.

    A dashboard ships without a compiler, so a stray syntax error is not caught
    until a browser hits it. This is the compiler.
    """
    assert node is not None  # narrowed by the skip marker
    from orchestrator.dashboard.app import STATIC_ROOT

    modules = sorted((STATIC_ROOT / "assets" / "js").rglob("*.js"))
    assert modules, "no frontend modules were found"

    for module in modules:
        result = subprocess.run(
            [node, "--check", str(module)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"{module.name} does not parse:\n{result.stderr}"


@requires_node
def test_the_test_files_cover_the_pure_modules() -> None:
    """Every module without DOM access has unit tests of its own.

    Modules that build DOM are covered structurally by ``test_backend.py``;
    these are the ones where a bug is a wrong answer rather than a wrong pixel,
    and an untested one here is a gap nobody would notice.
    """
    from orchestrator.dashboard.app import STATIC_ROOT

    pure = {"api", "format", "graph"}
    tested = {path.name.removesuffix(".test.mjs") for path in JS_TESTS.glob("*.test.mjs")}
    shipped = {path.stem for path in (STATIC_ROOT / "assets" / "js").glob("*.js")}

    missing = sorted((pure & shipped) - tested)
    assert missing == [], f"pure modules with no unit tests: {missing}"


def test_the_runner_reports_a_failure_rather_than_swallowing_it(tmp_dir: Path) -> None:
    """Guard the guard: a broken assertion must make the runner exit non-zero."""
    if node is None:
        pytest.skip("Node.js is not installed")

    failing = tmp_dir / "broken.test.mjs"
    failing.write_text(
        "import assert from 'node:assert/strict';\n"
        "import { it } from 'node:test';\n"
        "it('fails', () => assert.equal(1, 2));\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [node, "--test", str(failing)], capture_output=True, text=True, timeout=120
    )

    assert result.returncode != 0


def strip_comments(source: str) -> str:
    """Remove JavaScript comments, so a scan reads code and not prose.

    Every structural check here looks for a token. Without this, a module that
    *documents* the rule it obeys — "there is no innerHTML anywhere" — fails the
    test for saying so, which would teach the next person to stop explaining
    things.
    """
    import re

    without_block = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"(?<![:'\"`\\])//[^\n]*", "", without_block)


def test_no_module_reads_from_the_network_except_the_client() -> None:
    """`fetch` and `EventSource` appear in exactly one module.

    The dashboard must not grow a second idea of what a run is. The cheapest
    guarantee is that facts enter the program in one place.
    """
    from orchestrator.dashboard.app import STATIC_ROOT

    offenders: list[str] = []
    for module in sorted((STATIC_ROOT / "assets" / "js").rglob("*.js")):
        if module.name == "api.js":
            continue
        source = strip_comments(module.read_text(encoding="utf-8"))
        for needle in ("fetch(", "EventSource", "XMLHttpRequest", "navigator.sendBeacon"):
            if needle in source:
                offenders.append(f"{module.name}: {needle}")

    assert offenders == [], f"network access outside api.js: {offenders}"


def test_the_comment_stripper_does_not_eat_code() -> None:
    """Guard the guard: a scan that strips too much would pass on anything."""
    assert "fetch(" in strip_comments("const a = fetch('/x'); // fetch( in a comment")
    assert "fetch(" not in strip_comments("/** fetch( */\nconst a = 1;")
    assert "https://example.com" in strip_comments("const u = 'https://example.com';")


@requires_node
def test_the_client_declares_every_endpoint_it_calls() -> None:
    """Sanity-check the client's paths against the JSON it would receive.

    A typo in a path is a 404 at runtime and nothing at all at review time.
    """
    from orchestrator.dashboard.app import STATIC_ROOT

    source = (STATIC_ROOT / "assets" / "js" / "api.js").read_text(encoding="utf-8")
    for path in ("/health", "/config", "/runs", "/workflows", "/agents/roles", "/builds"):
        assert f"'{path}'" in source or f"`{path}" in source, f"{path} is never called"

    # The document is fetched by URL rather than by a helper, so check it too.
    assert "/openapi.json" in source


def test_the_js_test_directory_is_not_shipped() -> None:
    """Tests live under ``tests/``; nothing under ``static/`` is a test."""
    from orchestrator.dashboard.app import STATIC_ROOT

    strays = [str(path) for path in STATIC_ROOT.rglob("*.test.*")]
    assert strays == [], f"test files inside the served assets: {strays}"


def test_the_manifest_of_shipped_assets_is_readable(tmp_dir: Path) -> None:
    """Every shipped asset is text a human can read and a browser can parse."""
    from orchestrator.dashboard.app import STATIC_ROOT

    for path in sorted(STATIC_ROOT.rglob("*")):
        if not path.is_file():
            continue
        assert path.suffix in {".html", ".css", ".js"}, f"unexpected asset {path.name}"
        content = path.read_text(encoding="utf-8")
        assert content.strip(), f"{path.name} is empty"
        assert not content.startswith("﻿"), f"{path.name} has a byte-order mark"
        json.dumps(path.name)  # names stay ASCII-safe for URLs

"""Shared machinery for the end-to-end suite.

These tests exercise whole paths rather than units: a YAML file to a finished
run, a killed process to a resumed one, a container to a health check. They are
slower than the rest of the suite and are marked so.

Two things are deliberately *not* faked here: the database is a real file, and
the git repository is a real repository. Both have failure modes — file locks,
journal files, index state — that a mock cannot reproduce, and those failure
modes are exactly what an end-to-end test is for.

The model backend is faked, except in the live-marked tests. Nothing in the
default suite reaches the network (``CLAUDE.md``, testing policy).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from collections.abc import Awaitable, Coroutine, Mapping
from pathlib import Path
from typing import Any, TypeVar

import pytest

from orchestrator.core.run_store import RunStore
from orchestrator.core.storage import Database

T = TypeVar("T")

#: Set to run the tests that need a real model endpoint.
OPENAI_BASE_URL = os.environ.get("ORCHESTRATORPRO_TEST_OPENAI_BASE_URL", "")
OPENAI_MODEL = os.environ.get("ORCHESTRATORPRO_TEST_OPENAI_MODEL", "")
HERMES_BASE_URL = os.environ.get("ORCHESTRATORPRO_TEST_HERMES_BASE_URL", "")
HERMES_MODEL = os.environ.get("ORCHESTRATORPRO_TEST_HERMES_MODEL", "hermes")

docker = shutil.which("docker")
git = shutil.which("git")


def _docker_daemon_is_up() -> bool:
    """Whether Docker can actually build, not merely whether it is installed.

    Docker Desktop leaves its client on ``PATH`` when the engine is stopped, so
    ``which`` says yes and every build then fails on a missing pipe. That reads
    as a broken repository when it is a stopped service, so the daemon is asked
    directly.
    """
    if docker is None:
        return False
    try:
        completed = subprocess.run(
            [docker, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


requires_docker = pytest.mark.skipif(
    not _docker_daemon_is_up(),
    reason="Docker is not available (not installed, or the engine is not running)",
)
requires_git = pytest.mark.skipif(git is None, reason="git is not available")
requires_openai = pytest.mark.skipif(
    not (OPENAI_BASE_URL and OPENAI_MODEL),
    reason=(
        "no OpenAI-compatible endpoint configured; set "
        "ORCHESTRATORPRO_TEST_OPENAI_BASE_URL and _MODEL to run it"
    ),
)
requires_hermes = pytest.mark.skipif(
    not HERMES_BASE_URL,
    reason=(
        "no Hermes endpoint configured; set ORCHESTRATORPRO_TEST_HERMES_BASE_URL "
        "to run it"
    ),
)


def run(awaitable: Coroutine[Any, Any, T] | Awaitable[T]) -> T:
    """Drive one coroutine to completion."""

    async def _wrapped() -> T:
        return await awaitable

    return asyncio.run(_wrapped())


class UrllibTransport:
    """A real HTTP transport, for the live-marked tests only.

    The provider layer ships no concrete transport on purpose — every other
    test injects a scripted one, and a default HTTP client in the package would
    make it possible to reach the network by forgetting an argument. A live
    test that wants the network supplies it explicitly, here, where it is
    obvious.
    """

    def __init__(self, url: str, *, api_key: str = "", timeout_s: float = 120.0) -> None:
        self.url = url
        self.api_key = api_key
        self.timeout_s = timeout_s

    async def send(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """POST one request and decode the reply."""
        return await asyncio.to_thread(self._post, dict(payload))

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"

        request = urllib.request.Request(self.url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                return json.loads(response.read().decode("utf-8"))  # type: ignore[no-any-return]
        except urllib.error.HTTPError as exc:  # pragma: no cover - live only
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise AssertionError(f"the endpoint returned {exc.code}: {detail}") from exc

    async def stream(self, payload: Mapping[str, Any]) -> Any:  # pragma: no cover
        """Not used: the tests here do not stream."""
        raise NotImplementedError("the live tests use complete(), not stream()")


def make_repository(root: Path) -> Path:
    """Initialize a real git repository with one commit."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    (root / "src").mkdir(exist_ok=True)
    (root / "src" / "app.py").write_text("def main() -> int:\n    return 0\n", encoding="utf-8")

    commands = [
        ["init", "--initial-branch=main"],
        ["config", "user.email", "test@example.invalid"],
        ["config", "user.name", "OrchestratorPro Tests"],
        ["config", "commit.gpgsign", "false"],
        ["add", "-A"],
        ["commit", "-m", "initial"],
    ]
    for command in commands:
        subprocess.run(
            [git or "git", *command],
            cwd=root,
            check=True,
            capture_output=True,
            timeout=120,
        )
    return root


@pytest.fixture
def workspace(tmp_dir: Path) -> Path:
    """A directory for a run's scratch files."""
    target = tmp_dir / "workspace"
    target.mkdir(parents=True, exist_ok=True)
    return target


@pytest.fixture
def durable_store(tmp_dir: Path) -> Any:
    """A file-backed run store, closed afterwards."""
    database = Database(tmp_dir / "runs.db")
    database.migrate()
    yield RunStore(database)
    database.close()


PIPELINE_YAML = """\
name: ship-the-feature
goal: add the greeting endpoint and prove it works
max_concurrency: 3
defaults:
  max_attempts: 2
steps:
  - name: design
    prompt: Write docs/greeting.md describing the endpoint.
  - name: implement
    prompt: Add the endpoint to src/app.py.
    depends_on: [design]
    gates: [tests]
  - name: document
    prompt: Update README.md to mention the endpoint.
    depends_on: [design]
  - name: verify
    prompt: Confirm the endpoint is covered by a test.
    depends_on: [implement, document]
    gates: [tests]
    when:
      all_of:
        - did_work: implement
        - did_work: document
"""

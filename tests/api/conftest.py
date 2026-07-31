"""Shared fixtures for control-plane tests.

Every test runs the real application over a real (in-memory) database through
Starlette's test client, so routing, validation, serialization, and the error
envelope are all exercised as a client would meet them. What is substituted is
the execution backend: runs are performed by a scripted executor, and builds by
a scripted process runner, so nothing here calls a model or launches a compiler.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.api.state import AppState
from orchestrator.core.config import OrchestratorConfig
from orchestrator.task.dispatcher import AttemptOutcome
from orchestrator.task.model import Task
from orchestrator.test_runner.execution import ProcessResult, ScriptedRunner

#: How long a test waits for a background run or build before giving up. Runs
#: here finish in milliseconds; the ceiling exists so a hang fails loudly.
DEADLINE_S = 10.0


class RecordingExecutor:
    """A step executor returning prepared outcomes, keyed by step name."""

    def __init__(
        self,
        outcomes: Mapping[str, AttemptOutcome] | None = None,
        *,
        default: AttemptOutcome | None = None,
        delay_s: float = 0.0,
    ) -> None:
        self.outcomes = dict(outcomes or {})
        self.default = default or AttemptOutcome.success()
        self.delay_s = delay_s
        self.calls: list[str] = []
        self._plan: Any = None

    async def __call__(self, task: Task, attempt: int) -> AttemptOutcome:
        """Record the call and return the scripted outcome."""
        import asyncio

        name = self._plan.step_name(task.id) if self._plan else task.title
        self.calls.append(name)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        return self.outcomes.get(name, self.default)

    def factory(self) -> Callable[..., Any]:
        """Return a factory the engine can call."""

        def build(plan: Any, run_id: Any, emitter: Any) -> Any:
            self._plan = plan
            return self

        return build


def make_app(
    *,
    executor: RecordingExecutor | None = None,
    process_runner: Any = None,
    workspace_root: Path | None = None,
    config: OrchestratorConfig | None = None,
) -> Any:
    """Build an application over a fresh in-memory database."""
    state = AppState.in_memory(config=config or OrchestratorConfig())
    state.workspace_root = workspace_root
    state.process_runner = process_runner
    if executor is not None:
        state.executor_factory = executor.factory()
    return create_app(state=state)


@pytest.fixture
def executor() -> RecordingExecutor:
    """An executor whose every step succeeds."""
    return RecordingExecutor()


@pytest.fixture
def client(executor: RecordingExecutor) -> Iterator[TestClient]:
    """A client for an application that can execute runs."""
    with TestClient(make_app(executor=executor)) as test_client:
        yield test_client


@pytest.fixture
def bare_client() -> Iterator[TestClient]:
    """A client for an application with no execution backend."""
    with TestClient(make_app()) as test_client:
        yield test_client


@pytest.fixture
def state_of() -> Callable[[TestClient], AppState]:
    """Reach the application state behind a client."""

    def resolve(test_client: TestClient) -> AppState:
        return test_client.app.state.orchestrator  # type: ignore[no-any-return,union-attr]

    return resolve


def new_run(client: TestClient, goal: str = "ship it") -> str:
    """Create a run and return its identifier."""
    response = client.post("/runs", json={"goal": goal})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def new_task(client: TestClient, run_id: str, **body: Any) -> Mapping[str, Any]:
    """Add a task to a run and return it."""
    payload = {"title": "do the thing", "prompt": "please do it", **body}
    response = client.post(f"/runs/{run_id}/tasks", json=payload)
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


LINEAR_WORKFLOW: Mapping[str, Any] = {
    "name": "ship",
    "goal": "ship the feature",
    "steps": [
        {"name": "design", "prompt": "design it"},
        {"name": "build", "prompt": "build it", "depends_on": ["design"]},
        {"name": "verify", "prompt": "verify it", "depends_on": ["build"]},
    ],
}


def register(client: TestClient, workflow: Mapping[str, Any] | None = None) -> str:
    """Register a workflow and return its name."""
    response = client.post("/workflows", json=dict(workflow or LINEAR_WORKFLOW))
    assert response.status_code == 201, response.text
    return str(response.json()["name"])


def start(client: TestClient, name: str = "ship", **body: Any) -> str:
    """Start a workflow and return the run identifier."""
    response = client.post(f"/workflows/{name}/runs", json=dict(body))
    assert response.status_code == 202, response.text
    return str(response.json()["id"])


def wait_for_run(client: TestClient, run_id: str, *, deadline_s: float = DEADLINE_S) -> Mapping[str, Any]:
    """Poll a run until it stops executing.

    Returns:
        The final status payload.

    Raises:
        AssertionError: If it is still running when the deadline passes, which
            means something hung rather than that the machine was slow.
    """
    until = time.monotonic() + deadline_s
    while time.monotonic() < until:
        status = client.get(f"/runs/{run_id}/status")
        assert status.status_code == 200, status.text
        body = status.json()
        if not body["active"]:
            return body  # type: ignore[no-any-return]
        time.sleep(0.005)
    raise AssertionError(f"run {run_id} did not finish within {deadline_s}s")


def wait_for_build(client: TestClient, build_id: str, *, deadline_s: float = DEADLINE_S) -> Mapping[str, Any]:
    """Poll a build until it reports a terminal status."""
    until = time.monotonic() + deadline_s
    while time.monotonic() < until:
        response = client.get(f"/builds/{build_id}")
        assert response.status_code == 200, response.text
        body = response.json()
        if body["status"] != "running":
            return body  # type: ignore[no-any-return]
        time.sleep(0.005)
    raise AssertionError(f"build {build_id} did not finish within {deadline_s}s")


async def collect_sse(
    app: Any, path: str, *, frames: int = 1, timeout_s: float = 5.0
) -> tuple[int, dict[str, str], str]:
    """Drive the ASGI app directly and collect an SSE response.

    Starlette's ``TestClient`` buffers a whole response before returning it, so
    it can never read an endless stream: it would run the endpoint forever. This
    harness speaks ASGI to the application itself, collects ``frames`` data
    frames, and then delivers ``http.disconnect`` — which is exactly what a real
    client hanging up looks like, and is the path the endpoint has to handle
    anyway.

    Returns:
        The status code, the response headers, and the body received so far.
    """
    import asyncio

    url, _, query = path.partition("?")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "path": url,
        "raw_path": url.encode(),
        "root_path": "",
        "scheme": "http",
        "query_string": query.encode(),
        "headers": [(b"host", b"testserver")],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "state": {},
    }

    disconnected = asyncio.Event()
    sent_request = False
    chunks: list[bytes] = []
    status = 0
    headers: dict[str, str] = {}

    async def receive() -> Mapping[str, Any]:
        nonlocal sent_request
        if not sent_request:
            sent_request = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: Mapping[str, Any]) -> None:
        nonlocal status, headers
        if message["type"] == "http.response.start":
            status = int(message["status"])
            headers = {
                key.decode().lower(): value.decode()
                for key, value in message.get("headers", ())
            }
        elif message["type"] == "http.response.body":
            chunks.append(bytes(message.get("body", b"")))
            # Every SSE frame — event or heartbeat comment — ends in a blank
            # line, so counting those counts completed frames.
            if b"".join(chunks).count(b"\n\n") >= frames:
                disconnected.set()

    try:
        await asyncio.wait_for(app(scope, receive, send), timeout=timeout_s)
    except TimeoutError:  # pragma: no cover - a hung endpoint is a test failure
        disconnected.set()
        raise AssertionError(f"{path} did not finish within {timeout_s}s") from None

    return status, headers, b"".join(chunks).decode()


def sse(app: Any, path: str, *, frames: int = 1) -> tuple[int, dict[str, str], str]:
    """Synchronous wrapper around :func:`collect_sse`."""
    import asyncio

    return asyncio.run(collect_sse(app, path, frames=frames))


def parse_frames(body: str) -> list[dict[str, Any]]:
    """Parse an SSE body into ``{event, data, id}`` dictionaries."""
    import json

    parsed: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in body.splitlines():
        if line.startswith("id: "):
            current["id"] = line[len("id: ") :]
        elif line.startswith("event: "):
            current["event"] = line[len("event: ") :]
        elif line.startswith("data: "):
            current["data"] = json.loads(line[len("data: ") :])
            parsed.append(current)
            current = {}
    return parsed


def comments(body: str) -> list[str]:
    """Every comment line in an SSE body — heartbeats and notices."""
    return [line for line in body.splitlines() if line.startswith(":")]


def ok_process(stdout: str = "") -> ProcessResult:
    """A build command that succeeded."""
    return ProcessResult(exit_code=0, stdout=stdout)


def failing_process(stdout: str = "") -> ProcessResult:
    """A build command that failed."""
    return ProcessResult(exit_code=1, stdout=stdout)


def scripted_runner(*results: ProcessResult) -> ScriptedRunner:
    """A process runner returning prepared results, then succeeding."""
    return ScriptedRunner(results, default=ProcessResult(exit_code=0))


PYTHON_PROJECT: Mapping[str, str] = {
    "pyproject.toml": "[project]\nname = 'demo'\n",
    "core/__init__.py": "",
    "core/util.py": "def helper() -> int:\n    return 1\n",
    "app/__init__.py": "",
    "app/main.py": "import core\n",
}

GCC_OUTPUT = "src/main.c:12:5: error: 'x' undeclared\n"


def write_project(root: Path, files: Mapping[str, str] | None = None) -> Path:
    """Write a small project tree and return its root."""
    for name, content in (files or PYTHON_PROJECT).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root

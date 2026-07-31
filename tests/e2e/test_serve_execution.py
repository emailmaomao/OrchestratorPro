"""The `serve` path, actually executing something.

`cmd_serve` is the only composition root in production: `api/` never builds an
executor, and before `3f59746` nothing else did either, so a served instance
reported ``execution_available: false`` and returned 503 on every attempt to run
work. Every layer was tested; nothing tested the assembly, because each test
assembled the system itself.

The test that shipped with that fix asserts ``execution_available`` is *present
and boolean* in the dry-run report. That passes whether the value is ``true`` or
``false``, and it never runs a task — it would still pass if the wiring were
deleted and the key hard-coded.

This file closes that gap. It drives the real `cmd_serve` path:

* ``uvicorn.run`` is patched to **capture** the application instead of binding a
  port, so everything `cmd_serve` builds — hardening, auth, the executor factory
  — is exactly what the test then drives.
* ``build_default_registry`` is patched to inject a **scripted transport**, so no
  credential and no network are involved. The provider itself is the real one.
* The workflow is then registered and started over HTTP, exactly as an operator
  or Hermes would.

The served pipeline has two modes, and both are covered here. **Full mode**
(``--repo`` names a Git repository): worktree per attempt, commits, gates from
``[gates]``, merges to the run's integration branch. **Fallback mode** (no
repository, or Git setup failed): a plain shared directory in which nothing is
verified — a legitimate dry-run, so its limits are asserted deliberately, with
concurrency clamped to 1 so the shared directory can never be entered in
parallel.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.cli import EXIT_FAILED, EXIT_OK, main
from orchestrator.core.events import EventType
from orchestrator.provider.registry import ProviderRegistry
from orchestrator.provider.registry import build_default_registry as _real_registry
from tests.e2e.conftest import make_repository
from tests.provider.conftest import FakeTransport

#: What the scripted model returns: one turn, no tool calls, a clean stop. The
#: runtime treats a turn with no tool calls as a completed attempt, which is the
#: shortest path that still exercises runtime → provider → outcome → event log.
_REPLY: Mapping[str, Any] = {
    "content": [{"type": "text", "text": "Done: nothing needed changing."}],
    "stop_reason": "end_turn",
    "model": "claude-opus-5",
    "usage": {"input_tokens": 11, "output_tokens": 7},
}

_WORKFLOW: Mapping[str, Any] = {
    "name": "smoke",
    "goal": "prove the served composition root executes",
    # expects_changes=False throughout this file: the scripted transports
    # return text and never call a tool, so no attempt here writes a file.
    # These tests are about the pipeline — gates, merges, transcripts, usage —
    # not about producing a diff, and the no-op guard would otherwise fail
    # every one of them for being what they are.
    "steps": [
        {
            "name": "only-step",
            "prompt": "Report what you see and finish.",
            "expects_changes": False,
        }
    ],
}

#: Generous, because this drives a real event loop through a test client. A hang
#: is a failure, not a slow machine.
_DEADLINE_S = 20.0


class _ServedApp:
    """What `cmd_serve` built, plus the transport it was given."""

    def __init__(self, app: FastAPI, transport: FakeTransport) -> None:
        self.app = app
        self.transport = transport

    @property
    def state(self) -> Any:
        """The application state `cmd_serve` assembled."""
        return self.app.state.orchestrator


def _serve(tmp_dir: Path, monkeypatch: pytest.MonkeyPatch) -> _ServedApp:
    """Run `cmd_serve` far enough to get the application it would have served."""
    transport = FakeTransport(response=dict(_REPLY))

    def registry_with_scripted_transport(config: Any, **_: Any) -> ProviderRegistry:
        # The real builder, with the transport seam filled. Patching here rather
        # than constructing a registry by hand keeps the provider, its
        # translation, and its capability declaration in the path under test.
        # `_real_registry` is bound at import, before the patch: re-importing the
        # name here would resolve to this function and recurse.
        return _real_registry(config, transports={"claude": transport})

    monkeypatch.setattr(
        "orchestrator.provider.registry.build_default_registry",
        registry_with_scripted_transport,
    )

    captured: dict[str, FastAPI] = {}

    def capture(app: FastAPI, **_: Any) -> None:
        captured["app"] = app

    monkeypatch.setattr(uvicorn, "run", capture)

    code = main(
        [
            "--config", "/nonexistent",
            "--no-env",
            "--database", str(tmp_dir / "runs.db"),
            "serve",
            "--workspace", str(tmp_dir / "work"),
        ]
    )

    assert code == EXIT_OK
    assert "app" in captured, "cmd_serve never reached uvicorn.run"
    return _ServedApp(captured["app"], transport)


@pytest.fixture
def served(tmp_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_ServedApp]:
    """The application `orchestratorpro serve` would have bound to a port."""
    app = _serve(tmp_dir, monkeypatch)
    try:
        yield app
    finally:
        # `cmd_serve` never closes the database: in production the process exits
        # and the handle goes with it. Here the file lives in a temporary
        # directory, and Windows will not delete a file that is still open.
        app.state.database.close()


def _run_to_completion(client: TestClient) -> tuple[str, Mapping[str, Any]]:
    """Register the smoke workflow, start it, and wait for it to settle."""
    registered = client.post("/workflows", json=dict(_WORKFLOW))
    assert registered.status_code == 201, registered.text

    started = client.post("/workflows/smoke/runs", json={})
    assert started.status_code == 202, started.text
    run_id = started.json()["id"]

    until = time.monotonic() + _DEADLINE_S
    while time.monotonic() < until:
        status = client.get(f"/runs/{run_id}/status")
        assert status.status_code == 200, status.text
        body = status.json()
        if not body["active"]:
            return run_id, body
        time.sleep(0.01)
    raise AssertionError(f"run {run_id} did not settle within {_DEADLINE_S}s")


def _log(client: TestClient, run_id: str) -> list[Mapping[str, Any]]:
    """Every recorded event for a run, oldest first."""
    response = client.get(f"/runs/{run_id}/log", params={"limit": 500})
    assert response.status_code == 200, response.text
    return list(response.json())


class TestServedCompositionRoot:
    """What `cmd_serve` assembles, exercised through the HTTP surface."""

    def test_the_served_app_has_an_execution_backend(self, served: _ServedApp) -> None:
        """The assertion the dry-run test cannot make: not merely present, true."""
        with TestClient(served.app) as client:
            health = client.get("/health")

        assert health.status_code == 200, health.text
        assert health.json()["execution_available"] is True
        assert served.state.can_execute is True

    def test_a_workflow_runs_to_a_terminal_state(self, served: _ServedApp) -> None:
        """The whole point: goal in, run out, through the production wiring."""
        with TestClient(served.app) as client:
            run_id, final = _run_to_completion(client)

            assert final["complete"] is True, final
            assert final["succeeded"] == 1, final
            assert final["failed"] == 0, final
            assert final["healthy"] is True, final

            types = {event["type"] for event in _log(client, run_id)}

        assert EventType.TASK_SUCCEEDED.value in types
        assert EventType.RUN_FINISHED.value in types

    def test_the_agent_reached_the_model_provider(self, served: _ServedApp) -> None:
        """A run that never called a model would prove nothing about the wiring."""
        with TestClient(served.app) as client:
            _run_to_completion(client)

        assert served.transport.sent, "the agent never reached the provider"
        payload = served.transport.last
        assert payload["model"]
        assert payload["messages"], "the task prompt never reached the model"
        # Cheap regression on a rule that costs an HTTP 400 when broken.
        for banned in ("temperature", "top_p", "top_k", "budget_tokens"):
            assert banned not in payload, f"{banned} must never be sent"

    def test_the_run_survives_a_replay(self, served: _ServedApp) -> None:
        """Reads replay the log, so a completed run must reconstruct from it."""
        with TestClient(served.app) as client:
            run_id, _ = _run_to_completion(client)
            detail = client.get(f"/runs/{run_id}")

        assert detail.status_code == 200, detail.text
        assert detail.json()["tasks"], "a replayed run reported no tasks"


def _serve_repo(
    tmp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    gate_command: str,
) -> tuple[_ServedApp, Path]:
    """Serve in full mode against a real temporary Git repository.

    The gate command comes from ``[gates]`` in a repository-local
    ``orchestrator.toml``, exactly as an operator would configure it, and uses
    the ``exit_code`` parser so the test controls the verdict precisely.
    """
    repo_root = make_repository(tmp_dir / "repo")
    (repo_root / "orchestrator.toml").write_text(
        f'[gates]\ntest_command = "{gate_command}"\nparser = "exit_code"\n',
        encoding="utf-8",
    )

    transport = FakeTransport(response=dict(_REPLY))

    def registry_with_scripted_transport(config: Any, **_: Any) -> ProviderRegistry:
        return _real_registry(config, transports={"anthropic": transport})

    monkeypatch.setattr(
        "orchestrator.provider.registry.build_default_registry",
        registry_with_scripted_transport,
    )

    captured: dict[str, FastAPI] = {}
    monkeypatch.setattr(
        uvicorn, "run", lambda app, **_: captured.__setitem__("app", app)
    )

    code = main(
        [
            "--config", "/nonexistent",
            "--no-env",
            "--repo", str(repo_root),
            "--database", str(tmp_dir / "runs.db"),
            "serve",
            "--workspace", str(tmp_dir / "work"),
        ]
    )
    assert code == EXIT_OK
    assert "app" in captured, "cmd_serve never reached uvicorn.run"
    return _ServedApp(captured["app"], transport), repo_root


def _gated_workflow(*, max_attempts: int = 1) -> dict[str, Any]:
    """The smoke workflow, with its one step gated on ``tests``."""
    return {
        "name": "smoke",
        "goal": "prove the served full pipeline isolates, gates, and merges",
        "steps": [
            {
                "name": "only-step",
                "prompt": "Report what you see and finish.",
                "gates": ["tests"],
                "max_attempts": max_attempts,
                "expects_changes": False,
            }
        ],
    }


def _branches(repo_root: Path) -> str:
    """Every branch in the repository, one per line."""
    result = subprocess.run(
        ["git", "branch", "--list", "--all", "--format=%(refname:short)"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return result.stdout


class TestServedFullPipeline:
    """`serve --repo <git repo>`: isolation, gates, and merges, end to end.

    These replace the ``TestWhatIsNotWiredYet`` pins that recorded the old
    fallback-only wiring — the limitation they pinned is fixed, so they became
    the positive assertions they promised to become.
    """

    def test_a_passing_gate_lets_the_run_succeed(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Worktree, gate, merge, and a clean operator checkout, in one pass."""
        served, repo_root = _serve_repo(
            tmp_dir, monkeypatch, gate_command="python -c pass"
        )
        try:
            with TestClient(served.app) as client:
                registered = client.post("/workflows", json=_gated_workflow())
                assert registered.status_code == 201, registered.text
                started = client.post("/workflows/smoke/runs", json={})
                assert started.status_code == 202, started.text
                run_id = started.json()["id"]

                until = time.monotonic() + _DEADLINE_S
                while time.monotonic() < until:
                    body = client.get(f"/runs/{run_id}/status").json()
                    if not body["active"]:
                        break
                    time.sleep(0.01)

                assert body["healthy"] is True, body
                assert body["succeeded"] == 1, body

                events = _log(client, run_id)
        finally:
            served.state.database.close()

        gates = [e for e in events if e["type"] == EventType.GATE_EVALUATED.value]
        assert gates, "no gate verdict was recorded"
        assert gates[-1]["payload"]["verdict"] == "passed", gates[-1]

        # Isolation: the attempt ran in its own worktree under the serve
        # workspace, not in the repository's checkout and not in a shared root.
        worktrees = tmp_dir / "work" / "worktrees"
        attempt_dirs = list(worktrees.glob(f"{run_id}/*"))
        assert attempt_dirs, f"no worktree was created under {worktrees}"

        # The merge stage ran: the run's integration branch exists for review.
        assert f"orchestrator/{run_id}/integration" in _branches(repo_root)

        # The operator's checkout was never touched (FR-3.2).
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root, capture_output=True, text=True, check=True, timeout=60,
        ).stdout
        lines = [ln for ln in status.splitlines() if "orchestrator.toml" not in ln]
        assert lines == [], f"the operator's tree changed: {lines}"

        # FR-5.3: the attempt's transcript landed as JSONL beside the database.
        transcripts = list((tmp_dir / "transcripts").rglob("*.jsonl"))
        assert transcripts, "no transcript was written"
        assert transcripts[0].read_text(encoding="utf-8").strip(), (
            "the transcript file is empty"
        )

    def test_a_failing_gate_blocks_acceptance(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The agent cannot mark itself green: a red gate fails the task."""
        served, repo_root = _serve_repo(
            tmp_dir, monkeypatch, gate_command="python -m no_such_module_zz"
        )
        try:
            with TestClient(served.app) as client:
                client.post("/workflows", json=_gated_workflow(max_attempts=1))
                run_id = client.post("/workflows/smoke/runs", json={}).json()["id"]

                until = time.monotonic() + _DEADLINE_S
                while time.monotonic() < until:
                    body = client.get(f"/runs/{run_id}/status").json()
                    if not body["active"]:
                        break
                    time.sleep(0.01)

                assert body["healthy"] is False, body
                assert body["failed"] == 1, body
                events = _log(client, run_id)
        finally:
            served.state.database.close()

        gates = [e for e in events if e["type"] == EventType.GATE_EVALUATED.value]
        assert gates and gates[-1]["payload"]["verdict"] == "failed", gates

        # Rejected work never reaches the integration branch. The branch now
        # exists from the start — attempts branch *from* it, so that a later
        # step sees earlier merged work and a post-conflict retry is rebased
        # by construction. Its existence is therefore no longer evidence
        # either way; what matters, and what this asserts, is that a rejected
        # attempt put nothing *on* it.
        integration = f"orchestrator/{run_id}/integration"
        assert integration in _branches(repo_root)
        landed = subprocess.run(
            ["git", "log", "--oneline", f"main..{integration}"],
            cwd=repo_root, capture_output=True, text=True, check=True, timeout=60,
        ).stdout.strip()
        assert landed == "", f"a failed gate's work reached integration:\n{landed}"

    def test_concurrent_attempts_get_distinct_worktrees(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two independent steps never share a working tree (FR-3.1)."""
        served, _ = _serve_repo(tmp_dir, monkeypatch, gate_command="python -c pass")
        try:
            with TestClient(served.app) as client:
                client.post(
                    "/workflows",
                    json={
                        "name": "fan",
                        "goal": "two independent steps in parallel",
                        "steps": [
                            {
                                "name": "left",
                                "prompt": "Do the left half.",
                                "expects_changes": False,
                            },
                            {
                                "name": "right",
                                "prompt": "Do the right half.",
                                "expects_changes": False,
                            },
                        ],
                    },
                )
                run_id = client.post("/workflows/fan/runs", json={}).json()["id"]
                until = time.monotonic() + _DEADLINE_S
                while time.monotonic() < until:
                    body = client.get(f"/runs/{run_id}/status").json()
                    if not body["active"]:
                        break
                    time.sleep(0.01)
                assert body["succeeded"] == 2, body
        finally:
            served.state.database.close()

        attempt_dirs = {
            p.name for p in (tmp_dir / "work" / "worktrees" / run_id).iterdir()
        }
        assert len(attempt_dirs) == 2, attempt_dirs


class TestServedClaudeCli:
    """The subscription-billed CLI backend through the full served pipeline.

    Selected purely by configuration — worktree, gate, and merge all run, with
    a scripted runner standing in for the real binary.
    """

    def test_a_run_completes_via_the_cli_backend(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`[provider.claude_cli]` in the repo config is all it takes."""
        from orchestrator.provider.claude_cli import CliResult
        from tests.provider.test_claude_cli import ScriptedCliRunner

        repo_root = make_repository(tmp_dir / "repo")
        (repo_root / "orchestrator.toml").write_text(
            "[provider.claude_cli]\n"
            'executable = "claude"\n'
            "timeout_s = 60.0\n"
            "[gates]\n"
            'test_command = "python -c pass"\n'
            'parser = "exit_code"\n',
            encoding="utf-8",
        )
        runner = ScriptedCliRunner(CliResult(0, "Done: generated the config.", ""))

        def registry_with_scripted_cli(config: Any, **_: Any) -> ProviderRegistry:
            return _real_registry(config, transports={"claude_cli": runner})

        monkeypatch.setattr(
            "orchestrator.provider.registry.build_default_registry",
            registry_with_scripted_cli,
        )
        captured: dict[str, FastAPI] = {}
        monkeypatch.setattr(
            uvicorn, "run", lambda app, **_: captured.__setitem__("app", app)
        )

        code = main(
            [
                "--config", "/nonexistent", "--no-env",
                "--repo", str(repo_root),
                "--database", str(tmp_dir / "runs.db"),
                "serve", "--workspace", str(tmp_dir / "work"),
            ]
        )
        assert code == EXIT_OK
        app = captured["app"]
        try:
            with TestClient(app) as client:
                client.post("/workflows", json=_gated_workflow())
                run_id = client.post("/workflows/smoke/runs", json={}).json()["id"]
                until = time.monotonic() + _DEADLINE_S
                while time.monotonic() < until:
                    body = client.get(f"/runs/{run_id}/status").json()
                    if not body["active"]:
                        break
                    time.sleep(0.01)
                assert body["healthy"] is True, body

                # OP-004: an estimate is never presented as a measurement.
                # The CLI cannot count, so its usage arrives flagged — and
                # non-zero, which before OP-004b it was not: every served
                # attempt used to replay with zero tokens.
                detail = client.get(f"/runs/{run_id}").json()
                assert detail["usage"]["tokens_in"] > 0, detail["usage"]
                assert detail["usage"]["tokens_estimated"] is True

                attempts = client.get(
                    f"/runs/{run_id}/tasks/{detail['tasks'][0]['id']}/attempts"
                ).json()
                assert attempts["attempts"][0]["tokens_estimated"] is True
        finally:
            app.state.orchestrator.database.close()

        # startup_all probed --version at serve time; the attempt then went
        # through -p. Both prove the CLI provider, not a stand-in, did the work.
        flags = [argv[1] for argv in runner.calls]
        assert "--version" in flags
        assert "-p" in flags


class TestFallbackMode:
    """No repository: a plain shared directory, deliberately and loudly.

    Fallback is a genuine dry-run mode, not a degraded accident. Its limits are
    asserted here on purpose: nothing is verified, nothing is committed, and —
    because every attempt shares one directory — concurrency is clamped to 1 so
    the shared root can never be entered in parallel.
    """

    def test_no_gate_is_evaluated(self, served: _ServedApp) -> None:
        """With no repository there is no gate; nothing pretends to verify."""
        with TestClient(served.app) as client:
            run_id, _ = _run_to_completion(client)
            types = {event["type"] for event in _log(client, run_id)}

        assert EventType.GATE_EVALUATED.value not in types

    def test_attempts_run_in_the_fallback_root(
        self, served: _ServedApp, tmp_dir: Path
    ) -> None:
        """The one shared directory is the mode's defining property."""
        with TestClient(served.app) as client:
            _run_to_completion(client)

        assert (tmp_dir / "work").is_dir(), "the fallback root was never created"
        assert not (tmp_dir / "work" / ".git").exists()

    def test_concurrency_is_clamped_to_one(self, served: _ServedApp) -> None:
        """A shared directory must never be entered in parallel."""
        with TestClient(served.app) as client:
            config = client.get("/config").json()

        assert config["run"]["max_concurrency"] == 1


class TestProviderStartup:
    """The served path must start the provider it intends to use.

    A real provider builds its transport in ``startup()`` and refuses to
    complete without one ("call startup() first"). The earlier e2e tests
    masked this by injecting transports at construction, so a production serve
    would have accepted runs and failed every attempt uniformly.
    """

    def test_a_provider_needing_startup_still_serves_a_run(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The regression: the transport arrives via startup(), not __init__."""
        from orchestrator.core.config import OrchestratorConfig
        from orchestrator.provider.base import Domain
        from orchestrator.provider.claude import ClaudeConfig, ClaudeProvider
        from orchestrator.provider.registry import ProviderRegistry as Registry

        transport = FakeTransport(response=dict(_REPLY))

        def registry_needing_startup(config: Any, **_: Any) -> ProviderRegistry:
            registry = Registry(config=config or OrchestratorConfig())
            registry.register(
                "anthropic",
                Domain.MODEL,
                lambda cfg, role: ClaudeProvider(
                    ClaudeConfig(),
                    transport_factory=lambda: transport,  # only startup() calls this
                ),
            )
            registry.bind(Domain.MODEL, "anthropic")
            return registry

        monkeypatch.setattr(
            "orchestrator.provider.registry.build_default_registry",
            registry_needing_startup,
        )
        captured: dict[str, FastAPI] = {}
        monkeypatch.setattr(
            uvicorn, "run", lambda app, **_: captured.__setitem__("app", app)
        )

        code = main(
            [
                "--config", "/nonexistent", "--no-env",
                "--database", str(tmp_dir / "runs.db"),
                "serve", "--workspace", str(tmp_dir / "work"),
            ]
        )
        assert code == EXIT_OK
        served = _ServedApp(captured["app"], transport)
        try:
            with TestClient(served.app) as client:
                _, final = _run_to_completion(client)
        finally:
            served.state.database.close()

        assert final["healthy"] is True, final
        assert transport.sent, "the provider was never started, or never reached"


class TestVerifyFlag:
    """`serve --verify`: the opt-in refusal, distinct from graceful default."""

    def test_verify_refuses_an_unavailable_backend(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--verify turns silent degradation into a non-zero exit."""

        def explode(config: Any, **_: Any) -> ProviderRegistry:
            raise RuntimeError("no provider SDK installed")

        monkeypatch.setattr(
            "orchestrator.provider.registry.build_default_registry", explode
        )

        code = main(
            [
                "--config", "/nonexistent", "--no-env",
                "--database", str(tmp_dir / "runs.db"),
                "serve", "--verify", "--dry-run",
            ]
        )

        assert code == EXIT_FAILED
        assert "refusing to serve" in capsys.readouterr().err

    def test_verify_passes_a_healthy_backend(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A working backend serves exactly as without the flag."""
        transport = FakeTransport(response=dict(_REPLY))

        def registry_with_scripted_transport(config: Any, **_: Any) -> ProviderRegistry:
            return _real_registry(config, transports={"anthropic": transport})

        monkeypatch.setattr(
            "orchestrator.provider.registry.build_default_registry",
            registry_with_scripted_transport,
        )

        code = main(
            [
                "--config", "/nonexistent", "--no-env",
                "--database", str(tmp_dir / "runs.db"),
                "serve", "--verify", "--dry-run",
            ]
        )
        payload = json.loads(capsys.readouterr().out)

        assert code == EXIT_OK
        assert payload["execution_available"] is True
        assert payload["execution_mode"] == "fallback"


class TestServeDegradesHonestly:
    """A server that cannot build an executor still starts, and says so."""

    def test_a_broken_provider_leaves_the_server_recording(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Recording and reporting survive; execution reports itself unavailable.

        The alternative — refusing to start — would take down approvals and the
        run history along with execution. The important half is that the server
        does not *claim* it can execute.
        """

        def explode(config: Any, **_: Any) -> ProviderRegistry:
            raise RuntimeError("no provider SDK installed")

        monkeypatch.setattr(
            "orchestrator.provider.registry.build_default_registry", explode
        )

        code = main(
            [
                "--config", "/nonexistent",
                "--no-env",
                "--database", str(tmp_dir / "runs.db"),
                "serve", "--dry-run",
            ]
        )
        payload = json.loads(capsys.readouterr().out)

        assert code == EXIT_OK
        assert payload["execution_available"] is False


class TestProviderSelectionIsHonest:
    """Settings must describe the provider that is actually bound.

    A configuration whose model and effort are silently ignored is the
    "silent degradation" failure `docs/030_PROVIDER_INTERFACE.md` §4 calls the
    most expensive one in a swappable-backend system: the operator believes
    something false and pays for it later.

    This was a strict xfail pinning a real defect: `cmd_serve` read the
    ``anthropic`` block while the registry bound the name ``claude``. Resolved
    by making ``anthropic`` the canonical key (it is the one the documented
    config surface has always used), keeping ``claude`` as an alias, and having
    `cmd_serve` read settings through `settings_for` for the name that is
    actually bound.
    """

    def _serve_with_toml(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch, toml: str
    ) -> _ServedApp:
        """Serve with a repository-level orchestrator.toml supplying ``toml``."""
        repo_dir = tmp_dir / "cfg"
        repo_dir.mkdir(parents=True, exist_ok=True)
        (repo_dir / "orchestrator.toml").write_text(toml, encoding="utf-8")

        transport = FakeTransport(response=dict(_REPLY))

        def registry_with_scripted_transport(config: Any, **_: Any) -> ProviderRegistry:
            return _real_registry(config, transports={"anthropic": transport})

        monkeypatch.setattr(
            "orchestrator.provider.registry.build_default_registry",
            registry_with_scripted_transport,
        )

        captured: dict[str, FastAPI] = {}
        monkeypatch.setattr(
            uvicorn, "run", lambda app, **_: captured.__setitem__("app", app)
        )

        code = main(
            [
                "--config", "/nonexistent",
                "--no-env",
                "--repo", str(repo_dir),
                "--database", str(tmp_dir / "runs.db"),
                "serve",
                "--workspace", str(tmp_dir / "work"),
            ]
        )
        assert code == EXIT_OK
        return _ServedApp(captured["app"], transport)

    @pytest.mark.parametrize("block", ["anthropic", "claude"])
    def test_the_configured_model_reaches_the_wire(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch, block: str
    ) -> None:
        """The strongest form of the assertion: config → RuntimeConfig → payload.

        Both the canonical block and the legacy alias must carry their model all
        the way to what is actually sent to the backend.
        """
        served = self._serve_with_toml(
            tmp_dir, monkeypatch, f'[provider.{block}]\nmodel = "configured-model"\n'
        )
        try:
            with TestClient(served.app) as client:
                _run_to_completion(client)
        finally:
            served.state.database.close()

        assert served.transport.sent, "the run never reached the provider"
        assert served.transport.last["model"] == "configured-model"


class TestConflictedMergeIsNotSuccess:
    """Two steps touching one file: the second merge must not pass silently.

    Reproduces the defect the first code-producing run against another
    repository exposed (PROJECT_STATUS D-2d). Both steps edited the same file;
    the second passed its gate, committed, and its merge into the integration
    branch conflicted — and the run reported 2/2 healthy while the integration
    branch, the artefact the operator is told to review, held only half the
    work. `_merge` returned a bool nobody inspected. FR-3.5 requires a
    conflict to surface as a structured task failure.
    """

    def _serve_writing(
        self,
        tmp_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        payloads: Mapping[str, str],
    ) -> tuple[Any, Path]:
        """Serve against a real repo, with a harness that writes real files.

        The scripted "agent" writes a fixed payload into `shared.txt` inside
        whatever worktree it is given, so two steps genuinely collide.
        """
        from orchestrator.adapter.claude_code import ClaudeCodeHarness, HarnessConfig
        from orchestrator.provider.claude_cli import CliResult

        repo_root = make_repository(tmp_dir / "repo")
        (repo_root / "shared.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=repo_root, check=True, timeout=60)
        subprocess.run(
            ["git", "commit", "-m", "add shared"], cwd=repo_root, check=True, timeout=60
        )
        (repo_root / "orchestrator.toml").write_text(
            '[agent]\nadapter = "claude_code"\n'
            '[gates]\ntest_command = "python -c pass"\nparser = "exit_code"\n',
            encoding="utf-8",
        )

        order: list[str] = []

        class WritingRunner:
            """Writes a distinct payload per step, in the worktree it is given."""

            async def run(
                self, argv: Sequence[str], *, timeout_s: float, cwd: Path | None = None
            ) -> CliResult:
                assert cwd is not None
                prompt = argv[argv.index("-p") + 1]
                which = "second" if "SECOND" in prompt else "first"
                order.append(which)
                (Path(cwd) / "shared.txt").write_text(
                    payloads[which], encoding="utf-8"
                )
                return CliResult(exit_code=0, stdout=f"wrote {which}", stderr="")

        harness = ClaudeCodeHarness(HarnessConfig(), runner=WritingRunner())

        def factory(config: Any, **_: Any) -> Any:
            raise AssertionError("the harness adapter needs no model provider")

        monkeypatch.setattr(
            "orchestrator.provider.registry.build_default_registry", factory
        )
        monkeypatch.setattr(
            "orchestrator.adapter.claude_code.ClaudeCodeHarness",
            lambda *a, **k: harness,
        )

        captured: dict[str, FastAPI] = {}
        monkeypatch.setattr(
            uvicorn, "run", lambda app, **_: captured.__setitem__("app", app)
        )
        code = main(
            [
                "--config", "/nonexistent", "--no-env",
                "--repo", str(repo_root),
                "--database", str(tmp_dir / "runs.db"),
                "serve", "--workspace", str(tmp_dir / "work"),
            ]
        )
        assert code == EXIT_OK
        return captured["app"], repo_root

    def test_a_conflicting_second_step_fails_the_run(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The scenario, end to end, through the served pipeline."""
        app, repo_root = self._serve_writing(
            tmp_dir,
            monkeypatch,
            # Both rewrite the same line, from the same base, incompatibly.
            payloads={"first": "FIRST WINS\n", "second": "SECOND WINS\n"},
        )
        try:
            with TestClient(app) as client:
                client.post(
                    "/workflows",
                    json={
                        "name": "collide",
                        "goal": "two steps, one file",
                        "steps": [
                            {"name": "step-one", "prompt": "write FIRST"},
                            {"name": "step-two", "prompt": "write SECOND"},
                        ],
                    },
                )
                run_id = client.post("/workflows/collide/runs", json={}).json()["id"]
                until = time.monotonic() + _DEADLINE_S
                while time.monotonic() < until:
                    response = client.get(f"/runs/{run_id}/status")
                    assert response.status_code == 200, response.text
                    body = response.json()
                    if not body["active"]:
                        break
                    # Slower than elsewhere in this file: this run drives a
                    # real Git merge per attempt, and a 10ms poll trips the
                    # server's own rate limiter (600/min) before it settles.
                    time.sleep(0.2)

                events = _log(client, run_id)
        finally:
            app.state.orchestrator.database.close()

        # The bar: a healthy run must mean the integration branch actually
        # holds every step's work. Reporting 2/2 while a commit sits orphaned
        # on its attempt branch is the defect this test exists for.
        if body["healthy"]:
            merged = subprocess.run(
                ["git", "log", "--oneline", f"orchestrator/{run_id}/integration"],
                cwd=repo_root, capture_output=True, text=True, check=True, timeout=60,
            ).stdout
            assert "step-one" in merged and "step-two" in merged, (
                "reported healthy, but the integration branch is missing a "
                "step's commit: " + merged
            )
        else:
            failures = [
                e for e in events if e["type"] == EventType.TASK_FAILED.value
            ]
            assert failures, "unhealthy but nothing was recorded as failed"
            codes = {e["payload"].get("error_code") for e in failures}
            details = [e["payload"].get("detail") for e in failures]
            assert "merge_conflict" in codes, (codes, details)
            assert "merge_conflict" in codes, codes


class TestTheMergeStoryIsInTheLog(TestConflictedMergeIsNotSuccess):
    """OP-011: whether the work landed must be answerable from events alone.

    D-2d made a merge *failure* loud. The success path stayed silent — a
    healthy run's log carried no merge verdict at all, so "did this step's work
    actually reach the integration branch?" could only be answered by running
    Git against the repository. That is the question the event log exists to
    answer, and this test deliberately never shells out to Git to answer it.

    Inherits ``_serve_writing`` from the D-2d case: the same harness that
    writes real files into the worktree it is given, which is what makes there
    be a merge to report at all.
    """

    def test_a_healthy_run_records_that_the_work_landed(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app, _repo_root = self._serve_writing(
            tmp_dir, monkeypatch, payloads={"first": "ONLY\n", "second": "unused\n"}
        )
        try:
            with TestClient(app) as client:
                client.post(
                    "/workflows",
                    json={
                        "name": "lands",
                        "goal": "one step that merges cleanly",
                        "steps": [{"name": "step-one", "prompt": "write FIRST"}],
                    },
                )
                run_id = client.post("/workflows/lands/runs", json={}).json()["id"]
                until = time.monotonic() + _DEADLINE_S
                while time.monotonic() < until:
                    body = client.get(f"/runs/{run_id}/status").json()
                    if not body["active"]:
                        break
                    time.sleep(0.2)

                events = _log(client, run_id)
                tasks = client.get(f"/runs/{run_id}/tasks").json()
                attempts = client.get(
                    f"/runs/{run_id}/tasks/{tasks[0]['id']}/attempts"
                ).json()["attempts"]
        finally:
            app.state.orchestrator.database.close()

        assert body["healthy"], body

        finished = [
            e for e in events if e["type"] == EventType.ATTEMPT_FINISHED.value
        ]
        assert finished, "no attempt was closed in the log"
        payload = finished[-1]["payload"]
        assert payload["merged"] is True, payload
        assert payload["merge_status"] == "merged", payload
        assert payload["changed_files"] == ["shared.txt"], payload

        # And the same story through the API a reviewer actually reads.
        assert attempts[-1]["merged"] is True
        assert attempts[-1]["merge_status"] == "merged"
        assert attempts[-1]["changed_files"] == ["shared.txt"]

    def test_a_no_op_attempt_is_visible_as_zero_changed_files(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """"Succeeded, 0 files" is the shape of a vacuous success.

        Run 2 against a real repository reported green having changed nothing.
        The guard now fails that attempt on an ordinary step — but the *number*
        has to be in the log too, or the next silent variant is invisible
        again. The agent here writes the file's existing content back, which is
        the honest way to produce a real attempt with no diff.
        """
        app, _repo_root = self._serve_writing(
            tmp_dir, monkeypatch, payloads={"first": "base\n", "second": "base\n"}
        )
        try:
            with TestClient(app) as client:
                client.post(
                    "/workflows",
                    json={
                        "name": "idle",
                        "goal": "a step that changes nothing",
                        "steps": [
                            {
                                "name": "verify",
                                "prompt": "look only",
                                "expects_changes": False,
                            }
                        ],
                    },
                )
                run_id = client.post("/workflows/idle/runs", json={}).json()["id"]
                until = time.monotonic() + _DEADLINE_S
                while time.monotonic() < until:
                    body = client.get(f"/runs/{run_id}/status").json()
                    if not body["active"]:
                        break
                    time.sleep(0.2)
                events = _log(client, run_id)
        finally:
            app.state.orchestrator.database.close()

        assert body["healthy"], body
        finished = [
            e for e in events if e["type"] == EventType.ATTEMPT_FINISHED.value
        ]
        assert finished, "no attempt was closed in the log"
        payload = finished[-1]["payload"]
        assert payload["status"] == "succeeded", payload
        assert payload["changed_files"] == [], payload
        assert payload["merged"] is False, payload
        assert payload["merge_status"] == "nothing_to_merge", payload


class TestConflictIsDeterministicallyExercised:
    """OP-012: make the conflict happen on purpose, then check both outcomes.

    D-2d's fix makes a *sequential* conflict structurally impossible: a
    dependent step now branches from the integration branch as it stands, so
    its merge is a fast-forward by construction. That is the right outcome and
    it is why run 4 came back clean — but it also means the `merge_conflict`
    verdict, its `conflicted_paths`, and the rebased retry were left covered by
    unit tests alone, with no end-to-end evidence.

    The only way a conflict still arises is genuine parallelism: two
    independent steps whose worktrees were both cut from the same integration
    tip, both editing one file. That is a race, and a race is not a test — so
    the scripted agents **rendezvous**. Neither returns until both have written
    their file, which guarantees both worktrees predate either merge. The
    conflict is then certain, not likely.

    Every assertion reads the OP-011 event fields. Nothing here shells out to
    Git: if the log cannot tell this story, the log is wrong.
    """

    def _serve_colliding(
        self,
        tmp_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        parties: int = 2,
    ) -> Path:
        """Serve against a real repo with two agents that collide on purpose."""
        from orchestrator.adapter.claude_code import ClaudeCodeHarness, HarnessConfig
        from orchestrator.provider.claude_cli import CliResult

        repo_root = make_repository(tmp_dir / "repo")
        (repo_root / "shared.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=repo_root, check=True, timeout=60)
        subprocess.run(
            ["git", "commit", "-m", "add shared"], cwd=repo_root, check=True, timeout=60
        )
        (repo_root / "orchestrator.toml").write_text(
            '[run]\nmax_concurrency = 2\n'
            '[agent]\nadapter = "claude_code"\n'
            '[gates]\ntest_command = "python -c pass"\nparser = "exit_code"\n',
            encoding="utf-8",
        )

        gate = asyncio.Barrier(parties)
        arrivals = 0

        class RendezvousRunner:
            """Writes a per-step payload, but only once both steps have."""

            async def run(
                self, argv: Sequence[str], *, timeout_s: float, cwd: Path | None = None
            ) -> CliResult:
                nonlocal arrivals
                assert cwd is not None
                prompt = argv[argv.index("-p") + 1]
                which = "SECOND" if "SECOND" in prompt else "FIRST"
                (Path(cwd) / "shared.txt").write_text(
                    f"{which} WINS\n", encoding="utf-8"
                )
                # Only the opening round waits: a retry runs alone, and a
                # barrier it could never fill would hang the whole suite.
                arrivals += 1
                if arrivals <= parties:
                    await gate.wait()
                return CliResult(exit_code=0, stdout=f"wrote {which}", stderr="")

        harness = ClaudeCodeHarness(HarnessConfig(), runner=RendezvousRunner())

        def factory(config: Any, **_: Any) -> Any:
            raise AssertionError("the harness adapter needs no model provider")

        monkeypatch.setattr(
            "orchestrator.provider.registry.build_default_registry", factory
        )
        monkeypatch.setattr(
            "orchestrator.adapter.claude_code.ClaudeCodeHarness",
            lambda *a, **k: harness,
        )
        captured: dict[str, FastAPI] = {}
        monkeypatch.setattr(
            uvicorn, "run", lambda app, **_: captured.__setitem__("app", app)
        )
        code = main(
            [
                "--config", "/nonexistent", "--no-env",
                "--repo", str(repo_root),
                "--database", str(tmp_dir / "runs.db"),
                "serve", "--workspace", str(tmp_dir / "work"),
            ]
        )
        assert code == EXIT_OK
        return captured["app"]

    @staticmethod
    def _drive(app: Any, *, max_attempts: int) -> tuple[dict[str, Any], list[Any]]:
        """Run the colliding workflow to a terminal state; return status + log."""
        with TestClient(app) as client:
            client.post(
                "/workflows",
                json={
                    "name": "collide-hard",
                    "goal": "two independent steps, one file, on purpose",
                    "max_concurrency": 2,
                    "steps": [
                        {
                            "name": "step-one",
                            "prompt": "write FIRST",
                            "max_attempts": max_attempts,
                        },
                        {
                            "name": "step-two",
                            "prompt": "write SECOND",
                            "max_attempts": max_attempts,
                        },
                    ],
                },
            )
            run_id = client.post(
                "/workflows/collide-hard/runs", json={"max_concurrency": 2}
            ).json()["id"]
            until = time.monotonic() + _DEADLINE_S
            while time.monotonic() < until:
                body = client.get(f"/runs/{run_id}/status").json()
                if not body["active"]:
                    break
                time.sleep(0.2)
            return body, _log(client, run_id)

    @staticmethod
    def _finished(events: list[Any]) -> list[dict[str, Any]]:
        """Every attempt.finished payload, in order."""
        return [
            e["payload"]
            for e in events
            if e["type"] == EventType.ATTEMPT_FINISHED.value
        ]

    def test_without_a_retry_the_run_fails_loudly(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One attempt each: the loser must fail with the files that collided."""
        app = self._serve_colliding(tmp_dir, monkeypatch)
        try:
            body, events = self._drive(app, max_attempts=1)
        finally:
            app.state.orchestrator.database.close()

        assert not body["healthy"], "a conflict that never merged reported healthy"

        payloads = self._finished(events)
        conflicted = [p for p in payloads if p.get("merge_status") == "conflict"]
        assert conflicted, (
            "the rendezvous guarantees a collision, but no attempt recorded "
            f"one: {payloads}"
        )
        losing = conflicted[0]
        assert losing["status"] == "failed"
        assert losing["merged"] is False
        assert losing["conflicted_paths"] == ["shared.txt"], losing
        assert losing["changed_files"] == ["shared.txt"], losing

        # And the task-level record names the cause, not just the attempt.
        failures = [
            e["payload"]
            for e in events
            if e["type"] == EventType.TASK_FAILED.value
        ]
        assert "merge_conflict" in {f.get("error_code") for f in failures}, failures

        # Exactly one side won: the other's work is not on the branch, and the
        # log says so without anyone opening a terminal.
        landed = [p for p in payloads if p.get("merged") is True]
        assert len(landed) == 1, payloads

    def test_with_a_retry_the_rebase_resolves_it(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two attempts each: the retry branches from the merged tip and lands.

        This is the claim D-2d's fix rests on — that a retry is rebased by
        construction — and run 4 could not prove it, because run 4 never
        conflicted.
        """
        app = self._serve_colliding(tmp_dir, monkeypatch)
        try:
            body, events = self._drive(app, max_attempts=2)
        finally:
            app.state.orchestrator.database.close()

        payloads = self._finished(events)
        conflicted = [p for p in payloads if p.get("merge_status") == "conflict"]
        assert conflicted, f"the collision did not happen: {payloads}"

        assert body["healthy"], (
            "the retry should have rebased onto the merged integration branch "
            f"and landed: {payloads}"
        )
        assert body["succeeded"] == 2, body

        landed = [p for p in payloads if p.get("merged") is True]
        assert len(landed) == 2, (
            "both steps must end up on the integration branch, and the log "
            f"must say so: {payloads}"
        )

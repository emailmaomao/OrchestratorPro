"""Shared fixtures for the operations tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.api.state import AppState
from orchestrator.core.config import OrchestratorConfig, SecurityConfig
from orchestrator.core.events import Event, EventType, RunId
from orchestrator.core.run_store import RunStore
from orchestrator.core.storage import Database
from orchestrator.ops.hardening import harden


def hardened_app(security: SecurityConfig | None = None, **kwargs: object) -> FastAPI:
    """An API application with the hardening stack applied."""
    state = AppState.in_memory(config=OrchestratorConfig(security=security or SecurityConfig()))
    return harden(create_app(state=state), security or SecurityConfig(), **kwargs)  # type: ignore[arg-type]


@pytest.fixture
def client() -> Iterator[TestClient]:
    """A client for a hardened application on default settings."""
    with TestClient(hardened_app()) as test_client:
        yield test_client


def populated_database(path: Path, *, runs: int = 2, events_per_run: int = 3) -> Database:
    """Create a file-backed database holding a few runs."""
    database = Database(path)
    database.migrate()
    store = RunStore(database)

    for index in range(runs):
        run_id = RunId.generate()
        store.record(
            Event.new(
                EventType.RUN_CREATED,
                run_id=run_id,
                payload={"goal": f"goal {index}", "repo_path": "/repo"},
            )
        )
        for step in range(events_per_run - 1):
            store.record(
                Event.new(
                    EventType.TOOL_CALLED, run_id=run_id, payload={"tool": "read", "n": step}
                )
            )
    return database


@pytest.fixture
def database(tmp_dir: Path) -> Iterator[Database]:
    """A file-backed database with a little history."""
    db = populated_database(tmp_dir / "runs.db")
    yield db
    db.close()


@pytest.fixture
def backups(tmp_dir: Path) -> Path:
    """An empty backup directory."""
    directory = tmp_dir / "backups"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


class FakeClock:
    """A monotonic clock that only moves when told."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        """Move time forward."""
        self.now += seconds

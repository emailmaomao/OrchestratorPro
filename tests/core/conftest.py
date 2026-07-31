"""Shared fixtures for the core test package."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from orchestrator.core.events import AttemptId, Event, EventType, RunId, TaskId

SequenceBuilder = Callable[[RunId], tuple[list[Event], TaskId, AttemptId]]


@pytest.fixture
def run_id() -> RunId:
    """A fresh run identifier."""
    return RunId.generate()


def _build_sequence(run_id: RunId) -> tuple[list[Event], TaskId, AttemptId]:
    """Build a complete, realistic event sequence for one run.

    Covers the full arc a real run walks: creation, a declared task, an
    attempt with tool calls and usage, a gate verdict, and termination.
    """
    task_id = TaskId.generate()
    attempt_id = AttemptId.generate()
    events = [
        Event.new(
            EventType.RUN_CREATED,
            run_id=run_id,
            payload={"goal": "migrate the client", "repo_path": "/repo"},
        ),
        Event.new(
            EventType.TASK_CREATED,
            run_id=run_id,
            task_id=task_id,
            payload={"title": "Update call sites", "prompt": "do it", "max_attempts": 3},
        ),
        Event.new(EventType.RUN_STARTED, run_id=run_id),
        Event.new(EventType.TASK_READY, run_id=run_id, task_id=task_id),
        Event.new(EventType.TASK_STARTED, run_id=run_id, task_id=task_id),
        Event.new(
            EventType.ATTEMPT_STARTED,
            run_id=run_id,
            task_id=task_id,
            attempt_id=attempt_id,
            payload={"number": 1, "branch": "orchestrator/x", "adapter": "tool_loop"},
        ),
        Event.new(
            EventType.TOOL_CALLED, run_id=run_id, task_id=task_id, payload={"tool": "read"}
        ),
        Event.new(
            EventType.TOOL_CALLED, run_id=run_id, task_id=task_id, payload={"tool": "edit"}
        ),
        Event.new(
            EventType.ATTEMPT_FINISHED,
            run_id=run_id,
            task_id=task_id,
            attempt_id=attempt_id,
            payload={
                "status": "succeeded",
                "tokens_in": 1000,
                "tokens_out": 250,
                "cost_usd": 0.011,
            },
        ),
        Event.new(
            EventType.GATE_EVALUATED,
            run_id=run_id,
            task_id=task_id,
            attempt_id=attempt_id,
            payload={"gate": "unit-tests", "verdict": "passed"},
        ),
        Event.new(EventType.TASK_SUCCEEDED, run_id=run_id, task_id=task_id),
        Event.new(EventType.RUN_FINISHED, run_id=run_id),
    ]
    return events, task_id, attempt_id


@pytest.fixture
def build_sequence() -> SequenceBuilder:
    """Return a factory producing a full event sequence for a given run."""
    return _build_sequence

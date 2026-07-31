"""Shared fixtures for workflow-engine tests.

The engine is exercised end to end but entirely offline: a fake model provider
drives the real agent runtime, and git and gate services are supplied only where
a test is about them. Nothing here reaches a network or a real repository.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Coroutine, Sequence
from pathlib import Path
from typing import Any, TypeVar

import pytest

from orchestrator.agent.model import AttemptResult, AttemptStatus, TaskSpec
from orchestrator.agent.runtime import AgentRuntime, RuntimeConfig
from orchestrator.agent.tools import ToolContext, default_registry
from orchestrator.core.events import Budget, RunId
from orchestrator.core.run_store import RunStore
from orchestrator.core.storage import Database
from orchestrator.task.dispatcher import AttemptOutcome
from orchestrator.task.model import Task
from orchestrator.workflow.definition import (
    StepDefinition,
    WorkflowDefinition,
    WorkflowPlan,
)
from orchestrator.workflow.executor import ExecutionServices
from orchestrator.workflow.progress import EventEmitter

from tests.agent.conftest import FakeProvider, finish_turn, tool_turn

T = TypeVar("T")


def run(awaitable: Coroutine[Any, Any, T] | Awaitable[T]) -> T:
    """Drive one coroutine to completion."""

    async def _wrapped() -> T:
        return await awaitable

    return asyncio.run(_wrapped())


SMALL_BUDGET = Budget(seconds=60.0, tokens=100_000, tool_calls=50)


def step(
    name: str,
    *,
    depends_on: Sequence[str] = (),
    prompt: str | None = None,
    **kwargs: Any,
) -> StepDefinition:
    """Build a step definition with test-friendly defaults.

    ``expects_changes`` defaults to **False** here, unlike in production: the
    scripted agents these fixtures drive never touch the filesystem, so every
    attempt is a no-op by construction and the guard would fail tests that are
    about gates, commits, or transcripts rather than about doing work. Tests
    that exercise the guard itself pass ``expects_changes=True`` explicitly.
    """
    kwargs.setdefault("budget", SMALL_BUDGET)
    kwargs.setdefault("max_attempts", 1)
    kwargs.setdefault("expects_changes", False)
    return StepDefinition(
        name=name,
        prompt=prompt or f"do {name}",
        depends_on=tuple(depends_on),
        **kwargs,
    )


def workflow(*steps: StepDefinition, name: str = "wf", **kwargs: Any) -> WorkflowDefinition:
    """Build a workflow definition."""
    return WorkflowDefinition(name=name, goal="a goal", steps=tuple(steps), **kwargs)


class ScriptedExecutor:
    """A step executor returning prepared outcomes, keyed by step name."""

    def __init__(
        self,
        plan: WorkflowPlan,
        outcomes: dict[str, AttemptOutcome] | None = None,
        *,
        default: AttemptOutcome | None = None,
        delay_s: float = 0.0,
    ) -> None:
        self.plan = plan
        self.outcomes = dict(outcomes or {})
        self.default = default or AttemptOutcome.success()
        self.delay_s = delay_s
        self.calls: list[tuple[str, int]] = []
        self.concurrent = 0
        self.peak_concurrent = 0

    async def __call__(self, task: Task, attempt: int) -> AttemptOutcome:
        """Record the call and return the scripted outcome."""
        name = self.plan.step_name(task.id)
        self.calls.append((name, attempt))
        self.concurrent += 1
        self.peak_concurrent = max(self.peak_concurrent, self.concurrent)
        try:
            await asyncio.sleep(self.delay_s)
            return self.outcomes.get(name, self.default)
        finally:
            self.concurrent -= 1

    @property
    def executed(self) -> list[str]:
        """Step names in the order they were first executed."""
        seen: list[str] = []
        for name, _ in self.calls:
            if name not in seen:
                seen.append(name)
        return seen

    def factory(self) -> Any:
        """Return a factory the engine can call."""

        def build(plan: WorkflowPlan, run_id: RunId, emitter: EventEmitter) -> Any:
            self.plan = plan
            return self

        return build


def agent_runtime(responses: Sequence[Any]) -> AgentRuntime:
    """Build a real agent runtime over a scripted provider."""
    return AgentRuntime(
        FakeProvider(list(responses), default=finish_turn("done")),
        config=RuntimeConfig(model="fake-model", max_iterations=10),
        registry=default_registry(),
    )


@pytest.fixture
def store() -> RunStore:
    """A run store over a migrated in-memory database."""
    db = Database.in_memory()
    db.migrate()
    return RunStore(db)


@pytest.fixture
def scratch(tmp_dir: Path) -> Path:
    """A directory for a workspace-less run."""
    target = tmp_dir / "scratch"
    target.mkdir(parents=True, exist_ok=True)
    return target


@pytest.fixture
def services(scratch: Path) -> ExecutionServices:
    """Services with no git and no gates — the agent runs in a plain directory."""
    return ExecutionServices(fallback_root=scratch)


def succeeded(**detail: Any) -> AttemptOutcome:
    """A successful outcome."""
    return AttemptOutcome.success(**detail)


def failed(code: str = "boom", *, retryable: bool = False, **detail: Any) -> AttemptOutcome:
    """A failed outcome."""
    return AttemptOutcome.failure(code, retryable=retryable, **detail)

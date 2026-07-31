"""Shared fixtures for task-package tests."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Coroutine, Iterable, Mapping, Sequence
from typing import Any, TypeVar

import pytest

from orchestrator.core.events import Budget, TaskId
from orchestrator.task.dispatcher import AttemptOutcome
from orchestrator.task.graph import TaskGraph
from orchestrator.task.model import Task

T = TypeVar("T")


def run(awaitable: Coroutine[Any, Any, T] | Awaitable[T]) -> T:
    """Drive one coroutine to completion.

    ``asyncio.run`` rather than a pytest plugin: pytest-asyncio is not installed,
    and the dispatcher's public surface is a single coroutine, so driving it
    directly is both sufficient and honest about what is being tested.
    """

    async def _wrapped() -> T:
        return await awaitable

    return asyncio.run(_wrapped())


@pytest.fixture
def budget() -> Budget:
    """A small, valid budget."""
    return Budget(seconds=60.0, tokens=1_000, tool_calls=10)


def make_task(
    title: str = "t",
    *,
    budget: Budget | None = None,
    depends_on: Iterable[TaskId] = (),
    max_attempts: int = 1,
    labels: Iterable[str] = (),
) -> Task:
    """Build a task with sensible test defaults."""
    return Task.create(
        title=title,
        prompt=f"do {title}",
        budget=budget or Budget(seconds=60.0, tokens=1_000, tool_calls=10),
        depends_on=depends_on,
        max_attempts=max_attempts,
        labels=labels,
    )


@pytest.fixture
def task_factory() -> Any:
    """Return the task builder."""
    return make_task


def chain(length: int, **kwargs: Any) -> list[Task]:
    """Build a linear chain of tasks, each depending on the previous."""
    tasks: list[Task] = []
    for index in range(length):
        depends = (tasks[-1].id,) if tasks else ()
        tasks.append(make_task(f"t{index}", depends_on=depends, **kwargs))
    return tasks


def fan_out(width: int, **kwargs: Any) -> list[Task]:
    """Build one root with ``width`` independent children."""
    root = make_task("root", **kwargs)
    return [root, *(make_task(f"leaf{i}", depends_on=(root.id,), **kwargs) for i in range(width))]


def diamond(**kwargs: Any) -> list[Task]:
    """Build a diamond: one root, two parallel middles, one join."""
    root = make_task("root", **kwargs)
    left = make_task("left", depends_on=(root.id,), **kwargs)
    right = make_task("right", depends_on=(root.id,), **kwargs)
    join = make_task("join", depends_on=(left.id, right.id), **kwargs)
    return [root, left, right, join]


class RecordingExecutor:
    """An executor that records calls and returns scripted outcomes.

    Standing in for the agent runtime, which does not exist yet and which this
    package is forbidden to import. Because the dispatcher never touches a
    provider, there is no provider to mock here — the seam is one level higher.
    """

    def __init__(
        self,
        outcomes: Mapping[TaskId, Sequence[AttemptOutcome]] | None = None,
        *,
        default: AttemptOutcome | None = None,
        delay_s: float = 0.0,
    ) -> None:
        self.outcomes = {k: list(v) for k, v in (outcomes or {}).items()}
        self.default = default or AttemptOutcome.success()
        self.delay_s = delay_s
        self.calls: list[tuple[TaskId, int]] = []
        self.concurrent = 0
        self.peak_concurrent = 0

    async def __call__(self, task: Task, attempt: int) -> AttemptOutcome:
        """Record the call and return the next scripted outcome."""
        self.calls.append((task.id, attempt))
        self.concurrent += 1
        self.peak_concurrent = max(self.peak_concurrent, self.concurrent)
        try:
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
            else:
                # Yield control so genuinely concurrent attempts overlap.
                await asyncio.sleep(0)
            scripted = self.outcomes.get(task.id)
            if scripted:
                return scripted.pop(0)
            return self.default
        finally:
            self.concurrent -= 1

    @property
    def order(self) -> list[TaskId]:
        """The task identifiers in the order they were first attempted."""
        seen: list[TaskId] = []
        for task_id, _ in self.calls:
            if task_id not in seen:
                seen.append(task_id)
        return seen

    def attempts_for(self, task_id: TaskId) -> int:
        """How many times a task was attempted."""
        return sum(1 for t, _ in self.calls if t == task_id)


class SleepRecorder:
    """Captures backoff delays instead of really waiting."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        """Record the delay and yield control."""
        self.delays.append(delay)
        await asyncio.sleep(0)


@pytest.fixture
def sleeper() -> SleepRecorder:
    """A sleep function that records rather than waits."""
    return SleepRecorder()


def graph_of(tasks: Iterable[Task]) -> TaskGraph:
    """Build a graph from tasks."""
    return TaskGraph(tasks)

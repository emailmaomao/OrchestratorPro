"""Performance benchmarks.

Not micro-benchmarks of arithmetic — measurements of the four things that
actually decide whether a long run stays responsive:

``events``
    How fast the log can be appended to. Every state change is a durable write
    with ``synchronous=FULL``; if this is slow, everything is.
``replay``
    How fast a run can be reconstructed. Recovery is replay, so this is the
    time between a crash and being useful again.
``scheduler``
    How fast the pure scheduling decision is over a large graph. It runs on
    every state change, so its cost is multiplied by the size of the run.
``graph``
    How fast a graph validates and layers. Paid once per run, but paid before
    anything else can start.

Each result carries operations per second and the p95, not just a mean. A mean
hides the pause that an operator actually notices.

Nothing here calls a model, touches a network, or needs a repository. A
benchmark that needs the internet is a benchmark nobody runs twice.
"""

from __future__ import annotations

import statistics
import tempfile
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.core.events import Budget, Event, EventType, RunId, TaskId
from orchestrator.core.projection import reconstruct
from orchestrator.core.run_store import RunStore
from orchestrator.core.storage import Database
from orchestrator.task.graph import TaskGraph
from orchestrator.task.model import Task
from orchestrator.task.scheduler import SchedulerState, next_ready

__all__ = [
    "BenchmarkResult",
    "BenchmarkSuite",
    "bench_events",
    "bench_graph",
    "bench_replay",
    "bench_scheduler",
    "run_benchmarks",
]

_BUDGET = Budget(seconds=60.0, tokens=1000, tool_calls=10)


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    """What one benchmark measured."""

    name: str
    operations: int
    seconds: float
    unit: str = "op"
    samples: tuple[float, ...] = ()
    detail: str = ""

    @property
    def per_second(self) -> float:
        """Operations per second."""
        return self.operations / self.seconds if self.seconds > 0 else float("inf")

    @property
    def mean_ms(self) -> float:
        """Mean time per operation, in milliseconds."""
        return (self.seconds / self.operations * 1000) if self.operations else 0.0

    @property
    def p95_ms(self) -> float:
        """The 95th-percentile sample, in milliseconds.

        Reported because a mean hides the pause a person notices. Falls back to
        the mean when a benchmark measured in bulk rather than per operation.
        """
        if not self.samples:
            return self.mean_ms
        ordered = sorted(self.samples)
        index = min(len(ordered) - 1, int(len(ordered) * 0.95))
        return ordered[index] * 1000

    def summary(self) -> str:
        """A one-line account."""
        return (
            f"{self.name}: {self.per_second:,.0f} {self.unit}/s "
            f"(mean {self.mean_ms:.3f}ms, p95 {self.p95_ms:.3f}ms, "
            f"n={self.operations})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Render for a report or a regression check."""
        return {
            "name": self.name,
            "operations": self.operations,
            "seconds": round(self.seconds, 6),
            "per_second": round(self.per_second, 2),
            "mean_ms": round(self.mean_ms, 4),
            "p95_ms": round(self.p95_ms, 4),
            "unit": self.unit,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class BenchmarkSuite:
    """Every result from one run of the benchmarks."""

    results: tuple[BenchmarkResult, ...] = ()
    python: str = ""
    platform: str = ""

    def get(self, name: str) -> BenchmarkResult | None:
        """Return one result by name."""
        return next((result for result in self.results if result.name == name), None)

    def summary(self) -> str:
        """One line per benchmark."""
        return "\n".join(result.summary() for result in self.results)

    def to_dict(self) -> dict[str, Any]:
        """Render the whole suite."""
        return {
            "python": self.python,
            "platform": self.platform,
            "results": [result.to_dict() for result in self.results],
        }


def _timed(operation: Callable[[int], None], count: int) -> tuple[float, list[float]]:
    """Run an operation ``count`` times, returning the total and each sample."""
    samples: list[float] = []
    started = time.perf_counter()
    for index in range(count):
        at = time.perf_counter()
        operation(index)
        samples.append(time.perf_counter() - at)
    return time.perf_counter() - started, samples


def _chain(size: int) -> tuple[TaskGraph, dict[str, TaskId]]:
    """Build a wide-then-deep graph of ``size`` tasks."""
    ids = [TaskId.generate() for _ in range(size)]
    tasks = [
        Task(
            id=task_id,
            title=f"task {index}",
            prompt="do the thing",
            budget=_BUDGET,
            # A fan-in every four tasks: neither a chain (no parallelism to
            # schedule) nor a flat set (no dependencies to resolve).
            depends_on=(ids[index - 1],) if index % 4 else (),
        )
        for index, task_id in enumerate(ids)
    ]
    return TaskGraph(tasks), {task.title: task.id for task in tasks}


def bench_events(count: int = 2000, *, directory: Path | None = None) -> BenchmarkResult:
    """Measure durable event appends against a real file-backed database."""
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(directory) if directory else Path(scratch)
        database = Database(root / "bench.db")
        database.migrate()
        store = RunStore(database)

        run_id = RunId.generate()
        store.record(
            Event.new(
                EventType.RUN_CREATED,
                run_id=run_id,
                payload={"goal": "benchmark", "repo_path": str(root)},
            )
        )

        def append(index: int) -> None:
            store.record(
                Event.new(
                    EventType.TOOL_CALLED, run_id=run_id, payload={"tool": "read", "n": index}
                )
            )

        seconds, samples = _timed(append, count)
        database.close()

    return BenchmarkResult(
        name="events.append",
        operations=count,
        seconds=seconds,
        unit="event",
        samples=tuple(samples),
        detail="durable append with synchronous=FULL",
    )


def bench_replay(events: int = 5000) -> BenchmarkResult:
    """Measure reconstructing a run's state from its log."""
    run_id = RunId.generate()
    task_id = TaskId.generate()
    log = [
        Event.new(
            EventType.RUN_CREATED, run_id=run_id, payload={"goal": "g", "repo_path": "/r"}
        ),
        Event.new(
            EventType.TASK_CREATED,
            run_id=run_id,
            task_id=task_id,
            payload={"title": "t", "prompt": "p", "max_attempts": 3},
        ),
        *[
            Event.new(EventType.TOOL_CALLED, run_id=run_id, task_id=task_id, payload={"n": i})
            for i in range(events)
        ],
    ]

    started = time.perf_counter()
    state = reconstruct(log, run_id=run_id)
    seconds = time.perf_counter() - started

    return BenchmarkResult(
        name="events.replay",
        operations=len(log),
        seconds=seconds,
        unit="event",
        detail=f"reconstructed {state.event_count} event(s) into state",
    )


def bench_scheduler(size: int = 400, rounds: int = 200) -> BenchmarkResult:
    """Measure the scheduling decision over a large graph."""
    graph, _ = _chain(size)
    from orchestrator.task.model import TaskState

    states = {task_id: TaskState.PENDING for task_id in graph.task_ids}
    state = SchedulerState(states=states, attempts=dict.fromkeys(graph.task_ids, 0))

    def decide(_: int) -> None:
        next_ready(graph, state, max_concurrency=8)

    seconds, samples = _timed(decide, rounds)
    return BenchmarkResult(
        name="scheduler.decide",
        operations=rounds,
        seconds=seconds,
        unit="decision",
        samples=tuple(samples),
        detail=f"{size}-task graph, concurrency 8",
    )


def bench_graph(size: int = 400, rounds: int = 50) -> BenchmarkResult:
    """Measure building and layering a task graph."""
    ids = [TaskId.generate() for _ in range(size)]
    tasks = [
        Task(
            id=task_id,
            title=f"task {index}",
            prompt="p",
            budget=_BUDGET,
            depends_on=(ids[index - 1],) if index % 4 else (),
        )
        for index, task_id in enumerate(ids)
    ]

    def build(_: int) -> None:
        TaskGraph(tasks).layers()

    seconds, samples = _timed(build, rounds)
    return BenchmarkResult(
        name="graph.build",
        operations=rounds,
        seconds=seconds,
        unit="graph",
        samples=tuple(samples),
        detail=f"{size} tasks, validated and layered",
    )


#: Every benchmark, with the scale each runs at by default.
BENCHMARKS: tuple[tuple[str, Callable[[], BenchmarkResult]], ...] = (
    ("events", lambda: bench_events()),
    ("replay", lambda: bench_replay()),
    ("scheduler", lambda: bench_scheduler()),
    ("graph", lambda: bench_graph()),
)


def run_benchmarks(
    only: Sequence[str] | None = None, *, scale: float = 1.0
) -> BenchmarkSuite:
    """Run the benchmark suite.

    Args:
        only: Names to run. All of them when omitted.
        scale: Multiplies each benchmark's default size. Below one for a quick
            check; above one for a measurement worth quoting.

    Returns:
        The suite.

    Raises:
        ValueError: If a requested benchmark does not exist — a typo that
            silently ran nothing would look like a fast machine.
    """
    import platform
    import sys

    available = {name for name, _ in BENCHMARKS}
    requested = set(only) if only else available
    unknown = sorted(requested - available)
    if unknown:
        raise ValueError(
            f"unknown benchmark(s): {', '.join(unknown)}; available: "
            f"{', '.join(sorted(available))}"
        )

    factor = max(0.01, scale)
    results: list[BenchmarkResult] = []
    for name, _ in BENCHMARKS:
        if name not in requested:
            continue
        results.append(_scaled(name, factor))

    return BenchmarkSuite(
        results=tuple(results),
        python=sys.version.split()[0],
        platform=platform.platform(),
    )


def _scaled(name: str, factor: float) -> BenchmarkResult:
    """Run one benchmark at a scaled size."""
    if name == "events":
        return bench_events(count=max(10, int(2000 * factor)))
    if name == "replay":
        return bench_replay(events=max(10, int(5000 * factor)))
    if name == "scheduler":
        return bench_scheduler(
            size=max(10, int(400 * factor)), rounds=max(5, int(200 * factor))
        )
    return bench_graph(size=max(10, int(400 * factor)), rounds=max(3, int(50 * factor)))


@dataclass(frozen=True, slots=True)
class Budgets:
    """Thresholds a release is expected to clear.

    Deliberately loose. These exist to catch an order-of-magnitude regression —
    an accidental O(n²), a lost index, a fsync per row — not to police a
    ten-percent drift between one machine and another. A tight threshold on a
    shared runner is a test that fails for reasons nobody can act on.
    """

    events_per_second: float = 50.0
    replay_events_per_second: float = 20_000.0
    scheduler_decisions_per_second: float = 100.0
    graphs_per_second: float = 10.0

    def check(self, suite: BenchmarkSuite) -> tuple[str, ...]:
        """Return one message per budget the suite missed."""
        failures: list[str] = []
        expectations = (
            ("events.append", self.events_per_second),
            ("events.replay", self.replay_events_per_second),
            ("scheduler.decide", self.scheduler_decisions_per_second),
            ("graph.build", self.graphs_per_second),
        )
        for name, floor in expectations:
            result = suite.get(name)
            if result is None:
                continue
            if result.per_second < floor:
                failures.append(
                    f"{name}: {result.per_second:,.0f}/s is below the floor of {floor:,.0f}/s"
                )
        return tuple(failures)


def compare(
    baseline: Iterable[dict[str, Any]], current: BenchmarkSuite, *, tolerance: float = 0.5
) -> tuple[str, ...]:
    """Compare a suite against a recorded baseline.

    Args:
        baseline: Results previously produced by :meth:`BenchmarkSuite.to_dict`.
        current: What was just measured.
        tolerance: How much slower is acceptable, as a fraction. The default of
            one half is wide on purpose: benchmark noise between machines is
            large, and a regression check that cries wolf gets muted.

    Returns:
        One message per benchmark that regressed beyond the tolerance.
    """
    previous = {entry["name"]: entry for entry in baseline}
    regressions: list[str] = []

    for result in current.results:
        before = previous.get(result.name)
        if not before:
            continue
        floor = before["per_second"] * (1 - tolerance)
        if result.per_second < floor:
            regressions.append(
                f"{result.name}: {result.per_second:,.0f}/s, was "
                f"{before['per_second']:,.0f}/s"
            )
    return tuple(regressions)

"""The task dependency graph.

A :class:`TaskGraph` is an immutable, validated DAG of :class:`~orchestrator.task.model.Task`.
Everything that can be wrong with a set of tasks is caught **at construction** —
a duplicate identifier, a dependency on a task that does not exist, a cycle —
so that no work ever begins against a graph that cannot finish (FR-1.3).

Like the rest of this package the module is pure: no I/O, no clock, no
processes. That is what lets the scheduler above it be exhaustively tested
(NFR-1.1).

:meth:`TaskGraph.layers` is the parallel-execution plan: each layer holds tasks
whose dependencies all live in earlier layers, so every task in a layer may run
concurrently. The scheduler does not use it to make decisions — it schedules
from live state, which is strictly better than a precomputed plan when attempts
fail and retry — but it is what you want for "how wide can this run get, and how
deep is it?" before committing to anything.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Iterator, Mapping
from typing import Final

from orchestrator.core.events import OrchestratorError, TaskId
from orchestrator.task.model import Task

__all__ = [
    "CycleError",
    "DuplicateTaskError",
    "GraphError",
    "MissingDependencyError",
    "TaskGraph",
]


class GraphError(OrchestratorError):
    """The task graph is not well-formed."""

    code = "graph"
    retryable = False


class DuplicateTaskError(GraphError):
    """The same task identifier appeared more than once."""

    code = "duplicate_task"
    retryable = False


class MissingDependencyError(GraphError):
    """A task depends on an identifier that is not in the graph."""

    code = "missing_dependency"
    retryable = False


class CycleError(GraphError):
    """The dependency edges form a cycle, so no task could ever start."""

    code = "dependency_cycle"
    retryable = False


class TaskGraph:
    """An immutable, validated DAG of tasks."""

    __slots__ = ("_dependents", "_order", "_tasks", "_unblock_weight")

    def __init__(self, tasks: Iterable[Task]) -> None:
        """Build and validate the graph.

        Args:
            tasks: The tasks to include, in any order.

        Raises:
            DuplicateTaskError: If an identifier appears twice.
            MissingDependencyError: If a dependency is not present.
            CycleError: If the edges form a cycle.
        """
        indexed: dict[TaskId, Task] = {}
        for task in tasks:
            if task.id in indexed:
                raise DuplicateTaskError(
                    f"task {task.id} appears more than once in the graph",
                    detail={"task_id": str(task.id)},
                )
            indexed[task.id] = task
        self._tasks: Final[Mapping[TaskId, Task]] = indexed

        dependents: dict[TaskId, set[TaskId]] = {task_id: set() for task_id in indexed}
        for task in indexed.values():
            for dependency in task.depends_on:
                if dependency not in indexed:
                    raise MissingDependencyError(
                        f"task {task.id} depends on {dependency}, which is not in the graph",
                        detail={"task_id": str(task.id), "missing": str(dependency)},
                    )
                dependents[dependency].add(task.id)
        self._dependents: Final[Mapping[TaskId, frozenset[TaskId]]] = {
            task_id: frozenset(children) for task_id, children in dependents.items()
        }

        self._order: Final[tuple[TaskId, ...]] = self._topological_sort()
        self._unblock_weight: Final[Mapping[TaskId, int]] = self._compute_weights()

    # ------------------------------------------------------------- validation

    def _topological_sort(self) -> tuple[TaskId, ...]:
        """Return a stable topological ordering, or raise on a cycle.

        Kahn's algorithm, taking ready identifiers in sorted order so the result
        is deterministic for a given set of tasks. Determinism matters: the same
        graph must schedule the same way every time (NFR-1.2).
        """
        remaining = {
            task_id: len(task.depends_on) for task_id, task in self._tasks.items()
        }
        frontier = deque(sorted(t for t, count in remaining.items() if count == 0))
        ordered: list[TaskId] = []

        while frontier:
            task_id = frontier.popleft()
            ordered.append(task_id)
            newly_ready: list[TaskId] = []
            for child in sorted(self._dependents[task_id]):
                remaining[child] -= 1
                if remaining[child] == 0:
                    newly_ready.append(child)
            frontier.extend(sorted(newly_ready))

        if len(ordered) != len(self._tasks):
            stuck = sorted(str(t) for t, count in remaining.items() if count > 0)
            raise CycleError(
                "the dependency edges form a cycle; these tasks can never start: "
                + ", ".join(stuck),
                detail={"tasks_in_cycle": stuck},
            )
        return tuple(ordered)

    def _compute_weights(self) -> Mapping[TaskId, int]:
        """Count each task's transitive dependents.

        Used as a scheduling priority: starting a task that unblocks a lot of
        downstream work keeps the fleet busier than starting a leaf.

        Walking the topological order in reverse means every child's reachable
        set is already known when its parent is visited, so one pass suffices.
        """
        reachable: dict[TaskId, frozenset[TaskId]] = {}
        for task_id in reversed(self._order):
            accumulated: set[TaskId] = set()
            for child in self._dependents[task_id]:
                accumulated.add(child)
                accumulated |= reachable[child]
            reachable[task_id] = frozenset(accumulated)
        return {task_id: len(found) for task_id, found in reachable.items()}

    # -------------------------------------------------------------- accessors

    @property
    def tasks(self) -> Mapping[TaskId, Task]:
        """Every task, keyed by identifier."""
        return self._tasks

    @property
    def task_ids(self) -> tuple[TaskId, ...]:
        """Every identifier, in topological order."""
        return self._order

    @property
    def roots(self) -> tuple[TaskId, ...]:
        """Tasks with no dependencies; they may start immediately."""
        return tuple(sorted(t for t, task in self._tasks.items() if not task.depends_on))

    @property
    def leaves(self) -> tuple[TaskId, ...]:
        """Tasks nothing else depends on."""
        return tuple(sorted(t for t in self._tasks if not self._dependents[t]))

    def __len__(self) -> int:
        return len(self._tasks)

    def __contains__(self, task_id: object) -> bool:
        return task_id in self._tasks

    def __iter__(self) -> Iterator[Task]:
        """Iterate tasks in topological order."""
        return (self._tasks[task_id] for task_id in self._order)

    def get(self, task_id: TaskId) -> Task:
        """Return one task.

        Raises:
            MissingDependencyError: If the identifier is not in the graph.
        """
        try:
            return self._tasks[task_id]
        except KeyError:
            raise MissingDependencyError(
                f"task {task_id} is not in the graph", detail={"task_id": str(task_id)}
            ) from None

    def dependencies(self, task_id: TaskId) -> tuple[TaskId, ...]:
        """Return the tasks ``task_id`` waits on."""
        return self.get(task_id).depends_on

    def dependents(self, task_id: TaskId) -> frozenset[TaskId]:
        """Return the tasks waiting directly on ``task_id``."""
        self.get(task_id)
        return self._dependents[task_id]

    def unblock_weight(self, task_id: TaskId) -> int:
        """Return how many tasks ``task_id`` transitively unblocks."""
        self.get(task_id)
        return self._unblock_weight[task_id]

    def descendants(self, task_id: TaskId) -> frozenset[TaskId]:
        """Return every task transitively downstream of ``task_id``."""
        self.get(task_id)
        out: set[TaskId] = set()
        stack = [task_id]
        while stack:
            for child in self._dependents[stack.pop()]:
                if child not in out:
                    out.add(child)
                    stack.append(child)
        return frozenset(out)

    def ancestors(self, task_id: TaskId) -> frozenset[TaskId]:
        """Return every task transitively upstream of ``task_id``."""
        out: set[TaskId] = set()
        stack = [task_id]
        while stack:
            for parent in self.get(stack.pop()).depends_on:
                if parent not in out:
                    out.add(parent)
                    stack.append(parent)
        return frozenset(out)

    # ------------------------------------------------------- execution shape

    def layers(self) -> tuple[tuple[TaskId, ...], ...]:
        """Return the parallel-execution plan.

        Each layer contains tasks whose dependencies all lie in earlier layers,
        so every task within a layer may run concurrently. The number of layers
        is the longest dependency chain; the widest layer is the most
        parallelism the graph can ever offer.

        This describes the graph's *shape*. It is not what the scheduler
        executes — the scheduler works from live state, which handles failure
        and retry that a precomputed plan cannot.
        """
        depth: dict[TaskId, int] = {}
        for task_id in self._order:
            dependencies = self._tasks[task_id].depends_on
            depth[task_id] = (
                0 if not dependencies else 1 + max(depth[d] for d in dependencies)
            )

        if not depth:
            return ()
        grouped: list[list[TaskId]] = [[] for _ in range(max(depth.values()) + 1)]
        for task_id, level in depth.items():
            grouped[level].append(task_id)
        return tuple(tuple(sorted(layer)) for layer in grouped)

    @property
    def depth(self) -> int:
        """The longest dependency chain, in tasks."""
        return len(self.layers())

    @property
    def max_width(self) -> int:
        """The most tasks that could ever run at once."""
        layers = self.layers()
        return max((len(layer) for layer in layers), default=0)

    def labels(self) -> frozenset[str]:
        """Every label attached to any task in the graph."""
        return frozenset(label for task in self._tasks.values() for label in task.labels)

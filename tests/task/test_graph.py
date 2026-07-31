"""Tests for the task dependency graph."""

from __future__ import annotations

import random

import pytest

from orchestrator.core.events import TaskId
from orchestrator.task.graph import (
    CycleError,
    DuplicateTaskError,
    MissingDependencyError,
    TaskGraph,
)

from tests.task.conftest import chain, diamond, fan_out, make_task


class TestConstruction:
    """Everything that can be wrong is caught before any work begins."""

    def test_an_empty_graph_is_valid(self) -> None:
        graph = TaskGraph([])
        assert len(graph) == 0
        assert graph.task_ids == ()
        assert graph.layers() == ()

    def test_a_single_task_is_its_own_root_and_leaf(self) -> None:
        task = make_task()
        graph = TaskGraph([task])
        assert graph.roots == (task.id,)
        assert graph.leaves == (task.id,)

    def test_duplicate_identifiers_are_rejected(self) -> None:
        task = make_task()
        with pytest.raises(DuplicateTaskError, match="more than once"):
            TaskGraph([task, task])

    def test_a_dependency_outside_the_graph_is_rejected(self) -> None:
        orphan = TaskId.generate()
        with pytest.raises(MissingDependencyError, match="not in the graph"):
            TaskGraph([make_task(depends_on=(orphan,))])

    def test_a_cycle_is_rejected_at_construction(self) -> None:
        """FR-1.3: a cyclic graph must never reach the scheduler."""
        first, second = make_task("a"), make_task("b")
        # Rebuild each with a dependency on the other.
        from dataclasses import replace

        cyclic_a = replace(first, depends_on=(second.id,))
        cyclic_b = replace(second, depends_on=(first.id,))
        with pytest.raises(CycleError, match="cycle"):
            TaskGraph([cyclic_a, cyclic_b])

    def test_a_longer_cycle_is_rejected(self) -> None:
        from dataclasses import replace

        a, b, c = make_task("a"), make_task("b"), make_task("c")
        graph_tasks = [
            replace(a, depends_on=(c.id,)),
            replace(b, depends_on=(a.id,)),
            replace(c, depends_on=(b.id,)),
        ]
        with pytest.raises(CycleError) as excinfo:
            TaskGraph(graph_tasks)
        assert len(excinfo.value.detail["tasks_in_cycle"]) == 3

    def test_a_valid_diamond_is_accepted(self) -> None:
        graph = TaskGraph(diamond())
        assert len(graph) == 4


class TestTopology:
    """Ordering is topological and deterministic."""

    def test_dependencies_precede_dependents(self) -> None:
        tasks = diamond()
        graph = TaskGraph(tasks)
        order = list(graph.task_ids)
        for task in tasks:
            for dependency in task.depends_on:
                assert order.index(dependency) < order.index(task.id)

    def test_ordering_is_independent_of_input_order(self) -> None:
        """NFR-1.2: the same graph must schedule the same way every time."""
        tasks = diamond()
        shuffled = list(tasks)
        random.Random(7).shuffle(shuffled)
        assert TaskGraph(tasks).task_ids == TaskGraph(shuffled).task_ids

    def test_roots_and_leaves(self) -> None:
        tasks = diamond()
        graph = TaskGraph(tasks)
        assert graph.roots == (tasks[0].id,)
        assert graph.leaves == (tasks[3].id,)

    def test_iteration_yields_topological_order(self) -> None:
        graph = TaskGraph(chain(4))
        assert [task.id for task in graph] == list(graph.task_ids)

    def test_membership_and_length(self) -> None:
        tasks = chain(3)
        graph = TaskGraph(tasks)
        assert len(graph) == 3
        assert tasks[0].id in graph
        assert TaskId.generate() not in graph


class TestRelationships:
    """Dependency queries answer both directions."""

    def test_dependencies_and_dependents(self) -> None:
        tasks = diamond()
        root, left, right, join = tasks
        graph = TaskGraph(tasks)

        assert graph.dependencies(join.id) == (left.id, right.id)
        assert graph.dependents(root.id) == frozenset({left.id, right.id})
        assert graph.dependents(join.id) == frozenset()

    def test_descendants_and_ancestors(self) -> None:
        tasks = diamond()
        root, left, right, join = tasks
        graph = TaskGraph(tasks)

        assert graph.descendants(root.id) == frozenset({left.id, right.id, join.id})
        assert graph.ancestors(join.id) == frozenset({root.id, left.id, right.id})
        assert graph.ancestors(root.id) == frozenset()

    def test_unblock_weight_counts_transitive_dependents(self) -> None:
        tasks = diamond()
        root, left, _right, join = tasks
        graph = TaskGraph(tasks)

        assert graph.unblock_weight(root.id) == 3
        assert graph.unblock_weight(left.id) == 1
        assert graph.unblock_weight(join.id) == 0

    def test_unblock_weight_does_not_double_count_diamonds(self) -> None:
        """The join is reachable by two paths but is still one task."""
        graph = TaskGraph(diamond())
        assert graph.unblock_weight(graph.roots[0]) == 3

    def test_weight_on_a_chain_is_the_remaining_length(self) -> None:
        tasks = chain(5)
        graph = TaskGraph(tasks)
        assert [graph.unblock_weight(t.id) for t in tasks] == [4, 3, 2, 1, 0]

    def test_querying_an_unknown_task_raises(self) -> None:
        graph = TaskGraph(chain(2))
        with pytest.raises(MissingDependencyError):
            graph.get(TaskId.generate())
        with pytest.raises(MissingDependencyError):
            graph.dependents(TaskId.generate())


class TestExecutionShape:
    """Layers describe how wide and how deep a run can get."""

    def test_a_chain_is_one_task_per_layer(self) -> None:
        graph = TaskGraph(chain(4))
        assert graph.layers() == tuple((task_id,) for task_id in graph.task_ids)
        assert graph.depth == 4
        assert graph.max_width == 1

    def test_a_fan_out_is_two_layers(self) -> None:
        graph = TaskGraph(fan_out(5))
        layers = graph.layers()
        assert len(layers) == 2
        assert len(layers[0]) == 1
        assert len(layers[1]) == 5
        assert graph.max_width == 5

    def test_a_diamond_is_three_layers(self) -> None:
        graph = TaskGraph(diamond())
        layers = graph.layers()
        assert len(layers) == 3
        assert len(layers[1]) == 2

    def test_every_task_appears_exactly_once_across_layers(self) -> None:
        graph = TaskGraph(diamond())
        flattened = [task_id for layer in graph.layers() for task_id in layer]
        assert sorted(flattened) == sorted(graph.task_ids)

    def test_dependencies_land_in_strictly_earlier_layers(self) -> None:
        graph = TaskGraph(diamond())
        level = {
            task_id: index
            for index, layer in enumerate(graph.layers())
            for task_id in layer
        }
        for task in graph:
            for dependency in task.depends_on:
                assert level[dependency] < level[task.id]

    def test_independent_tasks_share_one_layer(self) -> None:
        graph = TaskGraph([make_task(f"t{i}") for i in range(4)])
        assert len(graph.layers()) == 1
        assert graph.max_width == 4

    def test_labels_are_collected(self) -> None:
        graph = TaskGraph(
            [make_task("a", labels=("db",)), make_task("b", labels=("io", "db"))]
        )
        assert graph.labels() == frozenset({"db", "io"})


class TestRandomGraphs:
    """Properties that must hold across many shapes."""

    @pytest.mark.parametrize("seed", range(12))
    def test_random_dags_are_accepted_and_ordered(self, seed: int) -> None:
        rng = random.Random(seed)
        tasks: list = []
        for index in range(rng.randint(2, 14)):
            candidates = [t.id for t in tasks]
            rng.shuffle(candidates)
            depends = tuple(candidates[: rng.randint(0, min(3, len(candidates)))])
            tasks.append(make_task(f"t{index}", depends_on=depends))

        graph = TaskGraph(tasks)
        order = list(graph.task_ids)

        assert len(order) == len(tasks)
        for task in tasks:
            for dependency in task.depends_on:
                assert order.index(dependency) < order.index(task.id)

    @pytest.mark.parametrize("seed", range(12))
    def test_layers_partition_every_random_dag(self, seed: int) -> None:
        rng = random.Random(seed + 100)
        tasks: list = []
        for index in range(rng.randint(2, 14)):
            candidates = [t.id for t in tasks]
            rng.shuffle(candidates)
            depends = tuple(candidates[: rng.randint(0, min(3, len(candidates)))])
            tasks.append(make_task(f"t{index}", depends_on=depends))

        graph = TaskGraph(tasks)
        flattened = [task_id for layer in graph.layers() for task_id in layer]
        assert sorted(flattened) == sorted(graph.task_ids)

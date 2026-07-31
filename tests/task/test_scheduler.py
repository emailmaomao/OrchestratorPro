"""Tests for the pure scheduler.

The three invariants below carry property tests over randomly generated graphs,
because they are the ones whose violation would corrupt a run rather than merely
slow it down.
"""

from __future__ import annotations

import random

import pytest

from orchestrator.core.events import TaskId
from orchestrator.task.graph import TaskGraph
from orchestrator.task.model import TaskState
from orchestrator.task.scheduler import (
    SchedulerError,
    SchedulerState,
    is_complete,
    next_ready,
)

from tests.task.conftest import chain, diamond, fan_out, make_task


class TestValidation:
    """The scheduler refuses incoherent inputs rather than guessing."""

    def test_non_positive_concurrency_is_refused(self) -> None:
        graph = TaskGraph(chain(2))
        with pytest.raises(SchedulerError, match="at least 1"):
            next_ready(graph, SchedulerState(), max_concurrency=0)

    def test_state_referring_to_an_unknown_task_is_refused(self) -> None:
        graph = TaskGraph(chain(2))
        rogue = SchedulerState(states={TaskId.generate(): TaskState.RUNNING})
        with pytest.raises(SchedulerError, match="not in the graph"):
            next_ready(graph, rogue, max_concurrency=2)


class TestDependencyOrdering:
    """FR-2.2: nothing starts before its dependencies have succeeded."""

    def test_only_roots_start_on_an_empty_state(self) -> None:
        tasks = diamond()
        graph = TaskGraph(tasks)
        decision = next_ready(graph, SchedulerState(), max_concurrency=10)
        assert decision.start == (tasks[0].id,)

    def test_dependents_wait_until_the_dependency_succeeds(self) -> None:
        tasks = chain(2)
        graph = TaskGraph(tasks)

        running = SchedulerState(states={tasks[0].id: TaskState.RUNNING})
        assert next_ready(graph, running, max_concurrency=5).start == ()
        assert tasks[1].id in next_ready(graph, running, max_concurrency=5).waiting

        done = SchedulerState(states={tasks[0].id: TaskState.SUCCEEDED})
        assert next_ready(graph, done, max_concurrency=5).start == (tasks[1].id,)

    def test_a_join_waits_for_every_dependency(self) -> None:
        root, left, right, join = diamond()
        graph = TaskGraph([root, left, right, join])
        partial = SchedulerState(
            states={
                root.id: TaskState.SUCCEEDED,
                left.id: TaskState.SUCCEEDED,
                right.id: TaskState.RUNNING,
            }
        )
        assert join.id not in next_ready(graph, partial, max_concurrency=5).start

        complete = SchedulerState(
            states={
                root.id: TaskState.SUCCEEDED,
                left.id: TaskState.SUCCEEDED,
                right.id: TaskState.SUCCEEDED,
            }
        )
        assert next_ready(graph, complete, max_concurrency=5).start == (join.id,)

    def test_independent_tasks_start_together(self) -> None:
        tasks = [make_task(f"t{i}") for i in range(4)]
        graph = TaskGraph(tasks)
        decision = next_ready(graph, SchedulerState(), max_concurrency=10)
        assert len(decision.start) == 4


class TestConcurrencyCap:
    """FR-2.1: the cap is never exceeded."""

    def test_start_is_limited_by_the_cap(self) -> None:
        graph = TaskGraph([make_task(f"t{i}") for i in range(10)])
        decision = next_ready(graph, SchedulerState(), max_concurrency=3)
        assert len(decision.start) == 3
        assert len(decision.ready) == 7

    def test_running_tasks_consume_slots(self) -> None:
        tasks = [make_task(f"t{i}") for i in range(5)]
        graph = TaskGraph(tasks)
        state = SchedulerState(
            states={tasks[0].id: TaskState.RUNNING, tasks[1].id: TaskState.GATING}
        )
        decision = next_ready(graph, state, max_concurrency=3)
        assert len(decision.start) == 1

    def test_a_full_pool_starts_nothing(self) -> None:
        tasks = [make_task(f"t{i}") for i in range(4)]
        graph = TaskGraph(tasks)
        state = SchedulerState(
            states={tasks[0].id: TaskState.RUNNING, tasks[1].id: TaskState.RUNNING}
        )
        assert next_ready(graph, state, max_concurrency=2).start == ()

    def test_gating_counts_against_the_cap(self) -> None:
        """A task awaiting its gate still holds its workspace."""
        tasks = [make_task(f"t{i}") for i in range(3)]
        graph = TaskGraph(tasks)
        state = SchedulerState(states={tasks[0].id: TaskState.GATING})
        assert len(next_ready(graph, state, max_concurrency=1).start) == 0


class TestLabelLimits:
    """Per-label caps apply on top of the global one."""

    def test_a_label_limit_throttles_its_tasks(self) -> None:
        tasks = [make_task(f"t{i}", labels=("db",)) for i in range(4)]
        graph = TaskGraph(tasks)
        decision = next_ready(
            graph, SchedulerState(), max_concurrency=10, label_limits={"db": 2}
        )
        assert len(decision.start) == 2

    def test_unlabelled_tasks_are_unaffected(self) -> None:
        labelled = [make_task(f"db{i}", labels=("db",)) for i in range(3)]
        plain = [make_task(f"p{i}") for i in range(3)]
        graph = TaskGraph([*labelled, *plain])
        decision = next_ready(
            graph, SchedulerState(), max_concurrency=10, label_limits={"db": 1}
        )
        assert len(decision.start) == 4

    def test_running_labelled_tasks_count_toward_the_limit(self) -> None:
        tasks = [make_task(f"t{i}", labels=("db",)) for i in range(3)]
        graph = TaskGraph(tasks)
        state = SchedulerState(states={tasks[0].id: TaskState.RUNNING})
        decision = next_ready(
            graph, state, max_concurrency=10, label_limits={"db": 2}
        )
        assert len(decision.start) == 1


class TestBlocking:
    """FR-2.3: a task whose dependency died is blocked, never attempted."""

    @pytest.mark.parametrize("dead", [TaskState.BLOCKED, TaskState.ABANDONED])
    def test_a_dead_dependency_blocks_its_dependent(self, dead: TaskState) -> None:
        tasks = chain(2)
        graph = TaskGraph(tasks)
        state = SchedulerState(states={tasks[0].id: dead})
        decision = next_ready(graph, state, max_concurrency=5)
        assert decision.block == (tasks[1].id,)
        assert decision.start == ()

    def test_a_failed_dependency_with_attempts_left_only_delays(self) -> None:
        tasks = chain(2, max_attempts=3)
        graph = TaskGraph(tasks)
        state = SchedulerState(
            states={tasks[0].id: TaskState.FAILED}, attempts={tasks[0].id: 1}
        )
        decision = next_ready(graph, state, max_concurrency=5)
        assert decision.block == ()
        assert tasks[1].id in decision.waiting

    def test_a_failed_dependency_out_of_attempts_blocks(self) -> None:
        tasks = chain(2, max_attempts=2)
        graph = TaskGraph(tasks)
        state = SchedulerState(
            states={tasks[0].id: TaskState.FAILED}, attempts={tasks[0].id: 2}
        )
        assert next_ready(graph, state, max_concurrency=5).block == (tasks[1].id,)

    def test_a_task_out_of_its_own_attempts_is_blocked(self) -> None:
        task = make_task(max_attempts=2)
        graph = TaskGraph([task])
        state = SchedulerState(
            states={task.id: TaskState.RETRYING}, attempts={task.id: 2}
        )
        assert next_ready(graph, state, max_concurrency=5).block == (task.id,)

    def test_blocking_only_reaches_direct_dependents_per_pass(self) -> None:
        """Propagation happens one layer per scheduling pass, by design."""
        tasks = chain(3)
        graph = TaskGraph(tasks)
        state = SchedulerState(states={tasks[0].id: TaskState.ABANDONED})
        assert next_ready(graph, state, max_concurrency=5).block == (tasks[1].id,)


class TestPriority:
    """Ordering favours work that unblocks the most."""

    def test_the_task_unblocking_more_work_starts_first(self) -> None:
        deep = chain(4)
        shallow = make_task("shallow")
        graph = TaskGraph([*deep, shallow])
        decision = next_ready(graph, SchedulerState(), max_concurrency=1)
        assert decision.start == (deep[0].id,)

    def test_ordering_is_stable_across_calls(self) -> None:
        graph = TaskGraph(fan_out(6))
        first = next_ready(graph, SchedulerState(), max_concurrency=10)
        second = next_ready(graph, SchedulerState(), max_concurrency=10)
        assert first.start == second.start

    def test_retrying_tasks_are_startable(self) -> None:
        task = make_task(max_attempts=3)
        graph = TaskGraph([task])
        state = SchedulerState(
            states={task.id: TaskState.RETRYING}, attempts={task.id: 1}
        )
        assert next_ready(graph, state, max_concurrency=1).start == (task.id,)


class TestCompletion:
    """Knowing when to stop."""

    def test_an_all_succeeded_graph_is_complete(self) -> None:
        tasks = chain(3)
        graph = TaskGraph(tasks)
        state = SchedulerState(states=dict.fromkeys(graph.task_ids, TaskState.SUCCEEDED))
        assert is_complete(graph, state)
        assert next_ready(graph, state, max_concurrency=5).complete

    def test_a_running_graph_is_not_complete(self) -> None:
        tasks = chain(2)
        graph = TaskGraph(tasks)
        state = SchedulerState(states={tasks[0].id: TaskState.RUNNING})
        assert not is_complete(graph, state)

    def test_an_empty_graph_is_complete(self) -> None:
        assert is_complete(TaskGraph([]), SchedulerState())

    def test_terminal_failures_still_count_as_complete(self) -> None:
        tasks = chain(2, max_attempts=1)
        graph = TaskGraph(tasks)
        state = SchedulerState(
            states={tasks[0].id: TaskState.ABANDONED, tasks[1].id: TaskState.BLOCKED}
        )
        assert is_complete(graph, state)

    def test_a_failed_task_with_attempts_left_is_not_complete(self) -> None:
        tasks = chain(1, max_attempts=3)
        graph = TaskGraph(tasks)
        state = SchedulerState(
            states={tasks[0].id: TaskState.FAILED}, attempts={tasks[0].id: 1}
        )
        assert not is_complete(graph, state)


class TestSchedulerState:
    """The state snapshot is a value, not a handle."""

    def test_defaults_are_pending_and_zero(self) -> None:
        state = SchedulerState()
        task_id = TaskId.generate()
        assert state.state_of(task_id) is TaskState.PENDING
        assert state.attempts_of(task_id) == 0

    def test_with_state_does_not_mutate_the_original(self) -> None:
        task_id = TaskId.generate()
        original = SchedulerState(states={task_id: TaskState.PENDING})
        updated = original.with_state(task_id, TaskState.RUNNING)
        assert original.state_of(task_id) is TaskState.PENDING
        assert updated.state_of(task_id) is TaskState.RUNNING

    def test_running_lists_occupying_tasks(self) -> None:
        a, b = TaskId.generate(), TaskId.generate()
        state = SchedulerState(states={a: TaskState.RUNNING, b: TaskState.GATING})
        assert set(state.running()) == {a, b}
        assert state.occupied_slots == 2


class TestPropertiesOverRandomGraphs:
    """The invariants that would corrupt a run, checked broadly."""

    @staticmethod
    def _random_graph(seed: int) -> TaskGraph:
        rng = random.Random(seed)
        tasks: list = []
        for index in range(rng.randint(3, 16)):
            candidates = [t.id for t in tasks]
            rng.shuffle(candidates)
            depends = tuple(candidates[: rng.randint(0, min(3, len(candidates)))])
            tasks.append(
                make_task(
                    f"t{index}",
                    depends_on=depends,
                    max_attempts=rng.randint(1, 3),
                )
            )
        return TaskGraph(tasks)

    @pytest.mark.parametrize("seed", range(20))
    def test_nothing_starts_before_its_dependencies(self, seed: int) -> None:
        graph = self._random_graph(seed)
        rng = random.Random(seed + 500)
        assigned = {
            task_id: rng.choice(list(TaskState)) for task_id in graph.task_ids
        }
        state = SchedulerState(states=assigned)

        for task_id in next_ready(graph, state, max_concurrency=8).start:
            for dependency in graph.dependencies(task_id):
                assert state.state_of(dependency) is TaskState.SUCCEEDED

    @pytest.mark.parametrize("seed", range(20))
    def test_the_cap_is_never_exceeded(self, seed: int) -> None:
        graph = self._random_graph(seed)
        rng = random.Random(seed + 900)
        assigned = {
            task_id: rng.choice(list(TaskState)) for task_id in graph.task_ids
        }
        state = SchedulerState(states=assigned)

        for cap in (1, 2, 5):
            decision = next_ready(graph, state, max_concurrency=cap)
            # Randomly assigned states may already exceed the cap — a situation
            # the dispatcher never creates. The invariant is that the scheduler
            # never *adds* beyond the remaining headroom, and starts nothing at
            # all when there is none.
            headroom = max(0, cap - state.occupied_slots)
            assert len(decision.start) <= headroom

    @pytest.mark.parametrize("seed", range(20))
    def test_decisions_are_deterministic(self, seed: int) -> None:
        graph = self._random_graph(seed)
        state = SchedulerState()
        assert next_ready(graph, state, max_concurrency=4) == next_ready(
            graph, state, max_concurrency=4
        )

    @pytest.mark.parametrize("seed", range(20))
    def test_started_and_blocked_never_overlap(self, seed: int) -> None:
        graph = self._random_graph(seed)
        rng = random.Random(seed + 77)
        assigned = {
            task_id: rng.choice(list(TaskState)) for task_id in graph.task_ids
        }
        decision = next_ready(
            graph, SchedulerState(states=assigned), max_concurrency=6
        )
        assert not set(decision.start) & set(decision.block)

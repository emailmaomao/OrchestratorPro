"""Tests for the deterministic ready queue."""

from __future__ import annotations

import pytest

from orchestrator.core.events import TaskId
from orchestrator.task.queue import QueueError, ReadyQueue


@pytest.fixture
def ids() -> list[TaskId]:
    """Five identifiers in ascending order."""
    return [TaskId.generate() for _ in range(5)]


class TestBasics:
    """Push, pop, and the obvious container behaviours."""

    def test_a_new_queue_is_empty(self) -> None:
        queue = ReadyQueue()
        assert len(queue) == 0
        assert not queue
        assert queue.pop() is None
        assert queue.peek() is None

    def test_push_then_pop(self, ids: list[TaskId]) -> None:
        queue = ReadyQueue()
        queue.push(ids[0])
        assert len(queue) == 1
        assert ids[0] in queue
        assert queue.pop() == ids[0]
        assert not queue

    def test_initial_items_are_enqueued(self, ids: list[TaskId]) -> None:
        queue = ReadyQueue([(ids[0], 1), (ids[1], 5)])
        assert len(queue) == 2
        assert queue.pop() == ids[1]

    def test_peek_does_not_remove(self, ids: list[TaskId]) -> None:
        queue = ReadyQueue([(ids[0], 0)])
        assert queue.peek() == ids[0]
        assert len(queue) == 1

    def test_pushing_a_duplicate_is_refused(self, ids: list[TaskId]) -> None:
        """A task queued twice would be dispatched twice."""
        queue = ReadyQueue()
        queue.push(ids[0])
        with pytest.raises(QueueError, match="already queued"):
            queue.push(ids[0])

    def test_push_many_skips_duplicates(self, ids: list[TaskId]) -> None:
        queue = ReadyQueue()
        assert queue.push_many([(ids[0], 0), (ids[1], 0)]) == 2
        assert queue.push_many([(ids[1], 0), (ids[2], 0)]) == 1
        assert len(queue) == 3

    def test_remove_reports_whether_it_was_present(self, ids: list[TaskId]) -> None:
        queue = ReadyQueue([(ids[0], 0)])
        assert queue.remove(ids[0]) is True
        assert queue.remove(ids[0]) is False
        assert not queue

    def test_a_removed_task_is_never_popped(self, ids: list[TaskId]) -> None:
        queue = ReadyQueue([(ids[0], 5), (ids[1], 1)])
        queue.remove(ids[0])
        assert queue.pop() == ids[1]
        assert queue.pop() is None

    def test_clear_empties_the_queue(self, ids: list[TaskId]) -> None:
        queue = ReadyQueue([(i, 0) for i in ids])
        queue.clear()
        assert not queue
        assert queue.pop() is None

    def test_drain_returns_everything_in_order(self, ids: list[TaskId]) -> None:
        queue = ReadyQueue([(ids[0], 1), (ids[1], 9), (ids[2], 5)])
        assert queue.drain() == (ids[1], ids[2], ids[0])
        assert not queue


class TestOrdering:
    """Ordering is total and deterministic."""

    def test_higher_priority_pops_first(self, ids: list[TaskId]) -> None:
        queue = ReadyQueue([(ids[0], 1), (ids[1], 10), (ids[2], 5)])
        assert queue.drain() == (ids[1], ids[2], ids[0])

    def test_equal_priority_keeps_insertion_order(self, ids: list[TaskId]) -> None:
        queue = ReadyQueue()
        for task_id in reversed(ids):
            queue.push(task_id, 0)
        assert queue.drain() == tuple(reversed(ids))

    def test_the_same_inputs_always_dequeue_identically(
        self, ids: list[TaskId]
    ) -> None:
        """NFR-1.2: a scheduler that shuffles cannot be reproduced."""
        items = [(ids[0], 3), (ids[1], 3), (ids[2], 7), (ids[3], 1)]
        first = ReadyQueue(items).drain()
        second = ReadyQueue(items).drain()
        assert first == second

    def test_negative_priorities_sort_last(self, ids: list[TaskId]) -> None:
        queue = ReadyQueue([(ids[0], -5), (ids[1], 0)])
        assert queue.drain() == (ids[1], ids[0])

    def test_snapshot_matches_pop_order_without_consuming(
        self, ids: list[TaskId]
    ) -> None:
        queue = ReadyQueue([(ids[0], 1), (ids[1], 9), (ids[2], 5)])
        snapshot = queue.snapshot()
        assert len(queue) == 3
        assert snapshot == queue.drain()

    def test_iteration_does_not_consume(self, ids: list[TaskId]) -> None:
        queue = ReadyQueue([(i, 0) for i in ids[:3]])
        assert list(queue) == list(queue)
        assert len(queue) == 3


class TestPopMany:
    """Bulk dequeue for filling a batch of concurrency slots."""

    def test_pop_many_respects_priority(self, ids: list[TaskId]) -> None:
        queue = ReadyQueue([(ids[0], 1), (ids[1], 9), (ids[2], 5)])
        assert queue.pop_many(2) == (ids[1], ids[2])
        assert len(queue) == 1

    def test_pop_many_stops_at_the_end(self, ids: list[TaskId]) -> None:
        queue = ReadyQueue([(ids[0], 0)])
        assert queue.pop_many(10) == (ids[0],)

    def test_pop_many_zero_returns_nothing(self, ids: list[TaskId]) -> None:
        queue = ReadyQueue([(ids[0], 0)])
        assert queue.pop_many(0) == ()
        assert len(queue) == 1

    def test_a_negative_count_is_refused(self) -> None:
        with pytest.raises(QueueError, match="negative"):
            ReadyQueue().pop_many(-1)


def test_reused_identifiers_after_pop_can_be_requeued(ids: list[TaskId]) -> None:
    """A task that ran and failed must be able to re-enter the queue."""
    queue = ReadyQueue()
    queue.push(ids[0], 1)
    assert queue.pop() == ids[0]
    queue.push(ids[0], 5)
    assert queue.pop() == ids[0]

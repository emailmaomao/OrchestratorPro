"""The ready queue.

Holds tasks whose dependencies have all succeeded and which are waiting for a
concurrency slot. Ordering is **total and deterministic**: the same set of ready
tasks always dequeues in the same sequence, because a scheduler that shuffles
under identical inputs cannot be reasoned about or reproduced (NFR-1.2).

Priority is the number of tasks a task transitively unblocks. Starting work that
frees a lot of downstream work keeps the fleet busy; starting a leaf first can
leave the whole graph waiting on one long pole. Ties break by insertion order,
then by identifier — so the ordering is defined even when two tasks are
genuinely equivalent.

The queue is a data structure, not a policy: it does not know what "ready"
means. :mod:`orchestrator.task.scheduler` decides that.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from orchestrator.core.events import OrchestratorError, TaskId

__all__ = ["QueueEntry", "ReadyQueue", "QueueError"]


class QueueError(OrchestratorError):
    """A queue operation was invalid."""

    code = "queue"
    retryable = False


@dataclass(frozen=True, slots=True, order=True)
class QueueEntry:
    """One queued task, ordered by scheduling priority.

    The comparison key is ``(-priority, sequence, task_id)``: higher priority
    first, then insertion order, then identifier. ``task_id`` is last so that
    two entries are never equal, which keeps ordering total.
    """

    sort_priority: int
    sequence: int
    task_id: TaskId = field(compare=True)

    @property
    def priority(self) -> int:
        """The task's scheduling priority, higher being more urgent."""
        return -self.sort_priority


class ReadyQueue:
    """A deterministic priority queue of ready task identifiers."""

    __slots__ = ("_entries", "_heap", "_sequence")

    def __init__(self, initial: Iterable[tuple[TaskId, int]] = ()) -> None:
        """Create the queue.

        Args:
            initial: ``(task_id, priority)`` pairs to enqueue immediately.
        """
        self._heap: list[QueueEntry] = []
        self._entries: dict[TaskId, QueueEntry] = {}
        self._sequence = 0
        for task_id, priority in initial:
            self.push(task_id, priority)

    def push(self, task_id: TaskId, priority: int = 0) -> None:
        """Enqueue a task.

        Args:
            task_id: The task to enqueue.
            priority: Higher runs sooner.

        Raises:
            QueueError: If the task is already queued. A task queued twice would
                be dispatched twice, so this is a defect rather than a no-op.
        """
        if task_id in self._entries:
            raise QueueError(
                f"task {task_id} is already queued",
                detail={"task_id": str(task_id)},
            )
        entry = QueueEntry(
            sort_priority=-priority, sequence=self._sequence, task_id=task_id
        )
        self._sequence += 1
        self._entries[task_id] = entry
        heapq.heappush(self._heap, entry)

    def push_many(self, items: Iterable[tuple[TaskId, int]]) -> int:
        """Enqueue several tasks, skipping any already present.

        Args:
            items: ``(task_id, priority)`` pairs.

        Returns:
            How many were actually enqueued.
        """
        added = 0
        for task_id, priority in items:
            if task_id not in self._entries:
                self.push(task_id, priority)
                added += 1
        return added

    def pop(self) -> TaskId | None:
        """Remove and return the highest-priority task, or ``None`` if empty."""
        while self._heap:
            entry = heapq.heappop(self._heap)
            # A removed entry stays in the heap until it surfaces; skip it.
            if self._entries.get(entry.task_id) is entry:
                del self._entries[entry.task_id]
                return entry.task_id
        return None

    def pop_many(self, count: int) -> tuple[TaskId, ...]:
        """Remove and return up to ``count`` tasks, highest priority first."""
        if count < 0:
            raise QueueError(f"cannot pop a negative count: {count}")
        popped: list[TaskId] = []
        for _ in range(count):
            task_id = self.pop()
            if task_id is None:
                break
            popped.append(task_id)
        return tuple(popped)

    def peek(self) -> TaskId | None:
        """Return the highest-priority task without removing it."""
        while self._heap:
            entry = self._heap[0]
            if self._entries.get(entry.task_id) is entry:
                return entry.task_id
            heapq.heappop(self._heap)
        return None

    def remove(self, task_id: TaskId) -> bool:
        """Remove a task from the queue.

        Args:
            task_id: The task to remove.

        Returns:
            ``True`` if it was queued, ``False`` otherwise.
        """
        if task_id in self._entries:
            del self._entries[task_id]
            return True
        return False

    def drain(self) -> tuple[TaskId, ...]:
        """Remove and return every task, in priority order."""
        drained: list[TaskId] = []
        while (task_id := self.pop()) is not None:
            drained.append(task_id)
        return tuple(drained)

    def clear(self) -> None:
        """Discard every queued task."""
        self._heap.clear()
        self._entries.clear()

    def snapshot(self) -> tuple[TaskId, ...]:
        """Return the queued tasks in priority order, without dequeuing."""
        return tuple(
            entry.task_id
            for entry in sorted(self._entries.values())
        )

    def __len__(self) -> int:
        return len(self._entries)

    def __bool__(self) -> bool:
        return bool(self._entries)

    def __contains__(self, task_id: object) -> bool:
        return task_id in self._entries

    def __iter__(self) -> Iterator[TaskId]:
        """Iterate queued tasks in priority order without dequeuing."""
        return iter(self.snapshot())

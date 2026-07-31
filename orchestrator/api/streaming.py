"""Event streaming: the in-process broker, plus SSE and WebSocket framing.

A client watching a run needs two things that are easy to get subtly wrong:

* **Nothing missed between the replay and the live tail.** A naive
  implementation replays the log, then subscribes — and loses every event that
  arrived in between. Here the subscription is opened *first* and the replay is
  de-duplicated against what has already been sent, so the seam is closed.
* **A slow reader must not corrupt anyone else.** Each subscriber owns a bounded
  queue. A client that stops reading fills its own queue and is disconnected
  with an explicit ``lagged`` frame; it is never served a silently incomplete
  stream, and it cannot slow the publisher or the other subscribers down.

Publishing is synchronous and non-blocking. It is called from
:meth:`~orchestrator.api.state.AppState.record`, which sits on the write path of
every event in the system; anything that could block there would make the log
slower for the benefit of whoever happens to be watching.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from orchestrator.core.events import Event, RunId

__all__ = [
    "DEFAULT_QUEUE_SIZE",
    "EventBroker",
    "Subscription",
    "sse_frame",
    "ws_payload",
]

#: How many events a subscriber may fall behind before it is disconnected.
#: Large enough that an ordinary client never notices, small enough that a dead
#: connection cannot pin an unbounded amount of memory.
DEFAULT_QUEUE_SIZE = 512

#: Sent to a subscriber that could not keep up, immediately before its stream
#: ends. A truncated stream that says so is recoverable; one that does not is a
#: client quietly acting on a partial picture.
LAGGED = object()


@dataclass(slots=True)
class Subscription:
    """One client's view of the event stream."""

    queue: asyncio.Queue[Any]
    run_id: RunId | None = None
    lagged: bool = False
    seen: set[str] = field(default_factory=set)

    def wants(self, event: Event) -> bool:
        """Whether this subscription is interested in an event."""
        return self.run_id is None or event.run_id == self.run_id

    def mark(self, event: Event) -> bool:
        """Record an event as sent, returning whether it is new.

        Used to close the replay/live seam: an event delivered live while the
        replay was still being written must not be written twice.
        """
        key = str(event.id)
        if key in self.seen:
            return False
        self.seen.add(key)
        return True


class EventBroker:
    """Fans recorded events out to connected clients."""

    __slots__ = ("_closed", "_dropped", "_published", "_queue_size", "_subscribers")

    def __init__(self, *, queue_size: int = DEFAULT_QUEUE_SIZE) -> None:
        """Create the broker.

        Args:
            queue_size: Per-subscriber backlog before it is considered lagged.
        """
        self._subscribers: list[Subscription] = []
        self._queue_size = queue_size
        self._published = 0
        self._dropped = 0
        self._closed = False

    @property
    def subscribers(self) -> int:
        """How many clients are connected."""
        return len(self._subscribers)

    @property
    def published(self) -> int:
        """How many events have been fanned out."""
        return self._published

    @property
    def dropped(self) -> int:
        """How many subscribers were disconnected for falling behind."""
        return self._dropped

    def subscribe(self, run_id: RunId | None = None) -> Subscription:
        """Open a subscription, optionally scoped to one run."""
        subscription = Subscription(
            queue=asyncio.Queue(maxsize=self._queue_size), run_id=run_id
        )
        self._subscribers.append(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        """Close a subscription. Idempotent."""
        if subscription in self._subscribers:
            self._subscribers.remove(subscription)

    def publish(self, event: Event) -> int:
        """Deliver an event to every interested subscriber.

        Never blocks and never raises: this runs on the event-write path, and a
        streaming client must not be able to fail a write.

        Returns:
            How many subscribers received it.
        """
        if self._closed:
            return 0
        self._published += 1
        delivered = 0
        for subscription in list(self._subscribers):
            if not subscription.wants(event):
                continue
            try:
                subscription.queue.put_nowait(event)
            except asyncio.QueueFull:
                subscription.lagged = True
                self._dropped += 1
                self._subscribers.remove(subscription)
                _force(subscription.queue, LAGGED)
                continue
            delivered += 1
        return delivered

    async def close(self) -> None:
        """Release every subscriber and refuse further publishing."""
        self._closed = True
        for subscription in list(self._subscribers):
            _force(subscription.queue, None)
        self._subscribers.clear()

    async def stream(
        self,
        subscription: Subscription,
        *,
        replay: tuple[Event, ...] = (),
        heartbeat_s: float | None = 15.0,
        stop: Callable[[], bool] | None = None,
    ) -> AsyncIterator[Event | None]:
        """Yield replayed events, then live ones, then heartbeats.

        Args:
            subscription: The subscription to drain.
            replay: Events already in the log when the client connected. Sent
                first, de-duplicated against anything delivered live.
            heartbeat_s: Yield ``None`` after this long with no traffic, so a
                caller can keep an idle connection open. ``None`` disables it.
            stop: Consulted between events; returning true ends the stream.

        Yields:
            Events, and ``None`` for each heartbeat.
        """
        for event in replay:
            if subscription.mark(event):
                yield event

        while True:
            if stop is not None and stop():
                return
            try:
                item = await asyncio.wait_for(
                    subscription.queue.get(), timeout=heartbeat_s
                )
            except TimeoutError:
                yield None
                continue

            if item is None:
                return
            if item is LAGGED:
                subscription.lagged = True
                return
            if isinstance(item, Event) and subscription.mark(item):
                yield item


def _force(queue: asyncio.Queue[Any], item: Any) -> None:
    """Put an item on a queue, making room if it is full."""
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        try:
            queue.get_nowait()
            queue.put_nowait(item)
        except (asyncio.QueueEmpty, asyncio.QueueFull):  # pragma: no cover
            pass


def ws_payload(event: Event) -> dict[str, Any]:
    """Render an event as the JSON object sent over a WebSocket."""
    return {
        "id": str(event.id),
        "type": event.type.value,
        "ts": event.ts.isoformat(),
        "run_id": str(event.run_id) if event.run_id else None,
        "task_id": str(event.task_id) if event.task_id else None,
        "attempt_id": str(event.attempt_id) if event.attempt_id else None,
        "payload": dict(event.payload),
    }


def sse_frame(event: Event | None, *, comment: str = "keep-alive") -> str:
    """Render one Server-Sent Events frame.

    ``None`` becomes a comment line, which keeps proxies from closing an idle
    connection without the client seeing a spurious message.

    The event's own identifier becomes the SSE ``id``, so a reconnecting client
    has something meaningful to resume from.
    """
    if event is None:
        return f": {comment}\n\n"
    data = json.dumps(ws_payload(event), sort_keys=True, separators=(",", ":"))
    return f"id: {event.id}\nevent: {event.type.value}\ndata: {data}\n\n"

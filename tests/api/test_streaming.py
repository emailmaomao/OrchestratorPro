"""Tests for the event broker and the SSE and WebSocket endpoints."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from orchestrator.api.streaming import EventBroker, sse_frame, ws_payload
from orchestrator.core.events import Event, EventType, RunId

from tests.api.conftest import comments, new_run, new_task, parse_frames, sse


def run(coro: Any) -> Any:
    """Drive one coroutine to completion."""
    return asyncio.run(coro)


def an_event(run_id: RunId | None = None, kind: EventType = EventType.TOOL_CALLED) -> Event:
    """Build an event."""
    return Event.new(kind, run_id=run_id, payload={"tool": "read"})


class TestBroker:
    """Fan-out, filtering, and what happens to a client that stops reading."""

    def test_an_event_reaches_a_subscriber(self) -> None:
        async def scenario() -> Event:
            broker = EventBroker()
            subscription = broker.subscribe()
            event = an_event(RunId.generate())
            assert broker.publish(event) == 1
            return await subscription.queue.get()

        assert isinstance(run(scenario()), Event)

    def test_every_subscriber_gets_a_copy(self) -> None:
        async def scenario() -> tuple[int, int]:
            broker = EventBroker()
            one, two = broker.subscribe(), broker.subscribe()
            broker.publish(an_event(RunId.generate()))
            return one.queue.qsize(), two.queue.qsize()

        assert run(scenario()) == (1, 1)

    def test_a_scoped_subscription_ignores_other_runs(self) -> None:
        async def scenario() -> int:
            broker = EventBroker()
            mine = RunId.generate()
            subscription = broker.subscribe(mine)
            broker.publish(an_event(RunId.generate()))
            broker.publish(an_event(mine))
            return subscription.queue.qsize()

        assert run(scenario()) == 1

    def test_an_unscoped_subscription_sees_everything(self) -> None:
        async def scenario() -> int:
            broker = EventBroker()
            subscription = broker.subscribe()
            broker.publish(an_event(RunId.generate()))
            broker.publish(an_event(RunId.generate()))
            return subscription.queue.qsize()

        assert run(scenario()) == 2

    def test_unsubscribing_stops_delivery(self) -> None:
        async def scenario() -> int:
            broker = EventBroker()
            subscription = broker.subscribe()
            broker.unsubscribe(subscription)
            broker.publish(an_event(RunId.generate()))
            return subscription.queue.qsize()

        assert run(scenario()) == 0

    def test_unsubscribing_twice_is_harmless(self) -> None:
        async def scenario() -> int:
            broker = EventBroker()
            subscription = broker.subscribe()
            broker.unsubscribe(subscription)
            broker.unsubscribe(subscription)
            return broker.subscribers

        assert run(scenario()) == 0

    def test_publishing_with_no_subscribers_is_fine(self) -> None:
        async def scenario() -> int:
            broker = EventBroker()
            return broker.publish(an_event(RunId.generate()))

        assert run(scenario()) == 0

    def test_a_slow_client_is_dropped_not_silently_truncated(self) -> None:
        """A partial stream that does not say so is a client acting on half a picture."""

        async def scenario() -> tuple[int, bool]:
            broker = EventBroker(queue_size=2)
            subscription = broker.subscribe()
            for _ in range(5):
                broker.publish(an_event(RunId.generate()))
            return broker.dropped, subscription.lagged

        dropped, lagged = run(scenario())
        assert dropped == 1
        assert lagged is True

    def test_one_slow_client_does_not_affect_another(self) -> None:
        async def scenario() -> int:
            broker = EventBroker(queue_size=2)
            slow = broker.subscribe()
            fast = broker.subscribe()
            for _ in range(4):
                broker.publish(an_event(RunId.generate()))
                if fast.queue.qsize():
                    fast.queue.get_nowait()
            assert slow.lagged
            return broker.published

        assert run(scenario()) == 4

    def test_publishing_never_raises(self) -> None:
        """It sits on the event-write path; a watcher must not fail a write."""

        async def scenario() -> None:
            broker = EventBroker(queue_size=1)
            broker.subscribe()
            for _ in range(10):
                broker.publish(an_event(RunId.generate()))

        run(scenario())

    def test_closing_releases_everyone(self) -> None:
        async def scenario() -> tuple[int, int]:
            broker = EventBroker()
            broker.subscribe()
            broker.subscribe()
            await broker.close()
            return broker.subscribers, broker.publish(an_event(RunId.generate()))

        assert run(scenario()) == (0, 0)

    def test_counters_are_reported(self) -> None:
        async def scenario() -> int:
            broker = EventBroker()
            broker.subscribe()
            broker.publish(an_event(RunId.generate()))
            broker.publish(an_event(RunId.generate()))
            return broker.published

        assert run(scenario()) == 2


class TestStream:
    """Replay, live tail, and the seam between them."""

    def test_the_replay_comes_first(self) -> None:
        async def scenario() -> list[str]:
            broker = EventBroker()
            subscription = broker.subscribe()
            history = (an_event(), an_event())
            seen: list[str] = []
            async for event in broker.stream(
                subscription, replay=history, heartbeat_s=0.01, stop=lambda: len(seen) >= 2
            ):
                if event is not None:
                    seen.append(str(event.id))
            return seen

        assert len(run(scenario())) == 2

    def test_a_live_event_is_not_delivered_twice(self) -> None:
        """The seam: an event already delivered live must not repeat in the replay."""

        async def scenario() -> list[str]:
            broker = EventBroker()
            subscription = broker.subscribe()
            shared = an_event(RunId.generate())
            broker.publish(shared)

            seen: list[str] = []

            async def collect() -> None:
                async for event in broker.stream(
                    subscription, replay=(shared,), heartbeat_s=0.01
                ):
                    if event is not None:
                        seen.append(str(event.id))

            task = asyncio.ensure_future(collect())
            await asyncio.sleep(0.05)
            task.cancel()
            return seen

        seen = run(scenario())
        assert seen == [seen[0]]

    def test_a_heartbeat_is_yielded_when_idle(self) -> None:
        async def scenario() -> bool:
            broker = EventBroker()
            subscription = broker.subscribe()
            async for event in broker.stream(subscription, heartbeat_s=0.01):
                return event is None
            return False

        assert run(scenario()) is True

    def test_the_stream_ends_when_the_broker_closes(self) -> None:
        async def scenario() -> list[Any]:
            broker = EventBroker()
            subscription = broker.subscribe()
            seen: list[Any] = []

            async def collect() -> None:
                async for event in broker.stream(subscription, heartbeat_s=None):
                    seen.append(event)

            task = asyncio.ensure_future(collect())
            await asyncio.sleep(0.01)
            await broker.close()
            await asyncio.wait_for(task, timeout=1.0)
            return seen

        assert run(scenario()) == []

    def test_a_lagged_stream_ends_and_says_so(self) -> None:
        async def scenario() -> bool:
            broker = EventBroker(queue_size=1)
            subscription = broker.subscribe()
            for _ in range(4):
                broker.publish(an_event(RunId.generate()))

            async for _ in broker.stream(subscription, heartbeat_s=0.01):
                pass
            return subscription.lagged

        assert run(scenario()) is True


class TestFraming:
    """How an event looks on the wire."""

    def test_an_sse_frame_carries_id_type_and_data(self) -> None:
        event = an_event(RunId.generate())
        frame = sse_frame(event)

        assert frame.startswith(f"id: {event.id}\n")
        assert "event: tool.called\n" in frame
        assert frame.endswith("\n\n")

    def test_the_data_is_json(self) -> None:
        event = an_event(RunId.generate())
        data = [
            line[len("data: ") :]
            for line in sse_frame(event).splitlines()
            if line.startswith("data: ")
        ][0]

        assert json.loads(data)["id"] == str(event.id)

    def test_a_heartbeat_is_a_comment(self) -> None:
        assert sse_frame(None).startswith(": ")

    def test_the_websocket_payload_is_complete(self) -> None:
        run_id = RunId.generate()
        payload = ws_payload(an_event(run_id))

        assert payload["run_id"] == str(run_id)
        assert payload["type"] == "tool.called"
        assert payload["payload"] == {"tool": "read"}
        assert payload["task_id"] is None

    def test_payloads_are_json_serializable(self) -> None:
        json.dumps(ws_payload(an_event(RunId.generate())))


class TestSseEndpoint:
    """Streaming over HTTP."""

    def test_a_run_stream_replays_the_log(self, client: TestClient) -> None:
        run_id = new_run(client)
        new_task(client, run_id)

        status, headers, body = sse(
            client.app, f"/runs/{run_id}/events?heartbeat_s=0.05", frames=2
        )

        assert status == 200
        assert headers["content-type"].startswith("text/event-stream")
        assert [frame["event"] for frame in parse_frames(body)] == [
            "run.created",
            "task.created",
        ]

    def test_each_frame_carries_the_event_identifier(self, client: TestClient) -> None:
        """So a reconnecting client has something to resume from."""
        run_id = new_run(client)
        _, _, body = sse(client.app, f"/runs/{run_id}/events?heartbeat_s=0.05", frames=1)

        frame = parse_frames(body)[0]
        assert frame["id"] == frame["data"]["id"]

    def test_replay_can_be_turned_off(self, client: TestClient) -> None:
        run_id = new_run(client)

        _, _, body = sse(
            client.app,
            f"/runs/{run_id}/events?replay=false&heartbeat_s=0.02",
            frames=1,
        )

        assert parse_frames(body) == []
        assert comments(body)

    def test_the_stream_is_not_cached(self, client: TestClient) -> None:
        run_id = new_run(client)
        _, headers, _ = sse(client.app, f"/runs/{run_id}/events?heartbeat_s=0.05")

        assert headers["cache-control"] == "no-store"

    def test_an_unknown_run_is_a_404(self, client: TestClient) -> None:
        response = client.get(f"/runs/{RunId.generate()}/events")
        assert response.status_code == 404

    def test_the_global_stream_serves_a_heartbeat(self, client: TestClient) -> None:
        status, _, body = sse(client.app, "/events?heartbeat_s=0.02", frames=1)

        assert status == 200
        assert comments(body)

    def test_an_absurd_heartbeat_is_refused(self, client: TestClient) -> None:
        assert client.get("/events?heartbeat_s=0").status_code == 422

    def test_disconnecting_releases_the_subscription(
        self, client: TestClient, state_of: Any
    ) -> None:
        """Otherwise an idle server accumulates phantom watchers."""
        run_id = new_run(client)
        sse(client.app, f"/runs/{run_id}/events?heartbeat_s=0.05", frames=1)

        assert state_of(client).broker.subscribers == 0


class TestWebSocketEndpoint:
    """Streaming over a socket."""

    def test_the_log_is_replayed_on_connect(self, client: TestClient) -> None:
        run_id = new_run(client)
        new_task(client, run_id)

        with client.websocket_connect(f"/runs/{run_id}/ws") as socket:
            first = socket.receive_json()
            second = socket.receive_json()

        assert first["type"] == "run.created"
        assert second["type"] == "task.created"

    def test_a_live_event_arrives(self, client: TestClient) -> None:
        run_id = new_run(client)

        with client.websocket_connect(f"/runs/{run_id}/ws") as socket:
            socket.receive_json()  # run.created, replayed
            new_task(client, run_id, title="live")
            live = socket.receive_json()

        assert live["type"] == "task.created"
        assert live["payload"]["title"] == "live"

    def test_events_from_another_run_are_not_delivered(self, client: TestClient) -> None:
        mine = new_run(client)
        theirs = new_run(client)

        with client.websocket_connect(f"/runs/{mine}/ws") as socket:
            socket.receive_json()
            new_task(client, theirs, title="not mine")
            new_task(client, mine, title="mine")
            received = socket.receive_json()

        assert received["payload"]["title"] == "mine"

    def test_an_unknown_run_is_closed_with_an_application_code(
        self, client: TestClient
    ) -> None:
        """A WebSocket has no status line to put a 404 in."""
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect) as caught:
            with client.websocket_connect(f"/runs/{RunId.generate()}/ws") as socket:
                socket.receive_json()

        assert caught.value.code == 4404

    def test_disconnecting_releases_the_subscription(
        self, client: TestClient, state_of: Any
    ) -> None:
        run_id = new_run(client)
        with client.websocket_connect(f"/runs/{run_id}/ws") as socket:
            socket.receive_json()

        assert state_of(client).broker.subscribers == 0


class TestStreamingDuringARun:
    """The case the whole feature exists for."""

    def test_a_run_can_be_watched_end_to_end(self, client: TestClient) -> None:
        from tests.api.conftest import register, start, wait_for_run

        register(client)
        run_id = start(client)
        wait_for_run(client, run_id)

        # The run is over, so its whole log is in the replay; ask for exactly
        # that many frames and the stream ends without waiting on a heartbeat.
        total = client.get(f"/runs/{run_id}").json()["event_count"]
        _, _, body = sse(
            client.app, f"/runs/{run_id}/events?heartbeat_s=0.05", frames=total
        )
        kinds = [frame["event"] for frame in parse_frames(body)]

        assert kinds[0] == "run.created"
        assert "task.created" in kinds
        assert "run.finished" in kinds

    def test_persisted_and_streamed_events_agree(
        self, client: TestClient, state_of: Any
    ) -> None:
        """A subscriber must never see a state the log does not hold."""
        run_id = new_run(client)

        with client.websocket_connect(f"/runs/{run_id}/ws") as socket:
            socket.receive_json()
            new_task(client, run_id)
            live = socket.receive_json()

        persisted = state_of(client).store.events.read_run(RunId(run_id))
        assert live["id"] == str(persisted[-1].id)



"""Shared fixtures for provider tests.

Every test in this package runs **offline**. No provider ever reaches a network,
because the transport is a seam and each test injects a scripted one. That is
the property the whole provider layer was shaped to allow.

``asyncio.run`` drives the async port directly rather than depending on a
pytest-asyncio plugin, which is not installed.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Coroutine, Iterable, Mapping, Sequence
from typing import Any, TypeVar

import pytest

from orchestrator.provider.base import (
    CompletionRequest,
    Message,
    StreamEvent,
    TextBlock,
)

T = TypeVar("T")


def run(awaitable: Coroutine[Any, Any, T] | Awaitable[T]) -> T:
    """Drive one coroutine to completion."""
    return asyncio.run(_await(awaitable))


async def _await(awaitable: Awaitable[T]) -> T:
    return await awaitable


def collect(stream: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    """Drain an async stream into a list."""

    async def drain() -> list[StreamEvent]:
        return [event async for event in stream]

    return asyncio.run(drain())


class FakeTransport:
    """A scripted transport. Records what it was sent; returns what it was told.

    Standing in for the network is only half its job — the other half is being
    an assertion surface. Tests inspect :attr:`sent` to verify that translation
    produced exactly the payload it should have.
    """

    def __init__(
        self,
        response: Mapping[str, Any] | None = None,
        *,
        chunks: Sequence[Mapping[str, Any]] = (),
        error: BaseException | None = None,
        token_count: int | None = None,
    ) -> None:
        self.response = response or {}
        self.chunks = list(chunks)
        self.error = error
        self.token_count = token_count
        self.sent: list[dict[str, Any]] = []

    async def send(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Record the payload and return the scripted reply."""
        self.sent.append(dict(payload))
        if self.error is not None:
            raise self.error
        return self.response

    async def stream(self, payload: Mapping[str, Any]) -> AsyncIterator[Mapping[str, Any]]:
        """Record the payload and yield the scripted chunks."""
        self.sent.append(dict(payload))
        if self.error is not None:
            raise self.error
        for chunk in self.chunks:
            yield chunk

    @property
    def last(self) -> dict[str, Any]:
        """The most recent payload sent.

        Raises:
            AssertionError: If nothing was sent, which is itself a test failure.
        """
        assert self.sent, "no payload was sent to the transport"
        return self.sent[-1]


class CountingTransport(FakeTransport):
    """A transport that also offers an exact token-counting endpoint."""

    async def count_tokens(self, payload: Mapping[str, Any]) -> int:
        """Return the scripted token count."""
        self.sent.append(dict(payload))
        return self.token_count or 0


@pytest.fixture
def transport() -> FakeTransport:
    """A transport returning an empty reply."""
    return FakeTransport()


def simple_request(model: str = "claude-opus-5", **overrides: Any) -> CompletionRequest:
    """Build a minimal valid request, with optional field overrides."""
    fields: dict[str, Any] = {
        "model": model,
        "messages": (Message.user("hello"),),
        "system": (TextBlock("you are helpful"),),
    }
    fields.update(overrides)
    return CompletionRequest(**fields)


@pytest.fixture
def request_factory() -> Any:
    """Return the request builder, for tests that need several variants."""
    return simple_request


def texts(events: Iterable[StreamEvent]) -> str:
    """Concatenate the text of every text event in a stream."""
    from orchestrator.provider.base import StreamEventKind

    return "".join(e.text for e in events if e.kind is StreamEventKind.TEXT)

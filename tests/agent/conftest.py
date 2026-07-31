"""Shared fixtures for agent-runtime tests.

Everything runs against a **fake model provider**. The runtime only ever sees
:class:`~orchestrator.provider.base.ModelPort`, so a scripted provider is a
complete stand-in: no network, no vendor SDK, and full control over stop
reasons, tool calls, usage, and refusals — including the cases a real backend
produces rarely and inconveniently.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Coroutine, Mapping, Sequence
from pathlib import Path
from typing import Any, TypeVar

import pytest

from orchestrator.agent.model import AgentRole, TaskSpec
from orchestrator.agent.tools import ToolContext
from orchestrator.core.events import Budget, TaskId
from orchestrator.provider.base import (
    CapabilitySet,
    CompletionRequest,
    CompletionResponse,
    ContentBlock,
    Domain,
    Feature,
    HealthReport,
    ProviderContext,
    RefusalDetail,
    StopReason,
    StreamEvent,
    StreamEventKind,
    TextBlock,
    ToolCallBlock,
    Usage,
)

T = TypeVar("T")


def run(awaitable: Coroutine[Any, Any, T] | Awaitable[T]) -> T:
    """Drive one coroutine to completion."""

    async def _wrapped() -> T:
        return await awaitable

    return asyncio.run(_wrapped())


def text_turn(text: str, *, usage: Usage | None = None) -> CompletionResponse:
    """A response that ends the turn with plain text."""
    return CompletionResponse(
        content=(TextBlock(text),),
        stop_reason=StopReason.END,
        usage=usage or Usage(input_tokens=10, output_tokens=5),
        model_served="fake-model",
    )


def tool_turn(
    *calls: tuple[str, str, Mapping[str, Any]], usage: Usage | None = None
) -> CompletionResponse:
    """A response asking for one or more tool calls.

    Args:
        *calls: ``(id, name, arguments)`` triples.
        usage: Token usage to report.
    """
    blocks: list[ContentBlock] = [
        ToolCallBlock(id=call_id, name=name, arguments=dict(arguments))
        for call_id, name, arguments in calls
    ]
    return CompletionResponse(
        content=tuple(blocks),
        stop_reason=StopReason.TOOL_CALL,
        usage=usage or Usage(input_tokens=10, output_tokens=5),
        model_served="fake-model",
    )


def finish_turn(summary: str = "done", call_id: str = "finish-1") -> CompletionResponse:
    """A response calling the finish tool."""
    return tool_turn((call_id, "finish", {"summary": summary}))


def refusal_turn(category: str = "cyber") -> CompletionResponse:
    """A response the backend declined on policy grounds."""
    return CompletionResponse(
        content=(),
        stop_reason=StopReason.REFUSED,
        usage=Usage(input_tokens=10, output_tokens=0),
        refusal=RefusalDetail(category=category, explanation="declined"),
        model_served="fake-model",
    )


class FakeProvider:
    """A scripted model provider. Records requests; returns queued responses."""

    id = "model.fake"
    domain = Domain.MODEL
    version = "1.0"

    def __init__(
        self,
        responses: Sequence[CompletionResponse] = (),
        *,
        default: CompletionResponse | None = None,
        error: BaseException | None = None,
        context_tokens: int = 200_000,
    ) -> None:
        self._queue = list(responses)
        self._default = default
        self._error = error
        self._context_tokens = context_tokens
        self.requests: list[CompletionRequest] = []

    def capabilities(self) -> CapabilitySet:
        """Declare a fully-featured backend."""
        return CapabilitySet(
            features=frozenset(
                {Feature.STREAMING, Feature.TOOL_CALLING, Feature.REASONING}
            ),
            limits={"max_context_tokens": self._context_tokens},
        )

    async def startup(self, ctx: ProviderContext | None = None) -> None:
        """No-op."""

    async def health(self) -> HealthReport:
        """Always healthy."""
        return HealthReport(provider_id=self.id, healthy=True)

    async def shutdown(self) -> None:
        """No-op."""

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        """Record the request and return the next scripted response."""
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        if self._queue:
            return self._queue.pop(0)
        if self._default is not None:
            return self._default
        return text_turn("nothing further to do")

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        """Not used by the runtime; present to satisfy the port."""
        yield StreamEvent(kind=StreamEventKind.DONE)

    async def count_tokens(self, request: CompletionRequest) -> int:
        """Return a trivial estimate."""
        return 1

    @property
    def last_request(self) -> CompletionRequest:
        """The most recent request.

        Raises:
            AssertionError: If nothing was requested, which is a test failure.
        """
        assert self.requests, "the provider was never called"
        return self.requests[-1]


@pytest.fixture
def spec() -> TaskSpec:
    """A minimal worker task."""
    return TaskSpec(
        task_id=TaskId.generate(),
        title="Add a greeting",
        prompt="Create greeting.txt containing a greeting.",
        role=AgentRole.WORKER,
    )


@pytest.fixture
def workspace(tmp_dir: Path) -> Path:
    """An empty workspace directory."""
    target = tmp_dir / "workspace"
    target.mkdir(parents=True, exist_ok=True)
    return target


@pytest.fixture
def tool_ctx(workspace: Path) -> ToolContext:
    """A tool context confined to the workspace."""
    return ToolContext(workspace_root=workspace, allowlist=frozenset({"python"}))


@pytest.fixture
def budget() -> Budget:
    """A generous budget."""
    return Budget(seconds=600.0, tokens=1_000_000, tool_calls=100)


class FakeClock:
    """A monotonic clock that advances only when told."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

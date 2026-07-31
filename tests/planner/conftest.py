"""Shared fixtures for workflow-construction tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Coroutine, Sequence
from typing import Any, TypeVar

import pytest

from orchestrator.provider.base import (
    CompletionResponse,
    RefusalDetail,
    StopReason,
    TextBlock,
    Usage,
)

T = TypeVar("T")


def run(awaitable: Coroutine[Any, Any, T] | Awaitable[T]) -> T:
    """Drive one coroutine to completion."""

    async def _wrapped() -> T:
        return await awaitable

    return asyncio.run(_wrapped())


def json_response(payload: Any, *, usage: Usage | None = None) -> CompletionResponse:
    """A response carrying a JSON object, as structured output produces."""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return CompletionResponse(
        content=(TextBlock(text),),
        stop_reason=StopReason.END,
        usage=usage or Usage(input_tokens=100, output_tokens=200),
        model_served="fake-model",
    )


def refusal(category: str = "policy") -> CompletionResponse:
    """A response the backend declined."""
    return CompletionResponse(
        content=(),
        stop_reason=StopReason.REFUSED,
        usage=Usage(input_tokens=100, output_tokens=0),
        refusal=RefusalDetail(category=category, explanation="declined"),
        model_served="fake-model",
    )


VALID_PLAN: dict[str, Any] = {
    "name": "migrate-http-client",
    "goal": "migrate the HTTP client to httpx",
    "steps": [
        {
            "name": "client-uses-httpx",
            "prompt": "Replace the requests-based client in src/client.py with httpx.",
            "gates": ["tests"],
        },
        {
            "name": "call-sites-updated",
            "prompt": "Update every call site to the new client signature.",
            "depends_on": ["client-uses-httpx"],
            "gates": ["tests"],
        },
    ],
}

CYCLIC_PLAN: dict[str, Any] = {
    "name": "cyclic",
    "goal": "g",
    "steps": [
        {"name": "a", "prompt": "p", "depends_on": ["b"]},
        {"name": "b", "prompt": "p", "depends_on": ["a"]},
    ],
}

UNKNOWN_DEPENDENCY_PLAN: dict[str, Any] = {
    "name": "dangling",
    "goal": "g",
    "steps": [{"name": "a", "prompt": "p", "depends_on": ["ghost"]}],
}


class ScriptedProvider:
    """A model backend returning prepared responses."""

    id = "model.scripted"

    def __init__(
        self,
        responses: Sequence[CompletionResponse],
        *,
        default: CompletionResponse | None = None,
    ) -> None:
        self._queue = list(responses)
        self._default = default
        self.requests: list[Any] = []

    async def complete(self, request: Any) -> CompletionResponse:
        """Record the request and return the next scripted response."""
        self.requests.append(request)
        if self._queue:
            return self._queue.pop(0)
        if self._default is not None:
            return self._default
        return json_response(VALID_PLAN)

    async def stream(self, request: Any) -> Any:  # pragma: no cover - unused
        raise NotImplementedError

    async def count_tokens(self, request: Any) -> int:  # pragma: no cover - unused
        return 1

    @property
    def last_request(self) -> Any:
        """The most recent request."""
        assert self.requests, "the provider was never called"
        return self.requests[-1]


LINEAR_YAML = """\
name: ship
goal: ship the feature
steps:
  - name: design
    prompt: design it
  - name: build
    prompt: build it
    depends_on: [design]
  - name: verify
    prompt: verify it
    depends_on: [build]
"""


@pytest.fixture
def provider() -> ScriptedProvider:
    """A provider that returns one valid plan."""
    return ScriptedProvider([json_response(VALID_PLAN)])

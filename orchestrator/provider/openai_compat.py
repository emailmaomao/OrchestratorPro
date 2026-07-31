"""OpenAI-compatible provider — Ollama, Open WebUI, vLLM, LM Studio.

Many self-hosted runtimes expose the same ``/v1/chat/completions`` shape, so one
provider serves all of them. The base URL and model name are configuration; the
translation is identical.

**What this provider does not claim.** A self-hosted runtime has no vendor
price list and no policy-refusal concept, so :attr:`Feature.COST_REPORTING` and
:attr:`Feature.REFUSAL_SEMANTICS` are not declared, and ``cost_usd`` stays
``None``. It also has no effort control, so :attr:`Feature.EFFORT_LEVELS` is
absent and a neutral ``effort`` is **dropped rather than approximated** —
§4 requires an undeclared capability to be absent, not faked.

Token counting is an estimate, so :attr:`Feature.TOKEN_COUNTING` — which
promises exactness — is deliberately withheld even though
:meth:`OpenAICompatProvider.count_tokens` returns a number.

Context and output limits depend entirely on how the operator deployed the
model, so they are configuration with conservative defaults rather than facts
this module invents.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any

from orchestrator.provider.base import (
    CapabilitySet,
    CompletionRequest,
    CompletionResponse,
    ContentBlock,
    Domain,
    ErrorCode,
    Feature,
    HealthReport,
    Message,
    ProviderContext,
    ProviderError,
    StopReason,
    StreamEvent,
    StreamEventKind,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    Usage,
    estimate_tokens,
    request_text,
)
from orchestrator.provider.claude import Transport

__all__ = ["OpenAICompatConfig", "OpenAICompatProvider"]

_DEFAULT_CONTEXT = 8_192
_DEFAULT_MAX_OUTPUT = 4_096

_STOP_REASONS: Mapping[str, StopReason] = {
    "stop": StopReason.END,
    "length": StopReason.MAX_TOKENS,
    "tool_calls": StopReason.TOOL_CALL,
    "function_call": StopReason.TOOL_CALL,
    "content_filter": StopReason.REFUSED,
}


@dataclass(frozen=True, slots=True)
class OpenAICompatConfig:
    """Settings for an OpenAI-compatible endpoint."""

    model: str
    base_url: str = "http://localhost:11434/v1"
    context_tokens: int = _DEFAULT_CONTEXT
    max_output_tokens: int = _DEFAULT_MAX_OUTPUT
    supports_tools: bool = True
    provider_id: str = "model.openai_compat"

    def __post_init__(self) -> None:
        if not self.model:
            raise ProviderError(ErrorCode.INVALID_REQUEST, "model must not be empty")
        if not self.base_url:
            raise ProviderError(ErrorCode.INVALID_REQUEST, "base_url must not be empty")
        if self.context_tokens <= 0 or self.max_output_tokens <= 0:
            raise ProviderError(
                ErrorCode.INVALID_REQUEST, "token limits must be positive"
            )


class OpenAICompatProvider:
    """A chat-completions endpoint, exposed through the neutral model port."""

    domain = Domain.MODEL
    version = "1.0"

    __slots__ = ("_config", "_started", "_transport")

    def __init__(
        self, config: OpenAICompatConfig, *, transport: Transport | None = None
    ) -> None:
        """Construct the provider.

        Args:
            config: Endpoint and model settings.
            transport: Moves payloads. Required before use; tests inject a
                scripted one so no network is touched.
        """
        self._config = config
        self._transport = transport
        self._started = False

    @property
    def id(self) -> str:
        """The provider's stable identifier."""
        return self._config.provider_id

    @property
    def config(self) -> OpenAICompatConfig:
        """The provider's settings."""
        return self._config

    def capabilities(self) -> CapabilitySet:
        """Declare what this endpoint actually supports.

        Notably absent: effort levels, prompt caching, refusal semantics, cost
        reporting, and exact token counting. None of them exist here, so none
        are claimed.
        """
        features = {Feature.STREAMING}
        if self._config.supports_tools:
            features.add(Feature.TOOL_CALLING)
        return CapabilitySet(
            features=frozenset(features),
            limits={
                "max_context_tokens": self._config.context_tokens,
                "max_output_tokens": self._config.max_output_tokens,
            },
        )

    async def startup(self, ctx: ProviderContext | None = None) -> None:
        """Verify a transport is present.

        Raises:
            ProviderError: With :attr:`ErrorCode.UNAVAILABLE` if none was given.
        """
        if self._transport is None:
            raise ProviderError(
                ErrorCode.UNAVAILABLE,
                f"{self.id} was constructed without a transport and cannot reach "
                f"{self._config.base_url}",
                provider_id=self.id,
                domain=self.domain,
            )
        self._started = True

    async def health(self) -> HealthReport:
        """Report whether the provider is ready to serve."""
        ready = self._started and self._transport is not None
        return HealthReport(
            provider_id=self.id,
            healthy=ready,
            detail=f"endpoint {self._config.base_url}" if ready else "not started",
        )

    async def shutdown(self) -> None:
        """Release the transport. Idempotent."""
        self._started = False

    # ------------------------------------------------------------ translation

    def build_payload(self, request: CompletionRequest) -> dict[str, Any]:
        """Translate a neutral request into a chat-completions payload.

        Neutral ``effort`` and ``reasoning`` are **omitted**, not approximated:
        this endpoint has no equivalent, and inventing one would silently change
        behaviour the caller never asked for.
        """
        messages: list[dict[str, Any]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system_text})
        for message in request.messages:
            messages.extend(self._render(message))

        payload: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "max_tokens": min(request.max_output_tokens, self._config.max_output_tokens),
            "stream": False,
        }
        if request.stop:
            payload["stop"] = list(request.stop)
        if request.tools:
            if not self._config.supports_tools:
                raise ProviderError(
                    ErrorCode.NOT_SUPPORTED,
                    f"{self.id} is configured without tool support, but the request "
                    "declares tools",
                    provider_id=self.id,
                    domain=self.domain,
                )
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": dict(tool.schema),
                    },
                }
                for tool in request.tools
            ]
        if request.output_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"schema": dict(request.output_schema)},
            }
        return payload

    def _render(self, message: Message) -> list[dict[str, Any]]:
        """Render one neutral message as one or more wire messages."""
        tool_results = [b for b in message.content if isinstance(b, ToolResultBlock)]
        if tool_results:
            # Tool results are their own role in this API shape.
            return [
                {"role": "tool", "tool_call_id": block.call_id, "content": block.content}
                for block in tool_results
            ]

        rendered: dict[str, Any] = {
            "role": message.role.value,
            "content": "".join(
                b.text for b in message.content if isinstance(b, TextBlock)
            ),
        }
        calls = [b for b in message.content if isinstance(b, ToolCallBlock)]
        if calls:
            rendered["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(dict(call.arguments), sort_keys=True),
                    },
                }
                for call in calls
            ]
        return [rendered]

    def parse_response(self, raw: Mapping[str, Any]) -> CompletionResponse:
        """Translate a chat-completions reply into a neutral response."""
        choices = raw.get("choices") or []
        if not choices:
            raise ProviderError(
                ErrorCode.INTERNAL,
                f"{self.id} returned no choices",
                provider_id=self.id,
                domain=self.domain,
            )
        choice = choices[0]
        message = choice.get("message") or {}

        blocks: list[ContentBlock] = []
        text = message.get("content")
        if text:
            blocks.append(TextBlock(str(text)))
        for call in message.get("tool_calls") or ():
            function = call.get("function") or {}
            blocks.append(
                ToolCallBlock(
                    id=str(call.get("id", "")),
                    name=str(function.get("name", "")),
                    arguments=_decode_arguments(function.get("arguments")),
                )
            )

        usage = raw.get("usage") or {}
        return CompletionResponse(
            content=tuple(blocks),
            stop_reason=_STOP_REASONS.get(
                str(choice.get("finish_reason", "")), StopReason.END
            ),
            usage=Usage(
                input_tokens=int(usage.get("prompt_tokens", 0) or 0),
                output_tokens=int(usage.get("completion_tokens", 0) or 0),
                # Self-hosted: there is no vendor price list, and reporting 0.0
                # would understate cost silently.
                cost_usd=None,
            ),
            model_served=str(raw.get("model", self._config.model)),
        )

    # ---------------------------------------------------------------- port API

    def _require_transport(self) -> Transport:
        """Return the transport, or raise if the provider was never started."""
        if self._transport is None:
            raise ProviderError(
                ErrorCode.UNAVAILABLE,
                f"{self.id} has no transport; call startup() first",
                provider_id=self.id,
                domain=self.domain,
            )
        return self._transport

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        """Run one turn and return the whole response."""
        transport = self._require_transport()
        payload = self.build_payload(request)
        try:
            raw = await transport.send(payload)
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - backend errors must not escape
            raise ProviderError(
                ErrorCode.INTERNAL,
                f"unexpected failure from {self.id}: {exc}",
                provider_id=self.id,
                domain=self.domain,
                cause=str(exc),
            ) from exc
        return self.parse_response(raw)

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        """Run one turn, yielding incremental updates."""
        transport = self._require_transport()
        payload = {**self.build_payload(request), "stream": True}
        async for chunk in transport.stream(payload):
            for choice in chunk.get("choices") or ():
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    yield StreamEvent(
                        StreamEventKind.TEXT, text=str(delta["content"])
                    )
                if choice.get("finish_reason"):
                    yield StreamEvent(StreamEventKind.DONE)

    async def count_tokens(self, request: CompletionRequest) -> int:
        """Estimate the request's input token count.

        An estimate, which is why :attr:`Feature.TOKEN_COUNTING` is not declared.
        """
        return estimate_tokens(request_text(request))


def _decode_arguments(raw: Any) -> Mapping[str, Any]:
    """Decode a tool call's arguments, which arrive as a JSON string.

    A malformed payload yields an empty mapping rather than raising: one bad
    tool call should surface as a failed call the model can retry, not as a
    crashed turn.
    """
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str) and raw:
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return dict(decoded) if isinstance(decoded, dict) else {}
    return {}

"""Claude provider — the Anthropic Messages API behind the neutral model port.

This is the only module in the codebase permitted to know how Anthropic's API
is shaped. Everything vendor-specific stops here: the translation table below is
the single place where neutral intent becomes a wire payload.

**The transport is a seam.** The provider builds and interprets payloads; a
:class:`Transport` moves them. Tests inject a scripted transport, so the whole
provider is exercised with no network and no SDK installed. The real transport
is constructed lazily at :meth:`ClaudeProvider.startup` and, when the SDK is
absent, fails there with :attr:`ErrorCode.UNAVAILABLE` — at startup, where it is
cheap, rather than forty minutes into a run.

**Translation rules.** Each of these produces an HTTP 400 if broken, so they are
enforced in code and covered by tests rather than left to reviewer memory:

===========================  ==================================================
Neutral                      Wire
===========================  ==================================================
``reasoning=ADAPTIVE``       ``thinking={"type": "adaptive"}``
``reasoning=DISABLED``       ``thinking={"type": "disabled"}`` — **only** at
                             effort ``high`` or below; rejected locally at
                             ``xhigh``/``max`` before the request is sent
``effort``                   ``output_config.effort``
—                            ``budget_tokens`` is **never** sent (removed)
—                            ``temperature``/``top_p``/``top_k`` never sent
``max_output_tokens``        ``max_tokens`` — bounds reasoning *plus* text
``output_schema``            ``output_config.format`` (no assistant prefill)
``StopReason.REFUSED``       ``stop_reason == "refusal"`` on a **200** response
``cache_hint``               ``cache_control`` breakpoints
===========================  ==================================================

Server-side fallback is opted into by default, so a policy decline is re-served
rather than failing the attempt.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from orchestrator.core.config import Effort, ThinkingMode
from orchestrator.provider.base import (
    CapabilitySet,
    CompletionRequest,
    CompletionResponse,
    ContentBlock,
    Domain,
    ErrorCode,
    Feature,
    HealthReport,
    ProviderContext,
    ProviderError,
    ReasoningBlock,
    RefusalDetail,
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

__all__ = ["CLAUDE_PRICES", "ClaudeConfig", "ClaudeProvider", "Transport"]

#: Above this many output tokens the SDK's HTTP timeout becomes a real risk, so
#: the request must stream. Declared as a capability limit, not hidden in code.
_STREAM_ABOVE_TOKENS = 16_000

#: Effort levels at which reasoning may not be disabled.
_DEEP_EFFORTS = frozenset({Effort.XHIGH, Effort.MAX})

#: Beta flag enabling server-side fallback on a policy decline.
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

#: USD per million tokens, ``(input, output)``. Only models whose pricing is
#: known are listed; an unlisted model reports ``cost_usd=None`` rather than
#: guessing, per §5.1 M-7.
CLAUDE_PRICES: Mapping[str, tuple[float, float]] = {
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

#: Context windows in tokens, by model.
_CONTEXT_WINDOWS: Mapping[str, int] = {
    "claude-opus-5": 1_000_000,
    "claude-sonnet-5": 1_000_000,
    "claude-haiku-4-5": 200_000,
}

_DEFAULT_CONTEXT = 200_000
_DEFAULT_MAX_OUTPUT = 128_000

#: Minimum cacheable prefix, by model.
_CACHE_MINIMUMS: Mapping[str, int] = {
    "claude-opus-5": 512,
    "claude-sonnet-5": 1024,
    "claude-haiku-4-5": 4096,
}


class Transport(Protocol):
    """Moves a payload to a backend and returns its reply.

    The seam that keeps this provider testable offline. Implementations are
    responsible for transport concerns only — never for translation.
    """

    async def send(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Send one request and return the decoded reply."""
        ...

    def stream(self, payload: Mapping[str, Any]) -> AsyncIterator[Mapping[str, Any]]:
        """Send one request and yield decoded incremental chunks."""
        ...


@dataclass(frozen=True, slots=True)
class ClaudeConfig:
    """Settings for the Claude provider."""

    model: str = "claude-opus-5"
    enable_server_fallback: bool = True
    beta_flags: tuple[str, ...] = ()
    prices: Mapping[str, tuple[float, float]] = field(default_factory=lambda: CLAUDE_PRICES)

    def __post_init__(self) -> None:
        if not self.model:
            raise ProviderError(ErrorCode.INVALID_REQUEST, "claude.model must not be empty")


def _default_transport_factory() -> Transport:
    """Build the real SDK-backed transport.

    Raises:
        ProviderError: With :attr:`ErrorCode.UNAVAILABLE` when the vendor SDK is
            not installed. This is the only place the SDK is referenced.
    """
    try:  # pragma: no cover - exercised only where the SDK is installed
        import anthropic  # noqa: F401
    except ImportError as exc:
        raise ProviderError(
            ErrorCode.UNAVAILABLE,
            "the 'anthropic' package is not installed, so the Claude provider "
            "cannot reach the API; install it, or inject a transport",
            provider_id="model.claude",
            domain=Domain.MODEL,
            cause=str(exc),
        ) from exc
    raise ProviderError(  # pragma: no cover
        ErrorCode.UNAVAILABLE,
        "the SDK-backed transport is not wired up yet; inject a transport",
        provider_id="model.claude",
        domain=Domain.MODEL,
    )


class ClaudeProvider:
    """The Anthropic Messages API, exposed through the neutral model port."""

    id = "model.claude"
    domain = Domain.MODEL
    version = "1.0"

    __slots__ = ("_config", "_started", "_transport", "_transport_factory")

    def __init__(
        self,
        config: ClaudeConfig | None = None,
        *,
        transport: Transport | None = None,
        transport_factory: Callable[[], Transport] = _default_transport_factory,
    ) -> None:
        """Construct the provider.

        Args:
            config: Model and behaviour settings.
            transport: An explicit transport. Supplying one skips SDK discovery
                entirely, which is how tests run offline.
            transport_factory: Builds a transport when none is supplied.
        """
        self._config = config or ClaudeConfig()
        self._transport = transport
        self._transport_factory = transport_factory
        self._started = False

    @property
    def config(self) -> ClaudeConfig:
        """The provider's settings."""
        return self._config

    def capabilities(self) -> CapabilitySet:
        """Declare what this provider actually supports."""
        model = self._config.model
        features = {
            Feature.STREAMING,
            Feature.TOOL_CALLING,
            Feature.PARALLEL_TOOL_CALLS,
            Feature.STRUCTURED_OUTPUT,
            Feature.REASONING,
            Feature.EFFORT_LEVELS,
            Feature.PROMPT_CACHING,
            Feature.VISION,
            Feature.REFUSAL_SEMANTICS,
            Feature.TOKEN_COUNTING,
        }
        if self._config.enable_server_fallback:
            features.add(Feature.SERVER_FALLBACK)
        if model in self._config.prices:
            features.add(Feature.COST_REPORTING)
        return CapabilitySet(
            features=frozenset(features),
            limits={
                "max_context_tokens": _CONTEXT_WINDOWS.get(model, _DEFAULT_CONTEXT),
                "max_output_tokens": _DEFAULT_MAX_OUTPUT,
                "stream_above_tokens": _STREAM_ABOVE_TOKENS,
                "cache_min_tokens": _CACHE_MINIMUMS.get(model, 1024),
            },
            variants={"effort": frozenset(e.value for e in Effort)},
        )

    async def startup(self, ctx: ProviderContext | None = None) -> None:
        """Acquire a transport, failing fast if none can be built."""
        if self._transport is None:
            self._transport = self._transport_factory()
        self._started = True

    async def health(self) -> HealthReport:
        """Report whether a transport is available."""
        ready = self._started and self._transport is not None
        return HealthReport(
            provider_id=self.id,
            healthy=ready,
            detail="ready" if ready else "not started",
        )

    async def shutdown(self) -> None:
        """Release the transport. Idempotent."""
        self._transport = None
        self._started = False

    # ------------------------------------------------------------ translation

    def build_payload(self, request: CompletionRequest) -> dict[str, Any]:
        """Translate a neutral request into an Anthropic Messages payload.

        Public because it is the most valuable thing to test: every rule in the
        module docstring is observable here, without a network call.

        Raises:
            ProviderError: With :attr:`ErrorCode.INVALID_REQUEST` when the
                request would be rejected by the API — caught locally so the
                failure is ours and immediate, not a remote 400.
        """
        if request.reasoning is ThinkingMode.DISABLED and request.effort in _DEEP_EFFORTS:
            raise ProviderError(
                ErrorCode.INVALID_REQUEST,
                f"reasoning cannot be disabled at effort {request.effort.value!r}; "
                "disable it only at 'high' or below",
                provider_id=self.id,
                domain=self.domain,
            )

        payload: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_output_tokens,
            "messages": [self._message(m) for m in request.messages],
            "thinking": {"type": request.reasoning.value},
            "output_config": {"effort": request.effort.value},
        }

        if request.system:
            system_blocks: list[dict[str, Any]] = [
                {"type": "text", "text": block.text} for block in request.system
            ]
            if request.cache_hint is not None and request.cache_hint.after_system:
                system_blocks[-1]["cache_control"] = {"type": "ephemeral"}
            payload["system"] = system_blocks

        if request.tools:
            payload["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": dict(tool.schema),
                }
                for tool in request.tools
            ]

        if request.output_schema is not None:
            payload["output_config"]["format"] = {
                "type": "json_schema",
                "schema": dict(request.output_schema),
            }

        if request.stop:
            payload["stop_sequences"] = list(request.stop)

        if self._config.enable_server_fallback:
            payload["fallbacks"] = "default"
            payload["betas"] = [_FALLBACK_BETA, *self._config.beta_flags]
        elif self._config.beta_flags:
            payload["betas"] = list(self._config.beta_flags)

        # Streaming is mandatory above the declared limit: a large non-streaming
        # request reliably trips the SDK's HTTP timeout.
        if request.max_output_tokens > _STREAM_ABOVE_TOKENS:
            payload["stream"] = True

        return payload

    def _message(self, message: Any) -> dict[str, Any]:
        """Render one neutral message as a wire message."""
        content: list[dict[str, Any]] = []
        for block in message.content:
            if isinstance(block, TextBlock):
                content.append({"type": "text", "text": block.text})
            elif isinstance(block, ToolCallBlock):
                content.append(
                    {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": dict(block.arguments),
                    }
                )
            elif isinstance(block, ToolResultBlock):
                content.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.call_id,
                        "content": block.content,
                        "is_error": block.is_error,
                    }
                )
            elif isinstance(block, ReasoningBlock) and block.text:
                content.append({"type": "thinking", "thinking": block.text})
        return {"role": message.role.value, "content": content}

    def parse_response(self, raw: Mapping[str, Any]) -> CompletionResponse:
        """Translate an Anthropic reply into a neutral response.

        A refusal arrives here as an ordinary successful reply whose
        ``stop_reason`` is ``"refusal"``. It becomes
        :attr:`StopReason.REFUSED`, never an exception — see §5.1 M-1.
        """
        blocks: list[ContentBlock] = []
        for item in raw.get("content", ()):
            kind = item.get("type")
            if kind == "text":
                blocks.append(TextBlock(item.get("text", "")))
            elif kind == "thinking":
                blocks.append(ReasoningBlock(item.get("thinking", "")))
            elif kind == "tool_use":
                blocks.append(
                    ToolCallBlock(
                        id=item.get("id", ""),
                        name=item.get("name", ""),
                        arguments=dict(item.get("input", {})),
                    )
                )

        stop_reason = _STOP_REASONS.get(str(raw.get("stop_reason", "")), StopReason.END)
        refusal: RefusalDetail | None = None
        if stop_reason is StopReason.REFUSED:
            details = raw.get("stop_details") or {}
            refusal = RefusalDetail(
                category=details.get("category"),
                explanation=str(details.get("explanation", "")),
            )

        return CompletionResponse(
            content=tuple(blocks),
            stop_reason=stop_reason,
            usage=self._usage(raw),
            model_served=str(raw.get("model", self._config.model)),
            refusal=refusal,
        )

    def _usage(self, raw: Mapping[str, Any]) -> Usage:
        """Extract token usage and price it where the model's rates are known."""
        usage = raw.get("usage") or {}
        input_tokens = int(usage.get("input_tokens", 0) or 0)
        output_tokens = int(usage.get("output_tokens", 0) or 0)
        cached = int(usage.get("cache_read_input_tokens", 0) or 0)

        model = str(raw.get("model", self._config.model))
        rates = self._config.prices.get(model)
        cost = None
        if rates is not None:
            cost = (input_tokens * rates[0] + output_tokens * rates[1]) / 1_000_000

        return Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached,
            cost_usd=cost,
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
        except Exception as exc:  # noqa: BLE001 - vendor errors must not escape
            raise ProviderError(
                ErrorCode.INTERNAL,
                f"unexpected failure from the Claude transport: {exc}",
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
            kind = chunk.get("type")
            if kind == "text":
                yield StreamEvent(StreamEventKind.TEXT, text=str(chunk.get("text", "")))
            elif kind == "thinking":
                yield StreamEvent(
                    StreamEventKind.REASONING, text=str(chunk.get("text", ""))
                )
            elif kind == "message":
                yield StreamEvent(
                    StreamEventKind.DONE,
                    response=self.parse_response(chunk.get("message", {})),
                )

    async def count_tokens(self, request: CompletionRequest) -> int:
        """Return the request's input token count.

        Uses the backend's counting endpoint when the transport offers one, so
        the declared :attr:`Feature.TOKEN_COUNTING` is honest.
        """
        transport = self._require_transport()
        counter = getattr(transport, "count_tokens", None)
        if counter is not None:
            return int(await counter(self.build_payload(request)))
        return estimate_tokens(request_text(request))


_STOP_REASONS: Mapping[str, StopReason] = {
    "end_turn": StopReason.END,
    "max_tokens": StopReason.MAX_TOKENS,
    "tool_use": StopReason.TOOL_CALL,
    "stop_sequence": StopReason.STOP_SEQUENCE,
    "refusal": StopReason.REFUSED,
    "pause_turn": StopReason.PAUSED,
}

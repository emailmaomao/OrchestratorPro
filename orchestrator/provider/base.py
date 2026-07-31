"""The universal provider substrate and the model port.

Implements `docs/030_PROVIDER_INTERFACE.md`: a thin substrate every provider in
every domain satisfies (§2–4), plus the typed contract for the ``model`` domain
(§5.1). The rule the whole layer exists to enforce is §0 — **no module outside a
provider implementation may import a vendor SDK or contain a vendor-specific
conditional**. Nothing above this package knows that Claude, Hermes, or Ollama
exist.

Everything crossing a port boundary is a *neutral type* defined here. A vendor
object never escapes a provider, not in a return value, not in an exception, not
inside a ``dict``. When that rule is broken the port becomes a lie: swapping the
provider then breaks its callers.

Two deviations from the document, both recorded rather than hidden:

* **Effort and reasoning mode reuse the enums already in**
  :mod:`orchestrator.core.config`. The document sketches its own spellings, but
  a second vocabulary for the same concept would need a translation layer
  between our own modules and would drift. One vocabulary, defined once.
* **``config_schema()`` is not part of the protocol.** §2 specifies a Pydantic
  schema; Pydantic is not installed, so each provider validates its own frozen
  config dataclass at construction and fails fast there instead. See the open
  dependency decision in ``TASKS.md``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from orchestrator.core.config import Effort, ThinkingMode
from orchestrator.core.events import AttemptId, OrchestratorError, RunId, TaskId

__all__ = [
    "CacheHint",
    "CancelToken",
    "CapabilitySet",
    "CompletionRequest",
    "CompletionResponse",
    "ContentBlock",
    "Domain",
    "ErrorCode",
    "Feature",
    "HealthReport",
    "Message",
    "ModelPort",
    "Provider",
    "ProviderContext",
    "ProviderError",
    "ReasoningBlock",
    "RefusalDetail",
    "Role",
    "StopReason",
    "StreamEvent",
    "StreamEventKind",
    "TextBlock",
    "ToolCallBlock",
    "ToolResultBlock",
    "ToolSpec",
    "Usage",
]


# --------------------------------------------------------------------------- #
# Domains and errors
# --------------------------------------------------------------------------- #


class Domain(StrEnum):
    """A category of external capability. Exactly one port per domain."""

    MODEL = "model"
    AGENT = "agent"
    BUILD = "build"
    SHELL = "shell"
    FS = "fs"
    VCS = "vcs"
    FORGE = "forge"
    BROWSER = "browser"
    SECRETS = "secrets"
    TELEMETRY = "telemetry"


class ErrorCode(StrEnum):
    """The closed set of failures every provider maps its backend onto.

    Callers branch on these. They never see, and must never need, a vendor's
    own exception taxonomy.
    """

    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    OVERLOADED = "overloaded"
    AUTH_FAILED = "auth_failed"
    INVALID_REQUEST = "invalid_request"
    NOT_SUPPORTED = "not_supported"
    QUOTA_EXCEEDED = "quota_exceeded"
    REFUSED = "refused"
    CANCELLED = "cancelled"
    CONFLICT = "conflict"
    INTERNAL = "internal"


#: Which failures are worth trying again. Retrying an auth failure or a
#: malformed request burns budget to reproduce the identical error.
_RETRYABLE: Mapping[ErrorCode, bool] = {
    ErrorCode.UNAVAILABLE: True,
    ErrorCode.TIMEOUT: True,
    ErrorCode.RATE_LIMITED: True,
    ErrorCode.OVERLOADED: True,
    ErrorCode.AUTH_FAILED: False,
    ErrorCode.INVALID_REQUEST: False,
    ErrorCode.NOT_SUPPORTED: False,
    ErrorCode.QUOTA_EXCEEDED: False,
    ErrorCode.REFUSED: False,
    ErrorCode.CANCELLED: False,
    ErrorCode.CONFLICT: False,
    ErrorCode.INTERNAL: False,
}


class ProviderError(OrchestratorError):
    """A provider operation failed.

    Carries the neutral :class:`ErrorCode`, the provider that raised it, and the
    vendor's own message as a **string** — never as an object, which would leak
    a vendor type across the boundary.
    """

    code = "provider"
    retryable = False

    def __init__(
        self,
        error_code: ErrorCode,
        message: str,
        *,
        provider_id: str = "",
        domain: Domain | None = None,
        retry_after: float | None = None,
        cause: str | None = None,
    ) -> None:
        super().__init__(
            message,
            detail={
                "error_code": error_code.value,
                "provider_id": provider_id,
                "domain": domain.value if domain is not None else None,
                "retry_after": retry_after,
                "cause": cause,
            },
        )
        self.error_code = error_code
        self.provider_id = provider_id
        self.domain = domain
        self.retry_after = retry_after
        self.cause_text = cause

    @property
    def is_retryable(self) -> bool:
        """Whether retrying the identical operation could plausibly succeed."""
        return _RETRYABLE[self.error_code]


# --------------------------------------------------------------------------- #
# Capabilities
# --------------------------------------------------------------------------- #


class Feature(StrEnum):
    """Optional capabilities a provider may declare.

    A port says what *may* be asked; a capability set says what this
    implementation actually does. Callers check before relying on anything here.
    """

    STREAMING = "streaming"
    TOOL_CALLING = "tool_calling"
    PARALLEL_TOOL_CALLS = "parallel_tool_calls"
    STRUCTURED_OUTPUT = "structured_output"
    REASONING = "reasoning"
    REASONING_VISIBLE = "reasoning_visible"
    EFFORT_LEVELS = "effort_levels"
    PROMPT_CACHING = "prompt_caching"
    VISION = "vision"
    REFUSAL_SEMANTICS = "refusal_semantics"
    SERVER_FALLBACK = "server_fallback"
    COST_REPORTING = "cost_reporting"
    TOKEN_COUNTING = "token_counting"  # noqa: S105 - a capability flag, not a token
    #: The provider reports **measured** usage on its responses — the counts on
    #: :class:`Usage` are what the backend actually consumed, not
    #: :func:`estimate_tokens` approximations.
    #:
    #: Distinct from :attr:`TOKEN_COUNTING`, and the distinction is load-bearing.
    #: That one is about ``count_tokens`` — a *pre-flight* exact count, answered
    #: without running anything. A backend can easily have one and not the
    #: other: the ``claude`` CLI reports exact usage in its JSON envelope
    #: *after* a call, and offers no way at all to count a prompt beforehand.
    #: Before this existed the two were conflated, so such a backend had to
    #: either declare a pre-flight guarantee it could not keep or label
    #: measurements as estimates.
    USAGE_REPORTING = "usage_reporting"


@dataclass(frozen=True, slots=True)
class CapabilitySet:
    """What one provider implementation can actually do.

    Honesty is an obligation, not a courtesy (§2.1, P-3): a declared capability
    must work, and an undeclared one must raise :attr:`ErrorCode.NOT_SUPPORTED`
    rather than being silently ignored. Silent degradation is the most expensive
    failure mode in a swappable-backend system — the caller believes something
    false and pays for it later.
    """

    features: frozenset[Feature] = frozenset()
    limits: Mapping[str, int] = field(default_factory=dict)
    variants: Mapping[str, frozenset[str]] = field(default_factory=dict)

    def has(self, feature: Feature) -> bool:
        """Whether ``feature`` is supported."""
        return feature in self.features

    def require(self, *features: Feature, provider_id: str = "") -> None:
        """Raise unless every named feature is supported.

        Called at startup rather than at point of use, so a run that needs a
        capability fails in the first second instead of forty minutes in.

        Raises:
            ProviderError: With :attr:`ErrorCode.NOT_SUPPORTED`.
        """
        missing = sorted(f.value for f in features if f not in self.features)
        if missing:
            raise ProviderError(
                ErrorCode.NOT_SUPPORTED,
                f"provider {provider_id or '?'} does not support: {', '.join(missing)}",
                provider_id=provider_id,
            )

    def limit(self, name: str, default: int = 0) -> int:
        """Return a numeric limit, or ``default`` if undeclared."""
        return self.limits.get(name, default)

    def supports_variant(self, name: str, value: str) -> bool:
        """Whether an enumerated variant (e.g. an effort level) is supported."""
        return value in self.variants.get(name, frozenset())


# --------------------------------------------------------------------------- #
# Context and lifecycle
# --------------------------------------------------------------------------- #


class CancelToken:
    """A cooperative cancellation signal.

    Deliberately not an :class:`asyncio.Event`: a token may be created and
    inspected outside a running loop, and providers only ever read it.
    """

    __slots__ = ("_cancelled",)

    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        """Request cancellation."""
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        """Whether cancellation has been requested."""
        return self._cancelled

    def raise_if_cancelled(self, provider_id: str = "") -> None:
        """Raise if cancellation has been requested.

        Raises:
            ProviderError: With :attr:`ErrorCode.CANCELLED`.
        """
        if self._cancelled:
            raise ProviderError(
                ErrorCode.CANCELLED, "operation cancelled", provider_id=provider_id
            )


@dataclass(frozen=True, slots=True)
class ProviderContext:
    """Ambient state a provider receives. Nothing is reached through globals.

    The secrets and telemetry ports named in §2.2 arrive with their own domains;
    this is the subset the ``model`` domain needs today.
    """

    run_id: RunId | None = None
    task_id: TaskId | None = None
    attempt_id: AttemptId | None = None
    deadline: float | None = None
    cancel: CancelToken = field(default_factory=CancelToken)


@dataclass(frozen=True, slots=True)
class HealthReport:
    """The outcome of a provider's readiness check."""

    provider_id: str
    healthy: bool
    detail: str = ""
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@runtime_checkable
class Provider(Protocol):
    """Implemented by every provider in every domain, without exception.

    The identity members are declared as read-only properties, not settable
    variables: a caller never assigns a provider's identity, and declaring them
    settable forbids implementations that expose ``id`` as a property — which
    the OpenAI-compatible family does, because its id depends on configuration.
    A plain class attribute still satisfies a read-only property protocol.
    """

    @property
    def id(self) -> str:
        """Stable, dotted identifier, e.g. ``model.anthropic``."""
        ...

    @property
    def domain(self) -> Domain:
        """The port this provider satisfies."""
        ...

    @property
    def version(self) -> str:
        """Implementation version, not vendor version."""
        ...

    def capabilities(self) -> CapabilitySet:
        """Declare what this implementation can actually do."""
        ...

    async def startup(self, ctx: ProviderContext | None = None) -> None:
        """Verify the provider can work, and acquire what it needs."""
        ...

    async def health(self) -> HealthReport:
        """Report current readiness."""
        ...

    async def shutdown(self) -> None:
        """Release resources. Idempotent, and runs on the error path."""
        ...


# --------------------------------------------------------------------------- #
# Neutral model types
# --------------------------------------------------------------------------- #


class Role(StrEnum):
    """Who authored a message. System instructions travel separately."""

    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True, slots=True)
class TextBlock:
    """Plain text."""

    text: str


@dataclass(frozen=True, slots=True)
class ReasoningBlock:
    """Model reasoning, where a backend returns it.

    ``text`` is empty when the backend signals that reasoning occurred without
    exposing it. That is a normal state, not a failure.
    """

    text: str = ""


@dataclass(frozen=True, slots=True)
class ToolCallBlock:
    """A request from the model to invoke a tool."""

    id: str
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolResultBlock:
    """The result of a tool invocation, fed back to the model."""

    call_id: str
    content: str
    is_error: bool = False


#: Any block that may appear in a message or a response.
ContentBlock = TextBlock | ReasoningBlock | ToolCallBlock | ToolResultBlock


@dataclass(frozen=True, slots=True)
class Message:
    """One turn in a conversation."""

    role: Role
    content: tuple[ContentBlock, ...]

    @classmethod
    def user(cls, text: str) -> Message:
        """Build a user message from plain text."""
        return cls(role=Role.USER, content=(TextBlock(text),))

    @classmethod
    def assistant(cls, text: str) -> Message:
        """Build an assistant message from plain text."""
        return cls(role=Role.ASSISTANT, content=(TextBlock(text),))

    @property
    def text(self) -> str:
        """Concatenated text of every text block in this message."""
        return "".join(b.text for b in self.content if isinstance(b, TextBlock))


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A tool the model may call."""

    name: str
    description: str
    schema: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CacheHint:
    """An advisory request to cache the prompt prefix up to this point.

    Advisory by design: a backend without prefix caching ignores it and simply
    does not declare :attr:`Feature.PROMPT_CACHING`.
    """

    after_system: bool = True
    after_messages: bool = True


class StopReason(StrEnum):
    """Why generation stopped.

    :attr:`REFUSED` is a *result*, not an exception. A policy decline arrives as
    a successful response on some backends, so raising here would crash a caller
    that did nothing wrong (§5.1, M-1).
    """

    END = "end"
    MAX_TOKENS = "max_tokens"
    TOOL_CALL = "tool_call"
    STOP_SEQUENCE = "stop_sequence"
    REFUSED = "refused"
    PAUSED = "paused"


@dataclass(frozen=True, slots=True)
class RefusalDetail:
    """Why a backend declined, when it says."""

    category: str | None = None
    explanation: str = ""


@dataclass(frozen=True, slots=True)
class Usage:
    """Token consumption and cost for one call.

    ``cost_usd`` is ``None`` when the provider cannot price its own calls.
    Reporting ``0.0`` would understate a run's cost silently (§5.1, M-7).

    ``tokens_estimated`` is the same honesty rule applied to the counts
    themselves: a provider without :attr:`Feature.USAGE_REPORTING` reports
    approximations, and an approximation presented as a measurement is the
    quiet cousin of the ``$0.00`` problem. The flag travels with the numbers
    so every downstream surface — budgets, events, the API — can say
    "estimated" instead of implying precision that does not exist.

    ``notional_cost_usd`` is what a call **would have cost** at list API
    prices, for a backend that is not billed that way. A subscription-billed
    CLI reports such a figure; putting it in ``cost_usd`` would tell an
    operator they spent money they did not spend — the ``$0.00`` error
    inverted, and just as wrong. Kept in its own field so nothing can render
    it as a charge by accident.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cost_usd: float | None = None
    tokens_estimated: bool = False
    notional_cost_usd: float | None = None

    @property
    def total_tokens(self) -> int:
        """Input plus output."""
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class CompletionRequest:
    """A vendor-neutral request for one model turn.

    Note what is **absent**: ``temperature``, ``top_p``, and ``top_k``. They are
    rejected outright by some current backends and are semantically
    incomparable across the rest, so the neutral request has no place for them
    (§5.1, M-3). Behaviour is steered by prompting.
    """

    model: str
    messages: tuple[Message, ...]
    system: tuple[TextBlock, ...] = ()
    tools: tuple[ToolSpec, ...] = ()
    max_output_tokens: int = 16_000
    reasoning: ThinkingMode = ThinkingMode.ADAPTIVE
    effort: Effort = Effort.HIGH
    output_schema: Mapping[str, Any] | None = None
    cache_hint: CacheHint | None = None
    stop: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.model:
            raise ProviderError(ErrorCode.INVALID_REQUEST, "request.model must not be empty")
        if not self.messages:
            raise ProviderError(
                ErrorCode.INVALID_REQUEST, "request.messages must not be empty"
            )
        if self.max_output_tokens <= 0:
            raise ProviderError(
                ErrorCode.INVALID_REQUEST,
                f"request.max_output_tokens must be positive, got {self.max_output_tokens}",
            )
        if self.messages[-1].role is Role.ASSISTANT:
            # Assistant prefill is rejected by several current backends and is
            # not expressible neutrally. Structured output is the replacement.
            raise ProviderError(
                ErrorCode.INVALID_REQUEST,
                "a request must not end with an assistant turn; assistant prefill is "
                "not supported — use output_schema for structured output",
            )

    @property
    def system_text(self) -> str:
        """The system prompt as one string."""
        return "\n\n".join(block.text for block in self.system)


@dataclass(frozen=True, slots=True)
class CompletionResponse:
    """A vendor-neutral response for one model turn."""

    content: tuple[ContentBlock, ...]
    stop_reason: StopReason
    usage: Usage = field(default_factory=Usage)
    model_served: str = ""
    refusal: RefusalDetail | None = None

    @property
    def text(self) -> str:
        """Concatenated text of every text block."""
        return "".join(b.text for b in self.content if isinstance(b, TextBlock))

    @property
    def tool_calls(self) -> tuple[ToolCallBlock, ...]:
        """Every tool call the model requested."""
        return tuple(b for b in self.content if isinstance(b, ToolCallBlock))

    @property
    def refused(self) -> bool:
        """Whether the backend declined on policy grounds."""
        return self.stop_reason is StopReason.REFUSED


class StreamEventKind(StrEnum):
    """The kind of incremental update a stream is delivering."""

    TEXT = "text"
    REASONING = "reasoning"
    TOOL_CALL = "tool_call"
    DONE = "done"


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """One incremental update from a streaming call."""

    kind: StreamEventKind
    text: str = ""
    tool_call: ToolCallBlock | None = None
    response: CompletionResponse | None = None


@runtime_checkable
class ModelPort(Provider, Protocol):
    """The contract every AI backend satisfies.

    Claude, Hermes, an OpenAI-compatible server, or a fake — all equal citizens.
    Nothing above this layer may name any of them.
    """

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        """Run one turn and return the whole response."""
        ...

    def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        """Run one turn, yielding incremental updates."""
        ...

    async def count_tokens(self, request: CompletionRequest) -> int:
        """Return the request's input token count."""
        ...


# --------------------------------------------------------------------------- #
# Shared helpers for implementations
# --------------------------------------------------------------------------- #


def estimate_tokens(text: str) -> int:
    """Roughly estimate a token count from character length.

    A deliberate approximation for backends that expose no counting endpoint.
    Providers using it must **not** declare :attr:`Feature.TOKEN_COUNTING`,
    which promises an exact count.
    """
    return max(1, (len(text) + 3) // 4)


def request_text(request: CompletionRequest) -> str:
    """Flatten a request's system prompt and messages into one string."""
    parts: list[str] = [block.text for block in request.system]
    for message in request.messages:
        for block in message.content:
            if isinstance(block, TextBlock):
                parts.append(block.text)
            elif isinstance(block, ToolResultBlock):
                parts.append(block.content)
    return "\n".join(parts)


def blocks_to_dicts(content: Sequence[ContentBlock]) -> list[dict[str, Any]]:
    """Render neutral content blocks as plain JSON-ready dictionaries."""
    out: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, TextBlock):
            out.append({"type": "text", "text": block.text})
        elif isinstance(block, ReasoningBlock):
            out.append({"type": "reasoning", "text": block.text})
        elif isinstance(block, ToolCallBlock):
            out.append(
                {
                    "type": "tool_call",
                    "id": block.id,
                    "name": block.name,
                    "arguments": dict(block.arguments),
                }
            )
        elif isinstance(block, ToolResultBlock):
            out.append(
                {
                    "type": "tool_result",
                    "call_id": block.call_id,
                    "content": block.content,
                    "is_error": block.is_error,
                }
            )
    return out

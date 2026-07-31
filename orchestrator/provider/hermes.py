"""Hermes provider — an open-weights model family behind the neutral port.

Hermes models are typically self-hosted and served over a chat-completions-style
HTTP API, so this provider builds on :class:`OpenAICompatProvider` rather than
duplicating the wire format. What it adds is the part that genuinely differs:
an honest capability declaration and tolerance for a tool-calling convention
some open-weights deployments use.

**Deliberately conservative claims.** A self-hosted Hermes deployment has no
vendor price list, no policy-refusal concept, and no effort control, so none of
:attr:`Feature.COST_REPORTING`, :attr:`Feature.REFUSAL_SEMANTICS`, or
:attr:`Feature.EFFORT_LEVELS` are declared. Context and output limits depend on
how the operator built and served the model, so they are configuration with
conservative defaults — this module does not invent numbers for a deployment it
cannot see.

**Tool-calling fidelity.** Open-weights models vary in how reliably they emit
structured tool calls, and some Hermes-family deployments emit them as XML-ish
tags inside the assistant's text rather than in a ``tool_calls`` field. Set
``parse_inline_tool_calls`` when the deployment behaves that way. It defaults to
off, because guessing wrongly would silently reinterpret ordinary prose as a
tool invocation.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Final

from orchestrator.provider.base import (
    CapabilitySet,
    CompletionResponse,
    ContentBlock,
    ErrorCode,
    ProviderError,
    StopReason,
    TextBlock,
    ToolCallBlock,
)
from orchestrator.provider.claude import Transport
from orchestrator.provider.openai_compat import OpenAICompatConfig, OpenAICompatProvider

__all__ = ["HermesConfig", "HermesProvider"]

#: The tag some Hermes-family deployments wrap tool calls in.
_TOOL_CALL_PATTERN: Final = re.compile(
    r"<tool_call>\s*(?P<body>\{.*?\})\s*</tool_call>", re.DOTALL
)

_DEFAULT_CONTEXT = 8_192
_DEFAULT_MAX_OUTPUT = 4_096


@dataclass(frozen=True, slots=True)
class HermesConfig:
    """Settings for a self-hosted Hermes deployment.

    Attributes:
        model: The model name the endpoint serves.
        base_url: The chat-completions endpoint.
        context_tokens: Operator-declared context window. There is no reliable
            way to discover this remotely, and guessing would produce silent
            truncation, so it is configuration.
        max_output_tokens: Operator-declared output ceiling.
        supports_tools: Whether the deployment accepts tool definitions.
        parse_inline_tool_calls: Whether to recover tool calls embedded in
            assistant text as ``<tool_call>{...}</tool_call>``.
    """

    model: str = "hermes"
    base_url: str = "http://localhost:11434/v1"
    context_tokens: int = _DEFAULT_CONTEXT
    max_output_tokens: int = _DEFAULT_MAX_OUTPUT
    supports_tools: bool = True
    parse_inline_tool_calls: bool = False

    def __post_init__(self) -> None:
        if not self.model:
            raise ProviderError(ErrorCode.INVALID_REQUEST, "hermes.model must not be empty")
        if self.context_tokens <= 0 or self.max_output_tokens <= 0:
            raise ProviderError(
                ErrorCode.INVALID_REQUEST, "hermes token limits must be positive"
            )

    def to_compat(self) -> OpenAICompatConfig:
        """Render these settings as the underlying endpoint configuration."""
        return OpenAICompatConfig(
            model=self.model,
            base_url=self.base_url,
            context_tokens=self.context_tokens,
            max_output_tokens=self.max_output_tokens,
            supports_tools=self.supports_tools,
            provider_id="model.hermes",
        )


class HermesProvider(OpenAICompatProvider):
    """A Hermes-family model served over a chat-completions endpoint."""

    version = "1.0"

    __slots__ = ("_hermes",)

    def __init__(
        self, config: HermesConfig | None = None, *, transport: Transport | None = None
    ) -> None:
        """Construct the provider.

        Args:
            config: Deployment settings.
            transport: Moves payloads. Tests inject a scripted one.
        """
        self._hermes = config or HermesConfig()
        super().__init__(self._hermes.to_compat(), transport=transport)

    @property
    def hermes_config(self) -> HermesConfig:
        """The Hermes-specific settings."""
        return self._hermes

    def capabilities(self) -> CapabilitySet:
        """Declare what this deployment actually supports.

        Inherits the endpoint's capabilities and adds nothing it cannot deliver.
        The absences — effort, caching, refusal semantics, cost — are the point:
        a caller that needs one of them must be told, not quietly served
        something else.
        """
        base = super().capabilities()
        return CapabilitySet(
            features=base.features,
            limits=dict(base.limits),
            # Recorded so an operator can see what the deployment was declared
            # to be, without implying a capability the model does not have.
            variants={"family": frozenset({"hermes"})},
        )

    def parse_response(self, raw: Mapping[str, Any]) -> CompletionResponse:
        """Translate a reply, optionally recovering inline tool calls."""
        response = super().parse_response(raw)
        if not self._hermes.parse_inline_tool_calls or response.tool_calls:
            return response

        recovered: list[ContentBlock] = []
        found: list[ToolCallBlock] = []
        for block in response.content:
            if not isinstance(block, TextBlock):
                recovered.append(block)
                continue
            remainder, calls = _extract_inline_calls(block.text, len(found))
            found.extend(calls)
            if remainder.strip():
                recovered.append(TextBlock(remainder))

        if not found:
            return response
        # A recovered call means the turn ended to invoke a tool, whatever the
        # endpoint reported — but never override a more specific stop reason
        # such as a truncated response.
        stop_reason = (
            StopReason.TOOL_CALL
            if response.stop_reason is StopReason.END
            else response.stop_reason
        )
        return replace(response, content=(*recovered, *found), stop_reason=stop_reason)


def _extract_inline_calls(text: str, offset: int) -> tuple[str, list[ToolCallBlock]]:
    """Pull ``<tool_call>`` blocks out of assistant text.

    Args:
        text: The assistant's raw text.
        offset: How many calls were already recovered, so generated identifiers
            stay unique within one response.

    Returns:
        The text with the tool-call blocks removed, and the recovered calls.
        A block whose body is not valid JSON is left in the text untouched —
        better to show the operator something odd than to invent a call.
    """
    calls: list[ToolCallBlock] = []
    consumed: list[tuple[int, int]] = []

    for index, match in enumerate(_TOOL_CALL_PATTERN.finditer(text)):
        try:
            body = json.loads(match.group("body"))
        except json.JSONDecodeError:
            continue
        if not isinstance(body, dict) or "name" not in body:
            continue
        arguments = body.get("arguments", {})
        calls.append(
            ToolCallBlock(
                id=f"hermes-inline-{offset + index}",
                name=str(body["name"]),
                arguments=dict(arguments) if isinstance(arguments, dict) else {},
            )
        )
        consumed.append(match.span())

    if not consumed:
        return text, []

    remainder: list[str] = []
    cursor = 0
    for start, end in consumed:
        remainder.append(text[cursor:start])
        cursor = end
    remainder.append(text[cursor:])
    return "".join(remainder), calls

"""Tests for the Hermes provider — honest capabilities and inline tool calls."""

from __future__ import annotations

import pytest

from orchestrator.provider.base import (
    Domain,
    ErrorCode,
    Feature,
    ProviderError,
    StopReason,
    TextBlock,
)
from orchestrator.provider.hermes import HermesConfig, HermesProvider

from tests.provider.conftest import FakeTransport, run, simple_request


@pytest.fixture
def provider(transport: FakeTransport) -> HermesProvider:
    """A started Hermes provider backed by a scripted transport."""
    instance = HermesProvider(transport=transport)
    run(instance.startup())
    return instance


def reply(text: str, finish: str = "stop") -> dict[str, object]:
    """A chat-completions reply carrying ``text``."""
    return {"choices": [{"message": {"content": text}, "finish_reason": finish}]}


class TestConfig:
    """Deployment settings are the operator's to declare."""

    def test_defaults_are_conservative(self) -> None:
        config = HermesConfig()
        assert config.context_tokens == 8_192
        assert config.parse_inline_tool_calls is False

    def test_empty_model_is_rejected(self) -> None:
        with pytest.raises(ProviderError, match="must not be empty"):
            HermesConfig(model="")

    def test_non_positive_limits_are_rejected(self) -> None:
        with pytest.raises(ProviderError, match="positive"):
            HermesConfig(context_tokens=0)

    def test_settings_flow_through_to_the_endpoint(self) -> None:
        compat = HermesConfig(model="hermes-4", context_tokens=32_768).to_compat()
        assert compat.model == "hermes-4"
        assert compat.context_tokens == 32_768
        assert compat.provider_id == "model.hermes"


class TestIdentityAndCapabilities:
    """Hermes claims only what a self-hosted deployment can deliver."""

    def test_identity(self, provider: HermesProvider) -> None:
        assert provider.id == "model.hermes"
        assert provider.domain is Domain.MODEL

    def test_streaming_and_tools_are_declared(self, provider: HermesProvider) -> None:
        caps = provider.capabilities()
        assert caps.has(Feature.STREAMING)
        assert caps.has(Feature.TOOL_CALLING)

    @pytest.mark.parametrize(
        "absent",
        [
            Feature.EFFORT_LEVELS,
            Feature.PROMPT_CACHING,
            Feature.REFUSAL_SEMANTICS,
            Feature.COST_REPORTING,
            Feature.SERVER_FALLBACK,
            Feature.TOKEN_COUNTING,
        ],
    )
    def test_absent_capabilities_are_not_claimed(
        self, provider: HermesProvider, absent: Feature
    ) -> None:
        """An open-weights deployment has none of these; claiming one would lie."""
        assert not provider.capabilities().has(absent)

    def test_the_family_is_recorded_without_implying_capability(
        self, provider: HermesProvider
    ) -> None:
        assert provider.capabilities().supports_variant("family", "hermes")

    def test_requiring_effort_levels_raises(self, provider: HermesProvider) -> None:
        with pytest.raises(ProviderError) as excinfo:
            provider.capabilities().require(Feature.EFFORT_LEVELS, provider_id=provider.id)
        assert excinfo.value.error_code is ErrorCode.NOT_SUPPORTED


class TestPayload:
    """Hermes inherits the chat-completions translation unchanged."""

    def test_payload_uses_the_configured_model(self, provider: HermesProvider) -> None:
        payload = provider.build_payload(simple_request(model="hermes"))
        assert payload["model"] == "hermes"
        assert payload["messages"][0]["role"] == "system"

    def test_no_vendor_only_fields_are_sent(self, provider: HermesProvider) -> None:
        payload = provider.build_payload(simple_request(model="hermes"))
        for absent in ("thinking", "output_config", "fallbacks", "betas", "temperature"):
            assert absent not in payload


class TestInlineToolCalls:
    """Some deployments emit tool calls inside assistant text."""

    def test_inline_parsing_is_off_by_default(self, provider: HermesProvider) -> None:
        """Guessing would reinterpret ordinary prose as a tool invocation."""
        raw = reply('<tool_call>{"name": "read", "arguments": {"p": "a"}}</tool_call>')
        response = provider.parse_response(raw)
        assert response.tool_calls == ()
        assert "<tool_call>" in response.text

    def test_inline_calls_are_recovered_when_enabled(
        self, transport: FakeTransport
    ) -> None:
        instance = HermesProvider(
            HermesConfig(parse_inline_tool_calls=True), transport=transport
        )
        raw = reply(
            'Let me look.<tool_call>{"name": "read", "arguments": {"p": "a.py"}}</tool_call>'
        )
        response = instance.parse_response(raw)

        assert len(response.tool_calls) == 1
        assert response.tool_calls[0].name == "read"
        assert response.tool_calls[0].arguments == {"p": "a.py"}
        assert response.stop_reason is StopReason.TOOL_CALL
        assert "<tool_call>" not in response.text
        assert "Let me look." in response.text

    def test_several_inline_calls_get_unique_identifiers(
        self, transport: FakeTransport
    ) -> None:
        instance = HermesProvider(
            HermesConfig(parse_inline_tool_calls=True), transport=transport
        )
        raw = reply(
            '<tool_call>{"name": "a", "arguments": {}}</tool_call>'
            '<tool_call>{"name": "b", "arguments": {}}</tool_call>'
        )
        response = instance.parse_response(raw)
        ids = [call.id for call in response.tool_calls]
        assert len(ids) == 2
        assert len(set(ids)) == 2

    def test_malformed_inline_json_is_left_alone(
        self, transport: FakeTransport
    ) -> None:
        """Better to show the operator something odd than to invent a call."""
        instance = HermesProvider(
            HermesConfig(parse_inline_tool_calls=True), transport=transport
        )
        response = instance.parse_response(reply("<tool_call>{not json}</tool_call>"))
        assert response.tool_calls == ()
        assert "<tool_call>" in response.text

    def test_a_block_without_a_name_is_ignored(self, transport: FakeTransport) -> None:
        instance = HermesProvider(
            HermesConfig(parse_inline_tool_calls=True), transport=transport
        )
        response = instance.parse_response(reply('<tool_call>{"arguments": {}}</tool_call>'))
        assert response.tool_calls == ()

    def test_structured_tool_calls_take_precedence(
        self, transport: FakeTransport
    ) -> None:
        """If the endpoint returned proper calls, do not also scrape the text."""
        instance = HermesProvider(
            HermesConfig(parse_inline_tool_calls=True), transport=transport
        )
        raw = {
            "choices": [
                {
                    "message": {
                        "content": '<tool_call>{"name": "scraped", "arguments": {}}</tool_call>',
                        "tool_calls": [
                            {"id": "c1", "function": {"name": "structured", "arguments": "{}"}}
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }
        response = instance.parse_response(raw)
        assert [c.name for c in response.tool_calls] == ["structured"]

    def test_plain_text_is_untouched(self, transport: FakeTransport) -> None:
        instance = HermesProvider(
            HermesConfig(parse_inline_tool_calls=True), transport=transport
        )
        response = instance.parse_response(reply("just an ordinary answer"))
        assert response.text == "just an ordinary answer"
        assert response.stop_reason is StopReason.END

    def test_a_specific_stop_reason_is_not_overridden(
        self, transport: FakeTransport
    ) -> None:
        instance = HermesProvider(
            HermesConfig(parse_inline_tool_calls=True), transport=transport
        )
        raw = reply('<tool_call>{"name": "a", "arguments": {}}</tool_call>', finish="length")
        response = instance.parse_response(raw)
        assert response.stop_reason is StopReason.MAX_TOKENS


class TestCalls:
    """End-to-end through the scripted transport."""

    def test_complete_round_trips(self, transport: FakeTransport) -> None:
        transport.response = reply("hermes here")
        instance = HermesProvider(transport=transport)
        run(instance.startup())
        response = run(instance.complete(simple_request(model="hermes")))
        assert response.text == "hermes here"
        assert isinstance(response.content[0], TextBlock)

    def test_startup_without_a_transport_is_refused(self) -> None:
        with pytest.raises(ProviderError) as excinfo:
            run(HermesProvider().startup())
        assert excinfo.value.error_code is ErrorCode.UNAVAILABLE

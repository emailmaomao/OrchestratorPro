"""Tests for the Claude provider — translation rules and response parsing.

Every test runs offline against a scripted transport. The rules asserted here
each correspond to an HTTP 400 or a silent cost error in production, which is
why they are covered rather than trusted to review.
"""

from __future__ import annotations

import pytest

from orchestrator.core.config import Effort, ThinkingMode
from orchestrator.provider.base import (
    Domain,
    ErrorCode,
    Feature,
    Message,
    ProviderError,
    ReasoningBlock,
    Role,
    StopReason,
    StreamEventKind,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    ToolSpec,
)
from orchestrator.provider.claude import ClaudeConfig, ClaudeProvider

from tests.provider.conftest import CountingTransport, FakeTransport, collect, run, simple_request


@pytest.fixture
def provider(transport: FakeTransport) -> ClaudeProvider:
    """A started Claude provider backed by a scripted transport."""
    instance = ClaudeProvider(transport=transport)
    run(instance.startup())
    return instance


class TestLifecycle:
    """Startup fails fast when the backend cannot be reached."""

    def test_startup_with_an_injected_transport_succeeds(
        self, transport: FakeTransport
    ) -> None:
        instance = ClaudeProvider(transport=transport)
        run(instance.startup())
        assert run(instance.health()).healthy

    def test_a_missing_sdk_fails_at_startup_not_mid_run(self) -> None:
        """The failure belongs at startup, where it is cheap."""

        def broken_factory() -> object:
            raise ProviderError(
                ErrorCode.UNAVAILABLE, "the 'anthropic' package is not installed"
            )

        instance = ClaudeProvider(transport_factory=broken_factory)  # type: ignore[arg-type]
        with pytest.raises(ProviderError) as excinfo:
            run(instance.startup())
        assert excinfo.value.error_code is ErrorCode.UNAVAILABLE
        assert excinfo.value.is_retryable

    def test_using_the_provider_unstarted_is_refused(self) -> None:
        instance = ClaudeProvider()
        with pytest.raises(ProviderError) as excinfo:
            run(instance.complete(simple_request()))
        assert excinfo.value.error_code is ErrorCode.UNAVAILABLE

    def test_shutdown_is_idempotent(self, provider: ClaudeProvider) -> None:
        run(provider.shutdown())
        run(provider.shutdown())
        assert not run(provider.health()).healthy

    def test_identity_and_domain(self, provider: ClaudeProvider) -> None:
        assert provider.id == "model.claude"
        assert provider.domain is Domain.MODEL


class TestCapabilities:
    """Declarations must match what the provider can actually deliver."""

    def test_core_features_are_declared(self, provider: ClaudeProvider) -> None:
        caps = provider.capabilities()
        for feature in (
            Feature.STREAMING,
            Feature.TOOL_CALLING,
            Feature.REASONING,
            Feature.EFFORT_LEVELS,
            Feature.PROMPT_CACHING,
            Feature.REFUSAL_SEMANTICS,
            Feature.TOKEN_COUNTING,
        ):
            assert caps.has(feature), feature

    def test_every_effort_level_is_offered(self, provider: ClaudeProvider) -> None:
        caps = provider.capabilities()
        for effort in Effort:
            assert caps.supports_variant("effort", effort.value)

    def test_cost_reporting_only_for_priced_models(
        self, transport: FakeTransport
    ) -> None:
        priced = ClaudeProvider(ClaudeConfig(model="claude-opus-5"), transport=transport)
        unpriced = ClaudeProvider(ClaudeConfig(model="some-unlisted"), transport=transport)
        assert priced.capabilities().has(Feature.COST_REPORTING)
        assert not unpriced.capabilities().has(Feature.COST_REPORTING)

    def test_server_fallback_is_declared_when_enabled(
        self, transport: FakeTransport
    ) -> None:
        off = ClaudeProvider(
            ClaudeConfig(enable_server_fallback=False), transport=transport
        )
        assert not off.capabilities().has(Feature.SERVER_FALLBACK)

    def test_streaming_threshold_is_declared(self, provider: ClaudeProvider) -> None:
        assert provider.capabilities().limit("stream_above_tokens") == 16_000


class TestTranslationRules:
    """Each rule here maps to a 400 if broken."""

    def test_adaptive_reasoning_is_sent_as_adaptive(
        self, provider: ClaudeProvider
    ) -> None:
        payload = provider.build_payload(simple_request())
        assert payload["thinking"] == {"type": "adaptive"}

    def test_budget_tokens_is_never_sent(self, provider: ClaudeProvider) -> None:
        """The parameter is removed on this model family and returns a 400."""
        payload = provider.build_payload(simple_request())
        assert "budget_tokens" not in payload
        assert "budget_tokens" not in payload["thinking"]

    def test_sampling_parameters_are_never_sent(self, provider: ClaudeProvider) -> None:
        payload = provider.build_payload(simple_request())
        for forbidden in ("temperature", "top_p", "top_k"):
            assert forbidden not in payload

    def test_effort_goes_into_output_config(self, provider: ClaudeProvider) -> None:
        payload = provider.build_payload(simple_request(effort=Effort.XHIGH))
        assert payload["output_config"]["effort"] == "xhigh"

    @pytest.mark.parametrize("effort", [Effort.XHIGH, Effort.MAX])
    def test_disabled_reasoning_at_deep_effort_is_rejected_locally(
        self, provider: ClaudeProvider, effort: Effort
    ) -> None:
        """Caught here so the failure is ours and immediate, not a remote 400."""
        with pytest.raises(ProviderError) as excinfo:
            provider.build_payload(
                simple_request(reasoning=ThinkingMode.DISABLED, effort=effort)
            )
        assert excinfo.value.error_code is ErrorCode.INVALID_REQUEST
        assert not excinfo.value.is_retryable

    @pytest.mark.parametrize("effort", [Effort.LOW, Effort.MEDIUM, Effort.HIGH])
    def test_disabled_reasoning_is_allowed_at_moderate_effort(
        self, provider: ClaudeProvider, effort: Effort
    ) -> None:
        payload = provider.build_payload(
            simple_request(reasoning=ThinkingMode.DISABLED, effort=effort)
        )
        assert payload["thinking"] == {"type": "disabled"}

    def test_max_output_tokens_maps_to_max_tokens(
        self, provider: ClaudeProvider
    ) -> None:
        payload = provider.build_payload(simple_request(max_output_tokens=4096))
        assert payload["max_tokens"] == 4096

    def test_large_requests_are_forced_to_stream(self, provider: ClaudeProvider) -> None:
        """Above the threshold a non-streaming request trips the HTTP timeout."""
        assert provider.build_payload(simple_request(max_output_tokens=64_000))["stream"]
        assert "stream" not in provider.build_payload(
            simple_request(max_output_tokens=8_000)
        )

    def test_structured_output_replaces_prefill(self, provider: ClaudeProvider) -> None:
        schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
        payload = provider.build_payload(simple_request(output_schema=schema))
        assert payload["output_config"]["format"]["type"] == "json_schema"
        assert payload["output_config"]["format"]["schema"] == schema

    def test_server_fallback_is_opted_into_by_default(
        self, provider: ClaudeProvider
    ) -> None:
        payload = provider.build_payload(simple_request())
        assert payload["fallbacks"] == "default"
        assert "server-side-fallback-2026-07-01" in payload["betas"]

    def test_server_fallback_can_be_disabled(self, transport: FakeTransport) -> None:
        instance = ClaudeProvider(
            ClaudeConfig(enable_server_fallback=False), transport=transport
        )
        assert "fallbacks" not in instance.build_payload(simple_request())

    def test_cache_hint_places_a_breakpoint_on_the_last_system_block(
        self, provider: ClaudeProvider
    ) -> None:
        from orchestrator.provider.base import CacheHint

        payload = provider.build_payload(
            simple_request(
                system=(TextBlock("stable"), TextBlock("also stable")),
                cache_hint=CacheHint(),
            )
        )
        assert "cache_control" not in payload["system"][0]
        assert payload["system"][-1]["cache_control"] == {"type": "ephemeral"}

    def test_no_cache_control_without_a_hint(self, provider: ClaudeProvider) -> None:
        payload = provider.build_payload(simple_request())
        assert all("cache_control" not in block for block in payload["system"])

    def test_tools_are_translated(self, provider: ClaudeProvider) -> None:
        payload = provider.build_payload(
            simple_request(
                tools=(
                    ToolSpec(
                        name="read_file",
                        description="Read a file",
                        schema={"type": "object"},
                    ),
                )
            )
        )
        assert payload["tools"][0]["name"] == "read_file"
        assert payload["tools"][0]["input_schema"] == {"type": "object"}

    def test_tool_results_are_translated(self, provider: ClaudeProvider) -> None:
        payload = provider.build_payload(
            simple_request(
                messages=(
                    Message(
                        role=Role.USER,
                        content=(ToolResultBlock(call_id="tu_1", content="done"),),
                    ),
                )
            )
        )
        block = payload["messages"][0]["content"][0]
        assert block["type"] == "tool_result"
        assert block["tool_use_id"] == "tu_1"

    def test_stop_sequences_are_passed_through(self, provider: ClaudeProvider) -> None:
        payload = provider.build_payload(simple_request(stop=("STOP",)))
        assert payload["stop_sequences"] == ["STOP"]


class TestResponseParsing:
    """Replies become neutral responses, refusals included."""

    def test_text_and_usage_are_extracted(self, provider: ClaudeProvider) -> None:
        response = provider.parse_response(
            {
                "content": [{"type": "text", "text": "hello"}],
                "stop_reason": "end_turn",
                "model": "claude-opus-5",
                "usage": {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
            }
        )
        assert response.text == "hello"
        assert response.stop_reason is StopReason.END
        assert response.usage.input_tokens == 1_000_000

    def test_cost_is_priced_for_a_known_model(self, provider: ClaudeProvider) -> None:
        response = provider.parse_response(
            {
                "content": [],
                "stop_reason": "end_turn",
                "model": "claude-opus-5",
                "usage": {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
            }
        )
        # $5 per million input + $25 per million output.
        assert response.usage.cost_usd == pytest.approx(30.0)

    def test_cost_is_none_for_an_unknown_model(self, transport: FakeTransport) -> None:
        """Guessing a price would be worse than reporting nothing."""
        instance = ClaudeProvider(ClaudeConfig(model="mystery"), transport=transport)
        response = instance.parse_response(
            {"content": [], "stop_reason": "end_turn", "model": "mystery",
             "usage": {"input_tokens": 10, "output_tokens": 10}}
        )
        assert response.usage.cost_usd is None

    def test_a_refusal_is_a_result_not_an_exception(
        self, provider: ClaudeProvider
    ) -> None:
        """stop_reason 'refusal' arrives on a successful response."""
        response = provider.parse_response(
            {
                "content": [],
                "stop_reason": "refusal",
                "stop_details": {"category": "cyber", "explanation": "declined"},
            }
        )
        assert response.refused
        assert response.stop_reason is StopReason.REFUSED
        assert response.refusal is not None
        assert response.refusal.category == "cyber"

    def test_reading_content_after_a_refusal_is_safe(
        self, provider: ClaudeProvider
    ) -> None:
        response = provider.parse_response({"content": [], "stop_reason": "refusal"})
        assert response.text == ""
        assert response.tool_calls == ()

    def test_tool_calls_are_extracted(self, provider: ClaudeProvider) -> None:
        response = provider.parse_response(
            {
                "content": [
                    {"type": "tool_use", "id": "tu_1", "name": "read", "input": {"p": "a"}}
                ],
                "stop_reason": "tool_use",
            }
        )
        assert response.stop_reason is StopReason.TOOL_CALL
        assert response.tool_calls[0].arguments == {"p": "a"}

    def test_reasoning_blocks_are_preserved(self, provider: ClaudeProvider) -> None:
        response = provider.parse_response(
            {
                "content": [
                    {"type": "thinking", "thinking": "considering"},
                    {"type": "text", "text": "answer"},
                ],
                "stop_reason": "end_turn",
            }
        )
        assert any(isinstance(b, ReasoningBlock) for b in response.content)
        assert response.text == "answer"

    def test_empty_reasoning_is_not_an_error(self, provider: ClaudeProvider) -> None:
        """A backend may signal reasoning without exposing it."""
        response = provider.parse_response(
            {"content": [{"type": "thinking", "thinking": ""}], "stop_reason": "end_turn"}
        )
        assert isinstance(response.content[0], ReasoningBlock)

    @pytest.mark.parametrize(
        ("wire", "expected"),
        [
            ("end_turn", StopReason.END),
            ("max_tokens", StopReason.MAX_TOKENS),
            ("tool_use", StopReason.TOOL_CALL),
            ("stop_sequence", StopReason.STOP_SEQUENCE),
            ("refusal", StopReason.REFUSED),
            ("pause_turn", StopReason.PAUSED),
            ("something_new", StopReason.END),
        ],
    )
    def test_stop_reasons_map(
        self, provider: ClaudeProvider, wire: str, expected: StopReason
    ) -> None:
        parsed = provider.parse_response({"content": [], "stop_reason": wire})
        assert parsed.stop_reason is expected


class TestCalls:
    """The port methods drive the transport and translate both ways."""

    def test_complete_round_trips(self, transport: FakeTransport) -> None:
        transport.response = {
            "content": [{"type": "text", "text": "done"}],
            "stop_reason": "end_turn",
            "model": "claude-opus-5",
        }
        instance = ClaudeProvider(transport=transport)
        run(instance.startup())

        response = run(instance.complete(simple_request()))
        assert response.text == "done"
        assert transport.last["model"] == "claude-opus-5"

    def test_a_vendor_exception_never_escapes(self, transport: FakeTransport) -> None:
        """Callers cannot import a vendor taxonomy, so none may reach them."""
        transport.error = RuntimeError("vendor exploded")
        instance = ClaudeProvider(transport=transport)
        run(instance.startup())

        with pytest.raises(ProviderError) as excinfo:
            run(instance.complete(simple_request()))
        assert excinfo.value.error_code is ErrorCode.INTERNAL
        assert "vendor exploded" in str(excinfo.value.detail["cause"])

    def test_provider_errors_pass_through_unwrapped(
        self, transport: FakeTransport
    ) -> None:
        transport.error = ProviderError(ErrorCode.RATE_LIMITED, "429")
        instance = ClaudeProvider(transport=transport)
        run(instance.startup())

        with pytest.raises(ProviderError) as excinfo:
            run(instance.complete(simple_request()))
        assert excinfo.value.error_code is ErrorCode.RATE_LIMITED

    def test_streaming_yields_text_then_done(self) -> None:
        transport = FakeTransport(
            chunks=[
                {"type": "text", "text": "he"},
                {"type": "text", "text": "llo"},
                {
                    "type": "message",
                    "message": {
                        "content": [{"type": "text", "text": "hello"}],
                        "stop_reason": "end_turn",
                    },
                },
            ]
        )
        instance = ClaudeProvider(transport=transport)
        run(instance.startup())

        events = collect(instance.stream(simple_request()))
        assert "".join(e.text for e in events if e.kind is StreamEventKind.TEXT) == "hello"
        assert events[-1].kind is StreamEventKind.DONE
        assert events[-1].response is not None
        assert transport.last["stream"] is True

    def test_count_tokens_uses_the_backend_when_available(self) -> None:
        transport = CountingTransport(token_count=1234)
        instance = ClaudeProvider(transport=transport)
        run(instance.startup())
        assert run(instance.count_tokens(simple_request())) == 1234

    def test_count_tokens_falls_back_to_an_estimate(
        self, transport: FakeTransport
    ) -> None:
        instance = ClaudeProvider(transport=transport)
        run(instance.startup())
        assert run(instance.count_tokens(simple_request())) > 0


def test_no_vendor_type_escapes_the_provider(transport: FakeTransport) -> None:
    """docs/030 §2.1 P-1: only neutral types cross the boundary."""
    transport.response = {
        "content": [{"type": "text", "text": "x"}],
        "stop_reason": "end_turn",
    }
    instance = ClaudeProvider(transport=transport)
    run(instance.startup())
    response = run(instance.complete(simple_request()))

    for block in response.content:
        assert type(block).__module__.startswith("orchestrator.")
    assert type(response).__module__.startswith("orchestrator.")
    assert type(response.usage).__module__.startswith("orchestrator.")

"""Tests for the provider substrate: neutral types, capabilities, errors."""

from __future__ import annotations

import pytest

from orchestrator.core.config import Effort, ThinkingMode
from orchestrator.core.events import OrchestratorError
from orchestrator.provider.base import (
    CancelToken,
    CapabilitySet,
    CompletionRequest,
    CompletionResponse,
    Domain,
    ErrorCode,
    Feature,
    Message,
    ProviderError,
    ReasoningBlock,
    Role,
    StopReason,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    Usage,
    blocks_to_dicts,
    estimate_tokens,
    request_text,
)

from tests.provider.conftest import simple_request


class TestErrorTaxonomy:
    """Every backend failure maps onto one closed, neutral set."""

    def test_provider_error_is_an_orchestrator_error(self) -> None:
        assert issubclass(ProviderError, OrchestratorError)

    def test_error_carries_the_neutral_code(self) -> None:
        err = ProviderError(
            ErrorCode.RATE_LIMITED, "slow down", provider_id="model.x", retry_after=12.0
        )
        assert err.error_code is ErrorCode.RATE_LIMITED
        assert err.detail["retry_after"] == 12.0
        assert err.detail["provider_id"] == "model.x"

    @pytest.mark.parametrize(
        ("code", "retryable"),
        [
            (ErrorCode.UNAVAILABLE, True),
            (ErrorCode.TIMEOUT, True),
            (ErrorCode.RATE_LIMITED, True),
            (ErrorCode.OVERLOADED, True),
            (ErrorCode.AUTH_FAILED, False),
            (ErrorCode.INVALID_REQUEST, False),
            (ErrorCode.NOT_SUPPORTED, False),
            (ErrorCode.REFUSED, False),
            (ErrorCode.CANCELLED, False),
        ],
    )
    def test_retryability_is_declared_per_code(
        self, code: ErrorCode, retryable: bool
    ) -> None:
        assert ProviderError(code, "x").is_retryable is retryable

    def test_every_error_code_has_a_retryability(self) -> None:
        for code in ErrorCode:
            assert isinstance(ProviderError(code, "x").is_retryable, bool)

    def test_the_vendor_cause_is_a_string_not_an_object(self) -> None:
        """A vendor exception object must never cross the boundary."""
        err = ProviderError(ErrorCode.INTERNAL, "boom", cause=str(ValueError("vendor")))
        assert isinstance(err.detail["cause"], str)


class TestCapabilitySet:
    """A declared capability must work; an undeclared one must raise."""

    def test_has_reports_declared_features(self) -> None:
        caps = CapabilitySet(features=frozenset({Feature.STREAMING}))
        assert caps.has(Feature.STREAMING)
        assert not caps.has(Feature.VISION)

    def test_require_passes_for_declared_features(self) -> None:
        CapabilitySet(features=frozenset({Feature.STREAMING})).require(Feature.STREAMING)

    def test_require_raises_not_supported(self) -> None:
        caps = CapabilitySet(features=frozenset({Feature.STREAMING}))
        with pytest.raises(ProviderError) as excinfo:
            caps.require(Feature.VISION, Feature.PROMPT_CACHING, provider_id="model.x")
        assert excinfo.value.error_code is ErrorCode.NOT_SUPPORTED
        assert "prompt_caching" in str(excinfo.value)
        assert "vision" in str(excinfo.value)

    def test_limits_have_defaults(self) -> None:
        caps = CapabilitySet(limits={"max_context_tokens": 1000})
        assert caps.limit("max_context_tokens") == 1000
        assert caps.limit("unknown", 42) == 42

    def test_variants_are_queryable(self) -> None:
        caps = CapabilitySet(variants={"effort": frozenset({"low", "high"})})
        assert caps.supports_variant("effort", "low")
        assert not caps.supports_variant("effort", "max")
        assert not caps.supports_variant("missing", "low")

    def test_an_empty_set_claims_nothing(self) -> None:
        caps = CapabilitySet()
        assert not any(caps.has(f) for f in Feature)


class TestCancelToken:
    """Cancellation is cooperative and observable."""

    def test_starts_uncancelled(self) -> None:
        assert CancelToken().cancelled is False

    def test_cancel_is_observable(self) -> None:
        token = CancelToken()
        token.cancel()
        assert token.cancelled

    def test_raise_if_cancelled_is_quiet_until_cancelled(self) -> None:
        token = CancelToken()
        token.raise_if_cancelled()
        token.cancel()
        with pytest.raises(ProviderError) as excinfo:
            token.raise_if_cancelled(provider_id="model.x")
        assert excinfo.value.error_code is ErrorCode.CANCELLED


class TestNeutralTypes:
    """The vocabulary that crosses port boundaries."""

    def test_message_helpers_build_text_turns(self) -> None:
        assert Message.user("hi").role is Role.USER
        assert Message.assistant("yo").text == "yo"

    def test_message_text_concatenates_text_blocks_only(self) -> None:
        message = Message(
            role=Role.ASSISTANT,
            content=(TextBlock("a"), ReasoningBlock("ignored"), TextBlock("b")),
        )
        assert message.text == "ab"

    def test_response_exposes_text_and_tool_calls(self) -> None:
        response = CompletionResponse(
            content=(TextBlock("hello"), ToolCallBlock(id="1", name="read")),
            stop_reason=StopReason.TOOL_CALL,
        )
        assert response.text == "hello"
        assert len(response.tool_calls) == 1
        assert response.tool_calls[0].name == "read"
        assert not response.refused

    def test_refusal_is_a_result_not_an_exception(self) -> None:
        """A policy decline is a normal response, per docs/030 §5.1 M-1."""
        response = CompletionResponse(content=(), stop_reason=StopReason.REFUSED)
        assert response.refused
        assert response.text == ""

    def test_usage_totals(self) -> None:
        assert Usage(input_tokens=10, output_tokens=5).total_tokens == 15

    def test_usage_cost_defaults_to_none_not_zero(self) -> None:
        """Reporting 0.0 would understate an unpriced run silently."""
        assert Usage(input_tokens=10).cost_usd is None

    def test_blocks_render_to_plain_dicts(self) -> None:
        rendered = blocks_to_dicts(
            (
                TextBlock("t"),
                ReasoningBlock("r"),
                ToolCallBlock(id="1", name="n", arguments={"a": 1}),
                ToolResultBlock(call_id="1", content="ok"),
            )
        )
        assert [d["type"] for d in rendered] == [
            "text",
            "reasoning",
            "tool_call",
            "tool_result",
        ]


class TestCompletionRequest:
    """The neutral request encodes constraints that would otherwise be 400s."""

    def test_a_minimal_request_is_valid(self) -> None:
        request = simple_request()
        assert request.model == "claude-opus-5"
        assert request.reasoning is ThinkingMode.ADAPTIVE
        assert request.effort is Effort.HIGH

    def test_sampling_parameters_have_no_home(self) -> None:
        """temperature/top_p/top_k are deliberately absent (docs/030 §5.1 M-3)."""
        for forbidden in ("temperature", "top_p", "top_k"):
            assert not hasattr(simple_request(), forbidden)
        with pytest.raises(TypeError):
            CompletionRequest(  # type: ignore[call-arg]
                model="m", messages=(Message.user("x"),), temperature=0.7
            )

    def test_empty_model_is_rejected(self) -> None:
        with pytest.raises(ProviderError) as excinfo:
            CompletionRequest(model="", messages=(Message.user("x"),))
        assert excinfo.value.error_code is ErrorCode.INVALID_REQUEST

    def test_empty_messages_are_rejected(self) -> None:
        with pytest.raises(ProviderError, match="must not be empty"):
            CompletionRequest(model="m", messages=())

    def test_non_positive_max_output_is_rejected(self) -> None:
        with pytest.raises(ProviderError, match="max_output_tokens"):
            CompletionRequest(
                model="m", messages=(Message.user("x"),), max_output_tokens=0
            )

    def test_assistant_prefill_is_rejected(self) -> None:
        """Trailing assistant turns are 400s on several backends."""
        with pytest.raises(ProviderError, match="assistant prefill"):
            CompletionRequest(
                model="m",
                messages=(Message.user("x"), Message.assistant("partial")),
            )

    def test_system_text_joins_blocks(self) -> None:
        request = simple_request(system=(TextBlock("a"), TextBlock("b")))
        assert request.system_text == "a\n\nb"

    def test_request_is_immutable(self) -> None:
        with pytest.raises(AttributeError):
            simple_request().model = "other"  # type: ignore[misc]


class TestHelpers:
    """Shared helpers used by implementations."""

    def test_token_estimate_grows_with_length(self) -> None:
        assert estimate_tokens("a" * 400) > estimate_tokens("a" * 4)

    def test_token_estimate_is_at_least_one(self) -> None:
        assert estimate_tokens("") == 1

    def test_request_text_includes_system_and_messages(self) -> None:
        request = simple_request()
        flattened = request_text(request)
        assert "you are helpful" in flattened
        assert "hello" in flattened

    def test_request_text_includes_tool_results(self) -> None:
        request = simple_request(
            messages=(
                Message(
                    role=Role.USER,
                    content=(ToolResultBlock(call_id="1", content="tool output"),),
                ),
            )
        )
        assert "tool output" in request_text(request)


def test_domain_covers_every_documented_port() -> None:
    """docs/030 §5 defines ten domains; all must be expressible."""
    assert {d.value for d in Domain} == {
        "model",
        "agent",
        "build",
        "shell",
        "fs",
        "vcs",
        "forge",
        "browser",
        "secrets",
        "telemetry",
    }

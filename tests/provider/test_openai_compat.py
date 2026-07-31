"""Tests for the OpenAI-compatible provider (Ollama, Open WebUI, vLLM)."""

from __future__ import annotations

import json

import pytest

from orchestrator.core.config import Effort
from orchestrator.provider.base import (
    Domain,
    ErrorCode,
    Feature,
    Message,
    ProviderError,
    Role,
    StopReason,
    StreamEventKind,
    ToolCallBlock,
    ToolResultBlock,
    ToolSpec,
)
from orchestrator.provider.openai_compat import OpenAICompatConfig, OpenAICompatProvider

from tests.provider.conftest import FakeTransport, collect, run, simple_request


@pytest.fixture
def config() -> OpenAICompatConfig:
    """A local Ollama-style endpoint."""
    return OpenAICompatConfig(model="llama-local", base_url="http://localhost:11434/v1")


@pytest.fixture
def provider(
    config: OpenAICompatConfig, transport: FakeTransport
) -> OpenAICompatProvider:
    """A started provider backed by a scripted transport."""
    instance = OpenAICompatProvider(config, transport=transport)
    run(instance.startup())
    return instance


class TestConfig:
    """Deployment settings are validated at construction."""

    def test_empty_model_is_rejected(self) -> None:
        with pytest.raises(ProviderError, match="model must not be empty"):
            OpenAICompatConfig(model="")

    def test_empty_base_url_is_rejected(self) -> None:
        with pytest.raises(ProviderError, match="base_url"):
            OpenAICompatConfig(model="m", base_url="")

    @pytest.mark.parametrize("kwargs", [{"context_tokens": 0}, {"max_output_tokens": -1}])
    def test_non_positive_limits_are_rejected(self, kwargs: dict[str, int]) -> None:
        with pytest.raises(ProviderError, match="positive"):
            OpenAICompatConfig(model="m", **kwargs)


class TestLifecycle:
    """A provider with no transport cannot pretend to be ready."""

    def test_startup_without_a_transport_is_refused(
        self, config: OpenAICompatConfig
    ) -> None:
        instance = OpenAICompatProvider(config)
        with pytest.raises(ProviderError) as excinfo:
            run(instance.startup())
        assert excinfo.value.error_code is ErrorCode.UNAVAILABLE

    def test_health_reports_the_endpoint(self, provider: OpenAICompatProvider) -> None:
        report = run(provider.health())
        assert report.healthy
        assert "11434" in report.detail

    def test_domain_and_id(self, provider: OpenAICompatProvider) -> None:
        assert provider.domain is Domain.MODEL
        assert provider.id == "model.openai_compat"


class TestCapabilities:
    """What is absent matters as much as what is present."""

    def test_streaming_and_tools_are_declared(
        self, provider: OpenAICompatProvider
    ) -> None:
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
            Feature.TOKEN_COUNTING,
            Feature.SERVER_FALLBACK,
        ],
    )
    def test_unavailable_capabilities_are_not_claimed(
        self, provider: OpenAICompatProvider, absent: Feature
    ) -> None:
        """Declaring one of these would be a lie the caller pays for later."""
        assert not provider.capabilities().has(absent)

    def test_requiring_an_absent_capability_raises(
        self, provider: OpenAICompatProvider
    ) -> None:
        with pytest.raises(ProviderError) as excinfo:
            provider.capabilities().require(Feature.EFFORT_LEVELS)
        assert excinfo.value.error_code is ErrorCode.NOT_SUPPORTED

    def test_tools_can_be_disabled_per_deployment(
        self, transport: FakeTransport
    ) -> None:
        instance = OpenAICompatProvider(
            OpenAICompatConfig(model="m", supports_tools=False), transport=transport
        )
        assert not instance.capabilities().has(Feature.TOOL_CALLING)

    def test_limits_come_from_configuration(self, transport: FakeTransport) -> None:
        """A deployment's limits are the operator's to declare, not ours to guess."""
        instance = OpenAICompatProvider(
            OpenAICompatConfig(model="m", context_tokens=32_768), transport=transport
        )
        assert instance.capabilities().limit("max_context_tokens") == 32_768


class TestTranslation:
    """Neutral requests become chat-completions payloads."""

    def test_system_becomes_a_system_message(
        self, provider: OpenAICompatProvider
    ) -> None:
        payload = provider.build_payload(simple_request(model="llama-local"))
        assert payload["messages"][0]["role"] == "system"
        assert payload["messages"][0]["content"] == "you are helpful"
        assert payload["messages"][1]["role"] == "user"

    def test_effort_is_dropped_not_approximated(
        self, provider: OpenAICompatProvider
    ) -> None:
        """No equivalent exists here; inventing one would change behaviour."""
        payload = provider.build_payload(
            simple_request(model="llama-local", effort=Effort.MAX)
        )
        assert "effort" not in payload
        assert "output_config" not in payload
        assert "thinking" not in payload

    def test_sampling_parameters_are_absent(
        self, provider: OpenAICompatProvider
    ) -> None:
        payload = provider.build_payload(simple_request(model="llama-local"))
        for forbidden in ("temperature", "top_p", "top_k"):
            assert forbidden not in payload

    def test_max_tokens_is_capped_by_the_deployment(
        self, provider: OpenAICompatProvider
    ) -> None:
        payload = provider.build_payload(
            simple_request(model="llama-local", max_output_tokens=1_000_000)
        )
        assert payload["max_tokens"] == 4_096

    def test_tools_are_translated_to_functions(
        self, provider: OpenAICompatProvider
    ) -> None:
        payload = provider.build_payload(
            simple_request(
                model="llama-local",
                tools=(ToolSpec(name="read", description="Read", schema={"type": "object"}),),
            )
        )
        assert payload["tools"][0]["type"] == "function"
        assert payload["tools"][0]["function"]["name"] == "read"

    def test_tools_on_a_toolless_deployment_are_refused(
        self, transport: FakeTransport
    ) -> None:
        instance = OpenAICompatProvider(
            OpenAICompatConfig(model="m", supports_tools=False), transport=transport
        )
        with pytest.raises(ProviderError) as excinfo:
            instance.build_payload(
                simple_request(model="m", tools=(ToolSpec(name="t", description="d"),))
            )
        assert excinfo.value.error_code is ErrorCode.NOT_SUPPORTED

    def test_tool_results_use_the_tool_role(
        self, provider: OpenAICompatProvider
    ) -> None:
        payload = provider.build_payload(
            simple_request(
                model="llama-local",
                messages=(
                    Message(
                        role=Role.USER,
                        content=(ToolResultBlock(call_id="c1", content="out"),),
                    ),
                ),
            )
        )
        tool_message = payload["messages"][-1]
        assert tool_message["role"] == "tool"
        assert tool_message["tool_call_id"] == "c1"

    def test_assistant_tool_calls_are_serialized_as_json_strings(
        self, provider: OpenAICompatProvider
    ) -> None:
        payload = provider.build_payload(
            simple_request(
                model="llama-local",
                messages=(
                    Message.user("go"),
                    Message(
                        role=Role.ASSISTANT,
                        content=(ToolCallBlock(id="c1", name="read", arguments={"p": 1}),),
                    ),
                    Message.user("continue"),
                ),
            )
        )
        assistant = next(m for m in payload["messages"] if m.get("tool_calls"))
        assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"p": 1}

    def test_structured_output_is_translated(
        self, provider: OpenAICompatProvider
    ) -> None:
        payload = provider.build_payload(
            simple_request(model="llama-local", output_schema={"type": "object"})
        )
        assert payload["response_format"]["type"] == "json_schema"


class TestResponseParsing:
    """Replies become neutral responses."""

    def test_text_is_extracted(self, provider: OpenAICompatProvider) -> None:
        response = provider.parse_response(
            {
                "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
                "model": "llama-local",
            }
        )
        assert response.text == "hi"
        assert response.stop_reason is StopReason.END
        assert response.usage.input_tokens == 5

    def test_cost_is_never_invented_for_a_self_hosted_model(
        self, provider: OpenAICompatProvider
    ) -> None:
        response = provider.parse_response(
            {
                "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 500, "completion_tokens": 500},
            }
        )
        assert response.usage.cost_usd is None

    def test_tool_calls_are_decoded(self, provider: OpenAICompatProvider) -> None:
        response = provider.parse_response(
            {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "function": {
                                        "name": "read",
                                        "arguments": '{"path": "a.py"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        )
        assert response.stop_reason is StopReason.TOOL_CALL
        assert response.tool_calls[0].arguments == {"path": "a.py"}

    def test_malformed_tool_arguments_do_not_crash_the_turn(
        self, provider: OpenAICompatProvider
    ) -> None:
        """One bad call should be a retryable call, not a crashed response."""
        response = provider.parse_response(
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {"id": "c1", "function": {"name": "r", "arguments": "{oops"}}
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        )
        assert response.tool_calls[0].arguments == {}

    def test_a_reply_with_no_choices_is_an_error(
        self, provider: OpenAICompatProvider
    ) -> None:
        with pytest.raises(ProviderError) as excinfo:
            provider.parse_response({"choices": []})
        assert excinfo.value.error_code is ErrorCode.INTERNAL

    @pytest.mark.parametrize(
        ("wire", "expected"),
        [
            ("stop", StopReason.END),
            ("length", StopReason.MAX_TOKENS),
            ("tool_calls", StopReason.TOOL_CALL),
            ("content_filter", StopReason.REFUSED),
            ("unheard_of", StopReason.END),
        ],
    )
    def test_finish_reasons_map(
        self, provider: OpenAICompatProvider, wire: str, expected: StopReason
    ) -> None:
        parsed = provider.parse_response(
            {"choices": [{"message": {"content": ""}, "finish_reason": wire}]}
        )
        assert parsed.stop_reason is expected


class TestCalls:
    """The port methods drive the transport."""

    def test_complete_round_trips(
        self, config: OpenAICompatConfig, transport: FakeTransport
    ) -> None:
        transport.response = {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]
        }
        instance = OpenAICompatProvider(config, transport=transport)
        run(instance.startup())
        assert run(instance.complete(simple_request(model="llama-local"))).text == "ok"

    def test_a_backend_exception_never_escapes(
        self, config: OpenAICompatConfig
    ) -> None:
        transport = FakeTransport(error=RuntimeError("connection reset"))
        instance = OpenAICompatProvider(config, transport=transport)
        run(instance.startup())
        with pytest.raises(ProviderError) as excinfo:
            run(instance.complete(simple_request(model="llama-local")))
        assert excinfo.value.error_code is ErrorCode.INTERNAL

    def test_streaming_yields_deltas(self, config: OpenAICompatConfig) -> None:
        transport = FakeTransport(
            chunks=[
                {"choices": [{"delta": {"content": "he"}}]},
                {"choices": [{"delta": {"content": "llo"}}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            ]
        )
        instance = OpenAICompatProvider(config, transport=transport)
        run(instance.startup())

        events = collect(instance.stream(simple_request(model="llama-local")))
        assert "".join(e.text for e in events if e.kind is StreamEventKind.TEXT) == "hello"
        assert events[-1].kind is StreamEventKind.DONE

    def test_count_tokens_returns_an_estimate(
        self, provider: OpenAICompatProvider
    ) -> None:
        assert run(provider.count_tokens(simple_request(model="llama-local"))) > 0

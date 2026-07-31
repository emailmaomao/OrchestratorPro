"""Tests for the CLI-backed Claude Code provider.

No test here invokes a real ``claude`` binary. Everything goes through the
scripted :class:`CliRunner` seam — including the not-installed and timeout
paths — except one env-gated live test at the bottom and the kill-path test,
which runs a real *python* child because killing a process is exactly the
thing a script cannot prove.
"""

from __future__ import annotations

import os
import sys
import time
import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from orchestrator.core.config import OrchestratorConfig
from orchestrator.provider.base import (
    Domain,
    ErrorCode,
    Feature,
    Message,
    ProviderError,
    StopReason,
)
from orchestrator.provider.claude_cli import (
    ClaudeCliConfig,
    ClaudeCliProvider,
    CliResult,
    SubprocessCliRunner,
)
from orchestrator.provider.registry import build_default_registry, settings_for
from tests.provider.conftest import run, simple_request

#: The live test needs an installed, signed-in CLI and an explicit opt-in.
requires_claude_cli = pytest.mark.skipif(
    not os.environ.get("ORCHESTRATORPRO_TEST_CLAUDE_CLI"),
    reason=(
        "no live CLI opted in; set ORCHESTRATORPRO_TEST_CLAUDE_CLI=1 with "
        "Claude Code installed and signed in to run it"
    ),
)


class ScriptedCliRunner:
    """A scripted runner. Records argv; returns or raises what it was told."""

    def __init__(
        self,
        result: CliResult | None = None,
        *,
        error: BaseException | None = None,
        version: CliResult | None = None,
    ) -> None:
        """Script the runner: a result or error for ``-p``, a version reply."""
        self.result = result or CliResult(exit_code=0, stdout="ok", stderr="")
        self.error = error
        self.version = version or CliResult(exit_code=0, stdout="2.1.220", stderr="")
        self.calls: list[list[str]] = []

    async def run(self, argv: Sequence[str], *, timeout_s: float) -> CliResult:
        """Record the invocation and play the script."""
        self.calls.append(list(argv))
        if self.error is not None:
            raise self.error
        if "--version" in argv:
            return self.version
        return self.result

    @property
    def last(self) -> list[str]:
        """The most recent argv."""
        assert self.calls, "the runner was never invoked"
        return self.calls[-1]


def started(provider: ClaudeCliProvider) -> ClaudeCliProvider:
    """Start a provider and return it."""
    run(provider.startup())
    return provider


class TestTranslation:
    """Neutral request → argv, the most valuable thing to test."""

    def test_the_prompt_is_one_argument(self) -> None:
        """The whole prompt lands as a single ``-p`` value, never on stdin.

        The Windows ``claude.cmd`` shim swallows piped stdin and hangs — a
        lesson already paid for once in production.
        """
        runner = ScriptedCliRunner(CliResult(0, "hello", ""))
        provider = started(ClaudeCliProvider(runner=runner))

        run(provider.complete(simple_request()))

        argv = runner.last
        # Resolved to a launchable path where the CLI is installed, and left
        # as the bare name where it is not — `Popen(["claude"])` fails on
        # Windows, where npm installs it as claude.CMD.
        assert "claude" in argv[0].lower()
        assert argv[1] == "-p"
        prompt = argv[2]
        assert "you are helpful" in prompt and "hello" in prompt
        # JSON since OP-014: the envelope is where measured usage lives.
        assert argv[3:5] == ["--output-format", "json"]

    def test_no_model_flag_unless_configured(self) -> None:
        """The CLI's own default model is authoritative under a subscription."""
        runner = ScriptedCliRunner()
        provider = started(ClaudeCliProvider(runner=runner))
        run(provider.complete(simple_request()))
        assert "--model" not in runner.last

    def test_a_configured_model_is_passed(self) -> None:
        """An explicitly configured model rides along as ``--model``."""
        runner = ScriptedCliRunner()
        provider = started(
            ClaudeCliProvider(ClaudeCliConfig(model="opus"), runner=runner)
        )
        run(provider.complete(simple_request()))
        assert runner.last[-2:] == ["--model", "opus"]

    def test_the_answer_becomes_a_text_response(self) -> None:
        """Standard output maps to one text block, END, and unpriced usage."""
        runner = ScriptedCliRunner(CliResult(0, "the config\n", ""))
        provider = started(ClaudeCliProvider(runner=runner))

        response = run(provider.complete(simple_request()))

        assert response.text == "the config"
        assert response.stop_reason is StopReason.END
        assert response.usage.cost_usd is None, "an unpriced call must not read $0.00"
        assert response.usage.input_tokens > 0
        assert response.usage.output_tokens > 0


class TestErrorMapping:
    """Every backend failure lands on the closed taxonomy."""

    def test_a_missing_executable_fails_startup_as_unavailable(self) -> None:
        """Fail at startup, where it costs a second, not at an attempt."""
        runner = ScriptedCliRunner(error=FileNotFoundError("claude"))
        provider = ClaudeCliProvider(runner=runner)
        with pytest.raises(ProviderError) as excinfo:
            run(provider.startup())
        assert excinfo.value.error_code is ErrorCode.UNAVAILABLE
        assert "npm install" in str(excinfo.value)

    def test_a_failing_version_probe_is_unavailable(self) -> None:
        """A probe that exits nonzero means the backend is not usable."""
        runner = ScriptedCliRunner(version=CliResult(1, "", "boom"))
        with pytest.raises(ProviderError) as excinfo:
            run(ClaudeCliProvider(runner=runner).startup())
        assert excinfo.value.error_code is ErrorCode.UNAVAILABLE

    def test_an_expired_login_is_auth_failed_and_final(self) -> None:
        """Retrying an expired session burns wall-clock for nothing."""
        runner = ScriptedCliRunner(
            CliResult(1, "", "Failed to authenticate: OAuth session expired")
        )
        provider = started(ClaudeCliProvider(runner=runner))
        with pytest.raises(ProviderError) as excinfo:
            run(provider.complete(simple_request()))
        assert excinfo.value.error_code is ErrorCode.AUTH_FAILED
        assert not excinfo.value.is_retryable
        assert "/login" in str(excinfo.value)

    def test_a_timeout_is_retryable(self) -> None:
        """Overrunning is transient; the taxonomy says try again."""
        runner = ScriptedCliRunner(CliResult(-1, "", "", timed_out=True))
        provider = started(ClaudeCliProvider(runner=runner))
        with pytest.raises(ProviderError) as excinfo:
            run(provider.complete(simple_request()))
        assert excinfo.value.error_code is ErrorCode.TIMEOUT
        assert excinfo.value.is_retryable

    def test_any_other_failure_is_internal_with_stderr_as_cause(self) -> None:
        """An unclassifiable failure is INTERNAL, never guessed transient."""
        runner = ScriptedCliRunner(CliResult(2, "", "something broke"))
        provider = started(ClaudeCliProvider(runner=runner))
        with pytest.raises(ProviderError) as excinfo:
            run(provider.complete(simple_request()))
        assert excinfo.value.error_code is ErrorCode.INTERNAL
        assert excinfo.value.cause_text == "something broke"


class TestCapabilityHonesty:
    """Nothing is declared that the CLI cannot deliver (docs/030 §4)."""

    def test_no_optional_feature_is_declared(self) -> None:
        """The whole honesty contract in one loop."""
        capabilities = ClaudeCliProvider().capabilities()
        for feature in (
            Feature.STREAMING,
            Feature.TOOL_CALLING,
            Feature.TOKEN_COUNTING,
            Feature.COST_REPORTING,
            Feature.PROMPT_CACHING,
            Feature.REFUSAL_SEMANTICS,
        ):
            assert not capabilities.has(feature), feature

    def test_usage_reporting_is_declared_but_token_counting_is_not(self) -> None:
        """The distinction OP-014 turns on, pinned so it cannot blur.

        The envelope measures what a call *consumed*. ``TOKEN_COUNTING``
        promises ``count_tokens`` is exact — a pre-flight answer given without
        running anything, which the CLI cannot produce at any price. Declaring
        it because measured usage arrived would be a promise about a different
        method entirely.
        """
        capabilities = ClaudeCliProvider().capabilities()
        assert capabilities.has(Feature.USAGE_REPORTING)
        assert not capabilities.has(Feature.TOKEN_COUNTING)

    def test_count_tokens_is_still_an_estimate(self) -> None:
        """And behaves like one, rather than quietly running the CLI."""
        runner = ScriptedCliRunner()
        provider = started(ClaudeCliProvider(runner=runner))
        before = len(runner.calls)
        assert run(provider.count_tokens(simple_request())) > 0
        assert len(runner.calls) == before, "counting must not spend a call"


class TestMeasuredUsage:
    """OP-014: the envelope carries real numbers; use them, labelled as real."""

    @staticmethod
    def _envelope(**overrides: object) -> str:
        body: dict[str, object] = {
            "type": "result",
            "is_error": False,
            "result": "the answer",
            "total_cost_usd": 0.14103,
            "usage": {
                "input_tokens": 2,
                "cache_creation_input_tokens": 5967,
                "cache_read_input_tokens": 20570,
                "output_tokens": 22,
            },
        }
        body.update(overrides)
        return json.dumps(body)

    def test_measured_usage_is_not_labelled_estimated(self) -> None:
        runner = ScriptedCliRunner(
            CliResult(exit_code=0, stdout=self._envelope(), stderr="")
        )
        response = run(started(ClaudeCliProvider(runner=runner)).complete(simple_request()))

        assert response.usage.tokens_estimated is False
        assert response.usage.output_tokens == 22
        assert response.usage.input_tokens == 2 + 5967 + 20570
        assert response.usage.cached_input_tokens == 5967 + 20570
        assert response.text == "the answer"

    def test_the_dollar_figure_is_notional_never_a_charge(self) -> None:
        runner = ScriptedCliRunner(
            CliResult(exit_code=0, stdout=self._envelope(), stderr="")
        )
        response = run(started(ClaudeCliProvider(runner=runner)).complete(simple_request()))

        assert response.usage.cost_usd is None, "a subscription call is not a charge"
        assert response.usage.notional_cost_usd == 0.14103

    def test_a_missing_usage_block_degrades_to_estimates(self) -> None:
        runner = ScriptedCliRunner(
            CliResult(
                exit_code=0,
                stdout=json.dumps({"result": "still an answer", "is_error": False}),
                stderr="",
            )
        )
        response = run(started(ClaudeCliProvider(runner=runner)).complete(simple_request()))

        assert response.text == "still an answer"
        assert response.usage.tokens_estimated is True
        # Absence is never reported as zero: zero is a measurement, and false.
        assert response.usage.input_tokens > 0
        assert response.usage.output_tokens > 0

    def test_an_unreadable_envelope_degrades_to_estimates(self) -> None:
        runner = ScriptedCliRunner(
            CliResult(exit_code=0, stdout='{"result": "trunca', stderr="")
        )
        response = run(started(ClaudeCliProvider(runner=runner)).complete(simple_request()))

        assert response.usage.tokens_estimated is True
        assert response.text == '{"result": "trunca'

    def test_requiring_streaming_raises_not_supported(self) -> None:
        """``require()`` fails loudly up front, not at minute forty."""
        with pytest.raises(ProviderError) as excinfo:
            ClaudeCliProvider().capabilities().require(Feature.STREAMING)
        assert excinfo.value.error_code is ErrorCode.NOT_SUPPORTED

    def test_stream_refuses_rather_than_emulating(self) -> None:
        """One fake chunk would 'work' until a caller relied on increments."""
        provider = started(ClaudeCliProvider(runner=ScriptedCliRunner()))

        async def drain() -> None:
            async for _ in provider.stream(simple_request()):
                pass

        with pytest.raises(ProviderError) as excinfo:
            run(drain())
        assert excinfo.value.error_code is ErrorCode.NOT_SUPPORTED

    def test_count_tokens_is_an_estimate_that_grows_with_input(self) -> None:
        """Monotonic in input size — the property an estimate must keep."""
        provider = ClaudeCliProvider()
        small = run(provider.count_tokens(simple_request()))
        large = run(
            provider.count_tokens(
                simple_request(messages=(Message.user("long words " * 200),))
            )
        )
        assert 0 < small < large


class TestLifecycle:
    """The substrate obligations: startup, health, idempotent shutdown."""

    def test_health_reflects_the_probe(self) -> None:
        """Unhealthy before startup; the probed version string afterwards."""
        provider = ClaudeCliProvider(runner=ScriptedCliRunner())
        assert not run(provider.health()).healthy
        run(provider.startup())
        report = run(provider.health())
        assert report.healthy and report.detail == "2.1.220"

    def test_shutdown_is_idempotent(self) -> None:
        """Twice is fine; the substrate demands it (docs/030 §2.3)."""
        provider = started(ClaudeCliProvider(runner=ScriptedCliRunner()))
        run(provider.shutdown())
        run(provider.shutdown())
        assert not run(provider.health()).healthy

    def test_config_rejects_nonsense(self) -> None:
        """An empty executable or a zero timeout fails at construction."""
        with pytest.raises(ProviderError):
            ClaudeCliConfig(executable="")
        with pytest.raises(ProviderError):
            ClaudeCliConfig(timeout_s=0)


class TestRegistryIntegration:
    """`claude_cli` is selectable purely by configuration — and not default."""

    def test_config_selects_claude_cli(self) -> None:
        """The OP-002 regression pattern, applied to the new name."""
        config = OrchestratorConfig.from_mapping(
            {
                "provider": {
                    "claude_cli": {"executable": "my-claude", "timeout_s": 120.0}
                }
            },
            env={},
        )
        registry = build_default_registry(
            config, transports={"claude_cli": ScriptedCliRunner()}
        )

        assert registry.bound(Domain.MODEL) == "claude_cli"
        provider = registry.get(Domain.MODEL)
        assert isinstance(provider, ClaudeCliProvider)
        assert provider.config.executable == "my-claude"
        assert provider.config.timeout_s == 120.0
        assert settings_for(config, "claude_cli").executable == "my-claude"

    def test_the_block_model_is_not_forwarded(self) -> None:
        """Forwarding the API default would override the operator's CLI model."""
        config = OrchestratorConfig.from_mapping(
            {"provider": {"claude_cli": {"executable": "claude"}}}, env={}
        )
        registry = build_default_registry(
            config, transports={"claude_cli": ScriptedCliRunner()}
        )
        provider = registry.get(Domain.MODEL)
        assert isinstance(provider, ClaudeCliProvider)
        assert provider.config.model == ""

    def test_claude_cli_is_not_the_default(self) -> None:
        """An unconfigured installation still binds the API provider."""
        assert build_default_registry().bound(Domain.MODEL) == "anthropic"

    def test_anthropic_wins_when_both_are_configured(self) -> None:
        """Deterministic preference: the API block outranks the CLI block."""
        config = OrchestratorConfig.from_mapping(
            {
                "provider": {
                    "claude_cli": {"executable": "claude"},
                    "anthropic": {"model": "m"},
                }
            },
            env={},
        )
        assert build_default_registry(config).bound(Domain.MODEL) == "anthropic"


class TestKillPath:
    """A hung CLI is killed on timeout — proven against a real process.

    The child writes a start-file immediately, then sleeps, then writes a
    survived-file. If the kill worked, the survived-file never appears even
    after the child's natural runtime has elapsed. Asserting only that the
    call returned would pass with an orphan still running.
    """

    def test_a_hung_process_is_actually_terminated(self, tmp_dir: Path) -> None:
        """The survived-file never appears once the group has been killed."""
        started_file = tmp_dir / "started"
        survived_file = tmp_dir / "survived"
        child = (
            "import pathlib, time, sys; "
            f"pathlib.Path(r'{started_file}').write_text('x'); "
            "time.sleep(1.2); "
            f"pathlib.Path(r'{survived_file}').write_text('x')"
        )

        result = run(
            SubprocessCliRunner().run(
                [sys.executable, "-c", child], timeout_s=0.3
            )
        )

        assert result.timed_out
        assert started_file.exists(), "the child never even started"
        # Wait out the child's full natural runtime: if it survived the kill,
        # the survived-file appears in this window and the assertion fails.
        time.sleep(1.4)
        assert not survived_file.exists(), "the process outlived its kill"

    def test_a_missing_executable_raises_file_not_found(self, tmp_dir: Path) -> None:
        """The runner surfaces absence for startup to map to UNAVAILABLE."""
        with pytest.raises(FileNotFoundError):
            run(
                SubprocessCliRunner().run(
                    [str(tmp_dir / "no-such-binary")], timeout_s=5.0
                )
            )


@pytest.mark.live
@requires_claude_cli
class TestLive:
    """One real invocation, opt-in only. Never required for a milestone."""

    def test_a_real_one_shot_completion(self) -> None:
        """A deliberately tiny prompt; costs one real subscription call."""
        provider = ClaudeCliProvider(ClaudeCliConfig(timeout_s=120.0))
        run(provider.startup())
        response = run(
            provider.complete(
                simple_request(
                    messages=(Message.user("Reply with the single word: pong"),)
                )
            )
        )
        assert "pong" in response.text.lower()


class TestEstimatedUsage:
    """Estimates travel labelled as estimates, never as measurements."""

    def test_usage_is_flagged_estimated(self) -> None:
        """The CLI cannot count, so its usage says so."""
        runner = ScriptedCliRunner(CliResult(0, "answer", ""))
        provider = started(ClaudeCliProvider(runner=runner))
        response = run(provider.complete(simple_request()))
        assert response.usage.tokens_estimated is True

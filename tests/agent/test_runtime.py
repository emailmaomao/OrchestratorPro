"""Tests for the agent runtime and its tool-execution loop."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from orchestrator.agent.lifecycle import AgentState, StateChange
from orchestrator.agent.memory import ContextManager, TranscriptEntry
from orchestrator.agent.model import AttemptStatus, BudgetLedger, TaskSpec
from orchestrator.agent.prompt import PromptBuilder
from orchestrator.agent.runtime import AgentRuntime, RuntimeConfig
from orchestrator.agent.tools import ToolContext, ToolRegistry, default_registry
from orchestrator.core.events import Budget, TaskId
from orchestrator.provider.base import ErrorCode, ProviderError, Usage

from tests.agent.conftest import (
    FakeClock,
    FakeProvider,
    finish_turn,
    refusal_turn,
    run,
    text_turn,
    tool_turn,
)


def runtime_for(
    provider: FakeProvider,
    *,
    registry: ToolRegistry | None = None,
    max_iterations: int = 25,
    context_limit: int | None = None,
    **kwargs: object,
) -> AgentRuntime:
    """Build a runtime over a scripted provider."""
    return AgentRuntime(
        provider,
        config=RuntimeConfig(
            model="fake-model",
            max_iterations=max_iterations,
            context_limit=context_limit,
        ),
        registry=registry or default_registry(),
        **kwargs,  # type: ignore[arg-type]
    )


def ledger_for(budget: Budget) -> BudgetLedger:
    """A ledger with a controllable clock."""
    return BudgetLedger(budget, clock=FakeClock())


class TestConfig:
    """Runtime configuration is validated where it is declared."""

    def test_an_empty_model_is_refused(self) -> None:
        with pytest.raises(ValueError, match="model"):
            RuntimeConfig(model="")

    def test_a_non_positive_iteration_cap_is_refused(self) -> None:
        with pytest.raises(ValueError, match="max_iterations"):
            RuntimeConfig(model="m", max_iterations=0)


class TestHappyPath:
    """A turn, a tool, a finish."""

    def test_finishing_immediately_succeeds(
        self, spec: TaskSpec, tool_ctx: ToolContext, budget: Budget
    ) -> None:
        provider = FakeProvider([finish_turn("nothing needed")])
        result = run(runtime_for(provider).run(spec, tool_ctx, ledger_for(budget)))

        assert result.status is AttemptStatus.SUCCEEDED
        assert result.succeeded
        assert result.summary == "nothing needed"

    def test_a_write_then_finish(
        self, spec: TaskSpec, tool_ctx: ToolContext, budget: Budget
    ) -> None:
        provider = FakeProvider(
            [
                tool_turn(
                    ("c1", "write_file", {"path": "greeting.txt", "content": "hello\n"})
                ),
                finish_turn("created greeting.txt"),
            ]
        )
        result = run(runtime_for(provider).run(spec, tool_ctx, ledger_for(budget)))

        assert result.status is AttemptStatus.SUCCEEDED
        assert result.changed_files == ("greeting.txt",)
        assert (tool_ctx.workspace_root / "greeting.txt").read_text(encoding="utf-8") == "hello\n"

    def test_several_tool_calls_in_one_turn(
        self, spec: TaskSpec, tool_ctx: ToolContext, budget: Budget
    ) -> None:
        provider = FakeProvider(
            [
                tool_turn(
                    ("c1", "write_file", {"path": "a.txt", "content": "a"}),
                    ("c2", "write_file", {"path": "b.txt", "content": "b"}),
                ),
                finish_turn("wrote both"),
            ]
        )
        result = run(runtime_for(provider).run(spec, tool_ctx, ledger_for(budget)))
        assert set(result.changed_files) == {"a.txt", "b.txt"}

    def test_ending_a_turn_without_finish_still_completes(
        self, spec: TaskSpec, tool_ctx: ToolContext, budget: Budget
    ) -> None:
        provider = FakeProvider([text_turn("I have nothing to change.")])
        result = run(runtime_for(provider).run(spec, tool_ctx, ledger_for(budget)))

        assert result.status is AttemptStatus.SUCCEEDED
        assert "nothing to change" in result.summary

    def test_usage_accumulates_across_turns(
        self, spec: TaskSpec, tool_ctx: ToolContext, budget: Budget
    ) -> None:
        provider = FakeProvider(
            [
                tool_turn(
                    ("c1", "list_dir", {}), usage=Usage(input_tokens=100, output_tokens=20)
                ),
                finish_turn("done"),
            ]
        )
        result = run(runtime_for(provider).run(spec, tool_ctx, ledger_for(budget)))
        assert result.usage.input_tokens == 110
        assert result.usage.output_tokens == 25

    def test_the_result_reports_no_acceptance(
        self, spec: TaskSpec, tool_ctx: ToolContext, budget: Budget
    ) -> None:
        """Gating is the workflow engine's job, never the agent's."""
        provider = FakeProvider([finish_turn("done")])
        result = run(runtime_for(provider).run(spec, tool_ctx, ledger_for(budget)))
        for forbidden in ("passed", "accepted", "approved", "merged"):
            assert not hasattr(result, forbidden)


class TestLifecycleIntegration:
    """State transitions follow the loop."""

    def test_the_path_records_tool_waiting(
        self, spec: TaskSpec, tool_ctx: ToolContext, budget: Budget
    ) -> None:
        changes: list[StateChange] = []
        provider = FakeProvider(
            [tool_turn(("c1", "list_dir", {})), finish_turn("done")]
        )
        run(
            runtime_for(provider, lifecycle_observer=changes.append).run(
                spec, tool_ctx, ledger_for(budget)
            )
        )
        path = [change.target for change in changes]
        assert AgentState.WAITING_TOOL in path
        assert path[-1] is AgentState.COMPLETED

    def test_the_lifecycle_appears_in_the_result(
        self, spec: TaskSpec, tool_ctx: ToolContext, budget: Budget
    ) -> None:
        provider = FakeProvider([finish_turn("done")])
        result = run(runtime_for(provider).run(spec, tool_ctx, ledger_for(budget)))
        assert result.detail["lifecycle"][0] == "idle"
        assert result.detail["lifecycle"][-1] == "completed"

    def test_a_failure_ends_in_the_failed_state(
        self, spec: TaskSpec, tool_ctx: ToolContext, budget: Budget
    ) -> None:
        provider = FakeProvider([refusal_turn()])
        result = run(runtime_for(provider).run(spec, tool_ctx, ledger_for(budget)))
        assert result.detail["lifecycle"][-1] == "failed"


class TestRefusalAndErrors:
    """Bad outcomes are results, not exceptions."""

    def test_a_refusal_is_a_terminal_result(
        self, spec: TaskSpec, tool_ctx: ToolContext, budget: Budget
    ) -> None:
        provider = FakeProvider([refusal_turn(category="cyber")])
        result = run(runtime_for(provider).run(spec, tool_ctx, ledger_for(budget)))

        assert result.status is AttemptStatus.REFUSED
        assert result.error_code == "refused"
        assert result.detail["category"] == "cyber"

    def test_a_provider_error_becomes_an_errored_result(
        self, spec: TaskSpec, tool_ctx: ToolContext, budget: Budget
    ) -> None:
        provider = FakeProvider(
            error=ProviderError(ErrorCode.RATE_LIMITED, "429", provider_id="model.fake")
        )
        result = run(runtime_for(provider).run(spec, tool_ctx, ledger_for(budget)))

        assert result.status is AttemptStatus.ERRORED
        assert result.error_code == "rate_limited"
        assert result.detail["retryable"] is True

    def test_a_non_retryable_provider_error_says_so(
        self, spec: TaskSpec, tool_ctx: ToolContext, budget: Budget
    ) -> None:
        provider = FakeProvider(
            error=ProviderError(ErrorCode.AUTH_FAILED, "401", provider_id="model.fake")
        )
        result = run(runtime_for(provider).run(spec, tool_ctx, ledger_for(budget)))
        assert result.detail["retryable"] is False

    def test_a_context_overflow_becomes_an_errored_result(
        self, spec: TaskSpec, tool_ctx: ToolContext, budget: Budget
    ) -> None:
        provider = FakeProvider([finish_turn("done")], context_tokens=10)
        runtime = AgentRuntime(
            provider,
            config=RuntimeConfig(model="m", context_limit=10),
            registry=default_registry(),
            context_manager=ContextManager(reserve_tokens=0),
        )
        result = run(runtime.run(spec, tool_ctx, ledger_for(budget)))

        assert result.status is AttemptStatus.ERRORED
        assert result.error_code == "context_overflow"

    def test_a_tool_error_does_not_end_the_attempt(
        self, spec: TaskSpec, tool_ctx: ToolContext, budget: Budget
    ) -> None:
        """The model gets a correction and carries on."""
        provider = FakeProvider(
            [
                tool_turn(("c1", "read_file", {"path": "../escape"})),
                finish_turn("recovered"),
            ]
        )
        result = run(runtime_for(provider).run(spec, tool_ctx, ledger_for(budget)))

        assert result.status is AttemptStatus.SUCCEEDED
        assert len(provider.requests) == 2


class TestBudgets:
    """FR-2.6: all three axes, checked every turn."""

    def test_a_token_budget_stops_the_attempt(
        self, spec: TaskSpec, tool_ctx: ToolContext
    ) -> None:
        provider = FakeProvider(
            default=tool_turn(
                ("c1", "list_dir", {}), usage=Usage(input_tokens=60, output_tokens=0)
            )
        )
        ledger = BudgetLedger(
            Budget(seconds=600.0, tokens=100, tool_calls=100), clock=FakeClock()
        )
        result = run(runtime_for(provider).run(spec, tool_ctx, ledger))

        assert result.status is AttemptStatus.BUDGET_EXHAUSTED
        assert result.error_code == "budget_exhausted"

    def test_a_tool_call_budget_stops_the_attempt(
        self, spec: TaskSpec, tool_ctx: ToolContext
    ) -> None:
        provider = FakeProvider(default=tool_turn(("c1", "list_dir", {})))
        ledger = BudgetLedger(
            Budget(seconds=600.0, tokens=1_000_000, tool_calls=2), clock=FakeClock()
        )
        result = run(runtime_for(provider).run(spec, tool_ctx, ledger))

        assert result.status is AttemptStatus.BUDGET_EXHAUSTED
        assert result.detail["axis"] == "tool_calls"

    def test_a_wall_clock_budget_stops_the_attempt(
        self, spec: TaskSpec, tool_ctx: ToolContext
    ) -> None:
        clock = FakeClock()
        ledger = BudgetLedger(
            Budget(seconds=10.0, tokens=1_000_000, tool_calls=100), clock=clock
        )
        clock.advance(11.0)
        provider = FakeProvider([finish_turn("done")])
        result = run(runtime_for(provider).run(spec, tool_ctx, ledger))

        assert result.status is AttemptStatus.BUDGET_EXHAUSTED
        assert result.detail["axis"] == "seconds"
        assert provider.requests == []

    def test_partial_work_survives_an_exhausted_budget(
        self, spec: TaskSpec, tool_ctx: ToolContext
    ) -> None:
        """FR-2.7: the point of a partial attempt is to be inspectable."""
        provider = FakeProvider(
            [
                tool_turn(("c1", "write_file", {"path": "partial.txt", "content": "x"})),
                tool_turn(("c2", "list_dir", {})),
                tool_turn(("c3", "list_dir", {})),
            ]
        )
        ledger = BudgetLedger(
            Budget(seconds=600.0, tokens=1_000_000, tool_calls=2), clock=FakeClock()
        )
        result = run(runtime_for(provider).run(spec, tool_ctx, ledger))

        assert result.status is AttemptStatus.BUDGET_EXHAUSTED
        assert result.status.produced_work
        assert "partial.txt" in result.changed_files
        assert (tool_ctx.workspace_root / "partial.txt").exists()

    def test_the_iteration_cap_stops_a_loop_that_never_converges(
        self, spec: TaskSpec, tool_ctx: ToolContext, budget: Budget
    ) -> None:
        provider = FakeProvider(default=tool_turn(("c1", "list_dir", {})))
        result = run(
            runtime_for(provider, max_iterations=3).run(spec, tool_ctx, ledger_for(budget))
        )

        assert result.status is AttemptStatus.BUDGET_EXHAUSTED
        assert result.error_code == "max_iterations"
        assert len(provider.requests) == 3

    def test_every_tool_call_is_answered_even_when_the_budget_runs_out(
        self, spec: TaskSpec, tool_ctx: ToolContext
    ) -> None:
        """An unanswered tool call breaks the next turn on several backends."""
        provider = FakeProvider(
            [tool_turn(("c1", "list_dir", {}), ("c2", "list_dir", {}), ("c3", "list_dir", {}))]
        )
        ledger = BudgetLedger(
            Budget(seconds=600.0, tokens=1_000_000, tool_calls=1), clock=FakeClock()
        )
        result = run(runtime_for(provider).run(spec, tool_ctx, ledger))
        assert result.status is AttemptStatus.BUDGET_EXHAUSTED


class TestCancellation:
    """A cancelled attempt stops promptly."""

    def test_cancelling_before_the_first_turn(
        self, spec: TaskSpec, workspace: Path, budget: Budget
    ) -> None:
        ctx = ToolContext(workspace_root=workspace, cancelled=lambda: True)
        provider = FakeProvider([finish_turn("done")])
        result = run(runtime_for(provider).run(spec, ctx, ledger_for(budget)))

        assert result.status is AttemptStatus.CANCELLED
        assert provider.requests == []


class TestProviderAgnosticism:
    """The runtime never learns which backend serves it."""

    def test_the_request_is_a_neutral_type(
        self, spec: TaskSpec, tool_ctx: ToolContext, budget: Budget
    ) -> None:
        provider = FakeProvider([finish_turn("done")])
        run(runtime_for(provider).run(spec, tool_ctx, ledger_for(budget)))
        assert type(provider.last_request).__module__ == "orchestrator.provider.base"

    def test_tools_reach_the_provider_sorted(
        self, spec: TaskSpec, tool_ctx: ToolContext, budget: Budget
    ) -> None:
        provider = FakeProvider([finish_turn("done")])
        run(runtime_for(provider).run(spec, tool_ctx, ledger_for(budget)))
        names = [tool.name for tool in provider.last_request.tools]
        assert names == sorted(names)

    def test_the_stable_prefix_is_identical_across_turns(
        self, spec: TaskSpec, tool_ctx: ToolContext, budget: Budget
    ) -> None:
        """Any drift here silently disables prompt caching."""
        provider = FakeProvider(
            [tool_turn(("c1", "list_dir", {})), finish_turn("done")]
        )
        run(runtime_for(provider).run(spec, tool_ctx, ledger_for(budget)))

        prefixes = [
            tuple(block.text for block in request.system)
            for request in provider.requests
        ]
        assert len(prefixes) == 2
        assert prefixes[0] == prefixes[1]

    def test_the_conversation_grows_between_turns(
        self, spec: TaskSpec, tool_ctx: ToolContext, budget: Budget
    ) -> None:
        provider = FakeProvider(
            [tool_turn(("c1", "list_dir", {})), finish_turn("done")]
        )
        run(runtime_for(provider).run(spec, tool_ctx, ledger_for(budget)))
        assert len(provider.requests[1].messages) > len(provider.requests[0].messages)


class TestTranscript:
    """The durable record of what happened."""

    def test_entries_reach_the_sink(
        self, spec: TaskSpec, tool_ctx: ToolContext, budget: Budget
    ) -> None:
        captured: list[TranscriptEntry] = []
        provider = FakeProvider(
            [tool_turn(("c1", "list_dir", {})), finish_turn("done")]
        )
        run(
            runtime_for(provider, transcript_sink=captured.append).run(
                spec, tool_ctx, ledger_for(budget)
            )
        )

        kinds = {entry.kind.value for entry in captured}
        assert "tool_call" in kinds
        assert "tool_result" in kinds

    def test_entries_are_serializable(
        self, spec: TaskSpec, tool_ctx: ToolContext, budget: Budget
    ) -> None:
        import json

        captured: list[TranscriptEntry] = []
        provider = FakeProvider([finish_turn("done")])
        run(
            runtime_for(provider, transcript_sink=captured.append).run(
                spec, tool_ctx, ledger_for(budget)
            )
        )
        for entry in captured:
            json.dumps(entry.to_dict())

    def test_no_sink_is_fine(
        self, spec: TaskSpec, tool_ctx: ToolContext, budget: Budget
    ) -> None:
        provider = FakeProvider([finish_turn("done")])
        assert run(runtime_for(provider).run(spec, tool_ctx, ledger_for(budget))).succeeded


def test_the_agent_package_never_imports_the_task_package() -> None:
    """docs/020 §1: agent and task are siblings that never reference each other."""
    import orchestrator.agent as package

    root = Path(package.__path__[0])
    offenders: list[str] = []

    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            offenders.extend(
                f"{path.name}: {name}"
                for name in names
                if name.startswith("orchestrator.task")
            )

    assert offenders == [], f"forbidden imports in the agent package: {offenders}"


def test_the_agent_package_names_no_vendor() -> None:
    """Provider-agnostic: no backend may be named outside the provider layer."""
    import orchestrator.agent as package

    root = Path(package.__path__[0])
    vendor_terms = ("anthropic", "openai", "ollama", "hermes", "claude")
    offenders: list[str] = []

    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            offenders.extend(
                f"{path.name}: {name}"
                for name in names
                if any(term in name.lower() for term in vendor_terms)
            )

    assert offenders == [], f"vendor imports in the agent package: {offenders}"

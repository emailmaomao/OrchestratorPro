"""Tests for the prompt builder and its cache-stability guards."""

from __future__ import annotations

import pytest

from orchestrator.agent.model import AgentRole, TaskSpec
from orchestrator.agent.prompt import (
    DEFAULT_ROLE_PROMPTS,
    PromptBuilder,
    PromptStabilityError,
    RepositoryContext,
    check_stability,
)
from orchestrator.agent.tools import default_registry
from orchestrator.core.config import Effort, ThinkingMode
from orchestrator.core.events import RunId, TaskId


class TestStabilityGuard:
    """Volatile content in a frozen prompt is a silent cache bug."""

    def test_ordinary_text_passes(self) -> None:
        assert check_stability("You are a careful engineer.")

    def test_a_timestamp_is_refused(self) -> None:
        with pytest.raises(PromptStabilityError, match="a timestamp"):
            check_stability("The current time is 2026-07-25T14:03 UTC.")

    def test_an_identifier_is_refused(self) -> None:
        run_id = RunId.generate()
        with pytest.raises(PromptStabilityError, match="an identifier"):
            check_stability(f"You are working on run {run_id}.")

    def test_a_uuid_is_refused(self) -> None:
        with pytest.raises(PromptStabilityError, match="a UUID"):
            check_stability("Session 123e4567-e89b-12d3-a456-426614174000 begins.")

    def test_the_error_explains_the_consequence(self) -> None:
        with pytest.raises(PromptStabilityError) as excinfo:
            check_stability("at 2026-01-01T00:00")
        assert "invalidates the prompt cache" in str(excinfo.value)

    def test_every_shipped_role_prompt_is_stable(self) -> None:
        for role, text in DEFAULT_ROLE_PROMPTS.items():
            check_stability(text, where=role.value)

    def test_a_builder_rejects_a_volatile_role_prompt(self) -> None:
        """The bug fails here, not later as an unexplained cost increase."""
        with pytest.raises(PromptStabilityError):
            PromptBuilder(
                role_prompts={AgentRole.WORKER: "Today is 2026-07-25T09:00."}
            )


class TestSystemBlocks:
    """The stable prefix, assembled in order of stability."""

    def test_the_role_prompt_comes_first(self, spec: TaskSpec) -> None:
        blocks = PromptBuilder().system_blocks(spec)
        assert blocks[0].text == DEFAULT_ROLE_PROMPTS[AgentRole.WORKER]

    def test_the_task_comes_last(self, spec: TaskSpec) -> None:
        """It is the first thing that varies, so it sits latest in the prefix."""
        blocks = PromptBuilder().system_blocks(spec)
        assert spec.title in blocks[-1].text
        assert spec.prompt in blocks[-1].text

    def test_repository_context_sits_between_them(self, spec: TaskSpec) -> None:
        builder = PromptBuilder(
            context=RepositoryContext(summary="A CLI tool.", conventions="Use tabs.")
        )
        blocks = builder.system_blocks(spec)
        assert len(blocks) == 3
        assert "A CLI tool." in blocks[1].text
        assert "Use tabs." in blocks[1].text

    def test_empty_context_is_omitted(self, spec: TaskSpec) -> None:
        assert len(PromptBuilder().system_blocks(spec)) == 2

    def test_the_task_identifier_is_not_in_the_prefix(self, spec: TaskSpec) -> None:
        """It varies per task and tells the model nothing it can use."""
        rendered = "\n".join(b.text for b in PromptBuilder().system_blocks(spec))
        assert str(spec.task_id) not in rendered

    def test_labels_are_included_when_present(self) -> None:
        spec = TaskSpec(
            task_id=TaskId.generate(),
            title="t",
            prompt="p",
            labels=frozenset({"db", "slow"}),
        )
        rendered = PromptBuilder().system_blocks(spec)[-1].text
        assert "db" in rendered and "slow" in rendered

    def test_an_unknown_role_is_refused(self) -> None:
        """Falling back to a generic prompt would change behaviour silently."""
        builder = PromptBuilder(role_prompts={AgentRole.WORKER: "worker"})
        spec = TaskSpec(
            task_id=TaskId.generate(), title="t", prompt="p", role=AgentRole.PLANNER
        )
        with pytest.raises(PromptStabilityError, match="no system prompt is defined"):
            builder.system_blocks(spec)


class TestFeedback:
    """Prior-attempt feedback stays out of the cached prefix."""

    def test_no_feedback_yields_a_neutral_opening(self, spec: TaskSpec) -> None:
        messages = PromptBuilder().opening_messages(spec)
        assert len(messages) == 1
        assert "Begin the task" in messages[0].text

    def test_feedback_appears_in_the_opening_turn(self, spec: TaskSpec) -> None:
        with_feedback = spec.with_feedback("tests failed: test_login")
        messages = PromptBuilder().opening_messages(with_feedback)
        assert "test_login" in messages[0].text
        assert "Previous attempts" in messages[0].text

    def test_feedback_is_not_in_the_stable_prefix(self, spec: TaskSpec) -> None:
        """Putting it earlier would bust the cache on every retry."""
        with_feedback = spec.with_feedback("tests failed: test_login")
        prefix = "\n".join(b.text for b in PromptBuilder().system_blocks(with_feedback))
        assert "test_login" not in prefix

    def test_multiple_notes_are_numbered(self, spec: TaskSpec) -> None:
        twice = spec.with_feedback("first failure").with_feedback("second failure")
        rendered = PromptBuilder().opening_messages(twice)[0].text
        assert "1. first failure" in rendered
        assert "2. second failure" in rendered


class TestDeterminism:
    """Two renders of identical inputs must produce identical bytes."""

    def test_system_blocks_are_byte_identical(self, spec: TaskSpec) -> None:
        builder = PromptBuilder()
        first = [b.text for b in builder.system_blocks(spec)]
        second = [b.text for b in builder.system_blocks(spec)]
        assert first == second

    def test_the_fingerprint_is_stable(self, spec: TaskSpec) -> None:
        builder = PromptBuilder()
        tools = default_registry().specs()
        assert builder.fingerprint(spec, tools) == builder.fingerprint(spec, tools)

    def test_the_fingerprint_changes_with_the_task(self, spec: TaskSpec) -> None:
        builder = PromptBuilder()
        other = TaskSpec(task_id=TaskId.generate(), title="different", prompt="other")
        assert builder.fingerprint(spec) != builder.fingerprint(other)

    def test_the_fingerprint_ignores_feedback(self, spec: TaskSpec) -> None:
        """Feedback lives outside the prefix, so the cache survives a retry."""
        builder = PromptBuilder()
        assert builder.fingerprint(spec) == builder.fingerprint(
            spec.with_feedback("a failure")
        )

    def test_the_fingerprint_changes_with_the_tool_set(self, spec: TaskSpec) -> None:
        builder = PromptBuilder()
        tools = default_registry().specs()
        assert builder.fingerprint(spec, tools) != builder.fingerprint(spec, tools[:2])


class TestRequestAssembly:
    """The neutral request handed to any provider."""

    def test_the_request_carries_everything(self, spec: TaskSpec) -> None:
        builder = PromptBuilder()
        tools = default_registry().specs()
        request = builder.build_request(
            model="some-model",
            spec=spec,
            messages=builder.opening_messages(spec),
            tools=tools,
            max_output_tokens=8_000,
            effort=Effort.XHIGH,
            reasoning=ThinkingMode.ADAPTIVE,
        )

        assert request.model == "some-model"
        assert request.system == builder.system_blocks(spec)
        assert request.tools == tools
        assert request.max_output_tokens == 8_000
        assert request.effort is Effort.XHIGH

    def test_caching_is_requested_by_default(self, spec: TaskSpec) -> None:
        builder = PromptBuilder()
        request = builder.build_request(
            model="m", spec=spec, messages=builder.opening_messages(spec)
        )
        assert request.cache_hint is not None

    def test_caching_can_be_disabled(self, spec: TaskSpec) -> None:
        builder = PromptBuilder()
        request = builder.build_request(
            model="m", spec=spec, messages=builder.opening_messages(spec), cache=False
        )
        assert request.cache_hint is None

    def test_the_request_names_no_provider(self, spec: TaskSpec) -> None:
        """Provider-agnostic: the builder cannot know which backend serves this."""
        builder = PromptBuilder()
        request = builder.build_request(
            model="m", spec=spec, messages=builder.opening_messages(spec)
        )
        assert type(request).__module__ == "orchestrator.provider.base"

    def test_overhead_grows_with_the_tool_set(self, spec: TaskSpec) -> None:
        builder = PromptBuilder()
        tools = default_registry().specs()
        assert builder.overhead_tokens(spec, tools) > builder.overhead_tokens(spec)

    def test_overhead_is_positive_without_tools(self, spec: TaskSpec) -> None:
        assert PromptBuilder().overhead_tokens(spec) > 0


class TestRepositoryContext:
    """Run-stable facts, cached once per run."""

    def test_an_empty_context_renders_nothing(self) -> None:
        context = RepositoryContext()
        assert context.is_empty
        assert context.render() == ""

    def test_sections_are_labelled(self) -> None:
        context = RepositoryContext(
            summary="s", conventions="c", file_tree="src/\n  a.py"
        )
        rendered = context.render()
        assert "# Repository" in rendered
        assert "# Conventions" in rendered
        assert "# Layout" in rendered

    def test_rendering_is_deterministic(self) -> None:
        context = RepositoryContext(summary="s", conventions="c")
        assert context.render() == context.render()

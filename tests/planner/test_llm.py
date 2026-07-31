"""Tests for the LLM planner."""

from __future__ import annotations

import json

import pytest

from orchestrator.core.config import Effort
from orchestrator.planner.llm import (
    PLANNER_SYSTEM_PROMPT,
    PlannerError,
    PlannerInvalid,
    PlannerRefused,
    PlanRequest,
    WorkflowPlanner,
)

from tests.planner.conftest import (
    CYCLIC_PLAN,
    UNKNOWN_DEPENDENCY_PLAN,
    VALID_PLAN,
    ScriptedProvider,
    json_response,
    refusal,
    run,
)


class TestPlanRequest:
    """What goes in."""

    def test_a_goal_is_required(self) -> None:
        with pytest.raises(PlannerError, match="needs a goal"):
            PlanRequest(goal="   ")

    def test_an_absurd_step_ceiling_is_refused(self) -> None:
        with pytest.raises(PlannerError, match="max_steps"):
            PlanRequest(goal="g", max_steps=0)

    def test_the_goal_is_rendered(self) -> None:
        assert "migrate the client" in PlanRequest(goal="migrate the client").render()

    def test_the_repository_summary_is_included(self) -> None:
        """Without it a model invents a project structure and writes against it."""
        rendered = PlanRequest(goal="g", repo_summary="Python, src/ layout").render()

        assert "# Repository" in rendered
        assert "src/ layout" in rendered

    def test_constraints_are_listed(self) -> None:
        rendered = PlanRequest(goal="g", constraints=("do not touch the schema",)).render()
        assert "- do not touch the schema" in rendered

    def test_the_step_ceiling_is_stated(self) -> None:
        assert "at most 5 steps" in PlanRequest(goal="g", max_steps=5).render()

    def test_planning_defaults_to_deep_effort(self) -> None:
        """A bad decomposition costs every attempt that follows it."""
        assert PlanRequest(goal="g").effort is Effort.XHIGH


class TestSystemPrompt:
    """The frozen instructions."""

    def test_it_is_stable(self) -> None:
        """No timestamps or identifiers, so it sits in the cached prefix."""
        from orchestrator.agent.prompt import check_stability

        check_stability(PLANNER_SYSTEM_PROMPT, where="the planner prompt")

    def test_it_says_prompts_must_be_self_contained(self) -> None:
        assert "self-contained" in PLANNER_SYSTEM_PROMPT

    def test_it_forbids_cycles(self) -> None:
        assert "cycle" in PLANNER_SYSTEM_PROMPT


class TestPlanning:
    """The happy path."""

    def test_a_valid_plan_becomes_a_workflow(self) -> None:
        planner = WorkflowPlanner(ScriptedProvider([json_response(VALID_PLAN)]))

        result = run(planner.plan(PlanRequest(goal="migrate the HTTP client")))

        assert result.ok
        assert result.workflow is not None
        assert [s.name for s in result.workflow.steps] == [
            "client-uses-httpx",
            "call-sites-updated",
        ]

    def test_the_plan_compiles_to_a_graph(self) -> None:
        planner = WorkflowPlanner(ScriptedProvider([json_response(VALID_PLAN)]))
        workflow = run(planner.plan_or_raise(PlanRequest(goal="g")))

        assert workflow.compile().graph.depth == 2

    def test_usage_is_accounted(self) -> None:
        planner = WorkflowPlanner(ScriptedProvider([json_response(VALID_PLAN)]))
        result = run(planner.plan(PlanRequest(goal="g")))

        assert result.tokens_in == 100
        assert result.tokens_out == 200

    def test_one_attempt_is_reported(self) -> None:
        planner = WorkflowPlanner(ScriptedProvider([json_response(VALID_PLAN)]))
        assert run(planner.plan(PlanRequest(goal="g"))).attempts == 1

    def test_the_summary_reads_naturally(self) -> None:
        planner = WorkflowPlanner(ScriptedProvider([json_response(VALID_PLAN)]))
        assert "2 step(s)" in run(planner.plan(PlanRequest(goal="g"))).summary()


class TestTheRequest:
    """What the planner asks for."""

    def test_it_constrains_the_output(self) -> None:
        """Constraining generation is cheaper than repairing the result."""
        provider = ScriptedProvider([json_response(VALID_PLAN)])
        run(WorkflowPlanner(provider).plan(PlanRequest(goal="g")))

        schema = provider.last_request.output_schema
        assert schema is not None
        assert schema["type"] == "json_schema"

    def test_the_system_prompt_carries_the_instructions(self) -> None:
        provider = ScriptedProvider([json_response(VALID_PLAN)])
        run(WorkflowPlanner(provider).plan(PlanRequest(goal="g")))

        assert "decompose" in provider.last_request.system[0].text

    def test_the_goal_travels_as_a_message(self) -> None:
        """Not in the prefix: the goal changes per run and would break caching."""
        provider = ScriptedProvider([json_response(VALID_PLAN)])
        run(WorkflowPlanner(provider).plan(PlanRequest(goal="a distinctive goal")))

        assert "a distinctive goal" not in provider.last_request.system[0].text
        rendered = "".join(
            block.text for block in provider.last_request.messages[0].content
        )
        assert "a distinctive goal" in rendered

    def test_it_names_no_vendor(self) -> None:
        """Provider independence, checked at the request."""
        provider = ScriptedProvider([json_response(VALID_PLAN)])
        run(WorkflowPlanner(provider).plan(PlanRequest(goal="g", model="llama3.1:70b")))

        assert provider.last_request.model == "llama3.1:70b"

    def test_the_effort_is_passed_through(self) -> None:
        provider = ScriptedProvider([json_response(VALID_PLAN)])
        run(WorkflowPlanner(provider).plan(PlanRequest(goal="g", effort=Effort.LOW)))

        assert provider.last_request.effort is Effort.LOW


class TestCorrection:
    """FR-1.5: a bad plan is reported, then corrected."""

    def test_an_invalid_plan_is_retried_with_the_report(self) -> None:
        provider = ScriptedProvider(
            [json_response(UNKNOWN_DEPENDENCY_PLAN), json_response(VALID_PLAN)]
        )
        result = run(WorkflowPlanner(provider).plan(PlanRequest(goal="g")))

        assert result.ok
        assert result.attempts == 2

    def test_the_correction_names_the_specific_fault(self) -> None:
        """Not "try again" — the model is fixing something it can see."""
        provider = ScriptedProvider(
            [json_response(UNKNOWN_DEPENDENCY_PLAN), json_response(VALID_PLAN)]
        )
        run(WorkflowPlanner(provider).plan(PlanRequest(goal="g")))

        second = provider.requests[1]
        rendered = "".join(
            block.text
            for message in second.messages
            for block in message.content
            if hasattr(block, "text")
        )
        assert "ghost" in rendered

    def test_a_cycle_is_caught_and_corrected(self) -> None:
        """The schema cannot see a cycle; the compiler can."""
        provider = ScriptedProvider([json_response(CYCLIC_PLAN), json_response(VALID_PLAN)])
        result = run(WorkflowPlanner(provider).plan(PlanRequest(goal="g")))

        assert result.ok
        assert result.attempts == 2

    def test_non_json_is_corrected(self) -> None:
        provider = ScriptedProvider(
            [json_response("this is not a plan"), json_response(VALID_PLAN)]
        )
        result = run(WorkflowPlanner(provider).plan(PlanRequest(goal="g")))

        assert result.ok
        assert any("not JSON" in note for note in result.notes)

    def test_an_object_embedded_in_prose_is_read(self) -> None:
        """A backend without structured output degrades to prose around JSON."""
        wrapped = f"Here is the plan:\n\n{json.dumps(VALID_PLAN)}\n\nLet me know."
        provider = ScriptedProvider([json_response(wrapped)])

        assert run(WorkflowPlanner(provider).plan(PlanRequest(goal="g"))).ok

    def test_a_json_array_is_not_a_workflow(self) -> None:
        provider = ScriptedProvider([json_response([1, 2, 3]), json_response(VALID_PLAN)])
        result = run(WorkflowPlanner(provider).plan(PlanRequest(goal="g")))

        assert result.ok
        assert any("not a workflow object" in note for note in result.notes)

    def test_an_empty_response_is_corrected(self) -> None:
        provider = ScriptedProvider([json_response(""), json_response(VALID_PLAN)])
        assert run(WorkflowPlanner(provider).plan(PlanRequest(goal="g"))).ok


class TestGivingUp:
    """When no plan is produced."""

    def test_it_stops_after_the_attempt_limit(self) -> None:
        provider = ScriptedProvider([], default=json_response(UNKNOWN_DEPENDENCY_PLAN))
        result = run(WorkflowPlanner(provider, max_attempts=2).plan(PlanRequest(goal="g")))

        assert not result.ok
        assert result.attempts == 2
        assert len(provider.requests) == 2

    def test_failure_is_a_result_not_an_exception(self) -> None:
        """A planner that raises makes the caller choose crash or swallow."""
        provider = ScriptedProvider([], default=json_response(UNKNOWN_DEPENDENCY_PLAN))
        result = run(WorkflowPlanner(provider, max_attempts=1).plan(PlanRequest(goal="g")))

        assert result.workflow is None
        assert not result.report.ok

    def test_the_report_says_what_was_wrong(self) -> None:
        provider = ScriptedProvider([], default=json_response(UNKNOWN_DEPENDENCY_PLAN))
        result = run(WorkflowPlanner(provider, max_attempts=1).plan(PlanRequest(goal="g")))

        assert any("ghost" in str(issue) for issue in result.report.issues)

    def test_plan_or_raise_raises(self) -> None:
        provider = ScriptedProvider([], default=json_response(UNKNOWN_DEPENDENCY_PLAN))
        planner = WorkflowPlanner(provider, max_attempts=1)

        with pytest.raises(PlannerInvalid, match="no usable plan"):
            run(planner.plan_or_raise(PlanRequest(goal="g")))

    def test_the_raised_error_carries_the_detail(self) -> None:
        provider = ScriptedProvider([], default=json_response(UNKNOWN_DEPENDENCY_PLAN))
        planner = WorkflowPlanner(provider, max_attempts=1)

        with pytest.raises(PlannerInvalid) as caught:
            run(planner.plan_or_raise(PlanRequest(goal="g")))

        assert caught.value.detail["issues"]

    def test_nothing_is_silently_repaired(self) -> None:
        """Dropping the unknown dependency would produce a plan nobody proposed."""
        provider = ScriptedProvider([], default=json_response(UNKNOWN_DEPENDENCY_PLAN))
        result = run(WorkflowPlanner(provider, max_attempts=1).plan(PlanRequest(goal="g")))

        assert result.workflow is None


class TestRefusal:
    """A backend that declines."""

    def test_a_refusal_is_raised_not_retried(self) -> None:
        """Retrying burns the budget reproducing a refusal."""
        provider = ScriptedProvider([refusal()], default=json_response(VALID_PLAN))
        planner = WorkflowPlanner(provider, max_attempts=3)

        with pytest.raises(PlannerRefused):
            run(planner.plan(PlanRequest(goal="g")))

        assert len(provider.requests) == 1

    def test_the_refusal_is_not_retryable(self) -> None:
        provider = ScriptedProvider([refusal()])

        with pytest.raises(PlannerRefused) as caught:
            run(WorkflowPlanner(provider).plan(PlanRequest(goal="g")))

        assert caught.value.retryable is False

    def test_the_category_is_carried(self) -> None:
        provider = ScriptedProvider([refusal(category="cyber")])

        with pytest.raises(PlannerRefused) as caught:
            run(WorkflowPlanner(provider).plan(PlanRequest(goal="g")))

        assert caught.value.detail["category"] == "cyber"


class TestConstruction:
    """The planner itself."""

    def test_a_zero_attempt_limit_is_refused(self) -> None:
        with pytest.raises(PlannerError, match="at least 1"):
            WorkflowPlanner(ScriptedProvider([]), max_attempts=0)

    def test_the_system_prompt_can_be_replaced(self) -> None:
        provider = ScriptedProvider([json_response(VALID_PLAN)])
        planner = WorkflowPlanner(provider, system_prompt="Decompose it.")

        run(planner.plan(PlanRequest(goal="g")))
        assert provider.last_request.system[0].text == "Decompose it."

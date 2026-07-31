"""Tests for workflow definitions, compilation, and conditions."""

from __future__ import annotations

import pytest

from orchestrator.agent.model import AgentRole
from orchestrator.core.events import TaskId
from orchestrator.task.graph import CycleError
from orchestrator.workflow.definition import (
    AllOf,
    Always,
    AnyOf,
    ConditionContext,
    Not,
    StepDefinition,
    StepDidWork,
    StepSkipped,
    WorkflowDefinition,
    WorkflowDefinitionError,
    linear,
    parallel,
    test_gate as make_test_gate,
)

from tests.workflow.conftest import step, workflow


class TestStepDefinition:
    """A step validates itself where it is declared."""

    def test_defaults(self) -> None:
        declared = StepDefinition(name="build", prompt="compile it")
        assert declared.display_title == "build"
        assert declared.role is AgentRole.WORKER
        assert declared.max_attempts == 3

    def test_a_title_overrides_the_name(self) -> None:
        assert StepDefinition(name="s", prompt="p", title="Nice").display_title == "Nice"

    @pytest.mark.parametrize(("name", "prompt"), [("", "p"), ("  ", "p")])
    def test_a_blank_name_is_refused(self, name: str, prompt: str) -> None:
        with pytest.raises(WorkflowDefinitionError, match="must have a name"):
            StepDefinition(name=name, prompt=prompt)

    def test_a_blank_prompt_is_refused(self) -> None:
        with pytest.raises(WorkflowDefinitionError, match="must have a prompt"):
            StepDefinition(name="s", prompt="   ")

    def test_a_non_positive_attempt_cap_is_refused(self) -> None:
        with pytest.raises(WorkflowDefinitionError, match="at least one attempt"):
            StepDefinition(name="s", prompt="p", max_attempts=0)

    def test_a_self_dependency_is_refused(self) -> None:
        with pytest.raises(WorkflowDefinitionError, match="depends on itself"):
            StepDefinition(name="s", prompt="p", depends_on=("s",))


class TestWorkflowValidation:
    """A definition that cannot run is refused before anything starts."""

    def test_a_valid_workflow_compiles(self) -> None:
        definition = workflow(step("a"), step("b", depends_on=["a"]))
        plan = definition.compile()
        assert len(plan.graph) == 2

    def test_an_empty_workflow_is_refused(self) -> None:
        with pytest.raises(WorkflowDefinitionError, match="has no steps"):
            WorkflowDefinition(name="wf", goal="g", steps=())

    def test_a_blank_name_is_refused(self) -> None:
        with pytest.raises(WorkflowDefinitionError, match="must have a name"):
            WorkflowDefinition(name=" ", goal="g", steps=(step("a"),))

    def test_duplicate_step_names_are_refused(self) -> None:
        """Names are how conditions and recovery refer to steps."""
        with pytest.raises(WorkflowDefinitionError, match="two steps named"):
            workflow(step("a"), step("a"))

    def test_an_unknown_dependency_is_refused(self) -> None:
        with pytest.raises(WorkflowDefinitionError, match="not part of this workflow"):
            workflow(step("a", depends_on=["ghost"]))

    def test_a_non_positive_concurrency_is_refused(self) -> None:
        with pytest.raises(WorkflowDefinitionError, match="at least one concurrent"):
            workflow(step("a"), max_concurrency=0)

    def test_a_cycle_is_caught_at_compile_time(self) -> None:
        definition = workflow(
            step("a", depends_on=["b"]), step("b", depends_on=["a"])
        )
        with pytest.raises(CycleError):
            definition.compile()

    def test_a_condition_referencing_an_unknown_step_is_refused(self) -> None:
        with pytest.raises(WorkflowDefinitionError, match="not part of this workflow"):
            workflow(step("a", condition=StepDidWork("ghost")))

    def test_a_condition_must_reference_a_dependency(self) -> None:
        """Otherwise it could be evaluated before that step has finished."""
        with pytest.raises(WorkflowDefinitionError, match="must also depend on it"):
            workflow(step("a"), step("b", condition=StepDidWork("a")))

    def test_a_condition_on_a_dependency_is_accepted(self) -> None:
        definition = workflow(
            step("a"), step("b", depends_on=["a"], condition=StepDidWork("a"))
        )
        assert len(definition.compile().graph) == 2


class TestCompilation:
    """Names in, identifiers out, mapping preserved."""

    def test_names_map_to_identifiers_both_ways(self) -> None:
        plan = workflow(step("alpha"), step("beta", depends_on=["alpha"])).compile()
        alpha = plan.task_id("alpha")
        assert plan.step_name(alpha) == "alpha"

    def test_dependencies_are_translated(self) -> None:
        plan = workflow(step("a"), step("b", depends_on=["a"])).compile()
        assert plan.graph.dependencies(plan.task_id("b")) == (plan.task_id("a"),)

    def test_each_compile_mints_fresh_identifiers(self) -> None:
        definition = workflow(step("a"))
        assert definition.compile().task_id("a") != definition.compile().task_id("a")

    def test_step_lookup_by_identifier(self) -> None:
        plan = workflow(step("a", prompt="do a")).compile()
        assert plan.step_for(plan.task_id("a")).prompt == "do a"

    def test_an_unknown_name_is_refused(self) -> None:
        plan = workflow(step("a")).compile()
        with pytest.raises(WorkflowDefinitionError, match="no step named"):
            plan.task_id("ghost")

    def test_gates_survive_compilation(self) -> None:
        plan = workflow(step("a", gates=(make_test_gate("unit"),))).compile()
        assert plan.graph.get(plan.task_id("a")).gates[0].name == "unit"

    def test_budgets_and_attempts_survive(self) -> None:
        plan = workflow(step("a", max_attempts=5)).compile()
        assert plan.graph.get(plan.task_id("a")).max_attempts == 5

    def test_binding_reuses_given_identifiers(self) -> None:
        definition = workflow(step("a"), step("b", depends_on=["a"]))
        ids = {"a": TaskId.generate(), "b": TaskId.generate()}
        plan = definition.bind(ids)

        assert plan.task_id("a") == ids["a"]
        assert plan.graph.dependencies(ids["b"]) == (ids["a"],)

    def test_binding_refuses_a_missing_identifier(self) -> None:
        definition = workflow(step("a"), step("b"))
        with pytest.raises(WorkflowDefinitionError, match="no identifier for step"):
            definition.bind({"a": TaskId.generate()})

    def test_states_are_re_keyed_by_name(self) -> None:
        from orchestrator.task.model import TaskState

        plan = workflow(step("a")).compile()
        by_name = plan.states_by_name({plan.task_id("a"): TaskState.SUCCEEDED})
        assert by_name == {"a": TaskState.SUCCEEDED}


class TestConditions:
    """Only decidable facts, and the combinators over them."""

    def test_always_is_always_true(self) -> None:
        assert Always().evaluate(ConditionContext())
        assert Always().referenced_steps() == frozenset()

    def test_did_work_needs_success_without_a_skip(self) -> None:
        ctx = ConditionContext(succeeded=frozenset({"a", "b"}), skipped=frozenset({"b"}))
        assert StepDidWork("a").evaluate(ctx)
        assert not StepDidWork("b").evaluate(ctx)
        assert not StepDidWork("c").evaluate(ctx)

    def test_skipped_detects_a_pruned_step(self) -> None:
        ctx = ConditionContext(succeeded=frozenset({"a"}), skipped=frozenset({"a"}))
        assert StepSkipped("a").evaluate(ctx)
        assert not StepSkipped("b").evaluate(ctx)

    def test_did_work_and_skipped_are_complements(self) -> None:
        worked = ConditionContext(succeeded=frozenset({"a"}))
        pruned = ConditionContext(succeeded=frozenset({"a"}), skipped=frozenset({"a"}))
        assert StepDidWork("a").evaluate(worked) != StepSkipped("a").evaluate(worked)
        assert StepDidWork("a").evaluate(pruned) != StepSkipped("a").evaluate(pruned)

    def test_all_of(self) -> None:
        ctx = ConditionContext(succeeded=frozenset({"a", "b"}))
        assert AllOf((StepDidWork("a"), StepDidWork("b"))).evaluate(ctx)
        assert not AllOf((StepDidWork("a"), StepDidWork("z"))).evaluate(ctx)

    def test_an_empty_all_of_is_true(self) -> None:
        assert AllOf(()).evaluate(ConditionContext())

    def test_any_of(self) -> None:
        ctx = ConditionContext(succeeded=frozenset({"a"}))
        assert AnyOf((StepDidWork("a"), StepDidWork("z"))).evaluate(ctx)
        assert not AnyOf((StepDidWork("y"), StepDidWork("z"))).evaluate(ctx)

    def test_an_empty_any_of_is_false(self) -> None:
        assert not AnyOf(()).evaluate(ConditionContext())

    def test_not_inverts(self) -> None:
        ctx = ConditionContext(succeeded=frozenset({"a"}))
        assert not Not(StepDidWork("a")).evaluate(ctx)
        assert Not(StepDidWork("z")).evaluate(ctx)

    def test_referenced_steps_are_collected_recursively(self) -> None:
        condition = AllOf((StepDidWork("a"), Not(AnyOf((StepSkipped("b"),)))))
        assert condition.referenced_steps() == frozenset({"a", "b"})

    def test_descriptions_are_readable(self) -> None:
        assert StepDidWork("a").describe() == "a did work"
        assert StepSkipped("a").describe() == "a was skipped"
        assert "and" in AllOf((StepDidWork("a"), StepDidWork("b"))).describe()
        assert "or" in AnyOf((StepDidWork("a"), StepDidWork("b"))).describe()
        assert Not(Always()).describe() == "not (always)"


class TestHelpers:
    """The shorthand builders."""

    def test_linear_chains_the_steps(self) -> None:
        definition = linear("goal", ["one", "two", "three"])
        plan = definition.compile()
        assert plan.graph.depth == 3
        assert plan.graph.max_width == 1

    def test_parallel_leaves_steps_independent(self) -> None:
        plan = parallel("goal", ["a", "b", "c"]).compile()
        assert plan.graph.depth == 1
        assert plan.graph.max_width == 3

    def test_test_gate_is_required_by_default(self) -> None:
        assert make_test_gate().required
        assert not make_test_gate(required=False).required

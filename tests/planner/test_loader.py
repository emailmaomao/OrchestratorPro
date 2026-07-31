"""Tests for compiling YAML into an executable workflow."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.agent.model import AgentRole
from orchestrator.planner.loader import (
    MAX_DOCUMENT_BYTES,
    check_workflow,
    load_workflow,
    load_workflow_file,
    parse_document,
    workflow_from_document,
)
from orchestrator.planner.schema import SchemaError
from orchestrator.task.graph import CycleError
from orchestrator.task.model import GateKind

from tests.planner.conftest import LINEAR_YAML


class TestParse:
    """Reading the file."""

    def test_a_document_parses(self) -> None:
        assert parse_document("name: n\ngoal: g\n")["name"] == "n"

    def test_malformed_yaml_is_refused(self) -> None:
        with pytest.raises(SchemaError, match="not valid YAML"):
            parse_document("name: [unclosed\n")

    def test_an_empty_document_is_refused(self) -> None:
        with pytest.raises(SchemaError, match="is empty"):
            parse_document("")

    def test_a_list_document_is_refused(self) -> None:
        with pytest.raises(SchemaError, match="table of workflow settings"):
            parse_document("- a\n- b\n")

    def test_an_enormous_document_is_refused(self) -> None:
        with pytest.raises(SchemaError, match="larger than"):
            parse_document("x" * (MAX_DOCUMENT_BYTES + 1))

    def test_it_uses_the_safe_loader(self) -> None:
        """A workflow file may have been written by an agent."""
        dangerous = "name: !!python/object/apply:os.system ['echo pwned']\n"

        with pytest.raises(SchemaError, match="not valid YAML"):
            parse_document(dangerous)

    def test_the_source_is_named_in_errors(self) -> None:
        with pytest.raises(SchemaError, match="workflow.yaml"):
            parse_document("", source="workflow.yaml")


class TestCompile:
    """Document to definition."""

    def test_a_linear_workflow_compiles(self) -> None:
        workflow = load_workflow(LINEAR_YAML)

        assert workflow.name == "ship"
        assert [step.name for step in workflow.steps] == ["design", "build", "verify"]

    def test_dependencies_survive(self) -> None:
        workflow = load_workflow(LINEAR_YAML)
        assert workflow.steps[1].depends_on == ("design",)

    def test_the_graph_is_the_expected_shape(self) -> None:
        plan = load_workflow(LINEAR_YAML).compile()

        assert plan.graph.depth == 3
        assert plan.graph.max_width == 1

    def test_a_parallel_workflow_compiles(self) -> None:
        workflow = load_workflow(
            "name: fan\ngoal: g\nsteps:\n"
            "  - {name: a, prompt: p}\n"
            "  - {name: b, prompt: p}\n"
            "  - {name: c, prompt: p}\n"
        )
        assert workflow.compile().graph.max_width == 3

    def test_gates_compile_in_either_shape(self) -> None:
        workflow = load_workflow(
            "name: g\ngoal: g\nsteps:\n"
            "  - name: a\n    prompt: p\n    gates: [unit]\n"
            "  - name: b\n    prompt: p\n    gates:\n"
            "      - {name: lint, kind: lint, required: false}\n"
        )

        assert workflow.steps[0].gates[0].name == "unit"
        assert workflow.steps[0].gates[0].kind is GateKind.TEST
        assert workflow.steps[1].gates[0].kind is GateKind.LINT
        assert workflow.steps[1].gates[0].required is False

    def test_the_role_compiles(self) -> None:
        workflow = load_workflow("name: n\ngoal: g\nsteps: [{name: a, prompt: p, role: reviewer}]\n")
        assert workflow.steps[0].role is AgentRole.REVIEWER

    def test_conditions_compile(self) -> None:
        workflow = load_workflow(
            "name: n\ngoal: g\nsteps:\n"
            "  - {name: a, prompt: p}\n"
            "  - name: b\n    prompt: p\n    depends_on: [a]\n    when: {did_work: a}\n"
        )
        condition = workflow.steps[1].condition

        assert condition is not None
        assert condition.describe() == "a did work"

    def test_nested_conditions_compile(self) -> None:
        workflow = load_workflow(
            "name: n\ngoal: g\nsteps:\n"
            "  - {name: a, prompt: p}\n"
            "  - name: b\n    prompt: p\n    depends_on: [a]\n"
            "    when:\n      all_of:\n        - {did_work: a}\n        - not: {was_skipped: a}\n"
        )
        described = workflow.steps[1].condition.describe()  # type: ignore[union-attr]

        assert "and" in described
        assert "not" in described


class TestDefaults:
    """The defaults table."""

    def test_defaults_apply(self) -> None:
        workflow = load_workflow(
            "name: n\ngoal: g\n"
            "defaults: {max_attempts: 5, role: reviewer, gates: [tests]}\n"
            "steps: [{name: a, prompt: p}]\n"
        )

        assert workflow.steps[0].max_attempts == 5
        assert workflow.steps[0].role is AgentRole.REVIEWER
        assert workflow.steps[0].gates[0].name == "tests"

    def test_a_step_overrides_a_default(self) -> None:
        """A default that beat an explicit value would be the worst bug here."""
        workflow = load_workflow(
            "name: n\ngoal: g\n"
            "defaults: {max_attempts: 5}\n"
            "steps: [{name: a, prompt: p, max_attempts: 1}]\n"
        )
        assert workflow.steps[0].max_attempts == 1

    def test_an_empty_gate_list_overrides_a_default(self) -> None:
        workflow = load_workflow(
            "name: n\ngoal: g\n"
            "defaults: {gates: [tests]}\n"
            "steps: [{name: a, prompt: p, gates: []}]\n"
        )
        assert workflow.steps[0].gates == ()


class TestRefusals:
    """Documents that should not compile."""

    def test_an_invalid_document_is_refused(self) -> None:
        with pytest.raises(SchemaError, match="not valid"):
            load_workflow("name: n\ngoal: g\nsteps: []\n")

    def test_every_problem_is_named_at_once(self) -> None:
        with pytest.raises(SchemaError) as caught:
            load_workflow("steps:\n  - depends_on: [ghost]\n")

        assert str(caught.value).count("- ") >= 4

    def test_a_cycle_is_caught_at_load_time(self) -> None:
        """Discovering it at run time would mean a run identifier for no work."""
        with pytest.raises(CycleError):
            load_workflow(
                "name: n\ngoal: g\nsteps:\n"
                "  - {name: a, prompt: p, depends_on: [b]}\n"
                "  - {name: b, prompt: p, depends_on: [a]}\n"
            )

    def test_an_unknown_dependency_is_refused(self) -> None:
        with pytest.raises(SchemaError, match="ghost"):
            load_workflow("name: n\ngoal: g\nsteps: [{name: a, prompt: p, depends_on: [ghost]}]\n")


class TestFiles:
    """Loading from disk."""

    def test_a_file_loads(self, tmp_dir: Path) -> None:
        path = tmp_dir / "workflow.yaml"
        path.write_text(LINEAR_YAML, encoding="utf-8")

        assert load_workflow_file(path).name == "ship"

    def test_a_missing_file_is_refused(self, tmp_dir: Path) -> None:
        with pytest.raises(SchemaError, match="no workflow file"):
            load_workflow_file(tmp_dir / "absent.yaml")

    def test_the_path_is_named_in_errors(self, tmp_dir: Path) -> None:
        path = tmp_dir / "broken.yaml"
        path.write_text("steps: []\n", encoding="utf-8")

        with pytest.raises(SchemaError, match="broken.yaml"):
            load_workflow_file(path)


class TestCheck:
    """Validation without compilation, for an editor or a hook."""

    def test_a_good_document_reports_nothing(self) -> None:
        assert check_workflow(LINEAR_YAML).ok

    def test_a_bad_document_reports_everything(self) -> None:
        report = check_workflow("steps:\n  - depends_on: [ghost]\n")

        assert not report.ok
        assert len(report.issues) >= 4

    def test_unparseable_yaml_becomes_one_issue_rather_than_raising(self) -> None:
        """A caller rendering a report has one thing to render."""
        report = check_workflow("name: [unclosed\n")

        assert not report.ok
        assert len(report.issues) == 1


class TestOneCompilationPath:
    """FR-1.2: a file and a generated plan produce equal graphs."""

    def test_yaml_and_a_document_compile_identically(self) -> None:
        document = {
            "name": "ship",
            "goal": "ship the feature",
            "steps": [
                {"name": "design", "prompt": "design it"},
                {"name": "build", "prompt": "build it", "depends_on": ["design"]},
                {"name": "verify", "prompt": "verify it", "depends_on": ["build"]},
            ],
        }
        from_yaml = load_workflow(LINEAR_YAML)
        from_document = workflow_from_document(document)

        assert from_yaml.name == from_document.name
        assert from_yaml.goal == from_document.goal
        assert [s.name for s in from_yaml.steps] == [s.name for s in from_document.steps]
        assert [s.depends_on for s in from_yaml.steps] == [
            s.depends_on for s in from_document.steps
        ]

    def test_the_graphs_have_the_same_shape(self) -> None:
        one = load_workflow(LINEAR_YAML).compile()
        two = workflow_from_document(
            {
                "name": "ship",
                "goal": "ship the feature",
                "steps": [
                    {"name": "design", "prompt": "design it"},
                    {"name": "build", "prompt": "build it", "depends_on": ["design"]},
                    {"name": "verify", "prompt": "verify it", "depends_on": ["build"]},
                ],
            }
        ).compile()

        assert one.graph.depth == two.graph.depth
        assert one.graph.max_width == two.graph.max_width
        assert [
            sorted(one.step_name(t) for t in layer) for layer in one.graph.layers()
        ] == [sorted(two.step_name(t) for t in layer) for layer in two.graph.layers()]

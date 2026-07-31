"""Tests for the workflow document schema."""

from __future__ import annotations

from typing import Any

import pytest

from orchestrator.planner.schema import (
    SCHEMA_VERSION,
    WORKFLOW_JSON_SCHEMA,
    DocumentDefaults,
    SchemaError,
    ValidationIssue,
    ValidationReport,
    json_schema,
    validate_document,
)


def minimal(**over: Any) -> dict[str, Any]:
    """The smallest valid document."""
    return {
        "name": "wf",
        "goal": "do the thing",
        "steps": [{"name": "a", "prompt": "do a"}],
        **over,
    }


def paths_of(document: Any) -> list[str]:
    """The paths of every issue a document produces."""
    return [issue.path for issue in validate_document(document).issues]


class TestAcceptance:
    """Documents that are fine."""

    def test_the_minimal_document_validates(self) -> None:
        assert validate_document(minimal()).ok

    def test_a_full_document_validates(self) -> None:
        document = {
            "version": 1,
            "name": "ship",
            "goal": "ship it",
            "max_concurrency": 8,
            "labels": ["release"],
            "defaults": {"role": "worker", "max_attempts": 2, "gates": ["tests"]},
            "steps": [
                {"name": "a", "prompt": "do a", "title": "Do A", "labels": ["fast"]},
                {
                    "name": "b",
                    "prompt": "do b",
                    "depends_on": ["a"],
                    "role": "reviewer",
                    "max_attempts": 5,
                    "gates": [{"name": "unit", "kind": "test", "required": True}],
                    "when": {"did_work": "a"},
                },
            ],
        }
        assert validate_document(document).ok

    def test_a_single_string_is_accepted_where_a_list_belongs(self) -> None:
        """The most common YAML slip, and unambiguous."""
        assert validate_document(
            minimal(steps=[{"name": "a", "prompt": "p"}, {"name": "b", "prompt": "p", "depends_on": "a"}])
        ).ok

    def test_a_report_is_falsy_when_bad(self) -> None:
        assert bool(validate_document(minimal()))
        assert not bool(validate_document({}))


class TestDocumentLevel:
    """The top of the file."""

    def test_a_non_mapping_is_refused(self) -> None:
        report = validate_document(["not", "a", "workflow"])

        assert not report.ok
        assert "table of workflow settings" in report.issues[0].message

    def test_a_missing_name_is_reported(self) -> None:
        assert "name" in paths_of({"goal": "g", "steps": [{"name": "a", "prompt": "p"}]})

    def test_a_missing_goal_is_reported(self) -> None:
        assert "goal" in paths_of({"name": "n", "steps": [{"name": "a", "prompt": "p"}]})

    def test_an_unknown_key_is_reported(self) -> None:
        """A quietly ignored `dependson` runs the steps in the wrong order."""
        assert "dependson" in paths_of(minimal(dependson=[]))

    def test_a_future_version_is_refused(self) -> None:
        report = validate_document(minimal(version=SCHEMA_VERSION + 1))

        assert not report.ok
        assert "upgrade OrchestratorPro" in str(report.issues[0])

    def test_a_bad_concurrency_is_reported(self) -> None:
        assert "max_concurrency" in paths_of(minimal(max_concurrency=0))
        assert "max_concurrency" in paths_of(minimal(max_concurrency="lots"))

    def test_a_boolean_is_not_a_number(self) -> None:
        assert "max_concurrency" in paths_of(minimal(max_concurrency=True))

    def test_defaults_must_be_a_table(self) -> None:
        assert "defaults" in paths_of(minimal(defaults=["role"]))

    def test_an_unknown_default_is_reported(self) -> None:
        assert "defaults.budget" in paths_of(minimal(defaults={"budget": 1}))

    def test_an_unknown_default_role_is_reported(self) -> None:
        assert "defaults.role" in paths_of(minimal(defaults={"role": "wizard"}))


class TestSteps:
    """The list that does the work."""

    def test_steps_are_required(self) -> None:
        assert "steps" in paths_of({"name": "n", "goal": "g"})

    def test_an_empty_list_is_refused(self) -> None:
        assert "steps" in paths_of(minimal(steps=[]))

    def test_a_non_list_is_refused(self) -> None:
        assert "steps" in paths_of(minimal(steps={"a": "b"}))

    def test_a_step_must_be_a_table(self) -> None:
        assert "steps[0]" in paths_of(minimal(steps=["just a string"]))

    def test_a_step_needs_a_name_and_a_prompt(self) -> None:
        paths = paths_of(minimal(steps=[{}]))

        assert "steps[0].name" in paths
        assert "steps[0].prompt" in paths

    def test_an_empty_prompt_is_refused(self) -> None:
        assert "steps[0].prompt" in paths_of(minimal(steps=[{"name": "a", "prompt": "  "}]))

    def test_duplicate_names_are_reported(self) -> None:
        report = validate_document(
            minimal(steps=[{"name": "a", "prompt": "p"}, {"name": "a", "prompt": "p"}])
        )
        assert any("two steps are named" in issue.message for issue in report.issues)

    def test_an_unknown_dependency_is_reported(self) -> None:
        paths = paths_of(minimal(steps=[{"name": "a", "prompt": "p", "depends_on": ["ghost"]}]))
        assert "steps[0].depends_on[0]" in paths

    def test_the_error_lists_the_known_steps(self) -> None:
        """So the fix is visible without opening the file again."""
        report = validate_document(
            minimal(steps=[{"name": "a", "prompt": "p"}, {"name": "b", "prompt": "p", "depends_on": ["c"]}])
        )
        assert "a, b" in str(report.issues[0])

    def test_a_self_dependency_is_reported(self) -> None:
        paths = paths_of(minimal(steps=[{"name": "a", "prompt": "p", "depends_on": ["a"]}]))
        assert "steps[0].depends_on[0]" in paths

    def test_an_unknown_step_key_is_reported(self) -> None:
        assert "steps[0].retries" in paths_of(minimal(steps=[{"name": "a", "prompt": "p", "retries": 2}]))

    def test_an_unknown_role_is_reported(self) -> None:
        assert "steps[0].role" in paths_of(minimal(steps=[{"name": "a", "prompt": "p", "role": "wizard"}]))

    def test_an_absurd_attempt_count_is_reported(self) -> None:
        assert "steps[0].max_attempts" in paths_of(
            minimal(steps=[{"name": "a", "prompt": "p", "max_attempts": 0}])
        )


class TestGates:
    """Both accepted shapes."""

    def test_a_bare_name_is_accepted(self) -> None:
        assert validate_document(minimal(steps=[{"name": "a", "prompt": "p", "gates": ["unit"]}])).ok

    def test_a_table_is_accepted(self) -> None:
        document = minimal(
            steps=[{"name": "a", "prompt": "p", "gates": [{"name": "unit", "kind": "test"}]}]
        )
        assert validate_document(document).ok

    def test_an_unnamed_gate_is_reported(self) -> None:
        assert "steps[0].gates[0].name" in paths_of(
            minimal(steps=[{"name": "a", "prompt": "p", "gates": [{"kind": "test"}]}])
        )

    def test_an_unknown_kind_is_reported(self) -> None:
        assert "steps[0].gates[0].kind" in paths_of(
            minimal(steps=[{"name": "a", "prompt": "p", "gates": [{"name": "g", "kind": "vibes"}]}])
        )

    def test_a_non_boolean_required_is_reported(self) -> None:
        assert "steps[0].gates[0].required" in paths_of(
            minimal(steps=[{"name": "a", "prompt": "p", "gates": [{"name": "g", "required": "yes"}]}])
        )

    def test_an_empty_gate_name_is_reported(self) -> None:
        assert "steps[0].gates[0]" in paths_of(
            minimal(steps=[{"name": "a", "prompt": "p", "gates": [" "]}])
        )


class TestConditions:
    """The `when` clause."""

    def _with(self, when: Any) -> dict[str, Any]:
        return minimal(
            steps=[
                {"name": "a", "prompt": "p"},
                {"name": "b", "prompt": "p", "depends_on": ["a"], "when": when},
            ]
        )

    def test_did_work_is_accepted(self) -> None:
        assert validate_document(self._with({"did_work": "a"})).ok

    def test_was_skipped_is_accepted(self) -> None:
        assert validate_document(self._with({"was_skipped": "a"})).ok

    def test_combinators_nest(self) -> None:
        condition = {"all_of": [{"did_work": "a"}, {"not": {"was_skipped": "a"}}]}
        assert validate_document(self._with(condition)).ok

    def test_a_condition_naming_an_unknown_step_is_reported(self) -> None:
        assert any("ghost" in str(issue) for issue in validate_document(self._with({"did_work": "ghost"})).issues)

    def test_an_empty_condition_is_reported(self) -> None:
        report = validate_document(self._with({}))
        assert any("must name a condition" in issue.message for issue in report.issues)

    def test_two_conditions_at_once_are_reported(self) -> None:
        """Ambiguous — wrap them, do not guess."""
        report = validate_document(self._with({"did_work": "a", "was_skipped": "a"}))
        assert any("wrap them" in str(issue) for issue in report.issues)

    def test_an_unknown_condition_key_is_reported(self) -> None:
        report = validate_document(self._with({"succeeded": "a"}))
        assert any("succeeded" in issue.path for issue in report.issues)

    def test_an_empty_combinator_is_reported(self) -> None:
        report = validate_document(self._with({"all_of": []}))
        assert any("non-empty" in issue.message for issue in report.issues)

    def test_a_nested_fault_reports_its_position(self) -> None:
        report = validate_document(self._with({"any_of": [{"did_work": "a"}, {"did_work": "ghost"}]}))
        assert any("any_of[1]" in issue.path for issue in report.issues)


class TestReport:
    """How problems are reported."""

    def test_every_problem_is_collected(self) -> None:
        """Four mistakes should produce four messages, not the first one."""
        report = validate_document({"steps": [{"depends_on": ["ghost"]}]})
        assert len(report.issues) >= 4

    def test_raising_names_them_all(self) -> None:
        report = validate_document({})

        with pytest.raises(SchemaError) as caught:
            report.raise_if_bad(source="the file")

        assert "the file is not valid" in str(caught.value)
        assert caught.value.detail["issues"]

    def test_raising_on_a_good_document_does_nothing(self) -> None:
        validate_document(minimal()).raise_if_bad()

    def test_an_issue_reads_as_a_sentence(self) -> None:
        issue = ValidationIssue(path="steps[0].name", message="is required", hint="add one")
        assert str(issue) == "steps[0].name: is required (add one)"

    def test_a_document_level_issue_says_so(self) -> None:
        assert str(ValidationIssue(path="", message="is empty")).startswith("<document>")

    def test_the_summary_is_short(self) -> None:
        assert validate_document(minimal()).summary() == "valid"
        assert "problem" in validate_document({}).summary()

    def test_structural_faults_suppress_dependent_checks(self) -> None:
        """Reporting a missing name and then its consequences is noise."""
        report = validate_document({"name": "n", "goal": "g", "steps": "nope"})
        assert len(report.issues) == 1


class TestJsonSchema:
    """The constraint a model generates under."""

    def test_it_is_a_closed_object(self) -> None:
        assert WORKFLOW_JSON_SCHEMA["additionalProperties"] is False

    def test_the_essentials_are_required(self) -> None:
        assert set(WORKFLOW_JSON_SCHEMA["required"]) == {"name", "goal", "steps"}

    def test_a_step_requires_a_name_and_a_prompt(self) -> None:
        step = WORKFLOW_JSON_SCHEMA["properties"]["steps"]["items"]
        assert set(step["required"]) == {"name", "prompt"}
        assert step["additionalProperties"] is False

    def test_gates_are_constrained_to_one_shape(self) -> None:
        """A model given two shapes will use both within one document."""
        gates = WORKFLOW_JSON_SCHEMA["properties"]["steps"]["items"]["properties"]["gates"]
        assert gates["items"] == {"type": "string"}

    def test_the_step_count_is_bounded(self) -> None:
        steps = WORKFLOW_JSON_SCHEMA["properties"]["steps"]
        assert steps["minItems"] == 1
        assert steps["maxItems"] <= 50

    def test_it_renders_as_a_format_block(self) -> None:
        block = json_schema()

        assert block["type"] == "json_schema"
        assert block["schema"] is WORKFLOW_JSON_SCHEMA

    def test_anything_the_json_schema_allows_the_validator_allows(self) -> None:
        """The two must not disagree about what a valid plan is."""
        generated = {
            "name": "n",
            "goal": "g",
            "max_concurrency": 4,
            "steps": [
                {
                    "name": "a",
                    "title": "A",
                    "prompt": "p",
                    "depends_on": [],
                    "role": "worker",
                    "max_attempts": 3,
                    "labels": ["x"],
                    "gates": ["tests"],
                }
            ],
        }
        assert validate_document(generated).ok


class TestDefaults:
    """The defaults table, resolved."""

    def test_it_reads_the_table(self) -> None:
        defaults = DocumentDefaults.from_document(
            {"defaults": {"role": "reviewer", "max_attempts": 5, "gates": ["tests"]}}
        )

        assert defaults.role == "reviewer"
        assert defaults.max_attempts == 5
        assert defaults.gates == ("tests",)

    def test_an_absent_table_yields_the_built_ins(self) -> None:
        defaults = DocumentDefaults.from_document({})

        assert defaults.role == "worker"
        assert defaults.max_attempts == 3
        assert defaults.labels == ()

    def test_a_single_label_becomes_a_tuple(self) -> None:
        assert DocumentDefaults.from_document({"defaults": {"labels": "x"}}).labels == ("x",)

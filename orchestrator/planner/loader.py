"""Compiling a validated document into an executable workflow.

This is the single path from a document to a
:class:`~orchestrator.workflow.definition.WorkflowDefinition`. A hand-written
YAML file and an LLM's structured output both arrive here, which is what makes
FR-1.2 true rather than aspirational: they cannot diverge, because there is
only one translation.

The YAML is parsed with ``safe_load``. Nothing else would do — a workflow file
may have been written by an agent, and the full loader can construct arbitrary
Python objects from a document. That is a remote-code-execution primitive
wearing a configuration format.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from orchestrator.agent.model import AgentRole
from orchestrator.planner.schema import (
    DocumentDefaults,
    SchemaError,
    ValidationReport,
    validate_document,
)
from orchestrator.task.model import GateKind, GateSpec
from orchestrator.workflow.definition import (
    AllOf,
    AnyOf,
    Condition,
    Not,
    StepDefinition,
    StepDidWork,
    StepSkipped,
    WorkflowDefinition,
)

__all__ = [
    "load_workflow",
    "load_workflow_file",
    "parse_document",
    "workflow_from_document",
]

#: A ceiling on the document size. A workflow is a page or two; anything larger
#: is a mistake or an attempt, and YAML's parser is not the place to discover it.
MAX_DOCUMENT_BYTES = 1_048_576


def parse_document(text: str, *, source: str = "<string>") -> dict[str, Any]:
    """Parse YAML into a plain mapping.

    Args:
        text: The document.
        source: Named in errors.

    Returns:
        The parsed document.

    Raises:
        SchemaError: If it is too large, is not valid YAML, or is not a mapping.
    """
    if len(text.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise SchemaError(
            f"{source} is larger than {MAX_DOCUMENT_BYTES} bytes; a workflow "
            "document is a page or two",
            detail={"source": source},
        )

    try:
        # safe_load, never load: this document may have been written by an agent.
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SchemaError(
            f"{source} is not valid YAML: {exc}", detail={"source": source}
        ) from exc

    if parsed is None:
        raise SchemaError(f"{source} is empty", detail={"source": source})
    if not isinstance(parsed, Mapping):
        raise SchemaError(
            f"{source} must be a table of workflow settings, got "
            f"{type(parsed).__name__}",
            detail={"source": source},
        )
    return dict(parsed)


def _condition_from(value: Mapping[str, Any]) -> Condition:
    """Translate a validated ``when`` clause into a domain condition."""
    if "did_work" in value:
        return StepDidWork(str(value["did_work"]))
    if "was_skipped" in value:
        return StepSkipped(str(value["was_skipped"]))
    if "all_of" in value:
        return AllOf(tuple(_condition_from(item) for item in value["all_of"]))
    if "any_of" in value:
        return AnyOf(tuple(_condition_from(item) for item in value["any_of"]))
    return Not(_condition_from(value["not"]))


def _gate_from(value: Any, *, position: int) -> GateSpec:
    """Translate a gate entry, in either of the two accepted shapes."""
    if isinstance(value, str):
        return GateSpec(name=value)

    kind = value.get("kind", "test")
    try:
        gate_kind = GateKind(kind)
    except ValueError as exc:  # pragma: no cover - validation rejects this first
        raise SchemaError(
            f"gate {position} has an unknown kind {kind!r}", detail={"kind": kind}
        ) from exc

    return GateSpec(
        name=str(value["name"]),
        kind=gate_kind,
        required=bool(value.get("required", True)),
    )


def _gates_from(entries: Sequence[Any]) -> tuple[GateSpec, ...]:
    """Translate a list of gate entries."""
    return tuple(_gate_from(entry, position=index) for index, entry in enumerate(entries))


def workflow_from_document(
    document: Mapping[str, Any], *, source: str = "<document>", validate: bool = True
) -> WorkflowDefinition:
    """Compile a document into an executable workflow.

    Args:
        document: The parsed document.
        source: Named in errors.
        validate: Whether to run the schema first. Only turn this off if the
            document has already been validated by this same code — the domain
            objects will refuse malformed input anyway, one problem at a time,
            which is a worse experience than the report.

    Returns:
        The definition, already compiled once so a cycle is caught here rather
        than at execution.

    Raises:
        SchemaError: If the document is not valid.
        WorkflowDefinitionError: If the domain refuses it for a reason the
            schema does not model.
        CycleError: If the dependencies form a cycle.
    """
    if validate:
        validate_document(document).raise_if_bad(source=source)

    defaults = DocumentDefaults.from_document(document)
    steps: list[StepDefinition] = []

    for entry in document["steps"]:
        labels = entry.get("labels", defaults.labels)
        if isinstance(labels, str):
            labels = [labels]

        gates = entry.get("gates")
        gate_entries = _as_list(gates) if gates is not None else list(defaults.gates)

        when = entry.get("when")
        steps.append(
            StepDefinition(
                name=str(entry["name"]),
                prompt=str(entry["prompt"]),
                title=str(entry.get("title", "")),
                depends_on=tuple(_as_list(entry.get("depends_on"))),
                role=AgentRole(str(entry.get("role", defaults.role))),
                max_attempts=int(entry.get("max_attempts", defaults.max_attempts)),
                labels=frozenset(labels),
                gates=_gates_from(gate_entries),
                condition=_condition_from(when) if when else None,
                expects_changes=bool(
                    entry.get("expects_changes", defaults.expects_changes)
                ),
            )
        )

    definition = WorkflowDefinition(
        name=str(document["name"]),
        goal=str(document["goal"]),
        steps=tuple(steps),
        max_concurrency=int(document.get("max_concurrency", 4)),
    )
    # Compile once here so a cycle is a load-time error. Discovering it when the
    # run starts would mean a run identifier in the log for work that never was.
    definition.compile()
    return definition


def load_workflow(text: str, *, source: str = "<string>") -> WorkflowDefinition:
    """Parse, validate, and compile a YAML workflow.

    Args:
        text: The document.
        source: Named in errors.

    Returns:
        The definition.

    Raises:
        SchemaError: If the document is malformed or invalid.
    """
    return workflow_from_document(parse_document(text, source=source), source=source)


def load_workflow_file(path: Path) -> WorkflowDefinition:
    """Load a workflow from a file.

    Args:
        path: The ``.yaml`` file.

    Returns:
        The definition.

    Raises:
        SchemaError: If the file is missing, malformed, or invalid.
    """
    if not path.is_file():
        raise SchemaError(
            f"there is no workflow file at {path}", detail={"path": str(path)}
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SchemaError(
            f"{path} could not be read: {exc}", detail={"path": str(path)}
        ) from exc
    return load_workflow(text, source=str(path))


def check_workflow(text: str, *, source: str = "<string>") -> ValidationReport:
    """Validate a document without compiling it.

    For an editor, a pre-commit hook, or a CLI that should report every problem
    at once rather than the first one.

    Args:
        text: The document.
        source: Named in errors.

    Returns:
        The report. A document that fails to parse at all yields a single issue
        rather than raising, so a caller has one thing to render.
    """
    try:
        document = parse_document(text, source=source)
    except SchemaError as exc:
        from orchestrator.planner.schema import ValidationIssue

        return ValidationReport((ValidationIssue(path="", message=str(exc)),))
    return validate_document(document)


def _as_list(value: Any) -> list[Any]:
    """Coerce an optional scalar-or-list field into a list."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value) if isinstance(value, Sequence) else [value]

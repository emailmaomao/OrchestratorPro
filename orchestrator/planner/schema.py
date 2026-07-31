"""The workflow document schema, and the JSON Schema an LLM is held to.

One schema, two producers. A hand-written YAML file and a model's structured
output are validated by the same code and compiled by the same path (FR-1.2),
because two validators eventually disagree and the disagreement is always
discovered by a plan that ran when it should not have.

Validation is strict in both directions:

* **Unknown keys are errors.** A workflow whose ``dependson`` was quietly
  ignored is a workflow that runs its steps in the wrong order and looks fine
  doing it.
* **Errors are collected, not raised one at a time.** A file with four mistakes
  should report four mistakes. Fixing them one edit-and-rerun at a time is how
  a declarative format acquires a reputation for being fussy.

The JSON Schema here is the same shape, emitted for
``output_config.format`` so a model is constrained at generation time rather
than corrected afterwards.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from orchestrator.core.events import OrchestratorError

__all__ = [
    "SCHEMA_VERSION",
    "WORKFLOW_JSON_SCHEMA",
    "SchemaError",
    "ValidationIssue",
    "ValidationReport",
    "validate_document",
]

#: The document shape this build reads. A workflow may declare it; one that
#: does not is assumed to be current, because every document written so far is.
SCHEMA_VERSION = 1

#: Keys allowed at the top level of a workflow document.
_DOCUMENT_KEYS = frozenset(
    {"version", "name", "goal", "max_concurrency", "labels", "defaults", "steps"}
)

#: Keys allowed on one step.
_STEP_KEYS = frozenset(
    {
        "name",
        "prompt",
        "title",
        "depends_on",
        "role",
        "max_attempts",
        "labels",
        "gates",
        "when",
        "expects_changes",
    }
)

#: Keys allowed in the ``defaults`` table, each supplying a step field.
_DEFAULT_KEYS = frozenset(
    {"role", "max_attempts", "labels", "gates", "expects_changes"}
)

#: Keys allowed on a gate.
_GATE_KEYS = frozenset({"name", "kind", "required", "command"})

#: The conditions a document may express. Deliberately small: a workflow
#: language that grows an expression evaluator has become a programming
#: language, and this one has a scheduler to answer to.
_CONDITION_KEYS = frozenset({"did_work", "was_skipped", "all_of", "any_of", "not"})

_ROLES = ("worker", "planner", "reviewer", "summarizer")
_GATE_KINDS = ("test", "lint", "typecheck", "build", "human", "custom")


class SchemaError(OrchestratorError):
    """A workflow document does not conform to the schema."""

    code = "workflow_schema"
    retryable = False


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """One problem with a document.

    Attributes:
        path: Where it is, in dotted form — ``steps[2].depends_on``.
        message: What is wrong, in one sentence.
        hint: What to do about it, when there is something specific to say.
    """

    path: str
    message: str
    hint: str = ""

    def __str__(self) -> str:
        where = self.path or "<document>"
        return f"{where}: {self.message}" + (f" ({self.hint})" if self.hint else "")


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """Every problem with a document, together."""

    issues: tuple[ValidationIssue, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether the document is usable."""
        return not self.issues

    def __bool__(self) -> bool:
        return self.ok

    def raise_if_bad(self, *, source: str = "workflow") -> None:
        """Raise a single error describing everything that is wrong.

        Raises:
            SchemaError: If there are any issues.
        """
        if self.ok:
            return
        lines = "\n".join(f"  - {issue}" for issue in self.issues)
        raise SchemaError(
            f"{source} is not valid ({len(self.issues)} problem(s)):\n{lines}",
            detail={"issues": [{"path": i.path, "message": i.message} for i in self.issues]},
        )

    def summary(self) -> str:
        """A one-line account."""
        return "valid" if self.ok else f"{len(self.issues)} problem(s)"


def _issue(issues: list[ValidationIssue], path: str, message: str, hint: str = "") -> None:
    """Record one problem."""
    issues.append(ValidationIssue(path=path, message=message, hint=hint))


def _check_unknown(
    issues: list[ValidationIssue], path: str, data: Mapping[str, Any], known: Iterable[str]
) -> None:
    """Record any key outside ``known``."""
    allowed = set(known)
    for key in sorted(set(data) - allowed):
        _issue(
            issues,
            f"{path}.{key}" if path else key,
            "is not a recognized key",
            f"expected one of: {', '.join(sorted(allowed))}",
        )


def _string(
    issues: list[ValidationIssue],
    path: str,
    data: Mapping[str, Any],
    key: str,
    *,
    required: bool = False,
    max_length: int = 100_000,
) -> str | None:
    """Read a string field, recording anything wrong with it."""
    where = f"{path}.{key}" if path else key
    if key not in data:
        if required:
            _issue(issues, where, "is required")
        return None
    value = data[key]
    if not isinstance(value, str):
        _issue(issues, where, f"must be text, got {type(value).__name__}")
        return None
    if required and not value.strip():
        _issue(issues, where, "must not be empty")
        return None
    if len(value) > max_length:
        _issue(issues, where, f"is longer than {max_length} characters")
        return None
    return value


def _int(
    issues: list[ValidationIssue],
    path: str,
    data: Mapping[str, Any],
    key: str,
    *,
    minimum: int = 1,
    maximum: int = 1000,
) -> int | None:
    """Read an integer field within bounds."""
    where = f"{path}.{key}" if path else key
    if key not in data:
        return None
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int):
        _issue(issues, where, f"must be a whole number, got {type(value).__name__}")
        return None
    if not minimum <= value <= maximum:
        _issue(issues, where, f"must be between {minimum} and {maximum}, got {value}")
        return None
    return value


def _string_list(
    issues: list[ValidationIssue], path: str, data: Mapping[str, Any], key: str
) -> list[str]:
    """Read a list of strings."""
    where = f"{path}.{key}" if path else key
    if key not in data:
        return []
    value = data[key]
    if isinstance(value, str):
        # A single string where a list belongs is the most common YAML slip.
        # Accepting it silently would be a kindness that hides a typo, so it is
        # accepted and reported as nothing — the shape is unambiguous.
        return [value]
    if not isinstance(value, list):
        _issue(issues, where, f"must be a list, got {type(value).__name__}")
        return []

    collected: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            _issue(issues, f"{where}[{index}]", f"must be text, got {type(item).__name__}")
            continue
        collected.append(item)
    return collected


def _validate_condition(
    issues: list[ValidationIssue], path: str, value: Any, step_names: set[str]
) -> None:
    """Validate a ``when`` clause."""
    if not isinstance(value, Mapping):
        _issue(issues, path, f"must be a table, got {type(value).__name__}")
        return

    _check_unknown(issues, path, value, _CONDITION_KEYS)
    present = [key for key in _CONDITION_KEYS if key in value]
    if not present:
        _issue(
            issues,
            path,
            "must name a condition",
            f"one of: {', '.join(sorted(_CONDITION_KEYS))}",
        )
        return
    if len(present) > 1:
        _issue(
            issues,
            path,
            f"names {len(present)} conditions at once",
            "wrap them in all_of or any_of",
        )
        return

    key = present[0]
    inner = value[key]

    if key in ("did_work", "was_skipped"):
        if not isinstance(inner, str):
            _issue(issues, f"{path}.{key}", "must name a step")
        elif inner not in step_names:
            _issue(
                issues,
                f"{path}.{key}",
                f"names {inner!r}, which is not a step in this workflow",
                f"known steps: {', '.join(sorted(step_names))}",
            )
    elif key in ("all_of", "any_of"):
        if not isinstance(inner, list) or not inner:
            _issue(issues, f"{path}.{key}", "must be a non-empty list of conditions")
            return
        for index, item in enumerate(inner):
            _validate_condition(issues, f"{path}.{key}[{index}]", item, step_names)
    else:  # not
        _validate_condition(issues, f"{path}.not", inner, step_names)


def _validate_gate(issues: list[ValidationIssue], path: str, value: Any) -> None:
    """Validate one gate entry."""
    if isinstance(value, str):
        if not value.strip():
            _issue(issues, path, "must not be empty")
        return
    if not isinstance(value, Mapping):
        _issue(issues, path, f"must be a name or a table, got {type(value).__name__}")
        return

    _check_unknown(issues, path, value, _GATE_KEYS)
    _string(issues, path, value, "name", required=True, max_length=100)
    kind = _string(issues, path, value, "kind", max_length=40)
    if kind is not None and kind not in _GATE_KINDS:
        _issue(
            issues,
            f"{path}.kind",
            f"must be one of: {', '.join(_GATE_KINDS)}; got {kind!r}",
        )
    if "required" in value and not isinstance(value["required"], bool):
        _issue(issues, f"{path}.required", "must be true or false")


def validate_document(document: Any) -> ValidationReport:
    """Check a parsed workflow document against the schema.

    Args:
        document: The parsed YAML or the model's structured output.

    Returns:
        Every problem found, together. Structural problems suppress the checks
        that depend on them — reporting "step 3 has no name" and then "step 3's
        dependency is unknown" for the same missing name is noise.
    """
    issues: list[ValidationIssue] = []

    if not isinstance(document, Mapping):
        _issue(
            issues,
            "",
            f"must be a table of workflow settings, got {type(document).__name__}",
            "a workflow document begins with 'name:' and 'goal:'",
        )
        return ValidationReport(tuple(issues))

    _check_unknown(issues, "", document, _DOCUMENT_KEYS)

    version = _int(issues, "", document, "version", minimum=1, maximum=99)
    if version is not None and version > SCHEMA_VERSION:
        _issue(
            issues,
            "version",
            f"is {version}; this build reads version {SCHEMA_VERSION}",
            "upgrade OrchestratorPro",
        )

    _string(issues, "", document, "name", required=True, max_length=100)
    _string(issues, "", document, "goal", required=True, max_length=4000)
    _int(issues, "", document, "max_concurrency", minimum=1, maximum=64)
    _string_list(issues, "", document, "labels")

    defaults = document.get("defaults", {})
    if "defaults" in document:
        if isinstance(defaults, Mapping):
            _check_unknown(issues, "defaults", defaults, _DEFAULT_KEYS)
            role = _string(issues, "defaults", defaults, "role", max_length=40)
            if role is not None and role not in _ROLES:
                _issue(
                    issues,
                    "defaults.role",
                    f"must be one of: {', '.join(_ROLES)}; got {role!r}",
                )
            _int(issues, "defaults", defaults, "max_attempts", minimum=1, maximum=100)
            _string_list(issues, "defaults", defaults, "labels")
            for index, gate in enumerate(_as_list(defaults.get("gates"))):
                _validate_gate(issues, f"defaults.gates[{index}]", gate)
        else:
            _issue(issues, "defaults", "must be a table")

    steps = document.get("steps")
    if steps is None:
        _issue(issues, "steps", "is required", "a workflow with no steps does nothing")
        return ValidationReport(tuple(issues))
    if not isinstance(steps, list):
        _issue(issues, "steps", f"must be a list, got {type(steps).__name__}")
        return ValidationReport(tuple(issues))
    if not steps:
        _issue(issues, "steps", "must not be empty")
        return ValidationReport(tuple(issues))

    names: list[str] = []
    for index, step in enumerate(steps):
        if not isinstance(step, Mapping):
            _issue(issues, f"steps[{index}]", f"must be a table, got {type(step).__name__}")
            continue
        name = _string(issues, f"steps[{index}]", step, "name", required=True, max_length=100)
        if name:
            names.append(name)

    duplicates = sorted({name for name in names if names.count(name) > 1})
    for name in duplicates:
        _issue(
            issues,
            "steps",
            f"two steps are named {name!r}",
            "step names identify a step in dependencies, conditions, and the log",
        )
    known = set(names)

    for index, step in enumerate(steps):
        if not isinstance(step, Mapping):
            continue
        path = f"steps[{index}]"
        _check_unknown(issues, path, step, _STEP_KEYS)
        _string(issues, path, step, "prompt", required=True)
        _string(issues, path, step, "title", max_length=200)
        _int(issues, path, step, "max_attempts", minimum=1, maximum=100)
        _string_list(issues, path, step, "labels")

        role = _string(issues, path, step, "role", max_length=40)
        if role is not None and role not in _ROLES:
            _issue(issues, f"{path}.role", f"must be one of: {', '.join(_ROLES)}; got {role!r}")

        own_name = step.get("name") if isinstance(step.get("name"), str) else None
        for position, dependency in enumerate(_string_list(issues, path, step, "depends_on")):
            if dependency == own_name:
                _issue(issues, f"{path}.depends_on[{position}]", "depends on itself")
            elif dependency not in known:
                _issue(
                    issues,
                    f"{path}.depends_on[{position}]",
                    f"names {dependency!r}, which is not a step in this workflow",
                    f"known steps: {', '.join(sorted(known)) or '(none)'}",
                )

        for position, gate in enumerate(_as_list(step.get("gates"))):
            _validate_gate(issues, f"{path}.gates[{position}]", gate)

        if "when" in step:
            _validate_condition(issues, f"{path}.when", step["when"], known)

    return ValidationReport(tuple(issues))


def _as_list(value: Any) -> list[Any]:
    """Coerce an optional scalar-or-list field into a list."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


#: The schema a model's structured output is constrained by.
#:
#: Deliberately a subset of what the YAML reader accepts: a model given the
#: choice between ``gates: [unit]`` and ``gates: [{name: unit}]`` will use both
#: within one document. Constraining generation is cheaper than repairing it.
WORKFLOW_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "goal", "steps"],
    "properties": {
        "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 100,
            "description": "A short identifier for the workflow, in kebab-case.",
        },
        "goal": {
            "type": "string",
            "minLength": 1,
            "maxLength": 4000,
            "description": "The outcome the workflow achieves, restated.",
        },
        "max_concurrency": {"type": "integer", "minimum": 1, "maximum": 64},
        "steps": {
            "type": "array",
            "minItems": 1,
            "maxItems": 50,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "prompt"],
                "properties": {
                    "name": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 100,
                        "description": "Unique within the workflow. Referenced by depends_on.",
                    },
                    "title": {"type": "string", "maxLength": 200},
                    "prompt": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            "The complete instruction for one agent. It must be "
                            "self-contained: the agent sees this and the repository, "
                            "not the other steps."
                        ),
                    },
                    "depends_on": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Names of steps that must succeed first.",
                    },
                    "role": {"type": "string", "enum": list(_ROLES)},
                    "max_attempts": {"type": "integer", "minimum": 1, "maximum": 10},
                    "labels": {"type": "array", "items": {"type": "string"}},
                    "gates": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Named checks this step's output must clear.",
                    },
                },
            },
        },
    },
}


def json_schema(*, name: str = "workflow") -> dict[str, Any]:
    """Return the structured-output format block for a provider request."""
    return {"type": "json_schema", "name": name, "schema": WORKFLOW_JSON_SCHEMA}


@dataclass(frozen=True, slots=True)
class DocumentDefaults:
    """The ``defaults`` table, resolved.

    Split out so the loader applies defaults in one place and a step's own
    setting always wins — a default that overrode an explicit value would be
    the most confusing bug this format could have.
    """

    role: str = "worker"
    max_attempts: int = 3
    labels: tuple[str, ...] = ()
    gates: tuple[Any, ...] = field(default_factory=tuple)
    expects_changes: bool = True

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> DocumentDefaults:
        """Read the defaults table, which validation has already checked."""
        table = document.get("defaults") or {}
        labels = table.get("labels") or []
        return cls(
            role=str(table.get("role", "worker")),
            max_attempts=int(table.get("max_attempts", 3)),
            labels=tuple([labels] if isinstance(labels, str) else labels),
            gates=tuple(_as_list(table.get("gates"))),
            expects_changes=bool(table.get("expects_changes", True)),
        )


def known_roles() -> Sequence[str]:
    """The agent roles a document may name."""
    return _ROLES


def known_gate_kinds() -> Sequence[str]:
    """The gate kinds a document may name."""
    return _GATE_KINDS

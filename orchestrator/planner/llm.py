"""The LLM planner: a goal in, a validated workflow out.

Decomposition is a model call constrained by a JSON schema (FR-1.1), and the
result goes through the same loader a hand-written file does (FR-1.2). Nothing
here repairs a plan; a plan that does not validate is reported as a typed
failure naming what is wrong (FR-1.5), and the model is given that report and
asked again.

Three things this module refuses to do, each because the alternative looks like
it works:

* **It does not accept a plan it cannot compile.** A cycle, an unknown
  dependency, or a step that references itself comes back as an error, not as a
  graph that deadlocks at run time.
* **It does not silently repair.** Dropping an unknown dependency would produce
  a plan the model did not propose and nobody reviewed.
* **It does not execute anything.** A generated plan is a proposal; §6 of the
  specification requires human approval before it runs unless ``auto_approve``
  is set, and that decision belongs to the workflow engine, not here.

The planner is provider-independent: it takes the neutral ``ModelPort`` and
never names a vendor.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from orchestrator.core.events import OrchestratorError
from orchestrator.core.logging import get_logger
from orchestrator.planner.loader import workflow_from_document
from orchestrator.planner.schema import (
    ValidationReport,
    json_schema,
    validate_document,
)
from orchestrator.core.config import Effort
from orchestrator.provider.base import (
    CompletionRequest,
    Message,
    ModelPort,
    Role,
    StopReason,
    TextBlock,
)
from orchestrator.workflow.definition import WorkflowDefinition, WorkflowDefinitionError

__all__ = [
    "PLANNER_SYSTEM_PROMPT",
    "PlannerError",
    "PlannerResult",
    "PlanRequest",
    "WorkflowPlanner",
]

_log = get_logger(__name__)


class PlannerError(OrchestratorError):
    """A plan could not be produced."""

    code = "planner"
    retryable = True


class PlannerRefused(PlannerError):
    """The backend declined to produce a plan."""

    code = "planner_refused"
    retryable = False


class PlannerInvalid(PlannerError):
    """The backend produced something that is not a usable workflow."""

    code = "planner_invalid"
    retryable = True


#: The planner's frozen system prompt.
#:
#: Stable by construction — no timestamps, no identifiers, no goal text — so it
#: sits in the cacheable prefix. The goal arrives as a message.
PLANNER_SYSTEM_PROMPT = """\
You decompose a software change into a dependency graph of tasks.

Each task is handed to a separate agent working alone in its own checkout of \
the repository. An agent sees its own prompt and the repository. It does not \
see the goal, the other tasks, or what they produced. Every prompt must \
therefore be self-contained.

Rules for a good decomposition:

- Split on what can proceed independently, not on what reads tidily. Two steps \
  that must happen in order and cannot be reviewed separately are one step.
- Declare a dependency only when the later step genuinely needs the earlier \
  one's output. A dependency that is not real costs parallelism for nothing.
- Never create a cycle. A step must not depend on itself, directly or through \
  another step.
- Prefer few, substantial steps over many trivial ones. Each step costs a \
  checkout, an agent, and a gate run.
- Name steps in kebab-case, describing the outcome rather than the action: \
  `client-uses-httpx`, not `edit-the-client`.
- Attach the `tests` gate to any step that changes behaviour.

Write each prompt as an instruction to a competent engineer who has the \
repository open and no other context: what to change, where, and what "done" \
means. Do not write "as discussed" or refer to other steps by number.
"""


@dataclass(frozen=True, slots=True)
class PlanRequest:
    """What to decompose.

    Attributes:
        goal: The change to make, in the operator's words.
        repo_summary: What the repository looks like — languages, layout, test
            command. Optional but load-bearing: without it a model invents a
            project structure and writes prompts against the one it imagined.
        constraints: Anything the plan must respect.
        max_steps: A ceiling on the decomposition.
        model: The model to ask.
        effort: How much depth to spend. Planning is the one call in the system
            where depth is unambiguously worth paying for: a bad decomposition
            costs every attempt that follows it.
    """

    goal: str
    repo_summary: str = ""
    constraints: tuple[str, ...] = ()
    max_steps: int = 12
    model: str = "claude-opus-5"
    effort: Effort = Effort.XHIGH

    def __post_init__(self) -> None:
        if not self.goal.strip():
            raise PlannerError("a plan needs a goal")
        if not 1 <= self.max_steps <= 50:
            raise PlannerError(
                f"max_steps must be between 1 and 50, got {self.max_steps}"
            )

    def render(self) -> str:
        """Render the goal and its context as the opening message."""
        parts = [f"# Goal\n\n{self.goal.strip()}"]
        if self.repo_summary.strip():
            parts.append(f"# Repository\n\n{self.repo_summary.strip()}")
        if self.constraints:
            listed = "\n".join(f"- {item}" for item in self.constraints)
            parts.append(f"# Constraints\n\n{listed}")
        parts.append(
            f"# Output\n\nProduce at most {self.max_steps} steps. Return only the "
            "workflow object."
        )
        return "\n\n".join(parts)


@dataclass(frozen=True, slots=True)
class PlannerResult:
    """What one planning session produced."""

    workflow: WorkflowDefinition | None
    attempts: int = 1
    document: Mapping[str, Any] = field(default_factory=dict)
    report: ValidationReport = field(default_factory=ValidationReport)
    tokens_in: int = 0
    tokens_out: int = 0
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether a usable workflow was produced."""
        return self.workflow is not None

    def summary(self) -> str:
        """A one-line account."""
        # Bound locally rather than asserted: `assert` vanishes under
        # `python -O`, so it must never be what makes code correct. The
        # binding narrows for the type checker and survives optimisation.
        workflow = self.workflow
        if workflow is None:
            return f"no plan after {self.attempts} attempt(s): {self.report.summary()}"
        return (
            f"{len(workflow.steps)} step(s) in {self.attempts} attempt(s), "
            f"{self.tokens_in + self.tokens_out} token(s)"
        )


class WorkflowPlanner:
    """Turns a goal into a validated workflow, through a model."""

    __slots__ = ("_max_attempts", "_provider", "_system_prompt")

    def __init__(
        self,
        provider: ModelPort,
        *,
        max_attempts: int = 3,
        system_prompt: str = PLANNER_SYSTEM_PROMPT,
    ) -> None:
        """Create the planner.

        Args:
            provider: Any model backend. The planner never names a vendor.
            max_attempts: How many times to ask. Each retry carries the
                validation report, so the model is correcting a specific fault
                rather than trying again in the hope of a different sample.
            system_prompt: The frozen instructions.
        """
        if max_attempts < 1:
            raise PlannerError(f"max_attempts must be at least 1, got {max_attempts}")
        self._provider = provider
        self._max_attempts = max_attempts
        self._system_prompt = system_prompt

    async def plan(self, request: PlanRequest) -> PlannerResult:
        """Decompose a goal into a workflow.

        Args:
            request: What to decompose.

        Returns:
            The result. A failure is a result with no workflow and a report
            saying why, not an exception — a planner that raises makes the
            caller choose between crashing and swallowing.

        Raises:
            PlannerRefused: If the backend declined. That is not a retry
                candidate, and reporting it as one would burn the budget
                reproducing a refusal.
        """
        messages: list[Message] = [Message(role=Role.USER, content=(TextBlock(request.render()),))]
        tokens_in = 0
        tokens_out = 0
        notes: list[str] = []
        report = ValidationReport()
        document: Mapping[str, Any] = {}

        for attempt in range(1, self._max_attempts + 1):
            response = await self._provider.complete(
                CompletionRequest(
                    model=request.model,
                    system=(TextBlock(self._system_prompt),),
                    messages=tuple(messages),
                    max_output_tokens=16_000,
                    effort=request.effort,
                    output_schema=json_schema(),
                )
            )
            tokens_in += response.usage.input_tokens
            tokens_out += response.usage.output_tokens

            # A refusal is a successful response with a stop reason, not a
            # crash. Checking it before reading content is the whole point.
            if response.stop_reason is StopReason.REFUSED:
                detail = response.refusal
                raise PlannerRefused(
                    "the backend declined to plan this goal",
                    detail={
                        "category": detail.category if detail else None,
                        "explanation": detail.explanation if detail else "",
                    },
                )

            raw = response.text.strip()
            parsed, parse_note = _parse_json(raw)
            if parsed is None:
                notes.append(f"attempt {attempt}: {parse_note}")
                messages.extend(_correction(raw, parse_note))
                continue

            document = parsed
            report = validate_document(parsed)
            if not report.ok:
                faults = "\n".join(f"- {issue}" for issue in report.issues)
                notes.append(f"attempt {attempt}: {report.summary()}")
                messages.extend(_correction(raw, f"The plan is not valid:\n{faults}"))
                continue

            try:
                workflow = workflow_from_document(
                    parsed, source="the generated plan", validate=False
                )
            except (WorkflowDefinitionError, OrchestratorError) as exc:
                # The schema is satisfied but the domain still refuses it —
                # a cycle is the usual reason, and the schema cannot see one.
                notes.append(f"attempt {attempt}: {exc}")
                messages.extend(_correction(raw, f"The plan cannot be compiled: {exc}"))
                continue

            _log.info(
                "plan produced",
                steps=len(workflow.steps),
                attempts=attempt,
                tokens=tokens_in + tokens_out,
            )
            return PlannerResult(
                workflow=workflow,
                attempts=attempt,
                document=parsed,
                report=report,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                notes=tuple(notes),
            )

        _log.warning("no usable plan", attempts=self._max_attempts)
        return PlannerResult(
            workflow=None,
            attempts=self._max_attempts,
            document=document,
            report=report,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            notes=tuple(notes),
        )

    async def plan_or_raise(self, request: PlanRequest) -> WorkflowDefinition:
        """Decompose a goal, raising if no usable plan was produced.

        Raises:
            PlannerInvalid: If every attempt produced something unusable.
        """
        result = await self.plan(request)
        if result.workflow is None:
            raise PlannerInvalid(
                f"no usable plan after {result.attempts} attempt(s)",
                detail={
                    "notes": list(result.notes),
                    "issues": [str(issue) for issue in result.report.issues],
                },
            )
        return result.workflow


def _parse_json(text: str) -> tuple[dict[str, Any] | None, str]:
    """Decode a model's response as a JSON object.

    Structured output should make this exact, and mostly does. The fallback
    exists because a backend that does not support the constraint degrades to
    prose with an object in it, and the honest thing is to read what is there
    rather than to fail a capable model on a transport detail.
    """
    if not text:
        return None, "the response was empty"

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            return None, "the response was not JSON"
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            return None, f"the response was not JSON: {exc}"

    if not isinstance(parsed, dict):
        return None, f"the response was a {type(parsed).__name__}, not a workflow object"
    return parsed, ""


def _correction(previous: str, fault: str) -> Sequence[Message]:
    """Build the turns that ask a model to fix a specific fault."""
    return (
        Message(role=Role.ASSISTANT, content=(TextBlock(previous or "(no output)"),)),
        Message(
            role=Role.USER,
            content=(
                TextBlock(
                    f"{fault}\n\nReturn a corrected workflow object. Change only "
                    "what the problems above require."
                ),
            ),
        ),
    )

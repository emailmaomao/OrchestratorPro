"""Workflow definitions: named steps, conditions, and the plan they compile to.

A :class:`WorkflowDefinition` is what a human (or the planner) writes. It names
its steps, so the same definition can be re-run, resumed, and reasoned about
across runs â€” task identifiers are minted per run and are useless for that.

Compiling a definition produces a :class:`WorkflowPlan`: a validated
:class:`~orchestrator.task.graph.TaskGraph` plus the bidirectional map between
step names and the task identifiers minted for this run. Everything downstream
works in identifiers; everything an operator reads works in names.

**On conditional branches.** A condition is evaluated when its step becomes
ready, against the outcomes of the steps that ran before it. A step whose
condition is false is *skipped*, and a skipped step is recorded as **succeeded**
so that its dependents still run. That is a deliberate choice: the alternative â€”
treating a skip as a failure â€” would cascade a block through the rest of the
branch, which is almost never what "this step was not needed" means. The
distinction is preserved in the outcome detail, so a skipped step is never
mistaken for one that did work.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from orchestrator.agent.model import AgentRole
from orchestrator.core.events import Budget, OrchestratorError, TaskId
from orchestrator.task.graph import TaskGraph
from orchestrator.task.model import GateKind, GateSpec, Task, TaskState

__all__ = [
    "Always",
    "AllOf",
    "AnyOf",
    "Condition",
    "ConditionContext",
    "Not",
    "StepDefinition",
    "StepDidWork",
    "StepSkipped",
    "WorkflowDefinition",
    "WorkflowDefinitionError",
    "WorkflowPlan",
]

#: A generous default so a step that omits a budget still has a real ceiling.
_DEFAULT_BUDGET = Budget(seconds=1800.0, tokens=2_000_000, tool_calls=200)


class WorkflowDefinitionError(OrchestratorError):
    """A workflow definition is not well-formed."""

    code = "workflow_definition"
    retryable = False


# --------------------------------------------------------------------------- #
# Conditions
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ConditionContext:
    """What a condition may look at when it is evaluated.

    Only two facts, because only two are soundly knowable at the moment a step
    becomes ready. A condition may reference only the step's own dependencies
    (enforced by :meth:`WorkflowDefinition._validate_references`), and the
    scheduler starts a step only once every dependency has **succeeded** â€” so
    "did it succeed?" is not a real question. What remains genuinely open is
    whether that predecessor *did work* or was itself skipped, and that is what
    prunes a branch.
    """

    succeeded: frozenset[str] = frozenset()
    skipped: frozenset[str] = frozenset()

    def did_work(self, step: str) -> bool:
        """Whether a step succeeded by actually running."""
        return step in self.succeeded and step not in self.skipped

    def was_skipped(self, step: str) -> bool:
        """Whether a step succeeded only because its own condition was false."""
        return step in self.skipped


class Condition(Protocol):
    """Decides whether a step should run."""

    def evaluate(self, ctx: ConditionContext) -> bool:
        """Return whether the step should run."""
        ...

    def describe(self) -> str:
        """Return a short human-readable form, for reports and events."""
        ...

    def referenced_steps(self) -> frozenset[str]:
        """Return the step names this condition reads."""
        ...


@dataclass(frozen=True, slots=True)
class Always:
    """Runs unconditionally."""

    def evaluate(self, ctx: ConditionContext) -> bool:
        """Always true."""
        return True

    def describe(self) -> str:
        """Describe the condition."""
        return "always"

    def referenced_steps(self) -> frozenset[str]:
        """No references."""
        return frozenset()


@dataclass(frozen=True, slots=True)
class StepDidWork:
    """Runs only if a named predecessor actually ran, rather than being skipped.

    The workhorse of branch pruning: when the head of a branch is skipped, every
    step behind it can be skipped too, instead of doing work whose premise no
    longer holds.
    """

    step: str

    def evaluate(self, ctx: ConditionContext) -> bool:
        """Evaluate against what has happened so far."""
        return ctx.did_work(self.step)

    def describe(self) -> str:
        """Describe the condition."""
        return f"{self.step} did work"

    def referenced_steps(self) -> frozenset[str]:
        """The step read."""
        return frozenset({self.step})


@dataclass(frozen=True, slots=True)
class StepSkipped:
    """Runs only if a named predecessor was skipped.

    The complement of :class:`StepDidWork`, for an alternative branch that
    applies precisely when the main one did not.
    """

    step: str

    def evaluate(self, ctx: ConditionContext) -> bool:
        """Evaluate against what has happened so far."""
        return ctx.was_skipped(self.step)

    def describe(self) -> str:
        """Describe the condition."""
        return f"{self.step} was skipped"

    def referenced_steps(self) -> frozenset[str]:
        """The step read."""
        return frozenset({self.step})


@dataclass(frozen=True, slots=True)
class AllOf:
    """Runs only if every sub-condition holds."""

    conditions: tuple[Condition, ...]

    def evaluate(self, ctx: ConditionContext) -> bool:
        """Evaluate against the run's state."""
        return all(condition.evaluate(ctx) for condition in self.conditions)

    def describe(self) -> str:
        """Describe the condition."""
        return " and ".join(c.describe() for c in self.conditions) or "always"

    def referenced_steps(self) -> frozenset[str]:
        """The union of the steps read."""
        return frozenset().union(*(c.referenced_steps() for c in self.conditions)) if self.conditions else frozenset()


@dataclass(frozen=True, slots=True)
class AnyOf:
    """Runs if at least one sub-condition holds."""

    conditions: tuple[Condition, ...]

    def evaluate(self, ctx: ConditionContext) -> bool:
        """Evaluate against the run's state."""
        return any(condition.evaluate(ctx) for condition in self.conditions)

    def describe(self) -> str:
        """Describe the condition."""
        return " or ".join(c.describe() for c in self.conditions) or "never"

    def referenced_steps(self) -> frozenset[str]:
        """The union of the steps read."""
        return frozenset().union(*(c.referenced_steps() for c in self.conditions)) if self.conditions else frozenset()


@dataclass(frozen=True, slots=True)
class Not:
    """Inverts a condition."""

    condition: Condition

    def evaluate(self, ctx: ConditionContext) -> bool:
        """Evaluate against the run's state."""
        return not self.condition.evaluate(ctx)

    def describe(self) -> str:
        """Describe the condition."""
        return f"not ({self.condition.describe()})"

    def referenced_steps(self) -> frozenset[str]:
        """The steps read."""
        return self.condition.referenced_steps()


# --------------------------------------------------------------------------- #
# Definitions
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class StepDefinition:
    """One named unit of work in a workflow.

    Attributes:
        name: Stable, human-authored identifier. Unique within the workflow, and
            the thing conditions and recovery refer to.
        prompt: What the agent is asked to do.
        title: Short summary. Defaults to the name.
        depends_on: Names of steps that must finish first.
        condition: When present, decides whether this step actually runs.
        gates: Checks the resulting work must clear.
        max_attempts: How many times this step may be tried.
        budget: Per-attempt allowance.
        labels: Free-form tags, used for per-label concurrency caps.
        role: Which agent role performs the step.
        expects_changes: Whether an attempt that modifies nothing counts as
            having done the work. ``True`` for ordinary steps, because a green
            gate over unmodified code verifies the *baseline*, not the task —
            the first real harness run passed both gates having changed
            nothing at all, which is the failure this exists to refuse. Set it
            ``False`` for steps that legitimately produce no diff (a
            verification or audit step, one whose condition made it a no-op by
            design).
    """

    name: str
    prompt: str
    title: str = ""
    depends_on: tuple[str, ...] = ()
    condition: Condition | None = None
    gates: tuple[GateSpec, ...] = ()
    max_attempts: int = 3
    budget: Budget = _DEFAULT_BUDGET
    labels: frozenset[str] = frozenset()
    role: AgentRole = AgentRole.WORKER
    expects_changes: bool = True

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise WorkflowDefinitionError("a step must have a name")
        if not self.prompt.strip():
            raise WorkflowDefinitionError(
                f"step {self.name!r} must have a prompt", detail={"step": self.name}
            )
        if self.max_attempts < 1:
            raise WorkflowDefinitionError(
                f"step {self.name!r} must allow at least one attempt",
                detail={"step": self.name, "max_attempts": self.max_attempts},
            )
        if self.name in self.depends_on:
            raise WorkflowDefinitionError(
                f"step {self.name!r} depends on itself", detail={"step": self.name}
            )

    @property
    def display_title(self) -> str:
        """The title, falling back to the name."""
        return self.title or self.name


@dataclass(frozen=True, slots=True)
class WorkflowPlan:
    """A compiled definition: a graph, plus the name/identifier mapping."""

    definition: WorkflowDefinition
    graph: TaskGraph
    ids_by_name: Mapping[str, TaskId]
    names_by_id: Mapping[TaskId, str]

    def task_id(self, name: str) -> TaskId:
        """Return the identifier minted for a step.

        Raises:
            WorkflowDefinitionError: If the name is unknown.
        """
        try:
            return self.ids_by_name[name]
        except KeyError:
            raise WorkflowDefinitionError(
                f"no step named {name!r} in this workflow",
                detail={"step": name, "known": sorted(self.ids_by_name)},
            ) from None

    def step_name(self, task_id: TaskId) -> str:
        """Return the step name behind a task identifier."""
        return self.names_by_id.get(task_id, str(task_id))

    def step(self, name: str) -> StepDefinition:
        """Return a step definition by name."""
        for step in self.definition.steps:
            if step.name == name:
                return step
        raise WorkflowDefinitionError(
            f"no step named {name!r} in this workflow", detail={"step": name}
        )

    def step_for(self, task_id: TaskId) -> StepDefinition:
        """Return the step definition behind a task identifier."""
        return self.step(self.step_name(task_id))

    def states_by_name(
        self, states: Mapping[TaskId, TaskState]
    ) -> dict[str, TaskState]:
        """Re-key a state mapping from identifiers to step names."""
        return {self.step_name(task_id): state for task_id, state in states.items()}


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    """A named collection of steps and how they depend on one another."""

    name: str
    goal: str
    steps: tuple[StepDefinition, ...]
    max_concurrency: int = 4
    label_limits: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise WorkflowDefinitionError("a workflow must have a name")
        if not self.steps:
            raise WorkflowDefinitionError(
                f"workflow {self.name!r} has no steps", detail={"workflow": self.name}
            )
        if self.max_concurrency < 1:
            raise WorkflowDefinitionError(
                f"workflow {self.name!r} must allow at least one concurrent step"
            )
        self._validate_names()
        self._validate_references()

    def _validate_names(self) -> None:
        """Reject duplicate step names."""
        seen: set[str] = set()
        for step in self.steps:
            if step.name in seen:
                raise WorkflowDefinitionError(
                    f"workflow {self.name!r} has two steps named {step.name!r}; "
                    "names must be unique because conditions and recovery use them",
                    detail={"workflow": self.name, "step": step.name},
                )
            seen.add(step.name)

    def _validate_references(self) -> None:
        """Reject dependencies and conditions naming steps that do not exist."""
        known = {step.name for step in self.steps}
        for step in self.steps:
            for dependency in step.depends_on:
                if dependency not in known:
                    raise WorkflowDefinitionError(
                        f"step {step.name!r} depends on {dependency!r}, which is not "
                        "part of this workflow",
                        detail={"step": step.name, "missing": dependency},
                    )
            if step.condition is None:
                continue
            for referenced in step.condition.referenced_steps():
                if referenced not in known:
                    raise WorkflowDefinitionError(
                        f"the condition on {step.name!r} refers to {referenced!r}, "
                        "which is not part of this workflow",
                        detail={"step": step.name, "missing": referenced},
                    )
                if referenced not in step.depends_on:
                    # A condition on a step that may not have finished yet would
                    # be evaluated against an unfinished state and quietly do
                    # the wrong thing.
                    raise WorkflowDefinitionError(
                        f"the condition on {step.name!r} reads {referenced!r}, so "
                        f"{step.name!r} must also depend on it â€” otherwise the "
                        "condition could be evaluated before that step finishes",
                        detail={"step": step.name, "referenced": referenced},
                    )

    def step_names(self) -> tuple[str, ...]:
        """Every step name, in declaration order."""
        return tuple(step.name for step in self.steps)

    def compile(self) -> WorkflowPlan:
        """Compile into a validated graph with fresh task identifiers.

        Returns:
            The plan.

        Raises:
            CycleError: If the dependencies form a cycle. Raised by the graph,
                at construction, before any work can begin.
        """
        ids_by_name = {step.name: TaskId.generate() for step in self.steps}
        tasks = [
            Task(
                id=ids_by_name[step.name],
                title=step.display_title,
                prompt=step.prompt,
                budget=step.budget,
                depends_on=tuple(ids_by_name[name] for name in step.depends_on),
                gates=step.gates,
                max_attempts=step.max_attempts,
                labels=step.labels,
            )
            for step in self.steps
        ]
        return WorkflowPlan(
            definition=self,
            graph=TaskGraph(tasks),
            ids_by_name=ids_by_name,
            names_by_id={task_id: name for name, task_id in ids_by_name.items()},
        )

    def bind(self, ids_by_name: Mapping[str, TaskId]) -> WorkflowPlan:
        """Compile using identifiers that already exist.

        Used on recovery, where the identifiers come from the event log rather
        than being minted fresh â€” the same definition must map onto the run that
        is being resumed.

        Raises:
            WorkflowDefinitionError: If any step has no identifier.
        """
        missing = sorted(step.name for step in self.steps if step.name not in ids_by_name)
        if missing:
            raise WorkflowDefinitionError(
                "cannot bind this workflow to the persisted run: no identifier for "
                f"step(s) {', '.join(missing)}",
                detail={"missing": missing},
            )
        tasks = [
            Task(
                id=ids_by_name[step.name],
                title=step.display_title,
                prompt=step.prompt,
                budget=step.budget,
                depends_on=tuple(ids_by_name[name] for name in step.depends_on),
                gates=step.gates,
                max_attempts=step.max_attempts,
                labels=step.labels,
            )
            for step in self.steps
        ]
        return WorkflowPlan(
            definition=self,
            graph=TaskGraph(tasks),
            ids_by_name=dict(ids_by_name),
            names_by_id={ids_by_name[s.name]: s.name for s in self.steps},
        )


def test_gate(name: str = "tests", *, required: bool = True) -> GateSpec:
    """Build a standard test gate, for definitions written in Python."""
    return GateSpec(name=name, kind=GateKind.TEST, required=required)


def linear(goal: str, prompts: Sequence[str], *, name: str = "linear", **kwargs: object) -> WorkflowDefinition:
    """Build a workflow whose steps run one after another.

    A convenience for the common shape and for tests; anything non-trivial
    should declare its dependencies explicitly.
    """
    steps: list[StepDefinition] = []
    for index, prompt in enumerate(prompts):
        previous = (steps[-1].name,) if steps else ()
        steps.append(
            StepDefinition(name=f"step-{index + 1}", prompt=prompt, depends_on=previous)
        )
    return WorkflowDefinition(name=name, goal=goal, steps=tuple(steps), **kwargs)  # type: ignore[arg-type]


def parallel(goal: str, prompts: Iterable[str], *, name: str = "parallel", **kwargs: object) -> WorkflowDefinition:
    """Build a workflow whose steps are all independent."""
    steps = tuple(
        StepDefinition(name=f"step-{index + 1}", prompt=prompt)
        for index, prompt in enumerate(prompts)
    )
    return WorkflowDefinition(name=name, goal=goal, steps=steps, **kwargs)  # type: ignore[arg-type]

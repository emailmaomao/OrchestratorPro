"""Request and response models for the control plane.

Pydantic lives exactly here and nowhere else: the API is a trust boundary, and
these models are what turn arbitrary JSON into values the domain will accept
(``CLAUDE.md``, code conventions). Below this layer the codebase stays on frozen
dataclasses — a request model is not a domain object and must never be passed
into one as if it were.

Every model is strict about unknown fields. A client that sends ``max_attemps``
gets told so, rather than silently getting the default and wondering why its
retries never happened.

Responses are built by ``from_*`` constructors rather than by ORM-mode magic, so
the wire format is visible in one place and cannot drift when a domain object
gains a field.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from orchestrator.builder.model import BuildReport, ProjectLayout, UnitResult
from orchestrator.builder.planner import BuildPlan
from orchestrator.core.config import OrchestratorConfig
from orchestrator.core.events import Event
from orchestrator.core.records import RunState
from orchestrator.workflow.definition import StepDefinition, WorkflowDefinition
from orchestrator.workflow.progress import ProgressSnapshot

__all__ = [
    "AgentRoleResponse",
    "ApprovalDecisionBody",
    "ApprovalRequestBody",
    "BuildPlanRequest",
    "BuildPlanResponse",
    "BuildReportResponse",
    "BuildStartRequest",
    "ConfigResponse",
    "ErrorBody",
    "ErrorResponse",
    "EventResponse",
    "HealthResponse",
    "LayoutResponse",
    "PromptRequest",
    "PromptResponse",
    "RunCreateRequest",
    "RunResponse",
    "RunStatusResponse",
    "RunSummary",
    "StartRunRequest",
    "TaskCreateRequest",
    "TaskResponse",
    "TaskUpdateRequest",
    "ToolResponse",
    "WorkflowResponse",
    "WorkflowStepRequest",
    "WorkflowSubmitRequest",
]


class _Strict(BaseModel):
    """Base for request bodies: unknown fields are an error, not a shrug."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class _Payload(BaseModel):
    """Base for responses."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class ErrorBody(_Payload):
    """The body of a failed request."""

    code: str = Field(description="Stable machine-readable error code.")
    message: str = Field(description="What went wrong, in one sentence.")
    retryable: bool = Field(
        default=False, description="Whether repeating the request could succeed."
    )
    detail: Mapping[str, Any] = Field(
        default_factory=dict, description="Structured context for the error."
    )


class ErrorResponse(_Payload):
    """Every non-2xx response has this shape."""

    error: ErrorBody


# --------------------------------------------------------------------------- #
# Health and configuration
# --------------------------------------------------------------------------- #


class HealthResponse(_Payload):
    """Liveness and a few facts worth knowing before trusting the rest."""

    status: Literal["ok", "degraded"]
    version: str
    uptime_s: float
    database: str = Field(description="Path of the SQLite database, or ':memory:'.")
    schema_version: int
    runs: int = Field(description="How many runs the log holds.")
    active_runs: int = Field(description="How many are executing right now.")
    execution_available: bool = Field(
        description="Whether an execution backend is configured. Without one the "
        "server can record and report but not run anything."
    )


class ConfigResponse(_Payload):
    """The effective configuration, with credentials never included."""

    run: Mapping[str, Any]
    agent: Mapping[str, Any]
    git: Mapping[str, Any]
    gates: Mapping[str, Any]
    api: Mapping[str, Any]
    providers: Mapping[str, Mapping[str, Any]]
    roles: Mapping[str, Mapping[str, Any]]
    sources: list[str] = Field(description="Configuration files that contributed.")

    @classmethod
    def from_config(cls, config: OrchestratorConfig) -> ConfigResponse:
        """Render a configuration for the wire.

        ``api.auth_token_env`` is the *name* of an environment variable, so it
        is safe to publish; the value it names is never read here.
        """
        return cls(
            run={
                "max_concurrency": config.run.max_concurrency,
                "auto_approve": config.run.auto_approve,
            },
            agent={
                "budget_seconds": config.agent.budget_seconds,
                "budget_tokens": config.agent.budget_tokens,
                "budget_tool_calls": config.agent.budget_tool_calls,
                "adapter": config.agent.adapter,
            },
            git={
                field: getattr(config.git, field)
                for field in config.git.__dataclass_fields__
            },
            gates={
                field: getattr(config.gates, field)
                for field in config.gates.__dataclass_fields__
            },
            api={
                "host": config.api.host,
                "port": config.api.port,
                "auth_token_env": config.api.auth_token_env,
            },
            providers={
                name: {
                    "model": provider.model,
                    "effort": provider.effort.value,
                    "max_tokens": provider.max_tokens,
                    "thinking": provider.thinking.value,
                }
                for name, provider in config.providers.items()
            },
            roles={
                role: {
                    "model": override.model,
                    "effort": override.effort.value if override.effort else None,
                }
                for role, override in config.roles.items()
            },
            sources=[str(path) for path in config.sources],
        )


# --------------------------------------------------------------------------- #
# Runs and tasks
# --------------------------------------------------------------------------- #


class RunCreateRequest(_Strict):
    """Declare a run without executing it."""

    goal: str = Field(min_length=1, max_length=4000)
    repo_path: str = Field(default="", max_length=4000)


class TaskCreateRequest(_Strict):
    """Add a task to a run."""

    title: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=100_000)
    depends_on: list[str] = Field(default_factory=list)
    max_attempts: int = Field(default=3, ge=1, le=100)
    labels: list[str] = Field(default_factory=list)


class TaskUpdateRequest(_Strict):
    """Amend a task that has not started.

    Recorded as a fresh declaration in the log rather than an edit: the log is
    append-only, so an amendment is a new fact about the task, not a rewriting
    of the old one.
    """

    title: str | None = Field(default=None, min_length=1, max_length=200)
    prompt: str | None = Field(default=None, min_length=1, max_length=100_000)
    max_attempts: int | None = Field(default=None, ge=1, le=100)


class TaskResponse(_Payload):
    """One task, as the log currently describes it."""

    id: str
    run_id: str
    title: str
    prompt: str
    state: str
    depends_on: list[str]
    max_attempts: int
    attempts_made: int
    attempts_remaining: int

    @classmethod
    def from_projection(cls, task: Any) -> TaskResponse:
        """Render a :class:`~orchestrator.core.records.TaskProjection`."""
        return cls(
            id=str(task.id),
            run_id=str(task.run_id),
            title=task.title,
            prompt=task.prompt,
            state=task.state,
            depends_on=[str(d) for d in task.depends_on],
            max_attempts=task.max_attempts,
            attempts_made=task.attempts_made,
            attempts_remaining=task.attempts_remaining,
        )


class RunSummary(_Payload):
    """One line about a run, for a list."""

    id: str
    goal: str
    status: str
    tasks: int
    active: bool

    @classmethod
    def from_state(cls, state: RunState, *, active: bool = False) -> RunSummary:
        """Render a replayed run."""
        return cls(
            id=str(state.run_id),
            goal=state.goal,
            status=state.status.value,
            tasks=len(state.tasks),
            active=active,
        )


class RunResponse(_Payload):
    """A run in full, reconstructed from its log."""

    id: str
    goal: str
    repo_path: str
    status: str
    active: bool
    created_at: str | None
    finished_at: str | None
    event_count: int
    tool_calls: int
    tasks: list[TaskResponse]
    state_counts: Mapping[str, int]
    usage: Mapping[str, Any]

    @classmethod
    def from_state(cls, state: RunState, *, active: bool = False) -> RunResponse:
        """Render a replayed run."""
        return cls(
            id=str(state.run_id),
            goal=state.goal,
            repo_path=state.repo_path,
            status=state.status.value,
            active=active,
            created_at=state.created_at.isoformat() if state.created_at else None,
            finished_at=state.finished_at.isoformat() if state.finished_at else None,
            event_count=state.event_count,
            tool_calls=state.tool_calls,
            tasks=[TaskResponse.from_projection(t) for t in state.tasks.values()],
            state_counts=dict(state.state_counts()),
            usage={
                "tokens_in": state.usage.tokens_in,
                "tokens_out": state.usage.tokens_out,
                "cost_usd": state.usage.cost_usd,
                # OP-004: a total with one estimated contribution is an
                # estimate, and a client must be able to say so.
                "tokens_estimated": state.usage.estimated,
            },
        )


class RunStatusResponse(_Payload):
    """The compact status a poller wants."""

    id: str
    status: str
    active: bool
    total: int
    succeeded: int
    failed: int
    blocked: int
    running: int
    pending: int
    percent: float
    complete: bool = Field(
        description="Every step has reached a terminal state. It does not mean "
        "they all succeeded — check `healthy` for that."
    )
    healthy: bool = Field(
        description="Nothing has failed or been blocked."
    )
    summary: str

    @classmethod
    def from_state(
        cls, state: RunState, *, active: bool, progress: ProgressSnapshot
    ) -> RunStatusResponse:
        """Render a run's progress."""
        return cls(
            id=str(state.run_id),
            status=state.status.value,
            active=active,
            total=progress.total,
            succeeded=progress.succeeded,
            failed=progress.failed,
            blocked=progress.blocked,
            running=progress.running,
            pending=progress.pending,
            percent=round(progress.percent, 2),
            complete=progress.complete,
            healthy=progress.healthy,
            summary=progress.summary(),
        )


class EventResponse(_Payload):
    """One entry from the log."""

    id: str
    type: str
    ts: str
    run_id: str | None
    task_id: str | None
    attempt_id: str | None
    payload: Mapping[str, Any]

    @classmethod
    def from_event(cls, event: Event) -> EventResponse:
        """Render an event."""
        return cls(
            id=str(event.id),
            type=event.type.value,
            ts=event.ts.isoformat(),
            run_id=str(event.run_id) if event.run_id else None,
            task_id=str(event.task_id) if event.task_id else None,
            attempt_id=str(event.attempt_id) if event.attempt_id else None,
            payload=dict(event.payload),
        )


# --------------------------------------------------------------------------- #
# Workflows
# --------------------------------------------------------------------------- #


class WorkflowStepRequest(_Strict):
    """One step of a submitted workflow."""

    name: str = Field(min_length=1, max_length=100)
    prompt: str = Field(min_length=1, max_length=100_000)
    title: str = Field(default="", max_length=200)
    depends_on: list[str] = Field(default_factory=list)
    max_attempts: int = Field(default=3, ge=1, le=100)
    labels: list[str] = Field(default_factory=list)
    gates: list[str] = Field(
        default_factory=list, description="Gate names this step's output must clear."
    )
    expects_changes: bool = Field(
        default=True,
        description=(
            "Whether an attempt that modifies nothing counts as done. True for "
            "ordinary steps: a green gate over unmodified code verifies the "
            "baseline, not the task. Set false for a step that legitimately "
            "produces no diff."
        ),
    )


class WorkflowSubmitRequest(_Strict):
    """Register a workflow definition."""

    name: str = Field(min_length=1, max_length=100)
    goal: str = Field(min_length=1, max_length=4000)
    steps: list[WorkflowStepRequest] = Field(min_length=1)
    max_concurrency: int = Field(default=4, ge=1, le=64)


class WorkflowResponse(_Payload):
    """A registered workflow, and what it compiles to."""

    name: str
    goal: str
    max_concurrency: int
    steps: list[Mapping[str, Any]]
    layers: list[list[str]]
    depth: int
    max_width: int

    @classmethod
    def from_definition(cls, definition: WorkflowDefinition) -> WorkflowResponse:
        """Render a definition, compiling it to expose its shape."""
        plan = definition.compile()
        layers = [
            sorted(plan.step_name(task_id) for task_id in layer)
            for layer in plan.graph.layers()
        ]
        return cls(
            name=definition.name,
            goal=definition.goal,
            max_concurrency=definition.max_concurrency,
            steps=[_step_payload(step) for step in definition.steps],
            layers=layers,
            depth=plan.graph.depth,
            max_width=plan.graph.max_width,
        )


def _step_payload(step: StepDefinition) -> Mapping[str, Any]:
    """Render one step definition."""
    return {
        "name": step.name,
        "title": step.display_title,
        "prompt": step.prompt,
        "depends_on": list(step.depends_on),
        "max_attempts": step.max_attempts,
        "role": step.role.value,
        "labels": sorted(step.labels),
        "gates": [gate.name for gate in step.gates],
        "condition": step.condition.describe() if step.condition else None,
    }


class ApprovalRequestBody(_Strict):
    """Ask for a person to review a task."""

    reason: str = Field(default="", max_length=2000)
    attempt: int = Field(default=0, ge=0, le=100)


class ApprovalDecisionBody(_Strict):
    """A reviewer's decision.

    The actor is deliberately absent: it comes from the credential. An approval
    attributed to whoever the caller says they are is not attributable at all.
    """

    decision: Literal["approved", "rejected", "retry"]
    note: str = Field(default="", max_length=4000)


class StartRunRequest(_Strict):
    """Start executing a registered workflow."""

    repo_path: str = Field(default="", max_length=4000)
    max_concurrency: int | None = Field(default=None, ge=1, le=64)
    run_timeout_s: float | None = Field(default=None, gt=0)


# --------------------------------------------------------------------------- #
# Agents
# --------------------------------------------------------------------------- #


class AgentRoleResponse(_Payload):
    """How one agent role is configured."""

    role: str
    model: str
    effort: str
    max_tokens: int
    thinking: str
    budget_seconds: float
    budget_tokens: int
    budget_tool_calls: int


class ToolResponse(_Payload):
    """One tool the agent may call."""

    name: str
    description: str
    schema_: Mapping[str, Any] = Field(alias="schema")

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class PromptRequest(_Strict):
    """Render the prompt an agent would receive, without calling a model."""

    title: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=100_000)
    role: str = Field(default="worker")
    feedback: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)


class PromptResponse(_Payload):
    """The assembled prompt and its cache fingerprint."""

    role: str
    blocks: list[str] = Field(
        description="The cacheable system prefix, in stability order."
    )
    messages: list[str] = Field(
        description="The opening conversation turns. Retry feedback lives here "
        "rather than in the prefix, because it changes every attempt and would "
        "otherwise invalidate the cache each time."
    )
    fingerprint: str = Field(
        description="Digest of the cacheable prefix. Identical inputs must "
        "produce an identical fingerprint; if they do not, prompt caching is "
        "silently not happening."
    )
    tools: list[str]


# --------------------------------------------------------------------------- #
# Builds
# --------------------------------------------------------------------------- #


class BuildUnitRequest(_Strict):
    """One unit of a build manifest."""

    name: str = Field(min_length=1, max_length=100)
    command: str = Field(min_length=1, max_length=4000)
    sources: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    incremental: bool = True
    timeout_s: float = Field(default=600.0, gt=0)


class BuildAnalyzeRequest(_Strict):
    """Analyze a project."""

    path: str = Field(min_length=1, max_length=4000)
    manifest: list[BuildUnitRequest] | None = None


class BuildPlanRequest(BuildAnalyzeRequest):
    """Plan a build."""

    changed_paths: list[str] = Field(default_factory=list)
    force: list[str] = Field(default_factory=list)
    full: bool = False
    use_cache: bool = True


class BuildStartRequest(BuildPlanRequest):
    """Plan and execute a build."""

    max_concurrency: int | None = Field(default=None, ge=1, le=64)


class LayoutResponse(_Payload):
    """What a project looks like to the builder."""

    root: str
    kind: str
    units: list[Mapping[str, Any]]
    source_count: int

    @classmethod
    def from_layout(cls, layout: ProjectLayout, graph: Any) -> LayoutResponse:
        """Render an analyzed project and its dependency graph."""
        return cls(
            root=str(layout.root),
            kind=layout.kind.value,
            units=[
                {
                    "name": name,
                    "command": graph.get(name).command,
                    "sources": list(graph.get(name).sources),
                    "depends_on": list(graph.dependencies(name)),
                    "dependents": list(graph.dependents(name)),
                    "artifacts": list(graph.get(name).artifacts),
                    "incremental": graph.get(name).incremental,
                }
                for name in graph.names
            ],
            source_count=len(layout.sources),
        )


class BuildPlanResponse(_Payload):
    """What a build is about to do, and why."""

    units: list[Mapping[str, str]]
    cached: list[str]
    layers: list[list[str]]
    max_parallel: int
    empty: bool
    summary: str

    @classmethod
    def from_plan(cls, plan: BuildPlan) -> BuildPlanResponse:
        """Render a plan."""
        return cls(
            units=[
                {"name": u.name, "reason": u.reason.value, "why": u.reason.describe()}
                for u in plan.units
            ],
            cached=list(plan.cached),
            layers=[list(layer) for layer in plan.layers],
            max_parallel=plan.max_parallel,
            empty=plan.is_empty,
            summary=plan.summary(),
        )


class BuildReportResponse(_Payload):
    """What a build produced."""

    id: str
    status: Literal["running", "succeeded", "failed", "errored"]
    path: str
    ok: bool | None
    rebuilt: list[str]
    cached: list[str]
    failed: list[str]
    harness_problems: list[str]
    duration_s: float
    units: list[Mapping[str, Any]]
    summary: str
    feedback: str

    @classmethod
    def from_report(
        cls, build_id: str, path: str, report: BuildReport
    ) -> BuildReportResponse:
        """Render a finished build."""
        return cls(
            id=build_id,
            status="errored" if report.harness_problems else ("succeeded" if report.ok else "failed"),
            path=path,
            ok=report.ok,
            rebuilt=list(report.rebuilt_units),
            cached=list(report.cached_units),
            failed=list(report.failed_units),
            harness_problems=list(report.harness_problems),
            duration_s=round(report.duration_s, 4),
            units=[_unit_payload(result) for result in report.results],
            summary=report.summary(),
            feedback=report.feedback(),
        )

    @classmethod
    def running(cls, build_id: str, path: str) -> BuildReportResponse:
        """Render a build that has not finished yet."""
        return cls(
            id=build_id,
            status="running",
            path=path,
            ok=None,
            rebuilt=[],
            cached=[],
            failed=[],
            harness_problems=[],
            duration_s=0.0,
            units=[],
            summary="build in progress",
            feedback="",
        )


def _unit_payload(result: UnitResult) -> Mapping[str, Any]:
    """Render one unit's result."""
    return {
        "unit": result.unit,
        "status": result.status.value,
        "duration_s": round(result.duration_s, 4),
        "exit_code": result.exit_code,
        "summary": result.summary(),
        "artifacts": [a.path for a in result.artifacts],
        "diagnostics": [
            {
                "message": d.message,
                "file": d.file,
                "line": d.line,
                "column": d.column,
                "severity": d.severity.value,
                "code": d.code,
                "rendered": d.render(),
            }
            for d in result.diagnostics
        ],
    }


def steps_from_request(steps: Sequence[WorkflowStepRequest]) -> tuple[StepDefinition, ...]:
    """Translate submitted steps into domain step definitions.

    The one place a request becomes a domain object. Validation failures raise
    :class:`~orchestrator.workflow.definition.WorkflowDefinitionError`, which the
    application maps to a 400 — the domain's own rules, not a second copy of
    them living in the schema layer.
    """
    from orchestrator.task.model import GateSpec

    return tuple(
        StepDefinition(
            name=step.name,
            prompt=step.prompt,
            title=step.title,
            depends_on=tuple(step.depends_on),
            max_attempts=step.max_attempts,
            labels=frozenset(step.labels),
            gates=tuple(GateSpec(name=gate) for gate in step.gates),
            expects_changes=step.expects_changes,
        )
        for step in steps
    )

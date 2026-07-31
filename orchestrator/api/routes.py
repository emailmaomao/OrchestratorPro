"""The control plane's routes.

Every handler is thin by design: parse, delegate to the milestone that owns the
behaviour, render. Business rules stay in `workflow`, `builder`, `task`, and
`core` — a rule implemented here would be a rule the CLI and the dashboard do
not get.

Two conventions worth knowing before reading:

* **Reads replay the log.** ``GET /runs/{id}`` reconstructs from events rather
  than reading the materialized tables, because the log is authoritative
  (spec §5). It is slower and it cannot be wrong.
* **Writes are events.** Creating a task appends ``task.created``; amending one
  appends another. Nothing here mutates a row directly, so anything the API does
  survives a crash and shows up in a replay.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Sequence
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from orchestrator.agent.model import AgentRole, TaskSpec
from orchestrator.agent.prompt import PromptBuilder
from orchestrator.api import schemas as s
from orchestrator.api.security import audit, requires_operator
from orchestrator.api.state import AppState, Conflict, NotFound
from orchestrator.api.streaming import sse_frame, ws_payload
from orchestrator.builder.analysis import DependencyAnalyzer, ProjectAnalyzer, changed_units
from orchestrator.builder.model import BuildUnit
from orchestrator.builder.planner import BuildPlanner
from orchestrator.builder.runner import BuildExecutor, ExecutorConfig
from orchestrator.core.events import Event, EventId, EventType, RunId, TaskId
from orchestrator.core.records import RunStatus
from orchestrator.task.model import TaskState
from orchestrator.workflow.definition import WorkflowDefinition
from orchestrator.workflow.recovery import WorkflowRecovery

__all__ = ["build_router", "get_state", "router"]

#: States a task may still be amended from. Once an attempt has begun, the
#: prompt it was given is part of the record and changing it would make the
#: transcript describe work nobody asked for.
_AMENDABLE = frozenset({TaskState.PENDING, TaskState.READY})


def get_state(request: Request) -> AppState:
    """Resolve the application state for a request."""
    return request.app.state.orchestrator  # type: ignore[no-any-return]


State = Annotated[AppState, Depends(get_state)]

router = APIRouter()


# --------------------------------------------------------------------------- #
# Health and configuration
# --------------------------------------------------------------------------- #


@router.get(
    "/health",
    response_model=s.HealthResponse,
    tags=["system"],
    summary="Liveness and basic facts",
)
async def health(state: State) -> s.HealthResponse:
    """Report whether the server is up and what it is able to do."""
    try:
        runs = len(state.store.run_ids())
        database_ok = True
    except Exception:  # noqa: BLE001 - a broken database is a health answer
        runs = 0
        database_ok = False

    return s.HealthResponse(
        status="ok" if database_ok else "degraded",
        version=_version(),
        uptime_s=round(state.uptime_s, 3),
        database=state.database.path,
        schema_version=state.database.schema_version,
        runs=runs,
        active_runs=state.active_runs,
        execution_available=state.can_execute,
    )


@router.get(
    "/config",
    response_model=s.ConfigResponse,
    tags=["system"],
    summary="The effective configuration",
)
async def get_config(state: State) -> s.ConfigResponse:
    """Return the resolved configuration.

    Credentials are never in configuration to begin with — the config layer
    rejects them at load time — so there is nothing here to redact.
    """
    return s.ConfigResponse.from_config(state.config)


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #


@router.post(
    "/runs",
    response_model=s.RunResponse,
    status_code=201,
    tags=["runs"],
    summary="Declare a run",
    dependencies=[Depends(requires_operator)],
)
async def create_run(body: s.RunCreateRequest, state: State) -> s.RunResponse:
    """Create a run without executing it.

    Useful on its own: a run declared here can be given tasks, inspected, and
    later executed, and it exists in the log from this moment on.
    """
    run_id = RunId.generate()
    repo_path = str(state.resolve(body.repo_path, what="repo_path")) if body.repo_path else ""
    state.record(
        Event.new(
            EventType.RUN_CREATED,
            run_id=run_id,
            payload={"goal": body.goal, "repo_path": repo_path, "workflow": ""},
        )
    )
    return s.RunResponse.from_state(state.state_of(run_id))


@router.get(
    "/runs",
    response_model=list[s.RunSummary],
    tags=["runs"],
    summary="List runs",
)
async def list_runs(
    state: State,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    active: Annotated[bool | None, Query()] = None,
) -> list[s.RunSummary]:
    """List runs, newest first.

    Args:
        limit: How many to return.
        active: Filter to runs that are or are not currently executing.
    """
    summaries: list[s.RunSummary] = []
    for run_id in reversed(state.store.run_ids()):
        running = state.is_active(run_id)
        if active is not None and running is not active:
            continue
        summaries.append(
            s.RunSummary.from_state(state.store.replay(run_id), active=running)
        )
        if len(summaries) >= limit:
            break
    return summaries


@router.get(
    "/runs/{run_id}",
    response_model=s.RunResponse,
    tags=["runs"],
    summary="One run in full",
)
async def get_run(run_id: str, state: State) -> s.RunResponse:
    """Replay a run and return everything known about it."""
    identifier = RunId(run_id)
    return s.RunResponse.from_state(
        state.state_of(identifier), active=state.is_active(identifier)
    )


@router.get(
    "/runs/{run_id}/status",
    response_model=s.RunStatusResponse,
    tags=["runs"],
    summary="Compact run status",
)
async def get_run_status(run_id: str, state: State) -> s.RunStatusResponse:
    """Return progress in the form a poller wants."""
    identifier = RunId(run_id)
    run_state = state.state_of(identifier)
    return s.RunStatusResponse.from_state(
        run_state,
        active=state.is_active(identifier),
        progress=AppState.progress_of(run_state),
    )


@router.post(
    "/runs/{run_id}/cancel",
    response_model=s.RunStatusResponse,
    tags=["runs"],
    summary="Cancel a running run",
    dependencies=[Depends(requires_operator)],
)
async def cancel_run(run_id: str, state: State) -> s.RunStatusResponse:
    """Stop starting new work and let in-flight attempts finish (FR-2.9).

    Raises:
        Conflict: If the run is not executing. Cancelling something that has
            already stopped would write a misleading event into its log.
    """
    identifier = RunId(run_id)
    run_state = state.state_of(identifier)
    job = state.runs.get(identifier)
    if job is None or job.done:
        raise Conflict(
            f"run {run_id} is not executing, so there is nothing to cancel",
            detail={"run_id": run_id, "status": run_state.status.value},
        )
    job.engine.request_cancel()
    return s.RunStatusResponse.from_state(
        run_state, active=True, progress=AppState.progress_of(run_state)
    )


@router.post(
    "/runs/{run_id}/resume",
    response_model=s.RunStatusResponse,
    status_code=202,
    tags=["runs"],
    summary="Resume an interrupted run",
    dependencies=[Depends(requires_operator)],
)
async def resume_run(
    run_id: str,
    state: State,
    workflow: Annotated[str, Query(description="The workflow to resume it as.")],
    retry_failed: Annotated[bool, Query()] = False,
) -> s.RunStatusResponse:
    """Continue a run from its persisted state, without redoing completed work."""
    identifier = RunId(run_id)
    run_state = state.state_of(identifier)
    definition = state.workflow(workflow)
    factory = state.require_executor()

    if state.is_active(identifier):
        raise Conflict(
            f"run {run_id} is already executing", detail={"run_id": run_id}
        )

    # Recoverability is decided before the request is accepted, not inside the
    # background task: a 202 whose work fails immediately tells the client
    # nothing it can act on.
    recovery = WorkflowRecovery(state.store)
    recovered = recovery.plan(identifier, retry_failed=retry_failed)
    if recovered.already_finished:
        raise Conflict(
            f"run {run_id} already finished; there is nothing to resume",
            detail={"run_id": run_id, "summary": recovered.summary()},
        )
    recovery.bind(definition, recovered)

    engine = state.engine_for()
    task = asyncio.get_running_loop().create_task(
        engine.resume(definition, factory, run_id=identifier, retry_failed=retry_failed)
    )
    state.runs[identifier] = _job(identifier, workflow, engine, task)
    return s.RunStatusResponse.from_state(
        run_state, active=True, progress=AppState.progress_of(run_state)
    )


# --------------------------------------------------------------------------- #
# Tasks
# --------------------------------------------------------------------------- #


@router.post(
    "/runs/{run_id}/tasks",
    response_model=s.TaskResponse,
    status_code=201,
    tags=["tasks"],
    summary="Add a task to a run",
    dependencies=[Depends(requires_operator)],
)
async def create_task(
    run_id: str, body: s.TaskCreateRequest, state: State
) -> s.TaskResponse:
    """Declare a task.

    Raises:
        NotFound: If a named dependency is not part of this run. A task whose
            dependency does not exist can never become runnable, so accepting it
            would produce a run that is stuck by construction.
    """
    identifier = RunId(run_id)
    run_state = state.state_of(identifier)

    unknown = sorted(set(body.depends_on) - {str(t) for t in run_state.tasks})
    if unknown:
        raise NotFound(
            f"run {run_id} has no task(s) {', '.join(unknown)} to depend on",
            detail={"run_id": run_id, "unknown": unknown},
        )

    task_id = TaskId.generate()
    state.record(
        Event.new(
            EventType.TASK_CREATED,
            run_id=identifier,
            task_id=task_id,
            payload={
                "title": body.title,
                "prompt": body.prompt,
                "depends_on": list(body.depends_on),
                "max_attempts": body.max_attempts,
                "labels": sorted(body.labels),
            },
        )
    )
    return s.TaskResponse.from_projection(state.state_of(identifier).tasks[task_id])


@router.get(
    "/runs/{run_id}/tasks",
    response_model=list[s.TaskResponse],
    tags=["tasks"],
    summary="List a run's tasks",
)
async def list_tasks(
    run_id: str,
    state: State,
    task_state: Annotated[str | None, Query(alias="state")] = None,
) -> list[s.TaskResponse]:
    """List tasks, optionally filtered by state."""
    run_state = state.state_of(RunId(run_id))
    tasks = run_state.tasks.values()
    if task_state is not None:
        tasks = [task for task in tasks if task.state == task_state]  # type: ignore[assignment]
    return [s.TaskResponse.from_projection(task) for task in tasks]


@router.get(
    "/runs/{run_id}/tasks/{task_id}",
    response_model=s.TaskResponse,
    tags=["tasks"],
    summary="One task",
)
async def get_task(run_id: str, task_id: str, state: State) -> s.TaskResponse:
    """Return one task as the log describes it."""
    return s.TaskResponse.from_projection(_task(state, run_id, task_id))


@router.patch(
    "/runs/{run_id}/tasks/{task_id}",
    response_model=s.TaskResponse,
    tags=["tasks"],
    summary="Amend a task that has not started",
    dependencies=[Depends(requires_operator)],
)
async def update_task(
    run_id: str, task_id: str, body: s.TaskUpdateRequest, state: State
) -> s.TaskResponse:
    """Amend a task.

    The amendment is appended as a fresh declaration rather than applied as an
    edit: the log is append-only, and the earlier declaration remains part of
    the record.

    Raises:
        Conflict: If the task has already started. Its prompt is by then part of
            an attempt's transcript, and changing it would make the record
            describe work nobody asked for.
    """
    identifier = RunId(run_id)
    task = _task(state, run_id, task_id)
    if _as_state(task.state) not in _AMENDABLE:
        raise Conflict(
            f"task {task_id} is {task.state} and can no longer be amended",
            detail={"task_id": task_id, "state": task.state},
        )

    state.record(
        Event.new(
            EventType.TASK_CREATED,
            run_id=identifier,
            task_id=task.id,
            payload={
                "title": body.title if body.title is not None else task.title,
                "prompt": body.prompt if body.prompt is not None else task.prompt,
                "depends_on": [str(d) for d in task.depends_on],
                "max_attempts": (
                    body.max_attempts
                    if body.max_attempts is not None
                    else task.max_attempts
                ),
                "state": task.state,
                "amended": True,
            },
        )
    )
    return s.TaskResponse.from_projection(state.state_of(identifier).tasks[task.id])


@router.delete(
    "/runs/{run_id}/tasks/{task_id}",
    response_model=s.TaskResponse,
    tags=["tasks"],
    summary="Retire a task",
    dependencies=[Depends(requires_operator)],
)
async def delete_task(run_id: str, task_id: str, state: State) -> s.TaskResponse:
    """Retire a task that has not started.

    The log is append-only, so nothing is erased: the task is moved to
    ``abandoned``, which is the closest honest thing to a delete. Its history
    stays exactly where it was.

    Raises:
        Conflict: If the task has already started or already finished.
    """
    identifier = RunId(run_id)
    task = _task(state, run_id, task_id)
    if _as_state(task.state) not in _AMENDABLE:
        raise Conflict(
            f"task {task_id} is {task.state} and cannot be retired; the log is "
            "append-only and a task that ran keeps its record",
            detail={"task_id": task_id, "state": task.state},
        )

    state.record(
        Event.new(
            EventType.TASK_ABANDONED,
            run_id=identifier,
            task_id=task.id,
            payload={"reason": "retired through the API before it started"},
        )
    )
    return s.TaskResponse.from_projection(state.state_of(identifier).tasks[task.id])


# --------------------------------------------------------------------------- #
# Workflows
# --------------------------------------------------------------------------- #


@router.post(
    "/workflows",
    response_model=s.WorkflowResponse,
    status_code=201,
    tags=["workflows"],
    summary="Register a workflow",
    dependencies=[Depends(requires_operator)],
)
async def submit_workflow(
    body: s.WorkflowSubmitRequest, state: State
) -> s.WorkflowResponse:
    """Validate and register a workflow definition.

    Validation is the domain's own: an unknown dependency, a duplicate step
    name, or a cycle is rejected by
    :class:`~orchestrator.workflow.definition.WorkflowDefinition` and surfaces
    here as a 400.
    """
    definition = WorkflowDefinition(
        name=body.name,
        goal=body.goal,
        steps=s.steps_from_request(body.steps),
        max_concurrency=body.max_concurrency,
    )
    definition.compile()  # cycles are only detectable here
    state.workflows[definition.name] = definition
    return s.WorkflowResponse.from_definition(definition)


@router.get(
    "/workflows",
    response_model=list[s.WorkflowResponse],
    tags=["workflows"],
    summary="List registered workflows",
)
async def list_workflows(state: State) -> list[s.WorkflowResponse]:
    """List every registered workflow, in name order."""
    return [
        s.WorkflowResponse.from_definition(state.workflows[name])
        for name in sorted(state.workflows)
    ]


@router.get(
    "/workflows/{name}",
    response_model=s.WorkflowResponse,
    tags=["workflows"],
    summary="One workflow",
)
async def get_workflow(name: str, state: State) -> s.WorkflowResponse:
    """Return a workflow and the shape it compiles to."""
    return s.WorkflowResponse.from_definition(state.workflow(name))


@router.delete(
    "/workflows/{name}",
    status_code=204,
    tags=["workflows"],
    summary="Unregister a workflow",
    dependencies=[Depends(requires_operator)],
)
async def delete_workflow(name: str, state: State) -> None:
    """Unregister a workflow.

    Runs already started from it are untouched: their definition is in the log,
    not in this registry.
    """
    state.workflow(name)
    del state.workflows[name]


@router.post(
    "/workflows/{name}/runs",
    response_model=s.RunStatusResponse,
    status_code=202,
    tags=["workflows"],
    summary="Execute a workflow",
    dependencies=[Depends(requires_operator)],
)
async def start_workflow(
    name: str, body: s.StartRunRequest, state: State
) -> s.RunStatusResponse:
    """Start executing a workflow and return immediately.

    The run identifier is in the response; ``GET /runs/{id}/status`` follows its
    progress and ``GET /runs/{id}/events`` streams it.
    """
    definition = state.workflow(name)
    factory = state.require_executor()
    repo_path = str(state.resolve(body.repo_path, what="repo_path")) if body.repo_path else ""

    run_id = RunId.generate()
    engine = state.engine_for(
        repo_path=repo_path,
        max_concurrency=body.max_concurrency or state.config.run.max_concurrency,
        run_timeout_s=body.run_timeout_s,
    )
    task = asyncio.get_running_loop().create_task(
        engine.run(definition, factory, run_id=run_id)
    )
    state.runs[run_id] = _job(run_id, name, engine, task)

    # The engine records run.created as its first act; give it the chance to do
    # so before replying, so the identifier we hand back is already in the log.
    await asyncio.sleep(0)
    run_state = state.store.replay(run_id)
    return s.RunStatusResponse(
        id=str(run_id),
        status=run_state.status.value if run_state.event_count else RunStatus.CREATED.value,
        active=True,
        total=len(definition.steps),
        succeeded=0,
        failed=0,
        blocked=0,
        running=0,
        pending=len(definition.steps),
        percent=0.0,
        complete=False,
        healthy=True,
        summary=f"starting {len(definition.steps)} step(s)",
    )


# --------------------------------------------------------------------------- #
# Agents
# --------------------------------------------------------------------------- #


@router.get(
    "/agents/roles",
    response_model=list[s.AgentRoleResponse],
    tags=["agents"],
    summary="How each agent role is configured",
)
async def list_agent_roles(state: State) -> list[s.AgentRoleResponse]:
    """Report the resolved settings for every agent role.

    Provider-neutral: these are the settings a provider would be asked for, not
    a description of any particular backend.
    """
    roles = sorted({role.value for role in AgentRole} | set(state.config.roles))
    return [
        s.AgentRoleResponse(
            role=role,
            model=(resolved := state.config.provider_for(role)).model,
            effort=resolved.effort.value,
            max_tokens=resolved.max_tokens,
            thinking=resolved.thinking.value,
            budget_seconds=state.config.agent.budget_seconds,
            budget_tokens=state.config.agent.budget_tokens,
            budget_tool_calls=state.config.agent.budget_tool_calls,
        )
        for role in roles
    ]


@router.get(
    "/agents/tools",
    response_model=list[s.ToolResponse],
    tags=["agents"],
    summary="The tools an agent may call",
)
async def list_agent_tools(state: State) -> list[s.ToolResponse]:
    """List the tool surface, sorted by name as it is sent to a model."""
    return [
        s.ToolResponse(name=spec.name, description=spec.description, schema=dict(spec.schema))
        for spec in state.tools.specs()
    ]


@router.post(
    "/agents/prompt",
    response_model=s.PromptResponse,
    tags=["agents"],
    summary="Render the prompt an agent would receive",
)
async def render_prompt(body: s.PromptRequest, state: State) -> s.PromptResponse:
    """Assemble a prompt without calling a model.

    Exposed because prompt-prefix stability is a property people need to be able
    to check: the fingerprint of identical inputs must not change between calls,
    and if it does, caching is silently not happening (spec §9).
    """
    try:
        role = AgentRole(body.role)
    except ValueError:
        raise NotFound(
            f"there is no agent role named {body.role!r}",
            detail={"role": body.role, "known": [r.value for r in AgentRole]},
        ) from None

    spec = TaskSpec(
        task_id=TaskId.generate(),
        title=body.title,
        prompt=body.prompt,
        role=role,
        feedback=tuple(body.feedback),
        labels=frozenset(body.labels),
    )
    builder = PromptBuilder()
    tools = state.tools.specs()
    return s.PromptResponse(
        role=role.value,
        blocks=[block.text for block in builder.system_blocks(spec)],
        messages=[
            "".join(
                block.text for block in message.content if hasattr(block, "text")
            )
            if not isinstance(message.content, str)
            else message.content
            for message in builder.opening_messages(spec)
        ],
        fingerprint=builder.fingerprint(spec, tools),
        tools=[tool.name for tool in tools],
    )


# --------------------------------------------------------------------------- #
# Builds
# --------------------------------------------------------------------------- #


@router.post(
    "/builds/analyze",
    response_model=s.LayoutResponse,
    tags=["builds"],
    summary="Analyze a project",
)
async def analyze_project(body: s.BuildAnalyzeRequest, state: State) -> s.LayoutResponse:
    """Scan a project into build units and their dependency graph."""
    layout, graph = await _analyze(state, body)
    return s.LayoutResponse.from_layout(layout, graph)


@router.post(
    "/builds/plan",
    response_model=s.BuildPlanResponse,
    tags=["builds"],
    summary="Plan a build without running it",
)
async def plan_build(body: s.BuildPlanRequest, state: State) -> s.BuildPlanResponse:
    """Work out what would be rebuilt, and why."""
    layout, graph = await _analyze(state, body)
    plan = _plan(state, body, layout, graph)
    return s.BuildPlanResponse.from_plan(plan)


@router.post(
    "/builds",
    response_model=s.BuildReportResponse,
    status_code=202,
    tags=["builds"],
    summary="Plan and execute a build",
    dependencies=[Depends(requires_operator)],
)
async def start_build(body: s.BuildStartRequest, state: State) -> s.BuildReportResponse:
    """Start a build and return immediately with its identifier."""
    root = state.resolve(body.path, what="path")
    layout, graph = await _analyze(state, body)
    plan = _plan(state, body, layout, graph)

    executor = BuildExecutor(
        process_runner=state.process_runner,
        cache=state.build_cache,
        config=ExecutorConfig(max_concurrency=body.max_concurrency),
    )
    build_id = f"build_{int(time.time() * 1000)}_{len(state.builds)}"

    async def execute() -> Any:
        report = await executor.run(plan, root=root)
        state.build_reports[build_id] = report
        return report

    task = asyncio.get_running_loop().create_task(execute())
    state.builds[build_id] = _build_job(build_id, str(root), task)
    return s.BuildReportResponse.running(build_id, str(root))


@router.get(
    "/builds",
    response_model=list[s.BuildReportResponse],
    tags=["builds"],
    summary="List builds",
)
async def list_builds(state: State) -> list[s.BuildReportResponse]:
    """List every build this server has started, oldest first."""
    return [_build_response(state, build_id) for build_id in state.builds]


@router.get(
    "/builds/{build_id}",
    response_model=s.BuildReportResponse,
    tags=["builds"],
    summary="One build",
)
async def get_build(build_id: str, state: State) -> s.BuildReportResponse:
    """Return a build's report, or its running status."""
    if build_id not in state.builds:
        raise NotFound(f"no build with id {build_id}", detail={"build_id": build_id})
    return _build_response(state, build_id)


@router.delete(
    "/builds/cache",
    tags=["builds"],
    summary="Clear the build cache",
    dependencies=[Depends(requires_operator)],
)
async def clear_build_cache(state: State) -> dict[str, int]:
    """Forget every cached build. Artifacts on disk are left alone."""
    return {"cleared": state.build_cache.clear()}



# --------------------------------------------------------------------------- #
# Approvals, attempt history, transcripts, and diffs
# --------------------------------------------------------------------------- #


def _approvals(state: AppState) -> Any:
    """Build an approval service over this application's store."""
    from orchestrator.workflow.approval import ApprovalService

    return ApprovalService(state.store)


@router.get(
    "/approvals",
    tags=["approvals"],
    summary="Everything waiting for a person",
)
async def approval_queue(
    state: State, limit: Annotated[int, Query(ge=1, le=500)] = 100
) -> list[dict[str, Any]]:
    """List pending approvals across every run, oldest first.

    Oldest first on purpose: a queue sorted newest-first is a queue whose
    bottom never gets looked at.
    """
    return [request.to_public() for request in _approvals(state).queue(limit=limit)]


@router.get(
    "/runs/{run_id}/approvals",
    tags=["approvals"],
    summary="A run's approvals",
)
async def run_approvals(run_id: str, state: State) -> list[dict[str, Any]]:
    """Every approval request in one run, resolved or not."""
    identifier = RunId(run_id)
    state.state_of(identifier)
    return [request.to_public() for request in _approvals(state).for_run(identifier)]


@router.post(
    "/runs/{run_id}/tasks/{task_id}/approval",
    status_code=201,
    tags=["approvals"],
    summary="Ask for a person to review a task",
    dependencies=[Depends(requires_operator)],
)
async def request_approval(
    run_id: str, task_id: str, state: State, body: s.ApprovalRequestBody
) -> dict[str, Any]:
    """Record that a task is waiting for review."""
    identifier = RunId(run_id)
    state.state_of(identifier)
    request = _approvals(state).request(
        identifier, TaskId(task_id), reason=body.reason, attempt=body.attempt
    )
    return request.to_public()


@router.post(
    "/runs/{run_id}/tasks/{task_id}/approval/resolve",
    tags=["approvals"],
    summary="Approve, reject, or send a task back",
    dependencies=[Depends(requires_operator)],
)
async def resolve_approval(
    run_id: str,
    task_id: str,
    state: State,
    body: s.ApprovalDecisionBody,
    request: Request,
) -> dict[str, Any]:
    """Record a decision.

    The actor is taken from the credential, never from the body: an approval
    attributed to whoever the caller says they are is not attributable at all.
    """
    from orchestrator.api.security import ANONYMOUS
    from orchestrator.workflow.approval import ApprovalDecision

    identifier = RunId(run_id)
    state.state_of(identifier)
    principal = getattr(request.state, "principal", None) or ANONYMOUS

    resolved = _approvals(state).resolve(
        identifier,
        TaskId(task_id),
        ApprovalDecision(body.decision),
        actor=principal.label,
        note=body.note,
    )
    audit(
        request,
        f"approval.{body.decision}",
        target=f"{run_id}/{task_id}",
        detail=body.note,
    )
    return resolved.to_public()


@router.get(
    "/runs/{run_id}/tasks/{task_id}/attempts",
    tags=["approvals"],
    summary="Every attempt at a task",
)
async def attempt_history(run_id: str, task_id: str, state: State) -> dict[str, Any]:
    """Return each attempt, what it produced, and which gates it failed."""
    identifier = RunId(run_id)
    state.state_of(identifier)
    return _approvals(state).history(identifier, TaskId(task_id)).to_public()


@router.get(
    "/runs/{run_id}/tasks/{task_id}/transcript",
    tags=["approvals"],
    summary="What one attempt did",
)
async def transcript(
    run_id: str,
    task_id: str,
    state: State,
    attempt_id: Annotated[str, Query(description="One attempt; omitted, all of them.")] = "",
) -> dict[str, Any]:
    """Return the events of an attempt, in order."""
    from orchestrator.core.events import AttemptId

    identifier = RunId(run_id)
    state.state_of(identifier)
    return (
        _approvals(state)
        .transcript(
            identifier,
            TaskId(task_id),
            attempt_id=AttemptId(attempt_id) if attempt_id else None,
        )
        .to_public()
    )


@router.get(
    "/runs/{run_id}/tasks/{task_id}/diff",
    tags=["approvals"],
    summary="What a task changed",
)
async def task_diff(
    run_id: str,
    task_id: str,
    state: State,
    attempt_id: Annotated[str, Query()] = "",
    context: Annotated[int, Query(ge=0, le=20)] = 3,
) -> dict[str, Any]:
    """Return the diff an attempt produced.

    Read from the attempt's branch through the Git layer, because "what
    changed" is a question about a worktree and neither the log nor this route
    is in a position to answer it.
    """
    from orchestrator.git_manager.repo import GitRepository

    identifier = RunId(run_id)
    state.state_of(identifier)
    history = _approvals(state).history(identifier, TaskId(task_id))

    attempts = [a for a in history.attempts if not attempt_id or a.id == attempt_id]
    attempt = attempts[-1] if attempts else None
    if attempt is None:
        raise NotFound(
            f"task {task_id} has no attempt to diff",
            detail={"run_id": run_id, "task_id": task_id},
        )
    if not attempt.branch:
        raise Conflict(
            "this attempt has no branch, so there is nothing to diff; the run "
            "was executed without a workspace manager",
            detail={"attempt": attempt.id},
        )

    repo_path = state.state_of(identifier).repo_path
    if not repo_path:
        raise Conflict("this run recorded no repository path")

    repository = GitRepository(state.resolve(repo_path, what="repo_path"))
    patch = await repository.diff_branch(attempt.branch, context=context)
    return {
        "run_id": run_id,
        "task_id": task_id,
        "attempt_id": attempt.id,
        "branch": attempt.branch,
        "diff": patch,
    }

# --------------------------------------------------------------------------- #
# Event streaming
# --------------------------------------------------------------------------- #


@router.get(
    "/runs/{run_id}/log",
    response_model=list[s.EventResponse],
    tags=["events"],
    summary="Read a run's recorded events",
)
async def read_run_log(
    run_id: str,
    state: State,
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
    after: Annotated[
        str | None, Query(description="Return only events created after this one.")
    ] = None,
) -> list[s.EventResponse]:
    """Return a run's log, oldest first.

    The streaming endpoints are for watching a run happen; this is for reading
    one that already did. A client that only wants the history should not have
    to hold a connection open and guess when the replay has finished.

    Args:
        run_id: The run to read.
        limit: How many events to return, from the start of the range.
        after: Exclusive lower bound, for paging. Pass the last identifier you
            received; identifiers sort in creation order, so paging is stable
            even while the run is still writing.
    """
    identifier = RunId(run_id)
    state.state_of(identifier)
    events = (
        state.store.events.read_since(identifier, EventId(after))
        if after
        else state.store.events.read_run(identifier)
    )
    return [s.EventResponse.from_event(event) for event in events[:limit]]


@router.get(
    "/events",
    tags=["events"],
    summary="Stream every event (SSE)",
    response_class=StreamingResponse,
)
async def stream_all_events(
    request: Request,
    state: State,
    heartbeat_s: Annotated[float, Query(gt=0, le=300)] = 15.0,
) -> StreamingResponse:
    """Stream events from every run as Server-Sent Events."""
    return _sse(state, request, run_id=None, replay=(), heartbeat_s=heartbeat_s)


@router.get(
    "/runs/{run_id}/events",
    tags=["events"],
    summary="Stream one run's events (SSE)",
    response_class=StreamingResponse,
)
async def stream_run_events(
    run_id: str,
    request: Request,
    state: State,
    replay: Annotated[bool, Query(description="Send the log so far first.")] = True,
    heartbeat_s: Annotated[float, Query(gt=0, le=300)] = 15.0,
) -> StreamingResponse:
    """Stream one run's events, optionally replaying the log first.

    The subscription is opened before the log is read, so an event that arrives
    during the replay is delivered rather than lost in the seam.
    """
    identifier = RunId(run_id)
    state.state_of(identifier)
    history = state.store.events.read_run(identifier) if replay else ()
    return _sse(
        state, request, run_id=identifier, replay=history, heartbeat_s=heartbeat_s
    )


@router.websocket("/runs/{run_id}/ws")
async def stream_run_websocket(websocket: WebSocket, run_id: str) -> None:
    """Stream one run's events over a WebSocket.

    The same content as the SSE endpoint, for clients that would rather have a
    socket. Closes with 4404 when the run does not exist — an application code,
    because a WebSocket has no status line to put a 404 in.
    """
    state: AppState = websocket.app.state.orchestrator

    identifier = RunId(run_id)
    try:
        state.state_of(identifier)
    except NotFound:
        await websocket.close(code=4404, reason=f"no run with id {run_id}")
        return

    await websocket.accept()
    subscription = state.broker.subscribe(identifier)
    history = state.store.events.read_run(identifier)
    try:
        async for event in state.broker.stream(subscription, replay=history):
            if event is None:
                await websocket.send_json({"type": "keep-alive"})
                continue
            await websocket.send_json(ws_payload(event))
    except WebSocketDisconnect:
        pass
    finally:
        state.broker.unsubscribe(subscription)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _sse(
    state: AppState,
    request: Request,
    *,
    run_id: RunId | None,
    replay: Sequence[Event],
    heartbeat_s: float,
) -> StreamingResponse:
    """Build a Server-Sent Events response over a fresh subscription.

    The stream is unbounded, so the only thing that ends it is the client going
    away. That is checked on every event and every heartbeat: without it a
    dropped connection would leak its subscription and go on being served
    forever, which is how an idle server acquires a hundred phantom watchers.
    """
    subscription = state.broker.subscribe(run_id)

    async def frames() -> AsyncIterator[str]:
        try:
            async for event in state.broker.stream(
                subscription, replay=tuple(replay), heartbeat_s=heartbeat_s
            ):
                yield sse_frame(event)
                if await request.is_disconnected():
                    return
            if subscription.lagged:
                yield ": lagged — this client fell too far behind and was disconnected\n\n"
        finally:
            state.broker.unsubscribe(subscription)

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={"cache-control": "no-store", "x-accel-buffering": "no"},
    )


def _task(state: AppState, run_id: str, task_id: str) -> Any:
    """Return one task's projection.

    Raises:
        NotFound: If the run or the task is absent.
    """
    run_state = state.state_of(RunId(run_id))
    try:
        return run_state.tasks[TaskId(task_id)]
    except KeyError:
        raise NotFound(
            f"run {run_id} has no task {task_id}",
            detail={"run_id": run_id, "task_id": task_id},
        ) from None


def _as_state(value: str) -> TaskState:
    """Read a persisted state string, tolerating an unknown one."""
    try:
        return TaskState(value)
    except ValueError:
        return TaskState.PENDING


def _job(run_id: RunId, workflow: str, engine: Any, task: Any) -> Any:
    """Build a run job record."""
    from orchestrator.api.state import RunJob

    return RunJob(
        run_id=run_id,
        workflow=workflow,
        engine=engine,
        task=task,
        started_at=time.monotonic(),
    )


def _build_job(build_id: str, path: str, task: Any) -> Any:
    """Build a build job record."""
    from orchestrator.api.state import BuildJob

    return BuildJob(id=build_id, path=path, task=task, started_at=time.monotonic())


def _build_response(state: AppState, build_id: str) -> s.BuildReportResponse:
    """Render a build, finished or otherwise."""
    job = state.builds[build_id]
    report = state.build_reports.get(build_id)
    if report is None:
        return s.BuildReportResponse.running(build_id, job.path)
    return s.BuildReportResponse.from_report(build_id, job.path, report)


async def _analyze(state: AppState, body: s.BuildAnalyzeRequest) -> tuple[Any, Any]:
    """Analyze the project a build request names."""
    root = state.resolve(body.path, what="path")
    manifest = (
        [
            BuildUnit(
                name=u.name,
                command=u.command,
                sources=tuple(u.sources),
                depends_on=tuple(u.depends_on),
                artifacts=tuple(u.artifacts),
                incremental=u.incremental,
                timeout_s=u.timeout_s,
            )
            for u in body.manifest
        ]
        if body.manifest is not None
        else None
    )
    layout = await ProjectAnalyzer().analyze(root, manifest=manifest)
    graph = await DependencyAnalyzer().analyze(layout)
    return layout, graph


def _plan(state: AppState, body: s.BuildPlanRequest, layout: Any, graph: Any) -> Any:
    """Build a plan from a request."""
    changed = changed_units(layout, body.changed_paths) if body.changed_paths else ()
    return BuildPlanner(root=layout.root).plan(
        layout,
        graph,
        changed=changed,
        force=body.force,
        full=body.full,
        cache=state.build_cache if body.use_cache else None,
    )


def _version() -> str:
    """The control plane's version."""
    from orchestrator.api.state import API_VERSION

    return API_VERSION


def build_router() -> APIRouter:
    """Return the assembled router.

    A function rather than a bare module attribute so an embedder can mount the
    control plane more than once — under a prefix, or beside another app.
    """
    return router

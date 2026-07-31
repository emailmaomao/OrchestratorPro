"""Application state: what the control plane owns between requests.

FastAPI dependencies resolve to a single :class:`AppState` per application. It
holds the durable store, the registered workflows, the running jobs, and the
seams through which everything above M0 is reached.

Three decisions shape it:

* **Execution is injected, never constructed here.** The API knows how to start
  a run; it does not know what an agent is or which model serves it. A composition
  root supplies ``executor_factory``, and without one the server still records,
  replays, and reports — it just refuses to execute, and says so (503). That is
  what keeps the control plane provider-independent.
* **Every recorded event is published.** :meth:`AppState.record` writes to the
  log and fans the event out to subscribers in one call, so a streaming client
  cannot see a state the log does not have.
* **Paths from a request are confined.** ``repo_path`` and build paths arrive
  over HTTP, which makes them exactly as untrusted as a model-supplied path.
  They are canonicalized and checked against a configured root before any I/O
  (``CLAUDE.md``, security invariants).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.agent.tools import PathEscapeError, ToolRegistry, default_registry, resolve_within
from orchestrator.api.streaming import EventBroker
from orchestrator.builder.cache import BuildCache, MemoryCache
from orchestrator.core.config import OrchestratorConfig
from orchestrator.core.events import Event, OrchestratorError, RunId
from orchestrator.core.records import RunState
from orchestrator.core.run_store import RunStore
from orchestrator.core.storage import Database
from orchestrator.core.version import VERSION
from orchestrator.task.dispatcher import TaskExecutor
from orchestrator.task.model import TaskState
from orchestrator.test_runner.execution import ProcessRunner
from orchestrator.workflow.definition import WorkflowDefinition, WorkflowPlan
from orchestrator.workflow.engine import EngineConfig, WorkflowEngine
from orchestrator.workflow.progress import EventEmitter, ProgressSnapshot, ProgressTracker

__all__ = [
    "ApiError",
    "AppState",
    "BuildJob",
    "Conflict",
    "ExecutorFactory",
    "NotFound",
    "NotSupported",
    "RunJob",
]

#: Builds the executor a run's steps are performed by. Supplied by whoever
#: composes the application; the API never builds one itself.
ExecutorFactory = Callable[[WorkflowPlan, RunId, EventEmitter], TaskExecutor]

#: The control plane's version, reported by ``/health`` and in the OpenAPI
#: document. Distinct from the schema version, which describes the database.
#:
#: This is the distribution version, not a separately maintained number. It was
#: separate until v1.0 and drifted immediately: a 1.0.0 wheel served 0.1.0 from
#: ``/health``. A version a client reads must be the version it is talking to.
API_VERSION = VERSION


class ApiError(OrchestratorError):
    """A request could not be served."""

    code = "api"
    retryable = False
    status_code = 400


class NotFound(ApiError):
    """The addressed thing does not exist."""

    code = "not_found"
    status_code = 404


class Conflict(ApiError):
    """The request contradicts the current state."""

    code = "conflict"
    status_code = 409


class NotSupported(ApiError):
    """The server is not configured to do this."""

    code = "not_supported"
    retryable = True
    status_code = 503


@dataclass(slots=True)
class RunJob:
    """A run executing in the background."""

    run_id: RunId
    workflow: str
    engine: WorkflowEngine
    task: asyncio.Task[Any]
    started_at: float

    @property
    def done(self) -> bool:
        """Whether the run has finished, one way or another."""
        return self.task.done()


@dataclass(slots=True)
class BuildJob:
    """A build executing in the background."""

    id: str
    path: str
    task: asyncio.Task[Any]
    started_at: float

    @property
    def done(self) -> bool:
        """Whether the build has finished."""
        return self.task.done()


@dataclass(slots=True)
class AppState:
    """Everything one running control plane owns."""

    store: RunStore
    broker: EventBroker = field(default_factory=EventBroker)
    config: OrchestratorConfig = field(default_factory=OrchestratorConfig)
    workspace_root: Path | None = None
    executor_factory: ExecutorFactory | None = None
    process_runner: ProcessRunner | None = None
    build_cache: BuildCache = field(default_factory=MemoryCache)
    tools: ToolRegistry = field(default_factory=default_registry)
    workflows: dict[str, WorkflowDefinition] = field(default_factory=dict)
    runs: dict[RunId, RunJob] = field(default_factory=dict)
    builds: dict[str, BuildJob] = field(default_factory=dict)
    build_reports: dict[str, Any] = field(default_factory=dict)
    #: Identity, when the installation has it. ``None`` serves everything
    #: as a built-in operator, which is what a single person on loopback
    #: wants and what every build before authentication did.
    auth: Any = None
    started_at: float = field(default_factory=time.monotonic)

    # ------------------------------------------------------------- lifecycle

    @classmethod
    def in_memory(cls, **kwargs: Any) -> AppState:
        """Build a state over an ephemeral database. Intended for tests."""
        db = Database.in_memory()
        db.migrate()
        return cls(store=RunStore(db), **kwargs)

    @property
    def database(self) -> Database:
        """The underlying database."""
        return self.store.db

    @property
    def uptime_s(self) -> float:
        """How long this application has been up."""
        return time.monotonic() - self.started_at

    @property
    def can_execute(self) -> bool:
        """Whether an execution backend has been supplied."""
        return self.executor_factory is not None

    async def shutdown(self) -> None:
        """Cancel in-flight work and release subscribers.

        Runs are asked to stop rather than killed: an attempt that is mid-flight
        has already spent its tokens, and letting it record its outcome is worth
        the extra moment (FR-2.9).
        """
        for job in list(self.runs.values()):
            job.engine.request_cancel()
        pending = [job.task for job in self.runs.values() if not job.task.done()]
        pending += [job.task for job in self.builds.values() if not job.task.done()]
        if pending:
            await asyncio.wait(pending, timeout=5.0)
            for task in pending:
                if not task.done():
                    task.cancel()
        await self.broker.close()

    # ---------------------------------------------------------------- events

    def record(self, event: Event) -> None:
        """Persist an event and publish it to subscribers.

        Persistence first: a subscriber that saw an event the log does not hold
        would be reporting a state the system cannot recover to.
        """
        self.store.record(event)
        self.broker.publish(event)

    # ------------------------------------------------------------------ runs

    def state_of(self, run_id: RunId) -> RunState:
        """Replay one run.

        Raises:
            NotFound: If the log holds nothing for it.
        """
        state = self.store.replay(run_id)
        if state.event_count == 0:
            raise NotFound(
                f"no run with id {run_id}", detail={"run_id": str(run_id)}
            )
        return state

    def is_active(self, run_id: RunId) -> bool:
        """Whether a run is executing right now."""
        job = self.runs.get(run_id)
        return job is not None and not job.done

    @property
    def active_runs(self) -> int:
        """How many runs are executing."""
        return sum(1 for job in self.runs.values() if not job.done)

    @staticmethod
    def progress_of(state: RunState) -> ProgressSnapshot:
        """Derive a progress snapshot from a replayed run."""
        tracker = ProgressTracker(
            {task_id: task.title for task_id, task in state.tasks.items()},
            max_attempts={
                task_id: task.max_attempts for task_id, task in state.tasks.items()
            },
        )
        return tracker.update(
            {
                task_id: _as_state(task.state)
                for task_id, task in state.tasks.items()
            },
            {task_id: task.attempts_made for task_id, task in state.tasks.items()},
        )

    def engine_for(self, **overrides: Any) -> WorkflowEngine:
        """Build an engine wired to this application's store.

        Configured values are the floor; an explicit override in ``overrides``
        wins, so a request may ask for less concurrency than the installation's
        default but never has to restate it.
        """
        settings: dict[str, Any] = {"max_concurrency": self.config.run.max_concurrency}
        settings.update({k: v for k, v in overrides.items() if v is not None})
        return WorkflowEngine(config=EngineConfig(**settings), store=self.store)

    def require_executor(self) -> ExecutorFactory:
        """Return the execution backend.

        Raises:
            NotSupported: If none was configured. Reporting this as a 503 rather
                than a 500 is the honest answer: the request was fine, the
                server is simply not equipped to serve it.
        """
        if self.executor_factory is None:
            raise NotSupported(
                "no execution backend is configured, so this server can record "
                "and report runs but not execute them; supply an executor "
                "factory when creating the application"
            )
        return self.executor_factory

    def workflow(self, name: str) -> WorkflowDefinition:
        """Return a registered workflow.

        Raises:
            NotFound: If no workflow is registered under that name.
        """
        try:
            return self.workflows[name]
        except KeyError:
            raise NotFound(
                f"no workflow named {name!r} is registered",
                detail={"workflow": name, "known": sorted(self.workflows)},
            ) from None

    def forget_finished(self) -> int:
        """Drop completed jobs, returning how many were released."""
        done = [run_id for run_id, job in self.runs.items() if job.done]
        for run_id in done:
            del self.runs[run_id]
        return len(done)

    # ----------------------------------------------------------------- paths

    def resolve(self, candidate: str, *, what: str = "path") -> Path:
        """Canonicalize a client-supplied path and confine it.

        A path from an HTTP body is exactly as trustworthy as one from a model.
        When no ``workspace_root`` is configured the path is still resolved, but
        containment cannot be checked — that configuration is for a single
        operator on their own machine, and the docstring says so rather than the
        code pretending otherwise.

        Raises:
            ApiError: If the path escapes the configured root.
        """
        text = candidate.strip()
        if not text:
            raise ApiError(f"{what} must not be empty", detail={"field": what})
        if self.workspace_root is None:
            return Path(text).expanduser().resolve()
        try:
            return resolve_within(self.workspace_root, text)
        except PathEscapeError as exc:
            raise ApiError(
                f"{what} must stay inside the configured workspace root "
                f"{self.workspace_root}",
                detail={"field": what, "path": candidate, "reason": str(exc)},
            ) from exc


def _as_state(value: str) -> TaskState:
    """Convert a persisted state string, tolerating an unknown one."""
    try:
        return TaskState(value)
    except ValueError:
        return TaskState.PENDING


def status_code_for(error: Exception) -> int:
    """Map an exception onto an HTTP status.

    Domain errors carry a stable ``code`` but not a status: HTTP is this layer's
    concern, and pushing status codes down into the domain would put transport
    knowledge in modules that have no business with it.
    """
    from orchestrator.builder.model import BuilderError
    from orchestrator.core.config import ConfigError
    from orchestrator.core.event_store import DuplicateEventError
    from orchestrator.core.events import DomainValidationError
    from orchestrator.task.graph import GraphError
    from orchestrator.workflow.definition import WorkflowDefinitionError
    from orchestrator.workflow.engine import WorkflowEngineError
    from orchestrator.workflow.recovery import RecoveryError

    from orchestrator.auth.models import AuthError, Forbidden, Unauthorized

    if isinstance(error, ApiError):
        return error.status_code
    if isinstance(error, Unauthorized):
        return 401
    if isinstance(error, Forbidden):
        return 403
    if isinstance(error, AuthError):
        return 400
    if isinstance(error, DuplicateEventError):
        return 409
    if isinstance(
        error,
        (
            ConfigError,
            DomainValidationError,
            WorkflowDefinitionError,
            WorkflowEngineError,
            GraphError,
            BuilderError,
            RecoveryError,
            PathEscapeError,
        ),
    ):
        # Every one of these means the request described something the domain
        # will not accept. That is the client's problem to fix, not the server's.
        return 400
    if isinstance(error, OrchestratorError):
        return 500
    return 500


def error_body(error: Exception) -> Mapping[str, Any]:
    """Render an exception as the API's error envelope."""
    if isinstance(error, OrchestratorError):
        return {
            "code": error.code,
            "message": str(error),
            "retryable": error.retryable,
            "detail": dict(getattr(error, "detail", {}) or {}),
        }
    return {
        "code": "internal",
        "message": str(error) or error.__class__.__name__,
        "retryable": False,
        "detail": {},
    }

"""Build execution, failure analysis, and build event logging.

The executor turns a :class:`~orchestrator.builder.planner.BuildPlan` into a
:class:`~orchestrator.builder.model.BuildReport`. It does not schedule: the plan
becomes a :class:`~orchestrator.task.graph.TaskGraph` and the M4
:class:`~orchestrator.task.dispatcher.TaskDispatcher` runs it, so ordering and
concurrency behave exactly as they do everywhere else in the system.

Three things this module is careful about:

* **A broken tool is not a broken program.** A command that could not be
  executed, or that was killed for overrunning, comes back as
  :attr:`~orchestrator.builder.model.BuildStatus.ERRORED` or ``TIMED_OUT`` — the
  same distinction the gates draw (FR-4.4), and for the same reason: feedback
  that says "the build failed" when the compiler is missing sends the next
  attempt to rewrite working code.
* **Output is parsed into locations, not quoted at length.** A diagnostic with a
  file and a line is actionable; four hundred lines of build log is not
  (FR-4.3).
* **Nothing is cached until its artifacts have been seen.** The cache entry is
  written from what is on disk after the command finished, never from what the
  command said it wrote.

:class:`BuildGate` adapts the whole thing to the
:class:`~orchestrator.test_runner.base.GateRunner` protocol, so a workflow step
can gate on a real build without the workflow engine knowing this package
exists.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.builder.analysis import DependencyAnalyzer, ProjectAnalyzer, UnitGraph
from orchestrator.builder.cache import ArtifactTracker, BuildCache, CacheEntry
from orchestrator.builder.model import (
    Artifact,
    BuildReport,
    BuildStatus,
    BuildUnit,
    Diagnostic,
    ProjectLayout,
    Severity,
    UnitResult,
)
from orchestrator.builder.planner import BuildPlan, BuildPlanner
from orchestrator.core.events import Event, EventType, RunId, TaskId
from orchestrator.task.dispatcher import AttemptOutcome, TaskDispatcher
from orchestrator.task.model import Task
from orchestrator.task.retry import BackoffStrategy, RetryPolicy
from orchestrator.test_runner.base import (
    CaseResult,
    CaseStatus,
    Outcome,
    SuiteSpec,
    Verdict,
)
from orchestrator.test_runner.execution import ProcessResult, ProcessRunner, SubprocessRunner

__all__ = [
    "BuildEventLog",
    "BuildExecutor",
    "BuildGate",
    "analyze_output",
    "classify",
]

#: Exit codes that mean the shell could not run the command at all, rather than
#: that the command ran and disliked what it found.
_HARNESS_EXIT_CODES = frozenset({126, 127})

#: Phrases that mean the same thing on platforms that do not use those codes.
_HARNESS_PHRASES = (
    "command not found",
    "no such file or directory",
    "is not recognized as an internal or external command",
    "permission denied",
)

_SEVERITIES = {
    "error": Severity.ERROR,
    "fatal error": Severity.ERROR,
    "warning": Severity.WARNING,
    "note": Severity.NOTE,
    "info": Severity.NOTE,
}

#: ``path:line:col: error: message`` — gcc, clang, ruff, mypy, go vet.
_UNIX_STYLE = re.compile(
    r"^(?P<file>[^\s:][^:]*):(?P<line>\d+)(?::(?P<column>\d+))?:\s*"
    r"(?P<severity>fatal error|error|warning|note|info)\s*:\s*(?P<message>.+)$"
)

#: ``path(line,col): error TS2345: message`` — MSVC, tsc.
_PAREN_STYLE = re.compile(
    r"^(?P<file>[^\s(][^(]*)\((?P<line>\d+)(?:,(?P<column>\d+))?\)\s*:\s*"
    r"(?P<severity>fatal error|error|warning|note|info)\s*"
    r"(?P<code>[A-Z]+\d+)?\s*:\s*(?P<message>.+)$"
)

#: ``error[E0308]: mismatched types`` — cargo, followed by ``  --> path:l:c``.
_RUST_HEAD = re.compile(
    r"^(?P<severity>error|warning)(?:\[(?P<code>[A-Z]\d+)\])?\s*:\s*(?P<message>.+)$"
)
_RUST_LOCATION = re.compile(
    r"^\s*-->\s*(?P<file>[^\s:]+):(?P<line>\d+):(?P<column>\d+)\s*$"
)

#: ``  File "path", line 12`` — a Python traceback, whose real message is last.
_PYTHON_FRAME = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+)')
_PYTHON_ERROR = re.compile(r"^(?P<code>[A-Za-z_.]*(?:Error|Exception)):\s*(?P<message>.+)$")


def analyze_output(
    stdout: str, stderr: str = "", *, unit: str = "", limit: int = 200
) -> tuple[Diagnostic, ...]:
    """Extract structured diagnostics from a build tool's output.

    Several formats are recognized because a real project uses several tools at
    once, and a diagnostic that only works for one compiler is a diagnostic that
    disappears the first time somebody adds a linter.

    Args:
        stdout: The command's standard output.
        stderr: Its standard error.
        unit: Stamped onto each diagnostic, so a merged report stays attributable.
        limit: Stop after this many. A tool in a loop can emit tens of thousands,
            and nobody reads past the first few.

    Returns:
        The diagnostics, in the order they appeared, deduplicated.
    """
    found: list[Diagnostic] = []
    seen: set[tuple[str, int | None, str]] = set()
    lines = f"{stdout}\n{stderr}".splitlines()

    def add(diagnostic: Diagnostic) -> None:
        key = (diagnostic.file, diagnostic.line, diagnostic.message)
        if key not in seen and len(found) < limit:
            seen.add(key)
            found.append(diagnostic)

    pending_rust: Diagnostic | None = None
    python_frame: tuple[str, int] | None = None

    for line in lines:
        text = line.rstrip()
        if not text:
            continue

        if pending_rust is not None:
            location = _RUST_LOCATION.match(text)
            if location is not None:
                add(
                    Diagnostic(
                        message=pending_rust.message,
                        file=location.group("file"),
                        line=int(location.group("line")),
                        column=int(location.group("column")),
                        severity=pending_rust.severity,
                        code=pending_rust.code,
                        unit=unit,
                    )
                )
                pending_rust = None
                continue
            add(pending_rust)
            pending_rust = None

        match = _UNIX_STYLE.match(text) or _PAREN_STYLE.match(text)
        if match is not None:
            groups = match.groupdict()
            add(
                Diagnostic(
                    message=groups["message"].strip(),
                    file=groups["file"].strip(),
                    line=int(groups["line"]),
                    column=int(groups["column"]) if groups.get("column") else None,
                    severity=_SEVERITIES.get(groups["severity"], Severity.ERROR),
                    code=(groups.get("code") or "").strip(),
                    unit=unit,
                )
            )
            continue

        frame = _PYTHON_FRAME.match(text)
        if frame is not None:
            python_frame = (frame.group("file"), int(frame.group("line")))
            continue

        failure = _PYTHON_ERROR.match(text)
        if failure is not None:
            file, line = python_frame or ("", None)
            add(
                Diagnostic(
                    message=failure.group("message").strip(),
                    file=file,
                    line=line,
                    severity=Severity.ERROR,
                    code=failure.group("code"),
                    unit=unit,
                )
            )
            python_frame = None
            continue

        head = _RUST_HEAD.match(text)
        if head is not None:
            pending_rust = Diagnostic(
                message=head.group("message").strip(),
                severity=_SEVERITIES.get(head.group("severity"), Severity.ERROR),
                code=head.group("code") or "",
                unit=unit,
            )

    if pending_rust is not None:
        add(pending_rust)
    return tuple(found)


def classify(result: ProcessResult) -> tuple[BuildStatus, str]:
    """Decide what a finished process means.

    Returns:
        The status and a reason. The reason is only meaningful when the tooling
        is what went wrong; a plain compilation failure explains itself through
        its diagnostics.
    """
    if result.timed_out:
        return BuildStatus.TIMED_OUT, "the build command exceeded its timeout"
    if result.exit_code == 0:
        return BuildStatus.SUCCEEDED, ""
    if result.exit_code in _HARNESS_EXIT_CODES or result.exit_code < 0:
        return (
            BuildStatus.ERRORED,
            f"the build command could not be executed (exit {result.exit_code})",
        )
    haystack = f"{result.stdout}\n{result.stderr}".lower()
    for phrase in _HARNESS_PHRASES:
        if phrase in haystack:
            return BuildStatus.ERRORED, f"the build command could not be executed: {phrase}"
    return BuildStatus.FAILED, ""


class BuildEventLog:
    """Records build activity to a sink, without letting the sink break a build.

    The same bargain the workflow emitter makes: a run that loses its telemetry
    is a degraded run, and a run that dies because telemetry failed is a lost
    one.
    """

    __slots__ = ("_count", "_failures", "_run_id", "_sink")

    def __init__(
        self, run_id: RunId | None = None, sink: Callable[[Event], None] | None = None
    ) -> None:
        """Create the log.

        Args:
            run_id: The run these events belong to. Without one nothing is
                emitted — an event with no run cannot be replayed into anything.
            sink: Receives each event.
        """
        self._run_id = run_id
        self._sink = sink
        self._count = 0
        self._failures = 0

    @property
    def emitted(self) -> int:
        """How many events the sink accepted."""
        return self._count

    @property
    def failures(self) -> int:
        """How many the sink rejected."""
        return self._failures

    @property
    def enabled(self) -> bool:
        """Whether anything is being recorded at all."""
        return self._sink is not None and self._run_id is not None

    def emit(
        self, event_type: EventType, *, task_id: TaskId | None = None, **payload: object
    ) -> bool:
        """Record one event. Returns whether the sink accepted it."""
        # Bound locally rather than asserted: `assert` is stripped under
        # `python -O`, so no invariant may rest on one. Binding narrows the
        # types for the checker *and* holds under optimisation, which a bare
        # assert does not — and it needs no raise for a state the guard above
        # already makes unreachable.
        sink, run_id = self._sink, self._run_id
        if sink is None or run_id is None:
            return False
        try:
            sink(
                Event.new(
                    event_type, run_id=run_id, task_id=task_id, payload=payload
                )
            )
        except Exception:  # noqa: BLE001 - telemetry must not break the build
            self._failures += 1
            return False
        self._count += 1
        return True

    def build_started(self, plan: BuildPlan, root: Path) -> bool:
        """Record that a build began."""
        return self.emit(
            EventType.BUILD_STARTED,
            root=str(root),
            units=list(plan.names),
            cached=list(plan.cached),
            waves=len(plan.layers),
            reasons={unit.name: unit.reason.value for unit in plan.units},
        )

    def unit_finished(self, result: UnitResult, *, task_id: TaskId | None = None) -> bool:
        """Record one unit's outcome."""
        return self.emit(
            EventType.BUILD_UNIT_FINISHED,
            task_id=task_id,
            unit=result.unit,
            status=result.status.value,
            duration_s=result.duration_s,
            errors=len(result.errors),
            artifacts=[artifact.path for artifact in result.artifacts],
            fingerprint=result.fingerprint,
        )

    def build_finished(self, report: BuildReport) -> bool:
        """Record the build's disposition."""
        return self.emit(
            EventType.BUILD_FINISHED,
            outcome="succeeded" if report.ok else "failed",
            rebuilt=list(report.rebuilt_units),
            cached=list(report.cached_units),
            failed=list(report.failed_units),
            duration_s=report.duration_s,
        )


@dataclass(frozen=True, slots=True)
class ExecutorConfig:
    """How a build is executed.

    Attributes:
        max_concurrency: Units built at once. ``None`` uses the plan's widest
            wave, which is the most parallelism the graph can actually absorb.
        label_limits: Per-label caps, for units that contend over something the
            graph does not model — a GPU, a licence server.
        collect_artifacts: Whether to digest outputs after each unit. Off makes
            the build faster and the cache untrustworthy, so it stays on unless
            a caller has a reason.
        write_cache: Whether successful builds are remembered.
    """

    max_concurrency: int | None = None
    label_limits: Mapping[str, int] = field(default_factory=dict)
    collect_artifacts: bool = True
    write_cache: bool = True


class BuildExecutor:
    """Runs a build plan."""

    __slots__ = ("_cache", "_clock", "_config", "_process", "_tracker")

    def __init__(
        self,
        *,
        process_runner: ProcessRunner | None = None,
        cache: BuildCache | None = None,
        tracker: ArtifactTracker | None = None,
        config: ExecutorConfig | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create the executor.

        Args:
            process_runner: Executes build commands. Defaults to a real
                subprocess runner; tests inject a scripted one, which is what
                keeps this package provider-independent and offline-testable.
            cache: Consulted for nothing and written to on success. Planning
                does the reading; the executor only records.
            tracker: Finds the artifacts a unit produced.
            config: Concurrency and behaviour.
            clock: Monotonic time source, injected for deterministic tests.
        """
        self._process = process_runner or SubprocessRunner()
        self._cache = cache
        self._tracker = tracker or ArtifactTracker()
        self._config = config or ExecutorConfig()
        self._clock = clock

    @property
    def cache(self) -> BuildCache | None:
        """The cache successful builds are recorded in."""
        return self._cache

    async def run(
        self,
        plan: BuildPlan,
        *,
        root: Path | None = None,
        run_id: RunId | None = None,
        event_sink: Callable[[Event], None] | None = None,
    ) -> BuildReport:
        """Execute a plan and report what happened.

        Args:
            plan: What to build.
            root: Where to build it. Defaults to the layout's root.
            run_id: Stamped onto emitted events.
            event_sink: Receives build events — the M2 run store, in production.

        Returns:
            The report. A failing build is a report, not an exception; only a
            plan that cannot be turned into a graph raises.
        """
        where = root if root is not None else plan.layout.root
        log = BuildEventLog(run_id, event_sink)
        started = self._clock()

        if plan.is_empty:
            report = BuildReport(
                results=self._cached_results(plan), duration_s=0.0
            )
            log.build_started(plan, where)
            log.build_finished(report)
            return report

        graph, ids = plan.to_task_graph()
        names = {task_id: name for name, task_id in ids.items()}
        results: dict[str, UnitResult] = {}

        async def execute(task: Task, attempt: int) -> AttemptOutcome:
            name = names[task.id]
            result = await self._build_unit(plan, name, where)
            results[name] = result
            log.unit_finished(result, task_id=task.id)
            if result.ok:
                return AttemptOutcome.success(unit=name, status=result.status.value)
            return AttemptOutcome.failure(
                f"build_{result.status.value}",
                retryable=False,
                unit=name,
                errors=[d.render() for d in result.errors[:5]],
            )

        dispatcher = TaskDispatcher(
            graph,
            execute,
            max_concurrency=self._config.max_concurrency or max(1, plan.max_parallel),
            # A build command that failed will fail again on the same inputs.
            # Retrying is the planner's decision, made against a fresh analysis.
            policy=RetryPolicy(
                strategy=BackoffStrategy.NONE, base_delay_s=0.0, max_delay_s=0.0
            ),
            label_limits=self._config.label_limits,
            run_id=run_id,
            event_sink=None,
        )

        log.build_started(plan, where)
        dispatch = await dispatcher.run()

        for task_id in dispatch.blocked + dispatch.abandoned:
            name = names[task_id]
            results.setdefault(
                name,
                UnitResult(
                    unit=name,
                    status=BuildStatus.BLOCKED,
                    fingerprint=plan.fingerprint_of(name),
                    reason="a unit it depends on did not build",
                ),
            )

        report = BuildReport(
            results=tuple(
                sorted(
                    (*results.values(), *self._cached_results(plan)),
                    key=lambda r: r.unit,
                )
            ),
            cancelled=dispatch.cancelled,
            duration_s=self._clock() - started,
        )
        log.build_finished(report)
        return report

    # -------------------------------------------------------------- one unit

    async def _build_unit(self, plan: BuildPlan, name: str, root: Path) -> UnitResult:
        """Run one unit's command and interpret what came back."""
        unit = plan.graph.get(name)
        fingerprint = plan.fingerprint_of(name)
        started = self._clock()

        try:
            process = await self._process.run(
                unit.command,
                cwd=root,
                timeout_s=unit.timeout_s,
                env=dict(unit.env) or None,
            )
        except Exception as exc:  # noqa: BLE001 - a runner crash is an outcome
            return UnitResult(
                unit=name,
                status=BuildStatus.ERRORED,
                fingerprint=fingerprint,
                command=unit.command,
                duration_s=self._clock() - started,
                reason=f"the build command could not be started: {exc}",
            )

        status, reason = classify(process)
        diagnostics = analyze_output(process.stdout, process.stderr, unit=name)
        artifacts = self._collect(root, unit, status)

        if status is BuildStatus.FAILED and not any(d.is_error for d in diagnostics):
            # The tool failed and said nothing a parser recognized. Reporting no
            # errors would read as a pass in every summary downstream.
            diagnostics = (
                *diagnostics,
                Diagnostic(
                    message=_tail(process.stderr or process.stdout)
                    or f"the build command exited {process.exit_code}",
                    severity=Severity.ERROR,
                    unit=name,
                ),
            )

        result = UnitResult(
            unit=name,
            status=status,
            fingerprint=fingerprint,
            diagnostics=diagnostics,
            artifacts=artifacts,
            duration_s=process.duration_s or (self._clock() - started),
            exit_code=process.exit_code,
            command=unit.command,
            output=_tail(f"{process.stdout}\n{process.stderr}", limit=4000),
            reason=reason,
        )
        self._remember(result)
        return result

    def _collect(
        self, root: Path, unit: BuildUnit, status: BuildStatus
    ) -> tuple[Artifact, ...]:
        """Digest a unit's outputs, if it produced any worth trusting."""
        if not self._config.collect_artifacts or status is not BuildStatus.SUCCEEDED:
            return ()
        return self._tracker.collect(root, unit)

    def _remember(self, result: UnitResult) -> None:
        """Record a successful build in the cache."""
        if (
            self._cache is None
            or not self._config.write_cache
            or result.status is not BuildStatus.SUCCEEDED
            or not result.fingerprint
        ):
            return
        self._cache.put(
            CacheEntry(
                key=result.fingerprint,
                unit=result.unit,
                status=result.status,
                artifacts=result.artifacts,
                diagnostics=result.diagnostics,
                command=result.command,
                duration_s=result.duration_s,
                output=result.output,
            )
        )

    @staticmethod
    def _cached_results(plan: BuildPlan) -> tuple[UnitResult, ...]:
        """Report the units the plan skipped, so the report covers the project."""
        return tuple(
            UnitResult(
                unit=name,
                status=BuildStatus.CACHED,
                fingerprint=plan.fingerprint_of(name),
                reason="already built from these inputs",
            )
            for name in plan.cached
        )


def _tail(text: str, *, limit: int = 400) -> str:
    """The last meaningful part of a blob of output."""
    stripped = text.strip()
    return stripped[-limit:] if len(stripped) > limit else stripped


class BuildGate:
    """Runs a build and reports it as a gate verdict.

    This is how a build reaches the workflow engine (M8): the engine already
    accepts any :class:`~orchestrator.test_runner.base.GateRunner`, so a step can
    be gated on the project still building without the workflow package
    importing anything from here.

    Each call re-analyzes the directory it is given. An attempt's worktree is a
    different tree every time, and a layout computed against a previous one
    would fingerprint files that are not there.
    """

    __slots__ = ("_analyzer", "_cache", "_dependencies", "_executor", "_manifest")

    def __init__(
        self,
        executor: BuildExecutor,
        *,
        manifest: Sequence[BuildUnit] | None = None,
        analyzer: ProjectAnalyzer | None = None,
        dependencies: DependencyAnalyzer | None = None,
        cache: BuildCache | None = None,
    ) -> None:
        """Create the gate.

        Args:
            executor: Runs the plan.
            manifest: Build units, when the project has been described. Omitted,
                the analyzer proposes them.
            analyzer: Scans the directory.
            dependencies: Builds the unit graph.
            cache: Consulted when planning, so an unchanged worktree is a fast
                gate rather than a full rebuild.
        """
        self._executor = executor
        self._manifest = tuple(manifest) if manifest is not None else None
        self._analyzer = analyzer or ProjectAnalyzer()
        self._dependencies = dependencies or DependencyAnalyzer()
        self._cache = cache if cache is not None else executor.cache

    async def run(self, cwd: Path, spec: SuiteSpec) -> Verdict:
        """Build ``cwd`` and report whether it is sound.

        Args:
            cwd: The directory to build — an attempt's worktree, in practice.
            spec: The gate's spec. Only its name and timeout are used; the
                commands come from the project's own build units.

        Returns:
            The verdict. Like the test runner, this does not raise for a failing
            or broken build; both are outcomes.
        """
        started = time.monotonic()
        try:
            layout = await self._analyzer.analyze(cwd, manifest=self._manifest)
            graph = await self._dependencies.analyze(layout)
        except Exception as exc:  # noqa: BLE001 - analysis failing is a broken gate
            return Verdict(
                outcome=Outcome.ERRORED,
                gate=spec.name,
                reason=f"the project could not be analyzed: {exc}",
                duration_s=time.monotonic() - started,
            )

        if not layout.units:
            return Verdict(
                outcome=Outcome.SKIPPED,
                gate=spec.name,
                reason="no build units are configured for this project",
                duration_s=time.monotonic() - started,
            )

        plan = BuildPlanner(root=cwd).plan(layout, graph, cache=self._cache)
        report = await asyncio.wait_for(
            self._executor.run(plan, root=cwd), timeout=spec.timeout_s
        )
        return self.to_verdict(report, gate=spec.name)

    @staticmethod
    def to_verdict(report: BuildReport, *, gate: str = "build") -> Verdict:
        """Translate a build report into a gate verdict."""
        cases = tuple(
            CaseResult(
                id=result.unit,
                status=CaseStatus.PASSED if result.ok else CaseStatus.FAILED,
                name=result.unit,
                message=result.summary(),
                detail=result.feedback(),
                duration_s=result.duration_s,
            )
            for result in report.results
        )
        counts = {
            "passed": sum(1 for r in report.results if r.ok),
            "failed": len(report.failed_units),
        }

        if report.cancelled:
            outcome = Outcome.SKIPPED
        elif report.harness_problems:
            outcome = Outcome.ERRORED
        elif report.ok:
            outcome = Outcome.PASSED
        else:
            outcome = Outcome.FAILED

        return Verdict(
            outcome=outcome,
            gate=gate,
            cases=cases,
            counts=counts,
            duration_s=report.duration_s,
            reason=report.summary(),
            output=report.feedback(),
        )


async def build_project(
    root: Path,
    *,
    manifest: Sequence[BuildUnit] | None = None,
    changed_paths: Iterable[str] = (),
    cache: BuildCache | None = None,
    process_runner: ProcessRunner | None = None,
    run_id: RunId | None = None,
    event_sink: Callable[[Event], None] | None = None,
) -> BuildReport:
    """Analyze, plan, and build in one call.

    The convenience path, for a caller that has a directory and a list of files
    an agent touched and wants to know whether the project still builds.
    """
    from orchestrator.builder.analysis import changed_units

    layout = await ProjectAnalyzer().analyze(root, manifest=manifest)
    graph: UnitGraph = await DependencyAnalyzer().analyze(layout)
    changed = changed_units(layout, changed_paths) if changed_paths else graph.names
    plan = BuildPlanner(root=root).plan(layout, graph, changed=changed, cache=cache)
    executor = BuildExecutor(process_runner=process_runner, cache=cache)
    return await executor.run(plan, root=root, run_id=run_id, event_sink=event_sink)

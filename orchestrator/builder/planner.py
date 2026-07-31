"""Incremental build planning, and replanning after a failure.

Planning answers *what must be rebuilt, and in what order*. The rule is short:

1. A unit whose sources changed must be rebuilt.
2. A unit that depends on a rebuilt unit must be rebuilt.
3. A unit whose fingerprint matches a verified cache entry need not be.

Rule 2 is why a dependency graph exists at all, and it is the rule that
incremental builds most often get wrong. Skipping it produces a build that
succeeds and a binary that is quietly inconsistent — the failure mode that makes
people stop trusting incremental mode and start deleting the output directory
before every build.

**A plan is a proposal.** It names its reasons per unit, so an operator can see
*why* something is being rebuilt rather than inferring it from a duration. When
the reason is "the cache said so", the answer was verified against the disk
first (see :mod:`orchestrator.builder.cache`).

The plan converts to a :class:`~orchestrator.task.graph.TaskGraph`, which is how
this package reaches the M4 scheduler: ordering, concurrency caps, and retries
already exist there and are not reimplemented here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from orchestrator.builder.analysis import UnitGraph
from orchestrator.builder.cache import BuildCache, fingerprints_for
from orchestrator.builder.model import (
    BuildReport,
    BuildStatus,
    BuilderError,
    ProjectLayout,
)
from orchestrator.core.events import Budget, TaskId
from orchestrator.task.graph import TaskGraph
from orchestrator.task.model import Task

__all__ = [
    "BuildPlan",
    "BuildPlanner",
    "BuildReason",
    "PlannedUnit",
]

#: Token budget handed to a build task. Builds spend no tokens; the axis exists
#: because :class:`~orchestrator.core.events.Budget` caps three at once, and a
#: build's real limit is the unit's own wall-clock timeout.
_NOMINAL_TOKENS = 1
_NOMINAL_TOOL_CALLS = 1


class BuildReason(StrEnum):
    """Why a unit is in the plan."""

    SOURCE_CHANGED = "source_changed"
    DEPENDENCY_REBUILT = "dependency_rebuilt"
    NOT_CACHED = "not_cached"
    ARTIFACT_MISSING = "artifact_missing"
    FORCED = "forced"
    PREVIOUS_FAILURE = "previous_failure"

    def describe(self) -> str:
        """A phrase an operator can read in a log line."""
        return {
            BuildReason.SOURCE_CHANGED: "its sources changed",
            BuildReason.DEPENDENCY_REBUILT: "a unit it depends on was rebuilt",
            BuildReason.NOT_CACHED: "it has not been built with these inputs",
            BuildReason.ARTIFACT_MISSING: "its previous output is gone or altered",
            BuildReason.FORCED: "a rebuild was requested",
            BuildReason.PREVIOUS_FAILURE: "the previous build of it failed",
        }[self]


@dataclass(frozen=True, slots=True)
class PlannedUnit:
    """One unit scheduled for a rebuild."""

    name: str
    reason: BuildReason
    fingerprint: str = ""
    incremental: bool = True

    def describe(self) -> str:
        """A one-line account, in the operator's terms."""
        return f"{self.name}: rebuilding because {self.reason.describe()}"


@dataclass(frozen=True, slots=True)
class BuildPlan:
    """What a build is about to do."""

    layout: ProjectLayout
    graph: UnitGraph
    units: tuple[PlannedUnit, ...] = ()
    cached: tuple[str, ...] = ()
    fingerprints: Mapping[str, str] = field(default_factory=dict)

    @property
    def names(self) -> tuple[str, ...]:
        """Units to rebuild, in dependency order."""
        order = {name: i for i, name in enumerate(self.graph.names)}
        return tuple(sorted((u.name for u in self.units), key=lambda n: order[n]))

    @property
    def is_empty(self) -> bool:
        """Whether there is nothing to do."""
        return not self.units

    @property
    def layers(self) -> tuple[tuple[str, ...], ...]:
        """Waves of units that can be built at the same time."""
        return self.graph.layers(self.names)

    @property
    def max_parallel(self) -> int:
        """The most units that could usefully run at once."""
        return max((len(layer) for layer in self.layers), default=0)

    def reason_for(self, name: str) -> BuildReason | None:
        """Why a unit is being rebuilt, or ``None`` if it is not."""
        for unit in self.units:
            if unit.name == name:
                return unit.reason
        return None

    def fingerprint_of(self, name: str) -> str:
        """The cache key computed for a unit."""
        return self.fingerprints.get(name, "")

    def summary(self) -> str:
        """A one-line account of the plan."""
        if self.is_empty:
            return "everything is up to date"
        return (
            f"{len(self.units)} unit(s) to rebuild in {len(self.layers)} wave(s), "
            f"{len(self.cached)} up to date"
        )

    def explain(self) -> tuple[str, ...]:
        """One line per unit, in dependency order."""
        by_name = {unit.name: unit for unit in self.units}
        return tuple(by_name[name].describe() for name in self.names)

    def to_task_graph(self) -> tuple[TaskGraph, dict[str, TaskId]]:
        """Render the plan as a task graph for the M4 scheduler.

        Dependencies on units *outside* the plan are dropped: they were left out
        precisely because they are already up to date, and keeping them would
        produce a graph with unbuildable nodes.

        Returns:
            The graph and the identifier minted for each unit name.
        """
        selected = set(self.names)
        ids = {name: TaskId.generate() for name in self.names}
        tasks = [
            Task(
                id=ids[name],
                title=name,
                prompt=self.graph.get(name).command,
                budget=Budget(
                    seconds=self.graph.get(name).timeout_s,
                    tokens=_NOMINAL_TOKENS,
                    tool_calls=_NOMINAL_TOOL_CALLS,
                ),
                depends_on=tuple(
                    ids[dependency]
                    for dependency in self.graph.dependencies(name)
                    if dependency in selected
                ),
                max_attempts=1,
                labels=self.graph.get(name).labels,
            )
            for name in self.names
        ]
        return TaskGraph(tasks), ids


class BuildPlanner:
    """Decides what to rebuild."""

    __slots__ = ("_root",)

    def __init__(self, *, root: Path | None = None) -> None:
        """Create the planner.

        Args:
            root: Where artifacts live, for verifying cache hits. Without it a
                cache entry is trusted on its word, which is appropriate for a
                pure planning preview and not for a real build.
        """
        self._root = root

    def plan(
        self,
        layout: ProjectLayout,
        graph: UnitGraph,
        *,
        changed: Iterable[str] = (),
        cache: BuildCache | None = None,
        force: Iterable[str] = (),
        full: bool = False,
    ) -> BuildPlan:
        """Work out which units need rebuilding.

        Args:
            layout: The project, supplying source digests.
            graph: The validated dependency graph.
            changed: Unit names whose sources are known to have changed. Pass
                the output of
                :func:`~orchestrator.builder.analysis.changed_units`.
            cache: Consulted for units nothing else has already condemned.
            force: Units to rebuild regardless.
            full: Rebuild everything, cache and all.

        Returns:
            The plan.

        Raises:
            BuilderError: If ``changed`` or ``force`` names a unit that is not
                in the graph — silently ignoring it would produce a plan that
                looks right and rebuilds nothing.
        """
        fingerprints = fingerprints_for(layout, graph.names)
        reasons: dict[str, BuildReason] = {}

        if full:
            for name in graph.names:
                reasons[name] = BuildReason.FORCED
            return self._assemble(layout, graph, reasons, fingerprints)

        for name in self._validated(graph, force, "force"):
            reasons[name] = BuildReason.FORCED
        for name in self._validated(graph, changed, "changed"):
            reasons.setdefault(name, BuildReason.SOURCE_CHANGED)

        for name in graph.names:
            if name in reasons:
                continue
            verdict = self._consult_cache(cache, layout, graph, name, fingerprints)
            if verdict is not None:
                reasons[name] = verdict

        # Rule 2, applied last so it sees every other reason first: anything
        # downstream of a rebuild is rebuilt too, however far away.
        for name in sorted(graph.descendants(reasons)):
            reasons.setdefault(name, BuildReason.DEPENDENCY_REBUILT)

        return self._assemble(layout, graph, reasons, fingerprints)

    def replan(
        self,
        plan: BuildPlan,
        report: BuildReport,
        *,
        cache: BuildCache | None = None,
    ) -> BuildPlan:
        """Plan the next build after a failed one (automatic rebuild planning).

        What succeeded stays succeeded: its fingerprint is unchanged and its
        artifacts were verified, so re-running it would only cost time. What
        failed is retried, together with everything downstream of it — those
        units never got a usable dependency and their own previous state, if
        any, was built against something older.

        A unit whose *build tool* broke is retried the same way. The distinction
        matters for what the operator is told, not for what is scheduled: in
        both cases the unit has no trustworthy output.

        Args:
            plan: The plan that was executed.
            report: What executing it produced.
            cache: Unused for scheduling; accepted so a caller can pass the same
                cache it planned with without special-casing.

        Returns:
            The next plan. Empty when nothing failed.
        """
        # Only units that actually ran can have failed. A blocked unit never
        # got the chance, and calling that a previous failure would tell an
        # operator its build is broken when nothing of it was ever executed.
        failed = {
            result.unit
            for result in report.results
            if result.status.ran and not result.ok
        }
        unfinished = {
            planned.name
            for planned in plan.units
            if (result := report.by_unit.get(planned.name)) is None
            or result.status is BuildStatus.BLOCKED
        }
        if not failed and not unfinished:
            return BuildPlan(
                layout=plan.layout,
                graph=plan.graph,
                units=(),
                cached=tuple(sorted(plan.names)),
                fingerprints=plan.fingerprints,
            )

        reasons: dict[str, BuildReason] = {
            name: BuildReason.PREVIOUS_FAILURE for name in sorted(failed)
        }
        for name in sorted(plan.graph.descendants(failed)):
            reasons.setdefault(name, BuildReason.DEPENDENCY_REBUILT)

        # A unit that had already succeeded and is not downstream of a failure
        # keeps its result; anything else in the original plan that never ran
        # still has to.
        for planned in plan.units:
            if planned.name in reasons:
                continue
            result = report.by_unit.get(planned.name)
            if result is None or result.status is BuildStatus.BLOCKED:
                reasons[planned.name] = planned.reason

        return self._assemble(plan.layout, plan.graph, reasons, plan.fingerprints)

    # ------------------------------------------------------------- internals

    @staticmethod
    def _validated(
        graph: UnitGraph, names: Iterable[str], what: str
    ) -> tuple[str, ...]:
        """Return ``names``, refusing any that is not in the graph."""
        listed = tuple(names)
        unknown = sorted(name for name in listed if name not in graph)
        if unknown:
            raise BuilderError(
                f"{what} names unit(s) that are not in this project: "
                f"{', '.join(unknown)}",
                detail={"unknown": unknown, "known": list(graph.names)},
            )
        return listed

    def _consult_cache(
        self,
        cache: BuildCache | None,
        layout: ProjectLayout,
        graph: UnitGraph,
        name: str,
        fingerprints: Mapping[str, str],
    ) -> BuildReason | None:
        """Decide whether the cache excuses a unit from being rebuilt."""
        if cache is None:
            return BuildReason.NOT_CACHED

        entry = cache.get(fingerprints[name])
        if entry is None or not entry.usable:
            return BuildReason.NOT_CACHED

        root = self._root if self._root is not None else layout.root
        if root is not None and entry.artifacts:
            valid, _stale = entry.verify(root)
            if not valid:
                # The entry is real but its outputs are not. Trusting it would
                # turn a missing file into a failure much later, somewhere else.
                return BuildReason.ARTIFACT_MISSING
        return None

    @staticmethod
    def _assemble(
        layout: ProjectLayout,
        graph: UnitGraph,
        reasons: Mapping[str, BuildReason],
        fingerprints: Mapping[str, str],
    ) -> BuildPlan:
        """Freeze a set of decisions into a plan."""
        units = tuple(
            PlannedUnit(
                name=name,
                reason=reasons[name],
                fingerprint=fingerprints.get(name, ""),
                incremental=graph.get(name).incremental,
            )
            for name in graph.names
            if name in reasons
        )
        cached = tuple(name for name in graph.names if name not in reasons)
        return BuildPlan(
            layout=layout,
            graph=graph,
            units=units,
            cached=cached,
            fingerprints=dict(fingerprints),
        )


def plan_for_changes(
    layout: ProjectLayout,
    graph: UnitGraph,
    changed_paths: Sequence[str],
    *,
    cache: BuildCache | None = None,
) -> BuildPlan:
    """Plan a build from a list of changed files.

    The convenience form of the common case: an agent edited some files, and the
    question is what that implies.
    """
    from orchestrator.builder.analysis import changed_units

    return BuildPlanner(root=layout.root).plan(
        layout, graph, changed=changed_units(layout, changed_paths), cache=cache
    )

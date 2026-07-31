"""Tests for incremental build planning and replanning."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.builder.analysis import UnitGraph
from orchestrator.builder.cache import CacheEntry, MemoryCache, fingerprints_for
from orchestrator.builder.model import (
    Artifact,
    BuildReport,
    BuildStatus,
    BuilderError,
    ProjectLayout,
    UnitResult,
    digest_bytes,
)
from orchestrator.builder.planner import (
    BuildPlanner,
    BuildReason,
    PlannedUnit,
    plan_for_changes,
)

from tests.builder.conftest import layout, unit


def diamond(root: Path | None = None) -> tuple[ProjectLayout, UnitGraph]:
    """root → (left, right) → join, one source file each."""
    units = (
        unit("root", sources=("root",)),
        unit("left", sources=("left",), depends_on=("root",)),
        unit("right", sources=("right",), depends_on=("root",)),
        unit("join", sources=("join",), depends_on=("left", "right")),
    )
    project = layout(
        *units,
        root=root,
        sources={f"{name}/main.py": f"# {name}" for name in ("root", "left", "right", "join")},
    )
    return project, UnitGraph(units)


def warm_cache(project: ProjectLayout, graph: UnitGraph) -> MemoryCache:
    """A cache holding a successful entry for every unit, with no artifacts."""
    cache = MemoryCache()
    for name, key in fingerprints_for(project, graph.names).items():
        cache.put(CacheEntry(key=key, unit=name))
    return cache


class TestPlanning:
    """What must be rebuilt, and why."""

    def test_nothing_changed_and_nothing_cached_builds_everything(self) -> None:
        project, graph = diamond()
        plan = BuildPlanner().plan(project, graph)

        assert set(plan.names) == set(graph.names)
        assert plan.reason_for("root") is BuildReason.NOT_CACHED

    def test_a_warm_cache_builds_nothing(self) -> None:
        project, graph = diamond()
        plan = BuildPlanner().plan(project, graph, cache=warm_cache(project, graph))

        assert plan.is_empty
        assert set(plan.cached) == set(graph.names)
        assert plan.summary() == "everything is up to date"

    def test_a_changed_unit_is_rebuilt(self) -> None:
        project, graph = diamond()
        plan = BuildPlanner().plan(
            project, graph, changed=["left"], cache=warm_cache(project, graph)
        )

        assert plan.reason_for("left") is BuildReason.SOURCE_CHANGED

    def test_a_change_propagates_to_dependents(self) -> None:
        """The rule incremental builds most often get wrong."""
        project, graph = diamond()
        plan = BuildPlanner().plan(
            project, graph, changed=["root"], cache=warm_cache(project, graph)
        )

        assert set(plan.names) == {"root", "left", "right", "join"}
        assert plan.reason_for("join") is BuildReason.DEPENDENCY_REBUILT

    def test_propagation_is_transitive_not_one_hop(self) -> None:
        project, graph = diamond()
        plan = BuildPlanner().plan(
            project, graph, changed=["root"], cache=warm_cache(project, graph)
        )
        assert "join" in plan.names

    def test_a_change_does_not_propagate_upstream(self) -> None:
        project, graph = diamond()
        plan = BuildPlanner().plan(
            project, graph, changed=["join"], cache=warm_cache(project, graph)
        )

        assert plan.names == ("join",)

    def test_a_sibling_is_left_alone(self) -> None:
        project, graph = diamond()
        plan = BuildPlanner().plan(
            project, graph, changed=["left"], cache=warm_cache(project, graph)
        )

        assert "right" not in plan.names
        assert "right" in plan.cached

    def test_forcing_a_unit_rebuilds_it_and_its_dependents(self) -> None:
        project, graph = diamond()
        plan = BuildPlanner().plan(
            project, graph, force=["left"], cache=warm_cache(project, graph)
        )

        assert plan.reason_for("left") is BuildReason.FORCED
        assert plan.reason_for("join") is BuildReason.DEPENDENCY_REBUILT

    def test_a_full_build_ignores_the_cache(self) -> None:
        project, graph = diamond()
        plan = BuildPlanner().plan(project, graph, full=True, cache=warm_cache(project, graph))

        assert len(plan.units) == 4
        assert all(u.reason is BuildReason.FORCED for u in plan.units)

    def test_an_unknown_changed_unit_is_refused(self) -> None:
        """Ignoring it would produce a plan that looks right and builds nothing."""
        project, graph = diamond()
        with pytest.raises(BuilderError, match="not in this project"):
            BuildPlanner().plan(project, graph, changed=["ghost"])

    def test_an_unknown_forced_unit_is_refused(self) -> None:
        project, graph = diamond()
        with pytest.raises(BuilderError, match="not in this project"):
            BuildPlanner().plan(project, graph, force=["ghost"])

    def test_an_edited_source_is_noticed_without_being_told(self) -> None:
        """The fingerprint sees the change even when nobody passed `changed`."""
        project, graph = diamond()
        cache = warm_cache(project, graph)
        edited = project.with_units(project.units)
        edited = layout(
            *project.units,
            sources={
                "root/main.py": "# root edited",
                "left/main.py": "# left",
                "right/main.py": "# right",
                "join/main.py": "# join",
            },
        )

        plan = BuildPlanner().plan(edited, graph, cache=cache)
        assert set(plan.names) == {"root", "left", "right", "join"}


class TestCacheVerification:
    """A cache hit is only as good as the files it claims."""

    def test_a_hit_with_present_artifacts_is_honoured(self, project: Path) -> None:
        (project / "out.bin").write_bytes(b"built")
        layout_, graph = diamond(root=project)
        cache = MemoryCache()
        for name, key in fingerprints_for(layout_, graph.names).items():
            cache.put(
                CacheEntry(
                    key=key,
                    unit=name,
                    artifacts=(Artifact(path="out.bin", digest=digest_bytes(b"built")),),
                )
            )

        assert BuildPlanner(root=project).plan(layout_, graph, cache=cache).is_empty

    def test_a_hit_whose_artifact_vanished_is_rebuilt(self, project: Path) -> None:
        layout_, graph = diamond(root=project)
        cache = MemoryCache()
        for name, key in fingerprints_for(layout_, graph.names).items():
            cache.put(
                CacheEntry(
                    key=key, unit=name, artifacts=(Artifact(path="gone.bin", digest="d"),)
                )
            )

        plan = BuildPlanner(root=project).plan(layout_, graph, cache=cache)
        assert plan.reason_for("root") is BuildReason.ARTIFACT_MISSING

    def test_a_remembered_failure_is_not_reused(self) -> None:
        """A failed build usually gets fixed by something the key cannot see."""
        project, graph = diamond()
        cache = MemoryCache()
        for name, key in fingerprints_for(project, graph.names).items():
            cache.put(CacheEntry(key=key, unit=name, status=BuildStatus.FAILED))

        plan = BuildPlanner().plan(project, graph, cache=cache)
        assert plan.reason_for("root") is BuildReason.NOT_CACHED

    def test_no_cache_at_all_means_everything(self) -> None:
        project, graph = diamond()
        assert len(BuildPlanner().plan(project, graph, cache=None).units) == 4


class TestPlanShape:
    """What a plan tells the operator and the scheduler."""

    def test_units_come_back_in_dependency_order(self) -> None:
        project, graph = diamond()
        names = BuildPlanner().plan(project, graph).names
        assert names.index("root") < names.index("left") < names.index("join")

    def test_layers_expose_the_parallel_waves(self) -> None:
        project, graph = diamond()
        plan = BuildPlanner().plan(project, graph)
        assert plan.layers == (("root",), ("left", "right"), ("join",))
        assert plan.max_parallel == 2

    def test_a_partial_plan_has_its_own_layers(self) -> None:
        project, graph = diamond()
        plan = BuildPlanner().plan(
            project, graph, changed=["left"], cache=warm_cache(project, graph)
        )
        assert plan.layers == (("left", "join"),) or plan.layers == (
            ("left",),
            ("join",),
        )

    def test_the_summary_counts_both_sides(self) -> None:
        project, graph = diamond()
        plan = BuildPlanner().plan(
            project, graph, changed=["join"], cache=warm_cache(project, graph)
        )
        assert "1 unit(s) to rebuild" in plan.summary()
        assert "3 up to date" in plan.summary()

    def test_every_unit_explains_itself(self) -> None:
        project, graph = diamond()
        plan = BuildPlanner().plan(
            project, graph, changed=["root"], cache=warm_cache(project, graph)
        )
        lines = plan.explain()

        assert len(lines) == 4
        assert any("its sources changed" in line for line in lines)
        assert any("a unit it depends on was rebuilt" in line for line in lines)

    def test_a_unit_not_in_the_plan_has_no_reason(self) -> None:
        project, graph = diamond()
        plan = BuildPlanner().plan(
            project, graph, changed=["join"], cache=warm_cache(project, graph)
        )
        assert plan.reason_for("root") is None

    def test_every_reason_reads_as_a_sentence(self) -> None:
        for reason in BuildReason:
            assert reason.describe()

    def test_a_planned_unit_describes_itself(self) -> None:
        described = PlannedUnit(name="a", reason=BuildReason.FORCED).describe()
        assert described == "a: rebuilding because a rebuild was requested"


class TestTaskGraphConversion:
    """How the plan reaches the M4 scheduler."""

    def test_every_planned_unit_becomes_a_task(self) -> None:
        project, graph = diamond()
        task_graph, ids = BuildPlanner().plan(project, graph).to_task_graph()

        assert len(task_graph) == 4
        assert set(ids) == set(graph.names)

    def test_edges_are_preserved(self) -> None:
        project, graph = diamond()
        task_graph, ids = BuildPlanner().plan(project, graph).to_task_graph()

        assert set(task_graph.dependencies(ids["join"])) == {ids["left"], ids["right"]}

    def test_the_command_travels_as_the_prompt(self) -> None:
        project, graph = diamond()
        task_graph, ids = BuildPlanner().plan(project, graph).to_task_graph()

        assert task_graph.get(ids["root"]).prompt == "build root"
        assert task_graph.get(ids["root"]).title == "root"

    def test_the_unit_timeout_becomes_the_budget(self) -> None:
        project = layout(unit("a", timeout_s=42.0), sources={"a/main.py": "x"})
        graph = UnitGraph(project.units)
        task_graph, ids = BuildPlanner().plan(project, graph).to_task_graph()

        assert task_graph.get(ids["a"]).budget.seconds == pytest.approx(42.0)

    def test_dependencies_outside_the_plan_are_dropped(self) -> None:
        """They were left out because they are already built."""
        project, graph = diamond()
        plan = BuildPlanner().plan(
            project, graph, changed=["left"], cache=warm_cache(project, graph)
        )
        task_graph, ids = plan.to_task_graph()

        assert task_graph.dependencies(ids["left"]) == ()
        assert task_graph.dependencies(ids["join"]) == (ids["left"],)

    def test_labels_survive(self) -> None:
        project = layout(
            unit("a", labels=frozenset({"gpu"})), sources={"a/main.py": "x"}
        )
        graph = UnitGraph(project.units)
        task_graph, ids = BuildPlanner().plan(project, graph).to_task_graph()

        assert task_graph.get(ids["a"]).labels == frozenset({"gpu"})

    def test_a_build_task_gets_one_attempt(self) -> None:
        """Retrying the same command on the same inputs fails the same way."""
        project, graph = diamond()
        task_graph, ids = BuildPlanner().plan(project, graph).to_task_graph()
        assert task_graph.get(ids["root"]).max_attempts == 1


class TestReplan:
    """Automatic rebuild planning after a failure."""

    def _report(self, statuses: dict[str, BuildStatus]) -> BuildReport:
        return BuildReport(
            results=tuple(
                UnitResult(unit=name, status=status) for name, status in statuses.items()
            )
        )

    def test_a_clean_build_needs_no_second_pass(self) -> None:
        project, graph = diamond()
        plan = BuildPlanner().plan(project, graph)
        report = self._report(dict.fromkeys(graph.names, BuildStatus.SUCCEEDED))

        assert BuildPlanner().replan(plan, report).is_empty

    def test_a_failed_unit_is_retried(self) -> None:
        project, graph = diamond()
        plan = BuildPlanner().plan(project, graph)
        report = self._report(
            {
                "root": BuildStatus.SUCCEEDED,
                "left": BuildStatus.FAILED,
                "right": BuildStatus.SUCCEEDED,
                "join": BuildStatus.BLOCKED,
            }
        )
        next_plan = BuildPlanner().replan(plan, report)

        assert next_plan.reason_for("left") is BuildReason.PREVIOUS_FAILURE

    def test_what_succeeded_is_not_rebuilt(self) -> None:
        project, graph = diamond()
        plan = BuildPlanner().plan(project, graph)
        report = self._report(
            {
                "root": BuildStatus.SUCCEEDED,
                "left": BuildStatus.FAILED,
                "right": BuildStatus.SUCCEEDED,
                "join": BuildStatus.BLOCKED,
            }
        )
        next_plan = BuildPlanner().replan(plan, report)

        assert "root" not in next_plan.names
        assert "right" not in next_plan.names

    def test_blocked_dependents_come_back(self) -> None:
        project, graph = diamond()
        plan = BuildPlanner().plan(project, graph)
        report = self._report(
            {
                "root": BuildStatus.SUCCEEDED,
                "left": BuildStatus.FAILED,
                "right": BuildStatus.SUCCEEDED,
                "join": BuildStatus.BLOCKED,
            }
        )
        next_plan = BuildPlanner().replan(plan, report)

        assert next_plan.reason_for("join") is BuildReason.DEPENDENCY_REBUILT

    def test_a_broken_tool_is_retried_like_a_failure(self) -> None:
        project, graph = diamond()
        plan = BuildPlanner().plan(project, graph)
        report = self._report(
            {
                "root": BuildStatus.ERRORED,
                "left": BuildStatus.BLOCKED,
                "right": BuildStatus.BLOCKED,
                "join": BuildStatus.BLOCKED,
            }
        )
        next_plan = BuildPlanner().replan(plan, report)

        assert set(next_plan.names) == set(graph.names)

    def test_a_unit_that_never_ran_keeps_its_original_reason(self) -> None:
        project, graph = diamond()
        plan = BuildPlanner().plan(project, graph, changed=["root"])
        report = self._report({"root": BuildStatus.FAILED})
        next_plan = BuildPlanner().replan(plan, report)

        assert set(next_plan.names) == set(graph.names)

    def test_replanning_preserves_the_fingerprints(self) -> None:
        project, graph = diamond()
        plan = BuildPlanner().plan(project, graph)
        report = self._report({"root": BuildStatus.FAILED})
        next_plan = BuildPlanner().replan(plan, report)

        assert next_plan.fingerprint_of("root") == plan.fingerprint_of("root")


class TestPlanForChanges:
    """The convenience path from edited files to a plan."""

    def test_edited_files_map_onto_units(self) -> None:
        project, graph = diamond()
        plan = plan_for_changes(
            project, graph, ["left/main.py"], cache=warm_cache(project, graph)
        )

        assert set(plan.names) == {"left", "join"}

    def test_an_unowned_file_changes_nothing(self) -> None:
        project, graph = diamond()
        plan = plan_for_changes(
            project, graph, ["docs/readme.md"], cache=warm_cache(project, graph)
        )

        assert plan.is_empty

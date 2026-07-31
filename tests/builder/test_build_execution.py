"""Tests for build execution, failure analysis, event logging, and the gate."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from orchestrator.builder.analysis import UnitGraph
from orchestrator.builder.cache import CacheEntry, MemoryCache, fingerprints_for
from orchestrator.builder.model import (
    BuildStatus,
    ProjectLayout,
    Severity,
)
from orchestrator.builder.planner import BuildPlanner
from orchestrator.builder.runner import (
    BuildEventLog,
    BuildExecutor,
    BuildGate,
    ExecutorConfig,
    analyze_output,
    build_project,
    classify,
)
from orchestrator.core.events import Event, EventType, RunId
from orchestrator.core.run_store import RunStore
from orchestrator.core.storage import Database
from orchestrator.test_runner.base import Outcome, SuiteSpec
from orchestrator.test_runner.execution import ProcessResult, ScriptedRunner

from tests.builder.conftest import (
    GCC_OUTPUT,
    MISSING_TOOL_OUTPUT,
    PYTHON_PROJECT,
    PYTHON_TRACEBACK,
    RUST_OUTPUT,
    TSC_OUTPUT,
    FakeClock,
    bad,
    layout,
    ok,
    run,
    unit,
    write_tree,
)


def chain(root: Path | None = None) -> tuple[ProjectLayout, UnitGraph]:
    """core → app, one source file each."""
    units = (
        unit("core", command="build core", sources=("core",)),
        unit("app", command="build app", sources=("app",), depends_on=("core",)),
    )
    project = layout(
        *units, root=root, sources={"core/a.py": "core", "app/b.py": "app"}
    )
    return project, UnitGraph(units)


class TestAnalyzeOutput:
    """Build output becomes locations, not a wall of text (FR-4.3)."""

    def test_gcc_style_errors_and_warnings(self) -> None:
        found = analyze_output(GCC_OUTPUT)
        errors = [d for d in found if d.is_error]

        assert errors[0].file == "src/main.c"
        assert errors[0].line == 12
        assert errors[0].column == 5
        assert "undeclared" in errors[0].message
        assert any(d.severity is Severity.WARNING for d in found)

    def test_msvc_and_typescript_style(self) -> None:
        found = analyze_output(TSC_OUTPUT)

        assert found[0].file == "src/app.ts"
        assert found[0].line == 30
        assert found[0].column == 12
        assert found[0].code == "TS2345"

    def test_rust_style_pairs_a_head_with_its_location(self) -> None:
        found = analyze_output(RUST_OUTPUT)

        assert found[0].code == "E0308"
        assert found[0].file == "src/lib.rs"
        assert found[0].line == 42
        assert "mismatched types" in found[0].message

    def test_a_rust_error_without_a_location_is_still_reported(self) -> None:
        found = analyze_output("error[E0433]: failed to resolve\n")
        assert found[0].code == "E0433"
        assert found[0].file == ""

    def test_a_python_traceback_reports_the_last_frame(self) -> None:
        found = analyze_output(PYTHON_TRACEBACK)

        assert found[0].code == "ImportError"
        assert found[0].file == "build.py"
        assert found[0].line == 8

    def test_stderr_is_read_too(self) -> None:
        assert analyze_output("", GCC_OUTPUT)

    def test_duplicates_are_collapsed(self) -> None:
        doubled = GCC_OUTPUT + GCC_OUTPUT
        assert len(analyze_output(doubled)) == len(analyze_output(GCC_OUTPUT))

    def test_a_flood_is_truncated(self) -> None:
        flood = "\n".join(f"a.c:{i}:1: error: boom {i}" for i in range(500))
        assert len(analyze_output(flood, limit=10)) == 10

    def test_clean_output_yields_nothing(self) -> None:
        assert analyze_output("Compiling...\nDone in 4s\n") == ()

    def test_the_unit_is_stamped_on_each_diagnostic(self) -> None:
        found = analyze_output(GCC_OUTPUT, unit="core")
        assert all(d.unit == "core" for d in found)


class TestClassify:
    """A broken tool is not a broken program (FR-4.4)."""

    def test_a_clean_exit_succeeded(self) -> None:
        assert classify(ProcessResult(exit_code=0))[0] is BuildStatus.SUCCEEDED

    def test_a_nonzero_exit_failed(self) -> None:
        assert classify(ProcessResult(exit_code=1, stdout=GCC_OUTPUT))[0] is BuildStatus.FAILED

    def test_a_timeout_is_not_a_compilation_failure(self) -> None:
        status, reason = classify(ProcessResult(exit_code=-1, timed_out=True))
        assert status is BuildStatus.TIMED_OUT
        assert "timeout" in reason

    @pytest.mark.parametrize("code", [126, 127])
    def test_an_unrunnable_command_errored(self, code: int) -> None:
        status, reason = classify(ProcessResult(exit_code=code))
        assert status is BuildStatus.ERRORED
        assert "could not be executed" in reason

    def test_a_missing_tool_is_recognized_by_its_message(self) -> None:
        """Windows and Unix disagree about exit codes but not about the words."""
        status, _ = classify(ProcessResult(exit_code=1, stderr=MISSING_TOOL_OUTPUT))
        assert status is BuildStatus.ERRORED

    def test_a_signal_death_errored(self) -> None:
        assert classify(ProcessResult(exit_code=-9))[0] is BuildStatus.ERRORED


class TestBuildExecutor:
    """Running a plan."""

    def test_a_clean_build_succeeds(self) -> None:
        project, graph = chain()
        plan = BuildPlanner().plan(project, graph)
        executor = BuildExecutor(process_runner=ScriptedRunner([ok(), ok()]))

        report = run(executor.run(plan))

        assert report.ok
        assert report.rebuilt_units == ("app", "core")

    def test_units_build_in_dependency_order(self) -> None:
        project, graph = chain()
        runner = ScriptedRunner([ok(), ok()])
        report = run(BuildExecutor(process_runner=runner).run(BuildPlanner().plan(project, graph)))

        assert report.ok
        assert [r.command for r in runner.runs] == ["build core", "build app"]

    def test_a_failing_unit_is_reported_with_its_diagnostics(self) -> None:
        project, graph = chain()
        runner = ScriptedRunner([bad(GCC_OUTPUT)])
        report = run(BuildExecutor(process_runner=runner).run(BuildPlanner().plan(project, graph)))

        core = report.by_unit["core"]
        assert core.status is BuildStatus.FAILED
        assert core.errors[0].file == "src/main.c"

    def test_a_dependent_of_a_failure_is_blocked_not_failed(self) -> None:
        project, graph = chain()
        runner = ScriptedRunner([bad(GCC_OUTPUT)])
        report = run(BuildExecutor(process_runner=runner).run(BuildPlanner().plan(project, graph)))

        assert report.by_unit["app"].status is BuildStatus.BLOCKED
        assert len(runner.runs) == 1

    def test_a_silent_failure_still_reports_an_error(self) -> None:
        """Otherwise a failed build reads as green in every summary downstream."""
        project, graph = chain()
        runner = ScriptedRunner([bad("", exit_code=2)])
        report = run(BuildExecutor(process_runner=runner).run(BuildPlanner().plan(project, graph)))

        assert report.by_unit["core"].errors

    def test_a_broken_tool_is_reported_as_errored(self) -> None:
        project, graph = chain()
        runner = ScriptedRunner([ProcessResult(exit_code=127)])
        report = run(BuildExecutor(process_runner=runner).run(BuildPlanner().plan(project, graph)))

        assert report.harness_problems == ("core",)

    def test_a_crashing_process_runner_is_an_outcome(self) -> None:
        class Exploding:
            async def run(self, command: str, **kwargs: object) -> ProcessResult:
                raise OSError("no such executable")

        project, graph = chain()
        report = run(BuildExecutor(process_runner=Exploding()).run(BuildPlanner().plan(project, graph)))  # type: ignore[arg-type]

        assert report.by_unit["core"].status is BuildStatus.ERRORED
        assert "could not be started" in report.by_unit["core"].reason

    def test_an_empty_plan_does_nothing(self) -> None:
        project, graph = chain()
        cache = MemoryCache()
        for name, key in fingerprints_for(project, graph.names).items():
            cache.put(CacheEntry(key=key, unit=name))
        plan = BuildPlanner().plan(project, graph, cache=cache)
        runner = ScriptedRunner([])

        report = run(BuildExecutor(process_runner=runner).run(plan))

        assert plan.is_empty
        assert runner.runs == []
        assert report.ok
        assert report.cached_units == ("app", "core")

    def test_units_left_out_of_the_plan_are_reported_as_cached(self) -> None:
        """A report that only covers the rebuilt half is a misleading report."""
        project, graph = chain()
        cache = MemoryCache()
        keys = fingerprints_for(project, graph.names)
        cache.put(CacheEntry(key=keys["core"], unit="core"))
        plan = BuildPlanner().plan(project, graph, cache=cache)

        report = run(BuildExecutor(process_runner=ScriptedRunner([ok()])).run(plan))

        assert report.cached_units == ("core",)
        assert report.rebuilt_units == ("app",)

    def test_independent_units_can_run_together(self) -> None:
        project = layout(
            unit("a"), unit("b"), unit("c"), sources={f"{n}/main.py": n for n in "abc"}
        )
        graph = UnitGraph(project.units)
        plan = BuildPlanner().plan(project, graph)

        assert plan.max_parallel == 3
        report = run(
            BuildExecutor(process_runner=ScriptedRunner([ok(), ok(), ok()])).run(plan)
        )
        assert report.ok

    def test_the_concurrency_cap_is_configurable(self) -> None:
        project = layout(unit("a"), unit("b"), sources={"a/main.py": "a", "b/main.py": "b"})
        graph = UnitGraph(project.units)
        executor = BuildExecutor(
            process_runner=ScriptedRunner([ok(), ok()]),
            config=ExecutorConfig(max_concurrency=1),
        )
        assert run(executor.run(BuildPlanner().plan(project, graph))).ok

    def test_the_command_environment_is_passed_through(self) -> None:
        project = layout(
            unit("a", env={"MODE": "release"}), sources={"a/main.py": "a"}
        )
        graph = UnitGraph(project.units)
        runner = ScriptedRunner([ok()])
        run(BuildExecutor(process_runner=runner).run(BuildPlanner().plan(project, graph)))

        assert runner.runs[0].env == {"MODE": "release"}

    def test_the_unit_timeout_bounds_the_command(self) -> None:
        project = layout(unit("a", timeout_s=12.0), sources={"a/main.py": "a"})
        graph = UnitGraph(project.units)
        runner = ScriptedRunner([ok()])
        run(BuildExecutor(process_runner=runner).run(BuildPlanner().plan(project, graph)))

        assert runner.runs[0].timeout_s == pytest.approx(12.0)

    def test_the_build_runs_in_the_given_root(self, project: Path) -> None:
        layout_, graph = chain()
        runner = ScriptedRunner([ok(), ok()])
        run(BuildExecutor(process_runner=runner).run(BuildPlanner().plan(layout_, graph), root=project))

        assert runner.runs[0].cwd == project

    def test_the_report_is_timed(self) -> None:
        project, graph = chain()
        executor = BuildExecutor(
            process_runner=ScriptedRunner([ok(), ok()]), clock=FakeClock(step=1.0)
        )
        assert run(executor.run(BuildPlanner().plan(project, graph))).duration_s > 0


class TestArtifactsAndCache:
    """Nothing is cached until its outputs have been seen."""

    def _project(self, root: Path) -> tuple[ProjectLayout, UnitGraph]:
        units = (unit("a", sources=("a",), artifacts=("dist",)),)
        project = layout(*units, root=root, sources={"a/main.py": "a"})
        return project, UnitGraph(units)

    def test_artifacts_are_collected_after_a_success(self, project: Path) -> None:
        layout_, graph = self._project(project)
        (project / "dist").mkdir()
        (project / "dist/out.js").write_bytes(b"built")

        report = run(
            BuildExecutor(process_runner=ScriptedRunner([ok()])).run(
                BuildPlanner().plan(layout_, graph), root=project
            )
        )

        assert [a.path for a in report.artifacts] == ["dist/out.js"]

    def test_no_artifacts_are_claimed_for_a_failure(self, project: Path) -> None:
        layout_, graph = self._project(project)
        (project / "dist").mkdir()
        (project / "dist/stale.js").write_bytes(b"old")

        report = run(
            BuildExecutor(process_runner=ScriptedRunner([bad(GCC_OUTPUT)])).run(
                BuildPlanner().plan(layout_, graph), root=project
            )
        )

        assert report.artifacts == ()

    def test_a_success_is_remembered(self, project: Path) -> None:
        layout_, graph = self._project(project)
        cache = MemoryCache()
        run(
            BuildExecutor(process_runner=ScriptedRunner([ok()]), cache=cache).run(
                BuildPlanner().plan(layout_, graph), root=project
            )
        )

        key = fingerprints_for(layout_, graph.names)["a"]
        assert cache.get(key) is not None

    def test_a_failure_is_not_remembered(self, project: Path) -> None:
        layout_, graph = self._project(project)
        cache = MemoryCache()
        run(
            BuildExecutor(process_runner=ScriptedRunner([bad(GCC_OUTPUT)]), cache=cache).run(
                BuildPlanner().plan(layout_, graph), root=project
            )
        )

        assert len(cache) == 0

    def test_the_second_build_of_unchanged_sources_does_nothing(self, project: Path) -> None:
        """The whole point of the cache, end to end."""
        layout_, graph = self._project(project)
        cache = MemoryCache()
        runner = ScriptedRunner([ok(), ok()])
        executor = BuildExecutor(process_runner=runner, cache=cache)

        run(executor.run(BuildPlanner(root=project).plan(layout_, graph, cache=cache), root=project))
        second = BuildPlanner(root=project).plan(layout_, graph, cache=cache)

        assert second.is_empty
        assert len(runner.runs) == 1

    def test_an_edit_invalidates_the_cache(self, project: Path) -> None:
        layout_, graph = self._project(project)
        cache = MemoryCache()
        executor = BuildExecutor(process_runner=ScriptedRunner([ok()]), cache=cache)
        run(executor.run(BuildPlanner(root=project).plan(layout_, graph, cache=cache), root=project))

        edited = layout(*layout_.units, root=project, sources={"a/main.py": "a v2"})
        assert not BuildPlanner(root=project).plan(edited, graph, cache=cache).is_empty

    def test_a_deleted_artifact_invalidates_the_cache(self, project: Path) -> None:
        layout_, graph = self._project(project)
        (project / "dist").mkdir()
        (project / "dist/out.js").write_bytes(b"built")
        cache = MemoryCache()
        executor = BuildExecutor(process_runner=ScriptedRunner([ok()]), cache=cache)
        run(executor.run(BuildPlanner(root=project).plan(layout_, graph, cache=cache), root=project))

        (project / "dist/out.js").unlink()

        assert not BuildPlanner(root=project).plan(layout_, graph, cache=cache).is_empty

    def test_caching_can_be_turned_off(self, project: Path) -> None:
        layout_, graph = self._project(project)
        cache = MemoryCache()
        executor = BuildExecutor(
            process_runner=ScriptedRunner([ok()]),
            cache=cache,
            config=ExecutorConfig(write_cache=False),
        )
        run(executor.run(BuildPlanner().plan(layout_, graph), root=project))

        assert len(cache) == 0

    def test_a_persisted_cache_survives_a_restart(self, project: Path, db: Database) -> None:
        from orchestrator.builder.cache import SqliteCache

        layout_, graph = self._project(project)
        cache = SqliteCache(db)
        executor = BuildExecutor(process_runner=ScriptedRunner([ok()]), cache=cache)
        run(executor.run(BuildPlanner(root=project).plan(layout_, graph, cache=cache), root=project))

        fresh = SqliteCache(db)
        assert BuildPlanner(root=project).plan(layout_, graph, cache=fresh).is_empty


class TestBuildEventLog:
    """Build activity reaches the log, and a broken log does not stop a build."""

    def test_events_reach_the_sink(self) -> None:
        captured: list[Event] = []
        log = BuildEventLog(RunId.generate(), captured.append)

        assert log.emit(EventType.BUILD_STARTED, units=["a"])
        assert captured[0].type is EventType.BUILD_STARTED
        assert log.emitted == 1

    def test_nothing_is_emitted_without_a_run(self) -> None:
        """An event with no run cannot be replayed into anything."""
        captured: list[Event] = []
        log = BuildEventLog(None, captured.append)

        assert not log.emit(EventType.BUILD_STARTED)
        assert captured == []

    def test_a_raising_sink_does_not_break_the_build(self) -> None:
        def broken(event: Event) -> None:
            raise RuntimeError("down")

        log = BuildEventLog(RunId.generate(), broken)
        assert not log.emit(EventType.BUILD_STARTED)
        assert log.failures == 1

    def test_a_whole_build_is_logged(self) -> None:
        captured: list[Event] = []
        project, graph = chain()
        run(
            BuildExecutor(process_runner=ScriptedRunner([ok(), ok()])).run(
                BuildPlanner().plan(project, graph),
                run_id=RunId.generate(),
                event_sink=captured.append,
            )
        )
        kinds = [event.type for event in captured]

        assert kinds[0] is EventType.BUILD_STARTED
        assert kinds[-1] is EventType.BUILD_FINISHED
        assert kinds.count(EventType.BUILD_UNIT_FINISHED) == 2

    def test_the_started_event_says_what_will_be_built(self) -> None:
        captured: list[Event] = []
        project, graph = chain()
        run(
            BuildExecutor(process_runner=ScriptedRunner([ok(), ok()])).run(
                BuildPlanner().plan(project, graph),
                run_id=RunId.generate(),
                event_sink=captured.append,
            )
        )

        payload = captured[0].payload
        assert sorted(payload["units"]) == ["app", "core"]
        assert payload["reasons"]["core"] == "not_cached"

    def test_the_unit_event_carries_the_outcome(self) -> None:
        captured: list[Event] = []
        project, graph = chain()
        run(
            BuildExecutor(process_runner=ScriptedRunner([bad(GCC_OUTPUT)])).run(
                BuildPlanner().plan(project, graph),
                run_id=RunId.generate(),
                event_sink=captured.append,
            )
        )
        unit_events = [e for e in captured if e.type is EventType.BUILD_UNIT_FINISHED]

        assert unit_events[0].payload["unit"] == "core"
        assert unit_events[0].payload["status"] == "failed"
        assert unit_events[0].payload["errors"] >= 1

    def test_the_finished_event_reports_the_outcome(self) -> None:
        captured: list[Event] = []
        project, graph = chain()
        run(
            BuildExecutor(process_runner=ScriptedRunner([ok(), ok()])).run(
                BuildPlanner().plan(project, graph),
                run_id=RunId.generate(),
                event_sink=captured.append,
            )
        )

        assert captured[-1].payload["outcome"] == "succeeded"
        assert sorted(captured[-1].payload["rebuilt"]) == ["app", "core"]

    def test_build_events_persist_in_the_run_store(self, db: Database) -> None:
        """Integration with M2: the build is part of the run's record."""
        store = RunStore(db)
        run_id = RunId.generate()
        store.record(
            Event.new(
                EventType.RUN_CREATED,
                run_id=run_id,
                payload={"goal": "g", "repo_path": "/r"},
            )
        )
        project, graph = chain()
        run(
            BuildExecutor(process_runner=ScriptedRunner([ok(), ok()])).run(
                BuildPlanner().plan(project, graph),
                run_id=run_id,
                event_sink=store.record,
            )
        )

        kinds = [event.type for event in store.events.read_run(run_id)]
        assert EventType.BUILD_STARTED in kinds
        assert EventType.BUILD_FINISHED in kinds

    def test_build_events_replay_without_disturbing_the_run(self, db: Database) -> None:
        """An unrecognized-by-the-projection event must not corrupt a replay."""
        store = RunStore(db)
        run_id = RunId.generate()
        store.record(
            Event.new(
                EventType.RUN_CREATED,
                run_id=run_id,
                payload={"goal": "g", "repo_path": "/r"},
            )
        )
        project, graph = chain()
        run(
            BuildExecutor(process_runner=ScriptedRunner([ok(), ok()])).run(
                BuildPlanner().plan(project, graph),
                run_id=run_id,
                event_sink=store.record,
            )
        )

        state = store.replay(run_id)
        assert state.goal == "g"
        assert state.tasks == {}
        assert store.verify(run_id)[0]


class TestBuildGate:
    """How a build reaches the workflow engine (M8)."""

    def test_a_clean_build_passes(self, project: Path) -> None:
        write_tree(project, PYTHON_PROJECT)
        gate = BuildGate(BuildExecutor(process_runner=ScriptedRunner([ok(), ok()])))

        verdict = run(gate.run(project, SuiteSpec(name="build")))

        assert verdict.outcome is Outcome.PASSED
        assert verdict.gate == "build"

    def test_a_failing_build_fails_the_gate(self, project: Path) -> None:
        write_tree(project, PYTHON_PROJECT)
        gate = BuildGate(
            BuildExecutor(process_runner=ScriptedRunner([bad(GCC_OUTPUT)], default=ok()))
        )

        verdict = run(gate.run(project, SuiteSpec(name="build")))

        assert verdict.outcome is Outcome.FAILED
        assert verdict.blocks

    def test_the_failing_units_are_named(self, project: Path) -> None:
        """FR-4.3: 'the build failed' alone is not actionable."""
        write_tree(project, PYTHON_PROJECT)
        gate = BuildGate(
            BuildExecutor(process_runner=ScriptedRunner([bad(GCC_OUTPUT)], default=ok()))
        )

        verdict = run(gate.run(project, SuiteSpec(name="build")))
        assert verdict.failed_names
        assert "src/main.c" in verdict.output

    def test_a_broken_tool_errors_the_gate(self, project: Path) -> None:
        """FR-4.4: a missing compiler is not a failing program."""
        write_tree(project, PYTHON_PROJECT)
        gate = BuildGate(
            BuildExecutor(
                process_runner=ScriptedRunner([ProcessResult(exit_code=127)], default=ok())
            )
        )

        verdict = run(gate.run(project, SuiteSpec(name="build")))
        assert verdict.outcome is Outcome.ERRORED

    def test_a_project_with_no_units_is_skipped_not_passed(self, project: Path) -> None:
        write_tree(project, {"notes.txt": "nothing to build"})
        gate = BuildGate(BuildExecutor(process_runner=ScriptedRunner([])))

        verdict = run(gate.run(project, SuiteSpec(name="build")))

        assert verdict.outcome is Outcome.SKIPPED
        assert verdict.blocks

    def test_an_unanalyzable_directory_errors(self, tmp_dir: Path) -> None:
        gate = BuildGate(BuildExecutor(process_runner=ScriptedRunner([])))
        verdict = run(gate.run(tmp_dir / "absent", SuiteSpec(name="build")))

        assert verdict.outcome is Outcome.ERRORED
        assert "could not be analyzed" in verdict.reason

    def test_a_manifest_is_honoured(self, project: Path) -> None:
        write_tree(project, PYTHON_PROJECT)
        declared = (unit("everything", command="make", sources=("app", "core")),)
        runner = ScriptedRunner([ok()])
        gate = BuildGate(BuildExecutor(process_runner=runner), manifest=declared)

        run(gate.run(project, SuiteSpec(name="build")))
        assert [r.command for r in runner.runs] == ["make"]

    def test_the_gate_is_usable_by_the_workflow_executor(self, project: Path) -> None:
        """It satisfies GateRunner, which is all ExecutionServices asks for."""
        from orchestrator.workflow.executor import ExecutionServices

        write_tree(project, PYTHON_PROJECT)
        gate = BuildGate(BuildExecutor(process_runner=ScriptedRunner([ok(), ok()])))
        services = ExecutionServices(fallback_root=project, gates=gate)

        assert services.gates is gate

    def test_cases_carry_per_unit_detail(self, project: Path) -> None:
        write_tree(project, PYTHON_PROJECT)
        gate = BuildGate(BuildExecutor(process_runner=ScriptedRunner([ok(), ok()])))
        verdict = run(gate.run(project, SuiteSpec(name="build")))

        assert {case.id for case in verdict.cases} == {"app", "core"}

    def test_a_second_gate_run_uses_the_cache(self, project: Path) -> None:
        write_tree(project, PYTHON_PROJECT)
        cache = MemoryCache()
        runner = ScriptedRunner([], default=ok())
        gate = BuildGate(
            BuildExecutor(process_runner=runner, cache=cache), cache=cache
        )

        run(gate.run(project, SuiteSpec(name="build")))
        first = len(runner.runs)
        run(gate.run(project, SuiteSpec(name="build")))

        assert len(runner.runs) == first


class TestBuildProject:
    """The one-call path, end to end."""

    def test_a_whole_project_builds(self, project: Path) -> None:
        write_tree(project, PYTHON_PROJECT)
        report = run(
            build_project(project, process_runner=ScriptedRunner([], default=ok()))
        )

        assert report.ok
        assert sorted(report.rebuilt_units) == ["app", "core"]

    def test_changed_files_narrow_the_build(self, project: Path) -> None:
        write_tree(project, PYTHON_PROJECT)
        cache = MemoryCache()
        runner = ScriptedRunner([], default=ok())
        run(build_project(project, cache=cache, process_runner=runner))
        before = len(runner.runs)

        (project / "app/main.py").write_text("import core\n\nx = 2\n", encoding="utf-8")
        run(
            build_project(
                project,
                changed_paths=["app/main.py"],
                cache=cache,
                process_runner=runner,
            )
        )

        assert len(runner.runs) == before + 1

    def test_the_dependency_direction_is_respected(self, project: Path) -> None:
        """Editing core rebuilds app; editing app leaves core alone."""
        write_tree(project, PYTHON_PROJECT)
        cache = MemoryCache()
        runner = ScriptedRunner([], default=ok())
        run(build_project(project, cache=cache, process_runner=runner))

        (project / "core/util.py").write_text("def helper():\n    return 3\n", encoding="utf-8")
        report = run(
            build_project(
                project,
                changed_paths=["core/util.py"],
                cache=cache,
                process_runner=runner,
            )
        )

        assert sorted(report.rebuilt_units) == ["app", "core"]


def test_the_package_does_not_import_layers_above_it() -> None:
    """builder sits at layer 3: no workflow, api, or dashboard imports.

    ``workflow`` is a sibling, and the dependency runs the other way: a build
    reaches the engine by satisfying ``GateRunner``, so neither package needs to
    know the other exists.
    """
    import orchestrator.builder as package

    root = Path(package.__path__[0])
    forbidden = ("orchestrator.workflow", "orchestrator.api", "orchestrator.dashboard")
    offenders: list[str] = []

    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            offenders.extend(
                f"{path.name}: {name}" for name in names if name.startswith(forbidden)
            )

    assert offenders == [], f"layering violations: {offenders}"

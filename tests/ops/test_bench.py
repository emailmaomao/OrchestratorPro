"""Tests for the benchmark harness.

These check that the benchmarks measure something and report it honestly. They
do not assert on speed — a timing threshold on a shared runner is a test that
fails for reasons nobody can act on. The one exception is
:class:`Budgets`, whose floors are set low enough to catch an
order-of-magnitude regression and nothing tighter.
"""

from __future__ import annotations

import pytest

from orchestrator.ops.bench import (
    BENCHMARKS,
    BenchmarkResult,
    BenchmarkSuite,
    Budgets,
    bench_events,
    bench_graph,
    bench_replay,
    bench_scheduler,
    compare,
    run_benchmarks,
)


class TestResult:
    """What one measurement reports."""

    def _result(self, **over: object) -> BenchmarkResult:
        base = {"name": "x", "operations": 100, "seconds": 2.0}
        return BenchmarkResult(**{**base, **over})  # type: ignore[arg-type]

    def test_the_rate_is_derived(self) -> None:
        assert self._result().per_second == 50.0

    def test_the_mean_is_derived(self) -> None:
        assert self._result().mean_ms == 20.0

    def test_p95_comes_from_the_samples(self) -> None:
        """A mean hides the pause a person actually notices."""
        samples = tuple([0.001] * 95 + [0.5] * 5)
        result = self._result(operations=100, seconds=1.0, samples=samples)

        assert result.p95_ms > result.mean_ms

    def test_p95_falls_back_to_the_mean_without_samples(self) -> None:
        result = self._result()
        assert result.p95_ms == result.mean_ms

    def test_zero_operations_does_not_divide_by_zero(self) -> None:
        assert self._result(operations=0).mean_ms == 0.0

    def test_the_summary_reads_naturally(self) -> None:
        summary = self._result(unit="event").summary()

        assert "50 event/s" in summary
        assert "n=100" in summary

    def test_it_renders_for_a_regression_check(self) -> None:
        payload = self._result().to_dict()

        assert payload["name"] == "x"
        assert payload["per_second"] == 50.0
        assert "p95_ms" in payload


class TestIndividualBenchmarks:
    """Each one, at a size small enough to run in a test."""

    def test_events_measures_durable_appends(self) -> None:
        result = bench_events(count=20)

        assert result.name == "events.append"
        assert result.operations == 20
        assert result.seconds > 0
        assert len(result.samples) == 20

    def test_replay_measures_reconstruction(self) -> None:
        result = bench_replay(events=50)

        assert result.name == "events.replay"
        assert result.operations == 52  # the log, plus run and task creation
        assert "52" in result.detail

    def test_the_scheduler_benchmark_runs_over_a_real_graph(self) -> None:
        result = bench_scheduler(size=20, rounds=5)

        assert result.name == "scheduler.decide"
        assert result.operations == 5
        assert "20-task graph" in result.detail

    def test_the_graph_benchmark_validates_and_layers(self) -> None:
        result = bench_graph(size=20, rounds=3)

        assert result.name == "graph.build"
        assert result.operations == 3

    def test_no_benchmark_leaves_a_file_behind(self, tmp_dir: object) -> None:
        """A benchmark that litters is one people stop running."""
        import tempfile
        from pathlib import Path

        before = set(Path(tempfile.gettempdir()).glob("*/bench.db"))
        bench_events(count=5)
        after = set(Path(tempfile.gettempdir()).glob("*/bench.db"))

        assert after == before


class TestSuite:
    """Running them together."""

    def test_every_benchmark_runs(self) -> None:
        suite = run_benchmarks(scale=0.02)

        assert {result.name for result in suite.results} == {
            "events.append",
            "events.replay",
            "scheduler.decide",
            "graph.build",
        }

    def test_a_subset_can_be_selected(self) -> None:
        suite = run_benchmarks(["graph"], scale=0.02)

        assert [result.name for result in suite.results] == ["graph.build"]

    def test_an_unknown_name_is_refused(self) -> None:
        """Silently running nothing would look like a very fast machine."""
        with pytest.raises(ValueError, match="unknown benchmark"):
            run_benchmarks(["ghost"])

    def test_the_environment_is_recorded(self) -> None:
        """A number without a machine attached is not a measurement."""
        suite = run_benchmarks(["graph"], scale=0.02)

        assert suite.python
        assert suite.platform

    def test_a_result_can_be_looked_up(self) -> None:
        suite = run_benchmarks(["graph"], scale=0.02)

        assert suite.get("graph.build") is not None
        assert suite.get("absent") is None

    def test_the_summary_has_one_line_per_benchmark(self) -> None:
        suite = run_benchmarks(scale=0.02)
        assert len(suite.summary().splitlines()) == len(suite.results)

    def test_the_suite_renders_for_storage(self) -> None:
        payload = run_benchmarks(["graph"], scale=0.02).to_dict()

        assert payload["results"][0]["name"] == "graph.build"
        assert payload["python"]

    def test_scale_changes_the_size(self) -> None:
        small = run_benchmarks(["graph"], scale=0.02)
        larger = run_benchmarks(["graph"], scale=0.2)

        assert larger.results[0].operations >= small.results[0].operations

    def test_a_tiny_scale_still_measures_something(self) -> None:
        """Floors keep a scaled-down run from measuring zero operations."""
        suite = run_benchmarks(scale=0.0001)

        assert all(result.operations > 0 for result in suite.results)

    def test_the_registry_and_the_runner_agree(self) -> None:
        names = {name for name, _ in BENCHMARKS}
        assert names == {"events", "replay", "scheduler", "graph"}


class TestBudgets:
    """The regression floors."""

    def test_a_healthy_suite_clears_them(self) -> None:
        assert Budgets().check(run_benchmarks(scale=0.05)) == ()

    def test_a_slow_result_is_reported(self) -> None:
        suite = BenchmarkSuite(
            results=(
                BenchmarkResult(name="events.append", operations=1, seconds=1.0),
            )
        )
        failures = Budgets().check(suite)

        assert len(failures) == 1
        assert "events.append" in failures[0]

    def test_a_missing_result_is_not_a_failure(self) -> None:
        """Running one benchmark should not fail the budgets of the others."""
        suite = BenchmarkSuite(results=())
        assert Budgets().check(suite) == ()

    def test_the_floors_are_loose_on_purpose(self) -> None:
        """Tight thresholds on a shared runner are noise, not signal."""
        budgets = Budgets()

        assert budgets.events_per_second <= 100
        assert budgets.scheduler_decisions_per_second <= 1000


class TestCompare:
    """Checking one run against a recorded baseline."""

    def _suite(self, per_second: float) -> BenchmarkSuite:
        return BenchmarkSuite(
            results=(
                BenchmarkResult(name="graph.build", operations=100, seconds=100 / per_second),
            )
        )

    def test_no_regression_reports_nothing(self) -> None:
        baseline = [{"name": "graph.build", "per_second": 100.0}]
        assert compare(baseline, self._suite(110.0)) == ()

    def test_a_large_regression_is_reported(self) -> None:
        baseline = [{"name": "graph.build", "per_second": 100.0}]
        regressions = compare(baseline, self._suite(10.0))

        assert len(regressions) == 1
        assert "graph.build" in regressions[0]

    def test_small_drift_is_tolerated(self) -> None:
        """Benchmark noise between machines is large; crying wolf gets muted."""
        baseline = [{"name": "graph.build", "per_second": 100.0}]
        assert compare(baseline, self._suite(70.0)) == ()

    def test_the_tolerance_is_adjustable(self) -> None:
        baseline = [{"name": "graph.build", "per_second": 100.0}]
        assert compare(baseline, self._suite(70.0), tolerance=0.1) != ()

    def test_a_new_benchmark_is_not_a_regression(self) -> None:
        assert compare([], self._suite(10.0)) == ()

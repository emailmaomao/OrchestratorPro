"""Tests for the agent domain model: specs, usage, budgets, and attempts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from orchestrator.agent.model import (
    AgentRole,
    Attempt,
    AttemptResult,
    AttemptStatus,
    BudgetLedger,
    TaskSpec,
    TokenUsage,
    total_usage,
)
from orchestrator.core.events import (
    Budget,
    BudgetAxis,
    BudgetExhaustedError,
    DomainValidationError,
    StateTransitionError,
    TaskId,
)


class FakeClock:
    """A monotonic clock that only advances when told to."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def budget() -> Budget:
    """Ten seconds, one hundred tokens, five tool calls."""
    return Budget(seconds=10.0, tokens=100, tool_calls=5)


# --------------------------------------------------------------------------- #
# TaskSpec
# --------------------------------------------------------------------------- #


class TestTaskSpec:
    """The narrow value object handed to an agent."""

    def test_defaults_to_the_worker_role(self) -> None:
        spec = TaskSpec(task_id=TaskId.generate(), title="t", prompt="p")
        assert spec.role is AgentRole.WORKER
        assert spec.feedback == ()

    @pytest.mark.parametrize(("title", "prompt"), [("", "p"), ("t", ""), ("  ", "p")])
    def test_blank_fields_are_rejected(self, title: str, prompt: str) -> None:
        with pytest.raises(DomainValidationError):
            TaskSpec(task_id=TaskId.generate(), title=title, prompt=prompt)

    def test_with_feedback_returns_a_new_spec(self) -> None:
        original = TaskSpec(task_id=TaskId.generate(), title="t", prompt="p")
        updated = original.with_feedback("tests failed: test_login")

        assert original.feedback == ()
        assert updated.feedback == ("tests failed: test_login",)
        assert updated.task_id == original.task_id

    def test_feedback_accumulates_across_attempts(self) -> None:
        spec = TaskSpec(task_id=TaskId.generate(), title="t", prompt="p")
        spec = spec.with_feedback("first failure").with_feedback("second failure")
        assert spec.feedback == ("first failure", "second failure")

    def test_blank_feedback_is_rejected(self) -> None:
        spec = TaskSpec(task_id=TaskId.generate(), title="t", prompt="p")
        with pytest.raises(DomainValidationError):
            spec.with_feedback("   ")

    def test_spec_does_not_import_the_task_package(self) -> None:
        """Layering: agent and task are siblings that never reference each other."""
        import ast
        from pathlib import Path

        import orchestrator.agent.model as module

        assert module.__file__ is not None
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))

        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

        assert not any(name.startswith("orchestrator.task") for name in imported)


# --------------------------------------------------------------------------- #
# TokenUsage
# --------------------------------------------------------------------------- #


class TestTokenUsage:
    """Cost is summed where known and stays absent where it is not."""

    def test_total_tokens_excludes_cached_double_counting(self) -> None:
        usage = TokenUsage(input_tokens=100, output_tokens=50, cached_input_tokens=80)
        assert usage.total_tokens == 150

    def test_addition_sums_every_axis(self) -> None:
        combined = TokenUsage(input_tokens=10, output_tokens=5, cost_usd=0.01) + TokenUsage(
            input_tokens=20, output_tokens=7, cost_usd=0.02
        )
        assert combined.input_tokens == 30
        assert combined.output_tokens == 12
        assert combined.cost_usd == pytest.approx(0.03)

    def test_cost_stays_none_when_neither_side_reports_one(self) -> None:
        combined = TokenUsage(input_tokens=1) + TokenUsage(input_tokens=2)
        assert combined.cost_usd is None

    def test_partial_cost_is_preserved_rather_than_discarded(self) -> None:
        combined = TokenUsage(cost_usd=0.05) + TokenUsage(cost_usd=None)
        assert combined.cost_usd == pytest.approx(0.05)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"input_tokens": -1},
            {"output_tokens": -1},
            {"cached_input_tokens": -1},
            {"cost_usd": -0.01},
        ],
    )
    def test_negative_values_are_rejected(self, kwargs: dict[str, float]) -> None:
        with pytest.raises(DomainValidationError):
            TokenUsage(**kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# BudgetLedger
# --------------------------------------------------------------------------- #


class TestBudgetLedger:
    """All three axes are enforced, and whichever binds first wins."""

    def test_a_fresh_ledger_is_not_exhausted(self, budget: Budget) -> None:
        ledger = BudgetLedger(budget, clock=FakeClock())
        assert not ledger.is_exhausted
        assert ledger.exhausted_axis is None
        ledger.check()

    def test_seconds_axis_exhausts(self, budget: Budget) -> None:
        clock = FakeClock()
        ledger = BudgetLedger(budget, clock=clock)
        clock.advance(10.0)
        assert ledger.exhausted_axis is BudgetAxis.SECONDS

    def test_tokens_axis_exhausts(self, budget: Budget) -> None:
        ledger = BudgetLedger(budget, clock=FakeClock())
        ledger.record_tokens(100)
        assert ledger.exhausted_axis is BudgetAxis.TOKENS

    def test_tool_calls_axis_exhausts(self, budget: Budget) -> None:
        ledger = BudgetLedger(budget, clock=FakeClock())
        for _ in range(5):
            ledger.record_tool_call()
        assert ledger.exhausted_axis is BudgetAxis.TOOL_CALLS

    def test_check_raises_naming_the_axis(self, budget: Budget) -> None:
        ledger = BudgetLedger(budget, clock=FakeClock())
        ledger.record_tokens(150)
        with pytest.raises(BudgetExhaustedError) as excinfo:
            ledger.check()
        assert excinfo.value.axis is BudgetAxis.TOKENS
        assert excinfo.value.limit == 100
        assert excinfo.value.consumed == 150

    def test_axis_attribution_is_stable_when_several_are_blown(
        self, budget: Budget
    ) -> None:
        clock = FakeClock()
        ledger = BudgetLedger(budget, clock=clock)
        clock.advance(99.0)
        ledger.record_tokens(999)
        # Fixed check order means the reported axis never flaps between runs.
        assert ledger.exhausted_axis is BudgetAxis.SECONDS
        assert ledger.exhausted_axis is BudgetAxis.SECONDS

    def test_remaining_is_floored_at_zero(self, budget: Budget) -> None:
        ledger = BudgetLedger(budget, clock=FakeClock())
        ledger.record_tokens(250)
        assert ledger.remaining(BudgetAxis.TOKENS) == 0.0

    def test_remaining_reports_headroom(self, budget: Budget) -> None:
        ledger = BudgetLedger(budget, clock=FakeClock())
        ledger.record_tokens(40)
        assert ledger.remaining(BudgetAxis.TOKENS) == 60.0

    def test_elapsed_tracks_the_injected_clock(self, budget: Budget) -> None:
        clock = FakeClock()
        ledger = BudgetLedger(budget, clock=clock)
        clock.advance(3.5)
        assert ledger.elapsed_seconds == pytest.approx(3.5)

    def test_negative_token_counts_are_rejected(self, budget: Budget) -> None:
        ledger = BudgetLedger(budget, clock=FakeClock())
        with pytest.raises(DomainValidationError):
            ledger.record_tokens(-1)

    def test_non_positive_tool_call_counts_are_rejected(self, budget: Budget) -> None:
        ledger = BudgetLedger(budget, clock=FakeClock())
        with pytest.raises(DomainValidationError):
            ledger.record_tool_call(0)

    def test_snapshot_covers_every_axis(self, budget: Budget) -> None:
        ledger = BudgetLedger(budget, clock=FakeClock())
        ledger.record_tokens(5)
        ledger.record_tool_call()
        snapshot = ledger.snapshot()
        assert set(snapshot) == {axis.value for axis in BudgetAxis}
        assert snapshot["tokens"] == 5.0
        assert snapshot["tool_calls"] == 1.0


# --------------------------------------------------------------------------- #
# AttemptResult
# --------------------------------------------------------------------------- #


class TestAttemptResult:
    """A result reports what happened; it never declares itself accepted."""

    def test_running_is_not_a_valid_result(self) -> None:
        with pytest.raises(DomainValidationError, match="terminal"):
            AttemptResult(status=AttemptStatus.RUNNING)

    def test_succeeded_flag_reflects_status(self) -> None:
        assert AttemptResult(status=AttemptStatus.SUCCEEDED).succeeded
        assert not AttemptResult(status=AttemptStatus.ERRORED).succeeded

    def test_duplicate_changed_files_are_rejected(self) -> None:
        with pytest.raises(DomainValidationError, match="unique"):
            AttemptResult(status=AttemptStatus.SUCCEEDED, changed_files=("a.py", "a.py"))

    def test_result_carries_no_acceptance_field(self) -> None:
        """Gating is the workflow engine's job, never the agent's."""
        result = AttemptResult(status=AttemptStatus.SUCCEEDED)
        for forbidden in ("passed", "accepted", "approved", "merged"):
            assert not hasattr(result, forbidden)

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (AttemptStatus.SUCCEEDED, True),
            (AttemptStatus.BUDGET_EXHAUSTED, True),
            (AttemptStatus.ERRORED, False),
            (AttemptStatus.CANCELLED, False),
            (AttemptStatus.REFUSED, False),
        ],
    )
    def test_produced_work_marks_inspectable_outcomes(
        self, status: AttemptStatus, expected: bool
    ) -> None:
        assert status.produced_work is expected


# --------------------------------------------------------------------------- #
# Attempt
# --------------------------------------------------------------------------- #


class TestAttempt:
    """Attempts are immutable records, closed exactly once."""

    def test_start_creates_a_running_attempt(self) -> None:
        attempt = Attempt.start(task_id=TaskId.generate(), number=1)
        assert attempt.status is AttemptStatus.RUNNING
        assert attempt.result is None
        assert attempt.finished_at is None
        assert attempt.duration_seconds is None

    def test_finish_returns_a_new_terminal_record(self) -> None:
        attempt = Attempt.start(task_id=TaskId.generate(), number=1)
        result = AttemptResult(
            status=AttemptStatus.SUCCEEDED,
            changed_files=("src/a.py",),
            usage=TokenUsage(input_tokens=10, output_tokens=5, cost_usd=0.01),
        )
        finished = attempt.finish(result)

        assert attempt.status is AttemptStatus.RUNNING  # original untouched
        assert finished.status is AttemptStatus.SUCCEEDED
        assert finished.result is result
        assert finished.finished_at is not None
        assert finished.id == attempt.id

    def test_finishing_twice_is_refused(self) -> None:
        attempt = Attempt.start(task_id=TaskId.generate(), number=1).finish(
            AttemptResult(status=AttemptStatus.SUCCEEDED)
        )
        with pytest.raises(StateTransitionError, match="already finished"):
            attempt.finish(AttemptResult(status=AttemptStatus.ERRORED))

    def test_attempt_numbers_are_one_based(self) -> None:
        with pytest.raises(DomainValidationError, match="1-based"):
            Attempt.start(task_id=TaskId.generate(), number=0)

    def test_terminal_attempts_must_carry_a_result(self) -> None:
        with pytest.raises(DomainValidationError, match="must carry a result"):
            Attempt(
                id=Attempt.start(task_id=TaskId.generate(), number=1).id,
                task_id=TaskId.generate(),
                number=1,
                status=AttemptStatus.SUCCEEDED,
                started_at=datetime.now(UTC),
            )

    def test_running_attempts_must_not_carry_a_result(self) -> None:
        with pytest.raises(DomainValidationError, match="must not carry a result"):
            Attempt(
                id=Attempt.start(task_id=TaskId.generate(), number=1).id,
                task_id=TaskId.generate(),
                number=1,
                status=AttemptStatus.RUNNING,
                started_at=datetime.now(UTC),
                result=AttemptResult(status=AttemptStatus.SUCCEEDED),
            )

    def test_naive_timestamps_are_rejected(self) -> None:
        with pytest.raises(DomainValidationError, match="timezone-aware"):
            Attempt(
                id=Attempt.start(task_id=TaskId.generate(), number=1).id,
                task_id=TaskId.generate(),
                number=1,
                status=AttemptStatus.RUNNING,
                started_at=datetime.now(),  # noqa: DTZ005 - deliberately naive
            )

    def test_finish_time_may_not_precede_start(self) -> None:
        started = datetime.now(UTC)
        attempt = Attempt.start(task_id=TaskId.generate(), number=1)
        with pytest.raises(DomainValidationError, match="must not precede"):
            attempt.finish(
                AttemptResult(status=AttemptStatus.SUCCEEDED),
                at=started - timedelta(hours=1),
            )

    def test_duration_is_measured_once_finished(self) -> None:
        attempt = Attempt.start(task_id=TaskId.generate(), number=1)
        finished = attempt.finish(
            AttemptResult(status=AttemptStatus.SUCCEEDED),
            at=attempt.started_at + timedelta(seconds=42),
        )
        assert finished.duration_seconds == pytest.approx(42.0)

    def test_usage_is_zero_while_running(self) -> None:
        attempt = Attempt.start(task_id=TaskId.generate(), number=1)
        assert attempt.usage.total_tokens == 0

    def test_budget_exhausted_attempts_preserve_their_work(self) -> None:
        """FR-2.7: partial work survives an exhausted budget."""
        attempt = Attempt.start(task_id=TaskId.generate(), number=2)
        finished = attempt.finish(
            AttemptResult(
                status=AttemptStatus.BUDGET_EXHAUSTED,
                changed_files=("src/partial.py",),
                error_code="budget_exhausted",
            )
        )
        assert finished.status.produced_work
        assert finished.result is not None
        assert finished.result.changed_files == ("src/partial.py",)


def test_total_usage_sums_across_attempts() -> None:
    """Cost attribution rolls up from attempts to a run (FR-5.4)."""
    task_id = TaskId.generate()
    attempts = [
        Attempt.start(task_id=task_id, number=n).finish(
            AttemptResult(
                status=AttemptStatus.SUCCEEDED,
                usage=TokenUsage(input_tokens=100, output_tokens=50, cost_usd=0.02),
            )
        )
        for n in (1, 2, 3)
    ]
    total = total_usage(attempts)
    assert total.input_tokens == 300
    assert total.output_tokens == 150
    assert total.cost_usd == pytest.approx(0.06)


class TestEstimatedTokens:
    """OP-004: an estimate must never be presented as a measurement."""

    def test_token_usage_estimation_is_sticky_under_addition(self) -> None:
        """A total with one estimated component is an estimate."""
        from orchestrator.agent.model import TokenUsage

        measured = TokenUsage(input_tokens=10, output_tokens=5)
        estimated = TokenUsage(input_tokens=3, output_tokens=2, estimated=True)

        assert not measured.estimated
        assert (measured + estimated).estimated
        assert (estimated + measured).estimated
        assert not (measured + measured).estimated

    def test_the_ledger_remembers_estimation(self) -> None:
        """One estimated contribution marks the whole figure."""
        from orchestrator.agent.model import Budget, BudgetLedger

        ledger = BudgetLedger(Budget(seconds=60, tokens=1000, tool_calls=10))
        ledger.record_tokens(100)
        assert not ledger.tokens_estimated
        ledger.record_tokens(100, estimated=True)
        assert ledger.tokens_estimated
        ledger.record_tokens(100)
        assert ledger.tokens_estimated, "estimation must be sticky"

    def test_exhaustion_on_estimated_tokens_says_so(self) -> None:
        """A budget verdict on approximations must not read as precision."""
        from orchestrator.agent.model import Budget, BudgetLedger
        from orchestrator.core.events import BudgetExhaustedError

        ledger = BudgetLedger(Budget(seconds=60, tokens=50, tool_calls=10))
        ledger.record_tokens(60, estimated=True)
        with pytest.raises(BudgetExhaustedError) as excinfo:
            ledger.check()

        assert excinfo.value.estimated is True
        assert excinfo.value.detail["estimated"] is True
        assert "estimates" in str(excinfo.value)

    def test_exhaustion_on_measured_tokens_does_not(self) -> None:
        """The flag is absent when the counts were real."""
        from orchestrator.agent.model import Budget, BudgetLedger
        from orchestrator.core.events import BudgetExhaustedError

        ledger = BudgetLedger(Budget(seconds=60, tokens=50, tool_calls=10))
        ledger.record_tokens(60)
        with pytest.raises(BudgetExhaustedError) as excinfo:
            ledger.check()

        assert excinfo.value.estimated is False
        assert "estimates" not in str(excinfo.value)

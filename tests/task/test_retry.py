"""Tests for the retry and backoff policy."""

from __future__ import annotations

import pytest

from orchestrator.task.retry import (
    NO_BACKOFF,
    BackoffStrategy,
    RetryPolicy,
    RetryPolicyError,
)


def fixed_jitter(value: float) -> object:
    """Return a jitter source that always yields ``value``."""
    return lambda: value


class TestConfiguration:
    """A policy that cannot work is refused at construction."""

    def test_defaults_are_exponential(self) -> None:
        policy = RetryPolicy()
        assert policy.strategy is BackoffStrategy.EXPONENTIAL
        assert policy.base_delay_s == 1.0

    def test_negative_base_delay_is_refused(self) -> None:
        with pytest.raises(RetryPolicyError, match="base_delay_s"):
            RetryPolicy(base_delay_s=-1)

    def test_a_ceiling_below_the_floor_is_refused(self) -> None:
        with pytest.raises(RetryPolicyError, match="max_delay_s"):
            RetryPolicy(base_delay_s=10, max_delay_s=5)

    def test_a_multiplier_below_one_is_refused(self) -> None:
        with pytest.raises(RetryPolicyError, match="multiplier"):
            RetryPolicy(multiplier=0.5)

    @pytest.mark.parametrize("jitter", [-0.1, 1.5])
    def test_out_of_range_jitter_is_refused(self, jitter: float) -> None:
        with pytest.raises(RetryPolicyError, match="jitter"):
            RetryPolicy(jitter=jitter)


class TestBackoffCurves:
    """Each strategy grows the way it says it does."""

    def test_the_first_attempt_never_waits(self) -> None:
        """Nothing has failed yet, so there is nothing to back off from."""
        for strategy in BackoffStrategy:
            policy = RetryPolicy(strategy=strategy, jitter=0.0)
            assert policy.raw_delay_for(1) == 0.0
            assert policy.raw_delay_for(0) == 0.0

    def test_none_never_waits(self) -> None:
        policy = RetryPolicy(strategy=BackoffStrategy.NONE, jitter=0.0)
        assert [policy.raw_delay_for(n) for n in (2, 3, 4)] == [0.0, 0.0, 0.0]

    def test_fixed_is_constant(self) -> None:
        policy = RetryPolicy(
            strategy=BackoffStrategy.FIXED, base_delay_s=2.0, jitter=0.0
        )
        assert [policy.raw_delay_for(n) for n in (2, 3, 4)] == [2.0, 2.0, 2.0]

    def test_linear_grows_by_the_base(self) -> None:
        policy = RetryPolicy(
            strategy=BackoffStrategy.LINEAR, base_delay_s=3.0, jitter=0.0
        )
        assert [policy.raw_delay_for(n) for n in (2, 3, 4)] == [3.0, 6.0, 9.0]

    def test_exponential_doubles(self) -> None:
        policy = RetryPolicy(
            strategy=BackoffStrategy.EXPONENTIAL,
            base_delay_s=1.0,
            multiplier=2.0,
            jitter=0.0,
            max_delay_s=100.0,
        )
        assert [policy.raw_delay_for(n) for n in (2, 3, 4, 5)] == [1.0, 2.0, 4.0, 8.0]

    def test_the_ceiling_caps_growth(self) -> None:
        policy = RetryPolicy(
            strategy=BackoffStrategy.EXPONENTIAL,
            base_delay_s=1.0,
            max_delay_s=5.0,
            jitter=0.0,
        )
        assert policy.raw_delay_for(10) == 5.0


class TestJitter:
    """Jitter is deterministic under an injected source, and never adds delay."""

    def test_jitter_only_reduces(self) -> None:
        """A configured ceiling must be a real ceiling."""
        policy = RetryPolicy(
            strategy=BackoffStrategy.FIXED,
            base_delay_s=10.0,
            jitter=0.5,
            jitter_source=fixed_jitter(1.0),  # type: ignore[arg-type]
        )
        assert policy.delay_for(2) == pytest.approx(5.0)

    def test_zero_from_the_source_leaves_the_delay_intact(self) -> None:
        policy = RetryPolicy(
            strategy=BackoffStrategy.FIXED,
            base_delay_s=10.0,
            jitter=0.5,
            jitter_source=fixed_jitter(0.0),  # type: ignore[arg-type]
        )
        assert policy.delay_for(2) == pytest.approx(10.0)

    def test_zero_jitter_disables_randomization(self) -> None:
        policy = RetryPolicy(
            strategy=BackoffStrategy.FIXED,
            base_delay_s=7.0,
            jitter=0.0,
            jitter_source=fixed_jitter(0.9),  # type: ignore[arg-type]
        )
        assert policy.delay_for(2) == 7.0

    def test_an_injected_source_makes_delays_reproducible(self) -> None:
        policy = RetryPolicy(
            strategy=BackoffStrategy.EXPONENTIAL,
            jitter=0.3,
            jitter_source=fixed_jitter(0.5),  # type: ignore[arg-type]
        )
        assert policy.delay_for(3) == policy.delay_for(3)

    def test_a_zero_delay_stays_zero(self) -> None:
        policy = RetryPolicy(strategy=BackoffStrategy.NONE, jitter=0.9)
        assert policy.delay_for(5) == 0.0


class TestDecisions:
    """Whether to retry at all."""

    def test_a_retryable_failure_with_attempts_left_retries(self) -> None:
        decision = RetryPolicy(jitter=0.0).decide(
            attempt=1, max_attempts=3, retryable=True
        )
        assert decision.retry
        assert bool(decision) is True
        assert decision.delay_s > 0

    def test_a_non_retryable_failure_is_never_retried(self) -> None:
        """Retrying would burn budget to reproduce the identical error."""
        decision = RetryPolicy().decide(
            attempt=1, max_attempts=5, retryable=False, error_code="invalid_request"
        )
        assert not decision.retry
        assert "not retryable" in decision.reason
        assert "invalid_request" in decision.reason

    def test_the_last_attempt_does_not_retry(self) -> None:
        decision = RetryPolicy().decide(attempt=3, max_attempts=3, retryable=True)
        assert not decision.retry
        assert "exhausted" in decision.reason

    def test_exceeding_the_allowance_does_not_retry(self) -> None:
        assert not RetryPolicy().decide(attempt=9, max_attempts=3, retryable=True).retry

    def test_a_single_attempt_task_never_retries(self) -> None:
        assert not RetryPolicy().decide(attempt=1, max_attempts=1, retryable=True).retry

    def test_the_delay_is_for_the_next_attempt(self) -> None:
        policy = RetryPolicy(
            strategy=BackoffStrategy.LINEAR, base_delay_s=2.0, jitter=0.0
        )
        decision = policy.decide(attempt=2, max_attempts=5, retryable=True)
        assert decision.delay_s == policy.raw_delay_for(3)

    def test_attempts_remaining(self) -> None:
        policy = RetryPolicy()
        assert policy.attempts_remaining(attempt=1, max_attempts=3) == 2
        assert policy.attempts_remaining(attempt=3, max_attempts=3) == 0
        assert policy.attempts_remaining(attempt=9, max_attempts=3) == 0


def test_no_backoff_preset_never_waits() -> None:
    assert NO_BACKOFF.delay_for(5) == 0.0
    assert NO_BACKOFF.decide(attempt=1, max_attempts=3, retryable=True).delay_s == 0.0

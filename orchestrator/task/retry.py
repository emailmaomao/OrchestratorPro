"""Retry and backoff policy.

Deciding *whether* to retry and *how long to wait* are separate questions, and
this module keeps them separate. Both are pure: no sleeping happens here, and no
clock is read. The policy computes a delay; the dispatcher is what waits.

Two rules that matter more than the arithmetic:

* **A non-retryable failure is never retried.** Retrying a malformed request or
  an authentication failure burns budget to reproduce the identical error. The
  provider layer already classifies failures (``docs/030`` §3); this honours
  that classification rather than second-guessing it.
* **Jitter comes from an injected source.** Backoff without jitter synchronizes
  retries into a thundering herd, but a policy that reaches for
  :mod:`random` directly cannot be tested deterministically. The source is a
  parameter, defaulting to a seeded generator.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from orchestrator.core.events import OrchestratorError

__all__ = [
    "BackoffStrategy",
    "RetryDecision",
    "RetryPolicy",
    "RetryPolicyError",
]


class RetryPolicyError(OrchestratorError):
    """The retry policy was configured with values that cannot work."""

    code = "retry_policy"
    retryable = False


class BackoffStrategy(StrEnum):
    """How the delay grows between successive attempts."""

    NONE = "none"
    FIXED = "fixed"
    LINEAR = "linear"
    EXPONENTIAL = "exponential"


@dataclass(frozen=True, slots=True)
class RetryDecision:
    """Whether to retry, and how long to wait first."""

    retry: bool
    delay_s: float = 0.0
    reason: str = ""

    def __bool__(self) -> bool:
        return self.retry


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How failures are retried.

    Attributes:
        strategy: How the delay grows.
        base_delay_s: The delay before the second attempt.
        max_delay_s: A ceiling, so exponential growth cannot run away.
        multiplier: Growth factor for :attr:`BackoffStrategy.EXPONENTIAL`.
        jitter: Fraction of the delay to randomize, in ``[0, 1]``. At ``0.2`` a
            computed delay of 10s becomes a random value in ``[8, 10]``.
        jitter_source: Returns a float in ``[0, 1)``. Injected so tests are
            deterministic.
    """

    strategy: BackoffStrategy = BackoffStrategy.EXPONENTIAL
    base_delay_s: float = 1.0
    max_delay_s: float = 60.0
    multiplier: float = 2.0
    jitter: float = 0.2
    jitter_source: Callable[[], float] = field(
        # S311 waived: retry jitter, deliberately seeded and reproducible.
        # Cryptographic randomness here would make backoff untestable.
        default_factory=lambda: random.Random(0).random,  # noqa: S311
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if self.base_delay_s < 0:
            raise RetryPolicyError(
                f"base_delay_s must not be negative, got {self.base_delay_s}"
            )
        if self.max_delay_s < self.base_delay_s:
            raise RetryPolicyError(
                f"max_delay_s ({self.max_delay_s}) must be at least base_delay_s "
                f"({self.base_delay_s})"
            )
        if self.multiplier < 1:
            raise RetryPolicyError(
                f"multiplier must be at least 1, got {self.multiplier}"
            )
        if not 0.0 <= self.jitter <= 1.0:
            raise RetryPolicyError(f"jitter must be within [0, 1], got {self.jitter}")

    # ------------------------------------------------------------------ delay

    def raw_delay_for(self, attempt: int) -> float:
        """Return the un-jittered delay before ``attempt``.

        Args:
            attempt: The 1-based number of the attempt about to be made. The
                delay before the *first* attempt is always zero — nothing has
                failed yet.

        Returns:
            The delay in seconds, capped at :attr:`max_delay_s`.
        """
        if attempt <= 1:
            return 0.0
        previous = attempt - 1

        match self.strategy:
            case BackoffStrategy.NONE:
                delay = 0.0
            case BackoffStrategy.FIXED:
                delay = self.base_delay_s
            case BackoffStrategy.LINEAR:
                delay = self.base_delay_s * previous
            case BackoffStrategy.EXPONENTIAL:
                delay = self.base_delay_s * (self.multiplier ** (previous - 1))

        return min(delay, self.max_delay_s)

    def delay_for(self, attempt: int) -> float:
        """Return the delay before ``attempt``, with jitter applied.

        Jitter only ever *reduces* the delay, so a configured ceiling is a real
        ceiling.
        """
        delay = self.raw_delay_for(attempt)
        if delay <= 0 or self.jitter == 0:
            return delay
        factor = 1.0 - self.jitter * self.jitter_source()
        return delay * factor

    # --------------------------------------------------------------- decision

    def decide(
        self, *, attempt: int, max_attempts: int, retryable: bool, error_code: str = ""
    ) -> RetryDecision:
        """Decide whether the next attempt should be made.

        Args:
            attempt: The 1-based number of the attempt that just failed.
            max_attempts: The task's own ceiling, which is authoritative — the
                policy governs *how* to retry, the task governs *how many times*.
            retryable: Whether the failure could plausibly succeed on a retry,
                as classified by whatever produced it.
            error_code: The failure's stable code, for the explanation.

        Returns:
            The decision, carrying the delay and a human-readable reason.
        """
        if not retryable:
            return RetryDecision(
                retry=False,
                reason=(
                    f"failure {error_code or 'unknown'} is not retryable; retrying "
                    "would reproduce it"
                ),
            )
        if attempt >= max_attempts:
            return RetryDecision(
                retry=False,
                reason=f"attempt {attempt} of {max_attempts} exhausted the allowance",
            )
        return RetryDecision(
            retry=True,
            delay_s=self.delay_for(attempt + 1),
            reason=f"attempt {attempt} of {max_attempts} failed; retrying",
        )

    def attempts_remaining(self, *, attempt: int, max_attempts: int) -> int:
        """Return how many further attempts the allowance permits."""
        return max(0, max_attempts - attempt)


#: A policy that never waits. Useful in tests and for latency-sensitive runs
#: where a failure is more likely to be deterministic than transient.
NO_BACKOFF = RetryPolicy(strategy=BackoffStrategy.NONE, base_delay_s=0.0, jitter=0.0)

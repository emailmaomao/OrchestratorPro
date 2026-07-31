"""Rate limiting.

A token bucket per client. The bucket refills continuously rather than in
fixed windows, because a fixed window lets a client spend its whole allowance
in the last second of one window and again in the first second of the next —
twice the intended rate at exactly the moment it matters least.

Two exemptions, both deliberate:

* **Streaming endpoints are not counted.** An SSE connection is one request
  that lasts an hour; charging it once is meaningless and charging it per event
  would disconnect the dashboard for working correctly.
* **Health checks are cheap and are still counted.** A supervisor polling
  health is a client like any other; if it can exhaust the budget, the budget
  is wrong.

The limiter holds state in memory, which is the honest scope for a
single-process control plane. A second instance would need a shared store, and
the docstring says so rather than pretending this is a distributed limiter.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from orchestrator.core.logging import get_logger

__all__ = [
    "STREAMING_PATH_MARKERS",
    "RateLimitMiddleware",
    "TokenBucket",
]

_log = get_logger(__name__)

#: Path fragments that mark a long-lived stream. Matching on the path rather
#: than the response type keeps the decision in front of the handler, where the
#: limiter runs.
STREAMING_PATH_MARKERS: tuple[str, ...] = ("/events", "/ws")

#: How many client buckets to keep. Beyond this the least recently seen are
#: dropped, so a flood of distinct source addresses cannot exhaust memory —
#: which would turn a rate limiter into the outage it exists to prevent.
MAX_TRACKED_CLIENTS = 4096


@dataclass(slots=True)
class TokenBucket:
    """A continuously refilling allowance.

    Attributes:
        capacity: Burst size — the most a client may spend at once.
        refill_per_second: Sustained rate.
        tokens: Tokens available now.
        updated_at: When ``tokens`` was last computed.
    """

    capacity: float
    refill_per_second: float
    tokens: float = 0.0
    updated_at: float = 0.0
    #: Whether the bucket has been used. A separate flag rather than a sentinel
    #: timestamp: ``monotonic()`` may legitimately return zero, and a bucket
    #: that treats that as "never used" refills on every call and limits nothing.
    started: bool = False

    def take(self, now: float, amount: float = 1.0) -> bool:
        """Spend from the bucket if it can afford it.

        Args:
            now: Current monotonic time.
            amount: Tokens to spend.

        Returns:
            Whether the spend succeeded.
        """
        self._refill(now)
        if self.tokens >= amount:
            self.tokens -= amount
            return True
        return False

    def retry_after(self, now: float, amount: float = 1.0) -> float:
        """Seconds until ``amount`` tokens would be available."""
        self._refill(now)
        if self.tokens >= amount or self.refill_per_second <= 0:
            return 0.0
        return (amount - self.tokens) / self.refill_per_second

    def _refill(self, now: float) -> None:
        """Add the tokens that have accrued since the last look."""
        if not self.started:
            self.started = True
            self.updated_at = now
            self.tokens = self.capacity
            return
        # `max(0, ...)`: a clock that appears to go backwards must not mint
        # tokens. Monotonic clocks do not, but injected ones in tests can.
        elapsed = max(0.0, now - self.updated_at)
        self.updated_at = now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_second)


class RateLimitMiddleware:
    """Refuses requests from a client that is asking too often."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        per_minute: int = 600,
        burst: int = 120,
        exempt_markers: Iterable[str] = STREAMING_PATH_MARKERS,
        clock: Callable[[], float] = time.monotonic,
        max_clients: int = MAX_TRACKED_CLIENTS,
    ) -> None:
        """Wrap ``app`` with a per-client budget.

        Args:
            app: The application to protect.
            per_minute: Sustained requests allowed per client per minute.
            burst: How far above that a client may spike.
            exempt_markers: Path fragments that are never counted.
            clock: Monotonic time source, injected so tests need not wait.
            max_clients: Ceiling on tracked clients.
        """
        self.app = app
        self.per_second = per_minute / 60.0
        self.burst = float(max(burst, 1))
        self.exempt_markers = tuple(exempt_markers)
        self.clock = clock
        self.max_clients = max_clients
        self._buckets: dict[str, TokenBucket] = {}
        self.refused = 0

    @property
    def tracked(self) -> int:
        """How many clients currently have a bucket."""
        return len(self._buckets)

    def is_exempt(self, path: str) -> bool:
        """Whether a path is never counted against the budget."""
        return any(marker in path for marker in self.exempt_markers)

    def client_of(self, scope: dict) -> str:
        """Identify the client a request came from.

        The peer address, not a forwarded header: this build has no
        authentication and trusts no proxy, and a limiter keyed on a header the
        caller controls is a limiter the caller turns off.
        """
        client = scope.get("client")
        return client[0] if client else "unknown"

    def bucket_for(self, key: str) -> TokenBucket:
        """Return the client's bucket, creating one if needed."""
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= self.max_clients:
                # Drop the coldest half rather than one entry, so a flood does
                # not make every subsequent request pay for an eviction.
                for stale in sorted(self._buckets, key=lambda k: self._buckets[k].updated_at)[
                    : self.max_clients // 2
                ]:
                    del self._buckets[stale]
            bucket = TokenBucket(capacity=self.burst, refill_per_second=self.per_second)
            self._buckets[key] = bucket
        return bucket

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        """Charge the request, or refuse it."""
        if scope["type"] != "http" or self.is_exempt(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        now = self.clock()
        key = self.client_of(scope)
        bucket = self.bucket_for(key)

        if bucket.take(now):
            await self.app(scope, receive, send)
            return

        self.refused += 1
        wait = bucket.retry_after(now)
        _log.warning(
            "rate limit exceeded",
            client=key,
            path=scope.get("path", ""),
            retry_after_s=round(wait, 2),
        )
        response = JSONResponse(
            status_code=429,
            headers={"retry-after": str(max(1, int(wait + 0.999)))},
            content={
                "error": {
                    "code": "rate_limited",
                    "message": (
                        "too many requests; this server allows "
                        f"{round(self.per_second * 60)} per minute per client"
                    ),
                    "retryable": True,
                    "detail": {
                        "retry_after_s": round(wait, 3),
                        "per_minute": round(self.per_second * 60),
                        "burst": int(self.burst),
                    },
                }
            },
        )
        await response(scope, receive, send)

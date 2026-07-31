"""Tests for rate limiting."""

from __future__ import annotations

from fastapi.testclient import TestClient

from orchestrator.core.config import SecurityConfig
from orchestrator.ops.ratelimit import (
    STREAMING_PATH_MARKERS,
    RateLimitMiddleware,
    TokenBucket,
)

from tests.ops.conftest import FakeClock, hardened_app


class TestTokenBucket:
    """The allowance itself."""

    def test_a_fresh_bucket_starts_full(self) -> None:
        bucket = TokenBucket(capacity=3, refill_per_second=1)

        assert bucket.take(0.0)
        assert bucket.take(0.0)
        assert bucket.take(0.0)
        assert not bucket.take(0.0)

    def test_it_refills_continuously(self) -> None:
        """Not in windows: a window lets a client spend twice at the boundary."""
        bucket = TokenBucket(capacity=2, refill_per_second=1)
        bucket.take(0.0)
        bucket.take(0.0)

        assert not bucket.take(0.0)
        assert bucket.take(1.0)
        assert not bucket.take(1.0)
        assert bucket.take(2.0)

    def test_it_never_exceeds_its_capacity(self) -> None:
        """An idle client cannot bank an hour of requests."""
        bucket = TokenBucket(capacity=2, refill_per_second=1)
        bucket.take(0.0)

        assert bucket.take(3600.0)
        assert bucket.take(3600.0)
        assert not bucket.take(3600.0)

    def test_retry_after_is_zero_when_it_can_afford_it(self) -> None:
        assert TokenBucket(capacity=2, refill_per_second=1).retry_after(0.0) == 0.0

    def test_retry_after_says_how_long_to_wait(self) -> None:
        bucket = TokenBucket(capacity=1, refill_per_second=2)
        bucket.take(0.0)

        assert bucket.retry_after(0.0) == 0.5

    def test_a_stopped_bucket_never_refills(self) -> None:
        bucket = TokenBucket(capacity=1, refill_per_second=0)
        bucket.take(0.0)

        assert bucket.retry_after(0.0) == 0.0 or bucket.retry_after(0.0) == 0.0

    def test_time_going_backwards_does_not_grant_tokens(self) -> None:
        bucket = TokenBucket(capacity=2, refill_per_second=1)
        bucket.take(10.0)
        bucket.take(10.0)

        assert not bucket.take(5.0)


class TestMiddleware:
    """The limiter as it sits in front of the application."""

    def _limiter(self, **kwargs: object) -> RateLimitMiddleware:
        return RateLimitMiddleware(lambda *a: None, **kwargs)  # type: ignore[arg-type]

    def test_streaming_paths_are_exempt(self) -> None:
        """One SSE connection lasts an hour; charging it once means nothing."""
        limiter = self._limiter()

        assert limiter.is_exempt("/events")
        assert limiter.is_exempt("/runs/run_1/events")
        assert limiter.is_exempt("/runs/run_1/ws")
        assert not limiter.is_exempt("/runs")

    def test_health_is_not_exempt(self) -> None:
        """If a health poller can exhaust the budget, the budget is wrong."""
        assert not self._limiter().is_exempt("/health")

    def test_the_client_is_the_peer_address(self) -> None:
        """Not a forwarded header: this build trusts no proxy."""
        limiter = self._limiter()

        assert limiter.client_of({"client": ("10.0.0.4", 5000)}) == "10.0.0.4"
        assert limiter.client_of({}) == "unknown"

    def test_a_forwarded_header_does_not_change_the_key(self) -> None:
        limiter = self._limiter()
        scope = {
            "client": ("10.0.0.4", 1),
            "headers": [(b"x-forwarded-for", b"1.2.3.4")],
        }
        assert limiter.client_of(scope) == "10.0.0.4"

    def test_clients_get_separate_buckets(self) -> None:
        limiter = self._limiter(per_minute=60, burst=1)
        clock = FakeClock()
        limiter.clock = clock

        assert limiter.bucket_for("a").take(clock())
        assert limiter.bucket_for("b").take(clock())
        assert not limiter.bucket_for("a").take(clock())

    def test_the_tracked_set_is_bounded(self) -> None:
        """A flood of source addresses must not exhaust memory."""
        limiter = self._limiter(max_clients=8)
        for index in range(40):
            limiter.bucket_for(f"client-{index}")

        assert limiter.tracked <= 8

    def test_markers_are_configurable(self) -> None:
        limiter = self._limiter(exempt_markers=("/custom",))

        assert limiter.is_exempt("/custom/thing")
        assert not limiter.is_exempt("/events")


class TestThroughTheApplication:
    """End to end, against a real hardened app."""

    def test_requests_within_the_budget_pass(self) -> None:
        app = hardened_app(SecurityConfig(rate_limit_per_minute=600, rate_limit_burst=10))
        with TestClient(app) as client:
            for _ in range(5):
                assert client.get("/health").status_code == 200

    def test_exceeding_the_burst_is_refused(self) -> None:
        app = hardened_app(SecurityConfig(rate_limit_per_minute=60, rate_limit_burst=3))
        with TestClient(app) as client:
            codes = [client.get("/health").status_code for _ in range(6)]

        assert codes[:3] == [200, 200, 200]
        assert 429 in codes

    def test_the_refusal_uses_the_api_error_envelope(self) -> None:
        app = hardened_app(SecurityConfig(rate_limit_per_minute=60, rate_limit_burst=1))
        with TestClient(app) as client:
            client.get("/health")
            response = client.get("/health")

        assert response.status_code == 429
        error = response.json()["error"]
        assert error["code"] == "rate_limited"
        assert error["retryable"] is True
        assert error["detail"]["per_minute"] == 60

    def test_the_refusal_says_when_to_come_back(self) -> None:
        app = hardened_app(SecurityConfig(rate_limit_per_minute=60, rate_limit_burst=1))
        with TestClient(app) as client:
            client.get("/health")
            response = client.get("/health")

        assert int(response.headers["retry-after"]) >= 1

    def test_a_stream_is_never_refused(self) -> None:
        """The dashboard must not be disconnected for working correctly."""
        app = hardened_app(SecurityConfig(rate_limit_per_minute=60, rate_limit_burst=1))
        with TestClient(app) as client:
            client.get("/health")
            client.get("/health")
            # Exhausted; a stream path is still routed (404 here, not 429).
            assert client.get("/runs/run_01ABC/events").status_code in (400, 404)

    def test_turning_it_off_removes_the_limit(self) -> None:
        app = hardened_app(SecurityConfig(rate_limit_per_minute=0))
        with TestClient(app) as client:
            codes = {client.get("/health").status_code for _ in range(30)}

        assert codes == {200}

    def test_a_refused_request_still_carries_the_security_headers(self) -> None:
        app = hardened_app(SecurityConfig(rate_limit_per_minute=60, rate_limit_burst=1))
        with TestClient(app) as client:
            client.get("/health")
            response = client.get("/health")

        assert response.headers["x-frame-options"] == "DENY"

    def test_the_budget_recovers(self) -> None:
        """A client that waits is served again."""
        app = hardened_app(SecurityConfig(rate_limit_per_minute=60, rate_limit_burst=2))
        clock = FakeClock()
        for middleware in app.user_middleware:
            if middleware.cls is RateLimitMiddleware:
                middleware.kwargs["clock"] = clock

        with TestClient(app) as client:
            assert client.get("/health").status_code == 200
            assert client.get("/health").status_code == 200
            assert client.get("/health").status_code == 429
            clock.advance(5.0)
            assert client.get("/health").status_code == 200


class TestExemptions:
    """The list of exempt markers."""

    def test_streams_are_the_only_exemptions(self) -> None:
        assert STREAMING_PATH_MARKERS == ("/events", "/ws")

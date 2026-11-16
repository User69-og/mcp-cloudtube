"""Retry policy, deadlines and the circuit breaker."""

from __future__ import annotations

import time

import pytest

from mcp_cloudtube.config import Settings
from mcp_cloudtube.errors import ErrorCode, ToolError
from mcp_cloudtube.resilience import (
    CircuitBreaker,
    _backoff_delay,
    call_google,
    get_breaker,
    reset_breakers,
)

pytestmark = pytest.mark.anyio


class TestRetryPolicy:
    async def test_transient_failure_is_retried_then_succeeds(self, settings: Settings) -> None:
        calls = {"n": 0}

        def flaky() -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise ToolError(ErrorCode.UPSTREAM_UNAVAILABLE, "503")
            return "ok"

        result = await call_google(
            flaky, operation="test", service="svc", settings=settings
        )
        assert result == "ok"
        assert calls["n"] == 3

    async def test_non_retryable_failure_is_attempted_exactly_once(
        self, settings: Settings
    ) -> None:
        """The property that protects the daily quota."""
        calls = {"n": 0}

        def spent() -> str:
            calls["n"] += 1
            raise ToolError(ErrorCode.QUOTA_EXCEEDED, "quota gone")

        with pytest.raises(ToolError) as caught:
            await call_google(spent, operation="test", service="svc", settings=settings)

        assert caught.value.code == ErrorCode.QUOTA_EXCEEDED
        assert calls["n"] == 1, "a spent quota must never be retried"

    async def test_retries_are_bounded(self, settings: Settings) -> None:
        calls = {"n": 0}

        def always_fails() -> str:
            calls["n"] += 1
            raise ToolError(ErrorCode.NETWORK_ERROR, "down")

        with pytest.raises(ToolError):
            await call_google(
                always_fails, operation="test", service="svc", settings=settings
            )

        assert calls["n"] == settings.max_retries + 1

    async def test_retry_after_causes_a_retry(self, settings: Settings) -> None:
        """Behaviour, not duration -- see TestBackoffDelay for the timing rule.

        An earlier version asserted on elapsed wall-clock time and was flaky
        roughly one run in five: Windows' timer granularity lets a 50 ms sleep
        return a hair under 50 ms. Timing assertions belong on the pure
        function that computes the delay, not on a slept-through call.
        """
        calls = {"n": 0}

        def throttled() -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise ToolError(
                    ErrorCode.RATE_LIMITED, "slow down", retry_after_seconds=0.01
                )
            return "ok"

        result = await call_google(
            throttled, operation="test", service="svc", settings=settings
        )
        assert result == "ok"
        assert calls["n"] == 2


class TestBackoffDelay:
    """The delay calculation, tested deterministically."""

    def test_googles_retry_after_takes_priority_over_backoff(
        self, settings: Settings
    ) -> None:
        settings.backoff_max_seconds = 30.0
        error = ToolError(ErrorCode.RATE_LIMITED, "slow", retry_after_seconds=7.0)
        assert _backoff_delay(0, settings, error) == 7.0

    def test_an_absurd_retry_after_is_capped(self, settings: Settings) -> None:
        """A pathological header must not stall the chat for an hour."""
        settings.backoff_max_seconds = 30.0
        error = ToolError(ErrorCode.RATE_LIMITED, "slow", retry_after_seconds=3600.0)
        assert _backoff_delay(0, settings, error) == 30.0

    def test_jitter_stays_within_the_exponential_ceiling(
        self, settings: Settings
    ) -> None:
        settings.backoff_base_seconds = 1.0
        settings.backoff_max_seconds = 60.0
        error = ToolError(ErrorCode.UPSTREAM_UNAVAILABLE, "503")

        for attempt in range(5):
            ceiling = min(1.0 * (2**attempt), 60.0)
            for _ in range(50):
                delay = _backoff_delay(attempt, settings, error)
                assert 0.0 <= delay <= ceiling

    def test_jitter_actually_varies(self, settings: Settings) -> None:
        """Full jitter, not fixed backoff: synchronised retries are a herd."""
        settings.backoff_base_seconds = 1.0
        settings.backoff_max_seconds = 60.0
        error = ToolError(ErrorCode.UPSTREAM_UNAVAILABLE, "503")
        delays = {_backoff_delay(3, settings, error) for _ in range(50)}
        assert len(delays) > 1

    def test_the_ceiling_is_respected_at_high_attempt_counts(
        self, settings: Settings
    ) -> None:
        settings.backoff_base_seconds = 1.0
        settings.backoff_max_seconds = 10.0
        error = ToolError(ErrorCode.UPSTREAM_UNAVAILABLE, "503")
        assert all(_backoff_delay(20, settings, error) <= 10.0 for _ in range(50))


class TestDeadlines:
    async def test_overall_deadline_stops_the_retry_loop(self, settings: Settings) -> None:
        settings.max_retries = 100
        settings.backoff_base_seconds = 0.05
        settings.backoff_max_seconds = 0.05
        # Isolate the deadline: with the default threshold the circuit breaker
        # would trip first and we would be testing the wrong mechanism.
        settings.breaker_failure_threshold = 10_000

        def always_fails() -> str:
            raise ToolError(ErrorCode.NETWORK_ERROR, "down")

        started = time.monotonic()
        with pytest.raises(ToolError) as caught:
            await call_google(
                always_fails,
                operation="test",
                service="svc",
                settings=settings,
                deadline_seconds=0.3,
            )

        assert caught.value.code == ErrorCode.TIMEOUT
        assert time.monotonic() - started < 2.0, "the deadline must actually bound the wait"

    async def test_slow_call_hits_the_per_attempt_timeout(self, settings: Settings) -> None:
        def slow() -> str:
            time.sleep(2.0)
            return "never"

        with pytest.raises(ToolError) as caught:
            await call_google(
                slow,
                operation="test",
                service="svc",
                settings=settings,
                attempt_timeout=0.05,
                deadline_seconds=0.3,
            )
        assert caught.value.code == ErrorCode.TIMEOUT


class TestCircuitBreaker:
    def test_user_errors_never_trip_it(self) -> None:
        """Otherwise a user searching for missing files disables the server."""
        breaker = CircuitBreaker(name="svc", failure_threshold=2, reset_seconds=60)
        for _ in range(10):
            breaker.record_failure(ToolError(ErrorCode.NOT_FOUND, "missing"))
            breaker.record_failure(ToolError(ErrorCode.INVALID_INPUT, "bad"))
        assert breaker.state == "closed"

    def test_infrastructure_failures_trip_it(self) -> None:
        breaker = CircuitBreaker(name="svc", failure_threshold=3, reset_seconds=60)
        for _ in range(3):
            breaker.record_failure(ToolError(ErrorCode.UPSTREAM_UNAVAILABLE, "503"))
        assert breaker.state == "open"

    def test_an_open_breaker_fails_immediately(self) -> None:
        breaker = CircuitBreaker(name="svc", failure_threshold=1, reset_seconds=60)
        breaker.record_failure(ToolError(ErrorCode.NETWORK_ERROR, "down"))

        started = time.monotonic()
        with pytest.raises(ToolError) as caught:
            breaker.before_call()

        assert time.monotonic() - started < 0.05, "an open breaker must not wait"
        assert caught.value.retryable is True
        assert caught.value.retry_after_seconds is not None

    def test_it_reopens_for_a_probe_after_the_reset_window(self) -> None:
        breaker = CircuitBreaker(name="svc", failure_threshold=1, reset_seconds=0.05)
        breaker.record_failure(ToolError(ErrorCode.NETWORK_ERROR, "down"))
        assert breaker.state == "open"
        time.sleep(0.08)
        assert breaker.state == "half_open"

    def test_success_closes_it(self) -> None:
        breaker = CircuitBreaker(name="svc", failure_threshold=1, reset_seconds=0.01)
        breaker.record_failure(ToolError(ErrorCode.NETWORK_ERROR, "down"))
        breaker.record_success()
        assert breaker.state == "closed"

    async def test_a_tripped_breaker_short_circuits_real_calls(
        self, settings: Settings
    ) -> None:
        reset_breakers()
        settings.breaker_failure_threshold = 2
        settings.max_retries = 0

        def always_fails() -> str:
            raise ToolError(ErrorCode.NETWORK_ERROR, "down")

        for _ in range(2):
            with pytest.raises(ToolError):
                await call_google(
                    always_fails, operation="test", service="flaky", settings=settings
                )

        assert get_breaker("flaky", settings).state == "open"

        calls = {"n": 0}

        def would_succeed() -> str:
            calls["n"] += 1
            return "ok"

        with pytest.raises(ToolError) as caught:
            await call_google(
                would_succeed, operation="test", service="flaky", settings=settings
            )

        assert calls["n"] == 0, "the breaker should prevent the call entirely"
        assert "stopped sending it requests" in caught.value.message

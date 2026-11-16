"""Retry, backoff, deadlines and circuit breaking for outbound calls.

Every call to Google goes through `call_google`. Centralising it means the
retry policy is written once and is the same for Drive and YouTube, and that
the policy is testable without a network (see `tests/failure/`).

Three mechanisms, each guarding against a different failure shape:

*   **Bounded retry with exponential backoff and full jitter.** Handles the
    common case: a 503 or a dropped connection that succeeds a moment later.
    Full jitter (sleep uniformly in `[0, computed_backoff]`) rather than
    plain exponential, because several tool calls can be in flight at once and
    synchronised retries are how a brief blip becomes a thundering herd.

*   **Deadlines.** A retry loop without a deadline can hang a chat for
    minutes. Each attempt gets a per-attempt timeout, and the loop as a whole
    gets an overall budget; when the budget is spent we stop even if attempts
    remain.

*   **A circuit breaker per upstream service.** If Drive is comprehensively
    down, the eighth tool call should fail in a millisecond with a clear
    message rather than spend 30 seconds rediscovering it. The breaker also
    protects the daily quota: failed calls still cost units.

Deliberately *not* retried: anything the taxonomy marks non-retryable. In
particular an exhausted daily quota is never retried — retrying a 1600-unit
video insert against a spent quota is how you lose the next day as well.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

import anyio

from .config import Settings
from .errors import ErrorCode, ToolError
from .google_errors import translate
from .logging_setup import get_logger

T = TypeVar("T")

logger = get_logger(__name__)


class BreakerState:
    CLOSED = "closed"      # normal operation
    OPEN = "open"          # failing fast
    HALF_OPEN = "half_open"  # letting one probe through


@dataclass
class CircuitBreaker:
    """One breaker per upstream service.

    Counts consecutive *infrastructure* failures only. A 404 means the user
    asked for a file that isn't there; it says nothing about Google's health
    and must not push the breaker toward open, or ordinary user mistakes would
    disable the server.
    """

    name: str
    failure_threshold: int
    reset_seconds: float
    _failures: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)
    _state: str = field(default=BreakerState.CLOSED, init=False)

    #: Failures that indicate the upstream itself is unhealthy.
    TRIPPING: frozenset[ErrorCode] = field(
        default=frozenset(
            {
                ErrorCode.UPSTREAM_UNAVAILABLE,
                ErrorCode.NETWORK_ERROR,
                ErrorCode.TIMEOUT,
            }
        ),
        init=False,
        repr=False,
    )

    @property
    def state(self) -> str:
        if (
            self._state == BreakerState.OPEN
            and self._opened_at is not None
            and time.monotonic() - self._opened_at >= self.reset_seconds
        ):
            self._state = BreakerState.HALF_OPEN
            logger.info("breaker.half_open", service=self.name)
        return self._state

    def before_call(self) -> None:
        """Raise immediately if the circuit is open."""
        if self.state == BreakerState.OPEN:
            assert self._opened_at is not None
            remaining = self.reset_seconds - (time.monotonic() - self._opened_at)
            raise ToolError(
                ErrorCode.UPSTREAM_UNAVAILABLE,
                f"{self.name} has failed repeatedly and the server has stopped sending it "
                "requests for the moment, so this call failed immediately instead of hanging.",
                remediation=(
                    f"Wait about {max(1, int(remaining))} seconds and try again. If it keeps "
                    "happening, check https://www.google.com/appsstatus for an outage, and "
                    "confirm this machine has working internet access."
                ),
                retryable=True,
                retry_after_seconds=max(1.0, remaining),
                details={"service": self.name, "breaker_state": "open"},
            )

    def record_success(self) -> None:
        if self._state != BreakerState.CLOSED:
            logger.info("breaker.closed", service=self.name)
        self._failures = 0
        self._opened_at = None
        self._state = BreakerState.CLOSED

    def record_failure(self, error: ToolError) -> None:
        if error.code not in self.TRIPPING:
            # A user error; the upstream is fine. Don't count it.
            return
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._state = BreakerState.OPEN
            self._opened_at = time.monotonic()
            logger.warning(
                "breaker.open",
                service=self.name,
                failures=self._failures,
                reset_seconds=self.reset_seconds,
            )


_breakers: dict[str, CircuitBreaker] = {}


def get_breaker(service: str, settings: Settings) -> CircuitBreaker:
    breaker = _breakers.get(service)
    if breaker is None:
        breaker = CircuitBreaker(
            name=service,
            failure_threshold=settings.breaker_failure_threshold,
            reset_seconds=settings.breaker_reset_seconds,
        )
        _breakers[service] = breaker
    return breaker


def reset_breakers() -> None:
    """Test hook: forget all breaker state between cases."""
    _breakers.clear()


def _backoff_delay(attempt: int, settings: Settings, error: ToolError) -> float:
    """Full-jitter exponential backoff, with Google's Retry-After taking priority."""
    if error.retry_after_seconds is not None:
        # Google told us when to come back. Respect it, but don't let a
        # pathological value stall the chat.
        return min(error.retry_after_seconds, settings.backoff_max_seconds)
    ceiling = min(settings.backoff_base_seconds * (2**attempt), settings.backoff_max_seconds)
    return random.uniform(0.0, ceiling)


async def call_google(
    fn: Callable[[], T],
    *,
    operation: str,
    service: str,
    settings: Settings,
    deadline_seconds: float | None = None,
    attempt_timeout: float | None = None,
    retry_budget: int | None = None,
) -> T:
    """Run a blocking Google SDK call with the full resilience policy.

    `fn` is a zero-argument callable; it runs in a worker thread so the event
    loop stays responsive while the (synchronous) Google client blocks.

    Raises `ToolError` — never a Google exception.
    """
    breaker = get_breaker(service, settings)
    max_attempts = (settings.max_retries if retry_budget is None else retry_budget) + 1
    overall = deadline_seconds if deadline_seconds is not None else settings.upload_deadline_seconds
    per_attempt = (
        attempt_timeout if attempt_timeout is not None else settings.request_timeout_seconds
    )

    started = time.monotonic()
    last_error: ToolError | None = None

    for attempt in range(max_attempts):
        breaker.before_call()

        elapsed = time.monotonic() - started
        if elapsed >= overall:
            raise ToolError(
                ErrorCode.TIMEOUT,
                f"Gave up trying to {operation}: the overall deadline of {overall:.0f}s "
                f"elapsed after {attempt} attempt(s).",
                details={"operation": operation, "attempts": attempt},
                cause=last_error,
            )

        budget = min(per_attempt, overall - elapsed)

        try:
            with anyio.fail_after(budget):
                result = await anyio.to_thread.run_sync(fn, abandon_on_cancel=True)
        except TimeoutError as exc:
            error = ToolError(
                ErrorCode.TIMEOUT,
                f"Attempt {attempt + 1} to {operation} exceeded {budget:.0f}s.",
                details={"operation": operation, "attempt": attempt + 1},
                cause=exc,
            )
        except BaseException as exc:
            if isinstance(exc, anyio.get_cancelled_exc_class()):
                # The client went away or the session is shutting down.
                # Propagate: this is not a failure to retry.
                raise
            error = translate(exc, operation=operation)
        else:
            breaker.record_success()
            return result

        breaker.record_failure(error)
        last_error = error

        if not error.retryable or attempt == max_attempts - 1:
            logger.warning(
                "call.failed",
                operation=operation,
                service=service,
                code=str(error.code),
                attempts=attempt + 1,
                retryable=error.retryable,
            )
            raise error

        delay = _backoff_delay(attempt, settings, error)
        remaining = overall - (time.monotonic() - started)
        if delay >= remaining:
            raise ToolError(
                ErrorCode.TIMEOUT,
                f"Gave up trying to {operation}: the next retry would exceed the "
                f"{overall:.0f}s deadline.",
                details={"operation": operation, "attempts": attempt + 1},
                cause=error,
            )

        logger.info(
            "call.retrying",
            operation=operation,
            service=service,
            code=str(error.code),
            attempt=attempt + 1,
            sleeping=round(delay, 2),
        )
        await anyio.sleep(delay)

    # Unreachable: the loop either returns or raises.
    raise last_error or ToolError(
        ErrorCode.INTERNAL, f"Retry loop exited without result ({operation})."
    )


def summarise_breakers() -> dict[str, Any]:
    """Breaker states, for the health tool."""
    return {name: breaker.state for name, breaker in _breakers.items()}

"""Tests unitarios de las primitivas de resiliencia."""

from __future__ import annotations

import asyncio
import time

import pytest

from notifier.resilience import (
    BreakerState,
    CircuitBreaker,
    CircuitBreakerOpen,
    SlidingWindowRateLimiter,
    full_jitter_backoff,
)


async def test_rate_limiter_allows_the_burst_up_to_the_limit():
    limiter = SlidingWindowRateLimiter(max_events=5, window_seconds=10.0)
    started = time.monotonic()
    for _ in range(5):
        assert await limiter.acquire() == 0.0
    assert time.monotonic() - started < 0.05


async def test_rate_limiter_blocks_the_event_beyond_the_limit():
    limiter = SlidingWindowRateLimiter(max_events=2, window_seconds=0.3)
    await limiter.acquire()
    await limiter.acquire()
    started = time.monotonic()
    waited = await limiter.acquire()
    elapsed = time.monotonic() - started
    # El tercero sólo puede pasar cuando el primero sale de la ventana de 0,3 s.
    assert waited > 0
    assert 0.25 < elapsed < 0.6


async def test_rate_limiter_never_exceeds_the_limit_under_concurrency():
    """Invariante central: es lo que evita los 429 del provider."""
    limiter = SlidingWindowRateLimiter(max_events=10, window_seconds=0.5)
    timestamps: list[float] = []

    async def worker() -> None:
        await limiter.acquire()
        timestamps.append(time.monotonic())

    await asyncio.gather(*(worker() for _ in range(40)))

    timestamps.sort()
    # En ninguna ventana deslizante de 0,5 s puede haber más de 10 eventos.
    for index in range(len(timestamps) - 10):
        assert timestamps[index + 10] - timestamps[index] >= 0.5 - 0.02


async def test_breaker_opens_after_the_threshold_of_consecutive_failures():
    breaker = CircuitBreaker(failure_threshold=3, reset_seconds=60)
    for _ in range(2):
        breaker.check()
        breaker.record_failure()
    breaker.check()  # sigue cerrado al segundo fallo
    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN
    with pytest.raises(CircuitBreakerOpen):
        breaker.check()


async def test_breaker_counts_consecutive_failures_not_total():
    breaker = CircuitBreaker(failure_threshold=3, reset_seconds=60)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()   # el éxito reinicia la cuenta
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state is BreakerState.CLOSED


async def test_breaker_allows_a_single_probe_in_half_open():
    breaker = CircuitBreaker(failure_threshold=1, reset_seconds=0.05)
    breaker.record_failure()
    await asyncio.sleep(0.06)
    breaker.check()  # el sondeo pasa
    with pytest.raises(CircuitBreakerOpen):
        breaker.check()  # un segundo sondeo simultáneo, no


async def test_breaker_reopens_if_the_probe_fails():
    breaker = CircuitBreaker(failure_threshold=5, reset_seconds=0.05)
    for _ in range(5):
        breaker.record_failure()
    await asyncio.sleep(0.06)
    breaker.check()
    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN
    with pytest.raises(CircuitBreakerOpen):
        breaker.check()


async def test_breaker_can_be_disabled():
    breaker = CircuitBreaker(failure_threshold=1, reset_seconds=60, enabled=False)
    for _ in range(10):
        breaker.record_failure()
    breaker.check()  # no lanza
    assert breaker.state is BreakerState.CLOSED


def test_full_jitter_backoff_stays_within_bounds():
    for attempt in range(1, 10):
        for _ in range(200):
            delay = full_jitter_backoff(attempt, base_delay=0.25, max_delay=5.0)
            assert 0.0 <= delay <= 5.0
            assert delay <= 0.25 * (2 ** (attempt - 1)) + 1e-9


def test_full_jitter_backoff_grows_with_the_attempt_number():
    def mean(attempt: int) -> float:
        return sum(full_jitter_backoff(attempt, 0.1, 100.0) for _ in range(500)) / 500

    assert mean(1) < mean(3) < mean(5)

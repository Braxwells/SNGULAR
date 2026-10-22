"""Primitivas de resiliencia: rate limiter y circuit breaker.

Ambas son asíncronas y pensadas para un único event loop. No usan threads ni locks del SO.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from enum import Enum


class SlidingWindowRateLimiter:
    """Limitador de ventana deslizante: como máximo `max_events` por `window_seconds`.

    Se eligió ventana deslizante y no token bucket porque **espeja el algoritmo exacto del
    provider** (`provider/app.py` descarta timestamps de hace más de 10 s y rechaza al llegar
    a 50). Un token bucket con burst permitiría una ráfaga inicial que el provider
    contabilizaría en su ventana y nos devolvería `429`: es mejor no pedir que ser rechazado,
    porque un `429` cuesta un round-trip y no entrega nada.

    `acquire()` **espera** hasta que haya hueco, en lugar de fallar. El que espera es un
    worker de background, nunca una petición HTTP entrante, así que esa espera no se ve en
    la latencia que mide el scorecard: se traduce en items que siguen en `queued`.
    """

    __slots__ = ("_max_events", "_window", "_events", "_lock")

    def __init__(self, max_events: int, window_seconds: float) -> None:
        self._max_events = max(1, max_events)
        self._window = window_seconds
        self._events: deque[float] = deque()
        # El lock serializa la decisión de "¿hay hueco?" + "reservo el hueco". Sin él, N
        # workers despertando a la vez se concederían el mismo hueco (thundering herd).
        self._lock = asyncio.Lock()

    async def acquire(self) -> float:
        """Reserva un hueco. Devuelve los segundos que se ha esperado."""
        waited = 0.0
        while True:
            async with self._lock:
                now = time.monotonic()
                self._expire(now)
                if len(self._events) < self._max_events:
                    self._events.append(now)
                    return waited
                # El hueco se libera cuando el evento más antiguo sale de la ventana.
                sleep_for = self._window - (now - self._events[0])
            sleep_for = max(sleep_for, 0.0) + 0.005  # margen para no despertar justo antes
            waited += sleep_for
            await asyncio.sleep(sleep_for)

    def _expire(self, now: float) -> None:
        window = self._window
        events = self._events
        while events and now - events[0] >= window:
            events.popleft()

    @property
    def in_window(self) -> int:
        self._expire(time.monotonic())
        return len(self._events)


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerOpen(Exception):
    """El circuito está abierto; no se intenta la llamada."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"circuit open, retry in {retry_after:.2f}s")
        self.retry_after = retry_after


class CircuitBreaker:
    """Circuit breaker de tres estados.

    Importante: **sólo los fallos de transporte y los `5xx` cuentan como fallo.** Un `429` es
    backpressure legítima del provider, no una señal de que esté caído; contarlo abriría el
    circuito en condiciones perfectamente normales de este test.

    El umbral cuenta fallos *consecutivos*: con `FAIL_RATE = 0.1` en el provider, la
    probabilidad de 10 fallos consecutivos aleatorios es 1e-10, así que el circuito sólo se
    abre cuando el provider está realmente roto.
    """

    __slots__ = ("_threshold", "_reset_seconds", "_enabled", "_failures", "_state", "_opened_at", "_probing", "transitions")

    def __init__(self, failure_threshold: int, reset_seconds: float, enabled: bool = True) -> None:
        self._threshold = max(1, failure_threshold)
        self._reset_seconds = reset_seconds
        self._enabled = enabled
        self._failures = 0
        self._state = BreakerState.CLOSED
        self._opened_at = 0.0
        self._probing = False
        self.transitions = 0

    @property
    def state(self) -> BreakerState:
        if not self._enabled:
            return BreakerState.CLOSED
        if self._state is BreakerState.OPEN and self._elapsed_since_open() >= self._reset_seconds:
            return BreakerState.HALF_OPEN
        return self._state

    def check(self) -> None:
        """Lanza `CircuitBreakerOpen` si la llamada no debe intentarse."""
        if not self._enabled:
            return
        if self._state is BreakerState.CLOSED:
            return
        remaining = self._reset_seconds - self._elapsed_since_open()
        if remaining > 0:
            raise CircuitBreakerOpen(remaining)
        # Ventana half-open: se permite exactamente un sondeo en vuelo.
        if self._probing:
            raise CircuitBreakerOpen(self._reset_seconds)
        self._state = BreakerState.HALF_OPEN
        self._probing = True

    def record_success(self) -> None:
        self._failures = 0
        self._probing = False
        if self._state is not BreakerState.CLOSED:
            self._state = BreakerState.CLOSED
            self.transitions += 1

    def record_failure(self) -> None:
        if not self._enabled:
            return
        self._probing = False
        self._failures += 1
        if self._state is BreakerState.HALF_OPEN or self._failures >= self._threshold:
            if self._state is not BreakerState.OPEN:
                self.transitions += 1
            self._state = BreakerState.OPEN
            self._opened_at = time.monotonic()

    def _elapsed_since_open(self) -> float:
        return time.monotonic() - self._opened_at


def full_jitter_backoff(attempt: int, base_delay: float, max_delay: float) -> float:
    """Backoff exponencial con *full jitter*: `uniform(0, min(max, base * 2^(attempt-1)))`.

    Full jitter (el que recomienda el paper de AWS sobre backoff) es preferible al
    exponencial puro porque los N workers que fallan a la vez se desincronizan en lugar de
    reintentar todos en el mismo instante y volver a saturar al provider.
    """
    ceiling = min(max_delay, base_delay * (2 ** max(0, attempt - 1)))
    return random.uniform(0.0, ceiling)

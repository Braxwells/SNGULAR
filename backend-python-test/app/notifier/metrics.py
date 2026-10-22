"""Contadores en proceso, expuestos por `GET /internal/metrics`.

Se implementa a mano en lugar de usar `prometheus-client` para no añadir dependencia a la
imagen. La forma de los datos es deliberadamente plana para que migrar a Prometheus sea
sustituir `inc()` por `Counter.inc()` sin tocar los puntos de llamada.
"""

from __future__ import annotations

from collections import Counter


class Metrics:
    __slots__ = ("_counters", "_provider_latency_sum", "_provider_latency_count", "_rate_limit_wait_sum")

    def __init__(self) -> None:
        self._counters: Counter[str] = Counter()
        self._provider_latency_sum = 0.0
        self._provider_latency_count = 0
        self._rate_limit_wait_sum = 0.0

    def inc(self, name: str, amount: int = 1) -> None:
        self._counters[name] += amount

    def observe_provider_latency(self, seconds: float) -> None:
        self._provider_latency_sum += seconds
        self._provider_latency_count += 1

    def observe_rate_limit_wait(self, seconds: float) -> None:
        self._rate_limit_wait_sum += seconds

    def snapshot(self) -> dict[str, object]:
        mean_latency_ms = (
            round(self._provider_latency_sum / self._provider_latency_count * 1000, 2)
            if self._provider_latency_count
            else 0.0
        )
        return {
            "counters": dict(sorted(self._counters.items())),
            "provider_call_mean_latency_ms": mean_latency_ms,
            "provider_call_count": self._provider_latency_count,
            "rate_limit_wait_total_seconds": round(self._rate_limit_wait_sum, 2),
        }

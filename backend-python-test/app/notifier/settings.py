"""Configuración del servicio, resuelta desde variables de entorno.

Se usa `os.environ` directamente en lugar de `pydantic-settings` para no añadir una
dependencia extra a la imagen: la configuración es plana y se lee una sola vez al arrancar.
Todas las variables llevan el prefijo `NOTIFIER_`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal

OverflowPolicy = Literal["accept_and_fail", "reject"]


def _get_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def _get_int(name: str, default: int) -> int:
    try:
        return int(_get_str(name, str(default)))
    except ValueError:
        return default


def _get_float(name: str, default: float) -> float:
    try:
        return float(_get_str(name, str(default)))
    except ValueError:
        return default


def _get_bool(name: str, default: bool) -> bool:
    return _get_str(name, "1" if default else "0").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    """Parámetros de ejecución. Inmutable: se construye una vez en el lifespan."""

    # ---- Provider externo -------------------------------------------------------------
    # El contenedor `app` comparte el network namespace del `provider` (network_mode:
    # "service:provider"), por eso el default es localhost y no el nombre del servicio.
    provider_base_url: str = field(default_factory=lambda: _get_str("NOTIFIER_PROVIDER_URL", "http://localhost:3001"))
    provider_notify_path: str = field(default_factory=lambda: _get_str("NOTIFIER_PROVIDER_NOTIFY_PATH", "/v1/notify"))
    provider_api_key: str = field(default_factory=lambda: _get_str("NOTIFIER_PROVIDER_API_KEY", "test-dev-2026"))
    provider_priority: str = field(default_factory=lambda: _get_str("NOTIFIER_PROVIDER_PRIORITY", "normal"))

    # Timeouts separados: un `connect` corto detecta un provider caído al instante, mientras
    # que el `read` debe tolerar los 500 ms de latencia simulada más holgura.
    timeout_connect: float = field(default_factory=lambda: _get_float("NOTIFIER_TIMEOUT_CONNECT", 2.0))
    timeout_read: float = field(default_factory=lambda: _get_float("NOTIFIER_TIMEOUT_READ", 5.0))
    timeout_write: float = field(default_factory=lambda: _get_float("NOTIFIER_TIMEOUT_WRITE", 5.0))
    timeout_pool: float = field(default_factory=lambda: _get_float("NOTIFIER_TIMEOUT_POOL", 5.0))

    # ---- Pipeline ---------------------------------------------------------------------
    queue_max_size: int = field(default_factory=lambda: _get_int("NOTIFIER_QUEUE_MAX_SIZE", 100_000))
    queue_overflow_policy: OverflowPolicy = field(
        default_factory=lambda: "reject"
        if _get_str("NOTIFIER_QUEUE_OVERFLOW_POLICY", "accept_and_fail") == "reject"
        else "accept_and_fail"
    )
    worker_count: int = field(default_factory=lambda: _get_int("NOTIFIER_WORKERS", 16))
    provider_max_concurrency: int = field(default_factory=lambda: _get_int("NOTIFIER_PROVIDER_CONCURRENCY", 16))
    shutdown_drain_seconds: float = field(default_factory=lambda: _get_float("NOTIFIER_SHUTDOWN_DRAIN_SECONDS", 5.0))
    # Cuantas veces se puede devolver un item a la cola cuando el circuito estaba abierto
    # y por tanto NO se intento la entrega. Acotado para que no sea un bucle infinito.
    max_redeliveries: int = field(default_factory=lambda: _get_int("NOTIFIER_MAX_REDELIVERIES", 3))
    # Tope de la espera antes de reencolar. La espera real es el minimo entre esto y la
    # ventana del breaker: reencolar sin esperar seria un bucle ocupado que agota el
    # tope de reentregas en milisegundos sin dar tiempo al provider a recuperarse.
    redelivery_max_delay: float = field(default_factory=lambda: _get_float("NOTIFIER_REDELIVERY_MAX_DELAY", 2.0))

    # ---- Rate limiting --------------------------------------------------------------
    # El provider acepta 50 peticiones por ventana deslizante de 10 s. Pedimos 45 para
    # dejar un 10% de margen frente al desfase entre nuestro envío y su contabilización.
    rate_limit_max_events: int = field(default_factory=lambda: _get_int("NOTIFIER_RATE_LIMIT_MAX", 45))
    rate_limit_window_seconds: float = field(default_factory=lambda: _get_float("NOTIFIER_RATE_LIMIT_WINDOW", 10.0))

    # ---- Reintentos -----------------------------------------------------------------
    retry_max_attempts: int = field(default_factory=lambda: _get_int("NOTIFIER_RETRY_MAX_ATTEMPTS", 4))
    retry_base_delay: float = field(default_factory=lambda: _get_float("NOTIFIER_RETRY_BASE_DELAY", 0.25))
    retry_max_delay: float = field(default_factory=lambda: _get_float("NOTIFIER_RETRY_MAX_DELAY", 5.0))

    # ---- Circuit breaker ------------------------------------------------------------
    breaker_failure_threshold: int = field(default_factory=lambda: _get_int("NOTIFIER_BREAKER_THRESHOLD", 10))
    breaker_reset_seconds: float = field(default_factory=lambda: _get_float("NOTIFIER_BREAKER_RESET_SECONDS", 5.0))
    breaker_enabled: bool = field(default_factory=lambda: _get_bool("NOTIFIER_BREAKER_ENABLED", True))

    # ---- Estado / housekeeping ------------------------------------------------------
    repository_max_items: int = field(default_factory=lambda: _get_int("NOTIFIER_REPOSITORY_MAX_ITEMS", 500_000))
    dead_letter_max_items: int = field(default_factory=lambda: _get_int("NOTIFIER_DEAD_LETTER_MAX_ITEMS", 1_000))

    # ---- Observabilidad -------------------------------------------------------------
    log_level: str = field(default_factory=lambda: _get_str("NOTIFIER_LOG_LEVEL", "INFO").upper())

    @property
    def notify_url(self) -> str:
        return f"{self.provider_base_url.rstrip('/')}{self.provider_notify_path}"

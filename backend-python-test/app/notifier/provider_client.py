"""Cliente HTTP del proveedor externo, con rate limiting, reintentos y circuit breaker.

Toda la política de resiliencia está aquí, no en el pipeline: el pipeline sólo orquesta, y
así esta clase se puede testear de forma aislada con un transporte falso.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx

from notifier.metrics import Metrics
from notifier.models import NotificationRequest
from notifier.resilience import (
    CircuitBreaker,
    CircuitBreakerOpen,
    SlidingWindowRateLimiter,
    full_jitter_backoff,
)
from notifier.settings import Settings

logger = logging.getLogger(__name__)

# Códigos que merece la pena reintentar. 429 = backpressure (el hueco se libera solo);
# 5xx = fallo transitorio del provider. Todo lo demás (401, 422...) es un error nuestro:
# reintentarlo sólo gasta cuota y nunca va a funcionar.
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


@dataclass(slots=True)
class DeliveryResult:
    """Resultado de intentar entregar una notificación, tras agotar la política de retry."""

    ok: bool
    attempts: int
    provider_id: str | None = None
    error: str | None = None
    status_code: int | None = None


class ProviderClient:
    """Envoltorio resiliente sobre `POST {provider}/v1/notify`."""

    def __init__(
        self,
        settings: Settings,
        rate_limiter: SlidingWindowRateLimiter,
        breaker: CircuitBreaker,
        metrics: Metrics,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._rate_limiter = rate_limiter
        self._breaker = breaker
        self._metrics = metrics
        self._client = client
        self._owns_client = client is None
        # Desacopla la concurrencia contra el provider del número de workers: se pueden
        # tener muchos workers esperando en el rate limiter y pocas conexiones simultáneas.
        self._semaphore = asyncio.Semaphore(max(1, settings.provider_max_concurrency))
        # Las cabeceras viajan en CADA petición, no como defaults del AsyncClient. Si
        # vivieran en el cliente, inyectar un cliente propio (tests, o un cliente compartido
        # con otro servicio) perdería la autenticación en silencio. Un fallo de auth que
        # sólo aparece según quién construyó el cliente es exactamente lo que no queremos.
        self._headers = {
            "X-API-Key": settings.provider_api_key,
            "Content-Type": "application/json",
            "User-Agent": "singular-notification-service/1.0",
        }

    # ---- ciclo de vida -------------------------------------------------------------
    async def start(self) -> None:
        if self._client is not None:
            return
        settings = self._settings
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=settings.timeout_connect,
                read=settings.timeout_read,
                write=settings.timeout_write,
                pool=settings.timeout_pool,
            ),
            # Keep-alive: sin esto pagaríamos un handshake TCP por notificación.
            limits=httpx.Limits(
                max_connections=settings.provider_max_concurrency,
                max_keepalive_connections=settings.provider_max_concurrency,
                keepalive_expiry=30.0,
            ),
        )

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    # ---- entrega -------------------------------------------------------------------
    async def deliver(self, request: NotificationRequest) -> DeliveryResult:
        """Entrega la notificación aplicando la política completa de resiliencia."""
        settings = self._settings
        payload = {"to": request.to, "message": request.message, "type": request.type.value}
        params = {"trace_id": request.id, "priority": settings.provider_priority}

        last_error = "not attempted"
        last_status: int | None = None
        attempts = 0

        for attempt in range(1, settings.retry_max_attempts + 1):
            # 1) ¿Permite el circuito intentarlo?
            try:
                self._breaker.check()
            except CircuitBreakerOpen as exc:
                self._metrics.inc("provider_skipped_circuit_open")
                last_error = "circuit_open"
                if attempt == settings.retry_max_attempts:
                    break
                await asyncio.sleep(min(exc.retry_after, settings.retry_max_delay))
                continue

            # 2) Rate limiting: espeja la ventana del provider para no provocar 429.
            waited = await self._rate_limiter.acquire()
            if waited:
                self._metrics.observe_rate_limit_wait(waited)

            # 3) Llamada.
            attempts = attempt
            self._metrics.inc("provider_attempts")
            retryable, last_error, last_status, result = await self._attempt(payload, params, attempt)
            if result is not None:
                result.attempts = attempts
                return result
            if not retryable:
                break
            if attempt < settings.retry_max_attempts:
                self._metrics.inc("provider_retries")
                await asyncio.sleep(self._retry_delay(attempt, last_status))

        self._metrics.inc("provider_exhausted")
        return DeliveryResult(ok=False, attempts=attempts, error=last_error, status_code=last_status)

    async def _attempt(
        self, payload: dict[str, str], params: dict[str, str], attempt: int
    ) -> tuple[bool, str, int | None, DeliveryResult | None]:
        """Un único intento. Devuelve `(reintentable, error, status, resultado_final)`.

        `resultado_final` es `None` cuando hay que continuar el bucle de reintentos.
        """
        assert self._client is not None, "ProviderClient.start() no ha sido llamado"
        started = time.perf_counter()
        try:
            async with self._semaphore:
                response = await self._client.post(
                    self._settings.notify_url,
                    json=payload,
                    params=params,
                    headers=self._headers,
                )
        except httpx.TimeoutException as exc:
            # Un timeout suele significar provider saturado: cuenta como fallo para el
            # breaker, porque seguir martilleándolo sólo lo empeora.
            self._breaker.record_failure()
            self._metrics.inc("provider_timeouts")
            logger.warning("provider timeout on attempt %d: %s", attempt, exc)
            return True, f"timeout: {type(exc).__name__}", None, None
        except httpx.HTTPError as exc:
            self._breaker.record_failure()
            self._metrics.inc("provider_transport_errors")
            logger.warning("provider transport error on attempt %d: %s", attempt, exc)
            return True, f"transport: {type(exc).__name__}", None, None
        except Exception as exc:  # defensa en profundidad: nada debe escapar de aquí
            self._breaker.record_failure()
            self._metrics.inc("provider_unexpected_errors")
            logger.exception("unexpected error calling provider on attempt %d", attempt)
            return True, f"unexpected: {type(exc).__name__}", None, None
        finally:
            self._metrics.observe_provider_latency(time.perf_counter() - started)

        status = response.status_code

        if status == 200:
            self._breaker.record_success()
            self._metrics.inc("provider_success")
            success = DeliveryResult(
                ok=True,
                attempts=attempt,
                provider_id=self._extract_provider_id(response),
                status_code=status,
            )
            return False, "", status, success

        if status == 429:
            # Backpressure, no avería: NO cuenta para el circuit breaker.
            self._metrics.inc("provider_rate_limited")
            return True, "provider rate limited (429)", status, None

        if status in _RETRYABLE_STATUS:
            self._breaker.record_failure()
            self._metrics.inc("provider_server_errors")
            return True, f"provider error ({status})", status, None

        # 4xx no reintentable: el provider está sano; el problema es la petición o la clave.
        self._metrics.inc("provider_client_errors")
        if status in (401, 403):
            logger.error(
                "provider rejected our API key (%d). Revisa NOTIFIER_PROVIDER_API_KEY", status
            )
        else:
            logger.warning("provider returned non-retryable %d: %s", status, response.text[:200])
        return False, f"non-retryable provider response ({status})", status, None

    def _retry_delay(self, attempt: int, status: int | None) -> float:
        settings = self._settings
        if status == 429:
            # Con 429 el rate limiter ya nos frena en el siguiente intento, así que basta un
            # backoff corto para no quemar la cuota de intentos esperando de más.
            return min(settings.retry_base_delay, settings.retry_max_delay)
        return full_jitter_backoff(attempt, settings.retry_base_delay, settings.retry_max_delay)

    @staticmethod
    def _extract_provider_id(response: httpx.Response) -> str | None:
        """Lee `provider_id` sin que un cuerpo inesperado convierta un éxito en un fallo."""
        try:
            body = response.json()
        except ValueError:
            logger.warning("provider returned 200 with a non-JSON body")
            return None
        if isinstance(body, dict):
            value = body.get("provider_id")
            return str(value) if value is not None else None
        return None

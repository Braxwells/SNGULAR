"""Cliente HTTP para el provider (motor IA + notificaciones).

Distingue entre errores transitorios (timeouts, errores de conexión, 429, 5xx),
que se reintentan con backoff exponencial + jitter, y errores permanentes
(401, 422, otros 4xx), que no tiene sentido reintentar.
"""
import logging
import random
from typing import Callable, Dict, List, Optional

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type

from .config import settings
from .guardrails import Notification
from .rate_limiter import SlidingWindowRateLimiter

logger = logging.getLogger("notification_service.provider")


class ProviderError(Exception):
    pass


class TransientProviderError(ProviderError):
    def __init__(self, message: str, retry_after: Optional[float] = None, rate_limited: bool = False) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.rate_limited = rate_limited


class PermanentProviderError(ProviderError):
    pass


def _last_exception(retry_state) -> Optional[BaseException]:
    return retry_state.outcome.exception() if retry_state.outcome else None


def _backoff(retry_state) -> float:
    """Backoff exponencial con jitter; respeta Retry-After si llega.

    Un 429 usa una escala mayor (1s, 2s, 4s, 8s...) para que la espera acumulada
    supere la ventana del rate limit del provider (10s).
    """
    exc = _last_exception(retry_state)
    if isinstance(exc, TransientProviderError) and exc.retry_after is not None:
        return exc.retry_after
    rate_limited = isinstance(exc, TransientProviderError) and exc.rate_limited
    base, max_wait = (1.0, settings.notify_rate_window) if rate_limited else (0.5, 8.0)
    cap = min(max_wait, base * (2 ** (retry_state.attempt_number - 1)))
    return random.uniform(cap / 2, cap)


def _stop(max_attempts: int, rate_limited_budget: float):
    """Errores normales: límite de intentos. 429: límite de tiempo.

    Un 429 no indica que la petición sea mala, solo que hay que esperar; tirar
    a la basura una extracción IA ya pagada por un pico de tráfico es caro.
    """
    def stop(retry_state) -> bool:
        exc = _last_exception(retry_state)
        if isinstance(exc, TransientProviderError) and exc.rate_limited:
            return retry_state.seconds_since_start >= rate_limited_budget
        return retry_state.attempt_number >= max_attempts
    return stop


def _raise_for_status(response: httpx.Response, operation: str) -> None:
    if response.status_code < 400:
        return
    detail = f"{operation} returned HTTP {response.status_code}"
    if response.status_code == 429 or response.status_code >= 500:
        retry_after = response.headers.get("Retry-After")
        try:
            parsed = float(retry_after) if retry_after else None
        except ValueError:
            parsed = None
        raise TransientProviderError(detail, retry_after=parsed, rate_limited=response.status_code == 429)
    raise PermanentProviderError(detail)


class ProviderClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http
        self._notify_limiter = SlidingWindowRateLimiter(settings.notify_rate_limit, settings.notify_rate_window)

    def _retrying(self, attempts: int) -> AsyncRetrying:
        return AsyncRetrying(
            stop=_stop(attempts, settings.rate_limited_retry_budget),
            wait=_backoff,
            retry=retry_if_exception_type(TransientProviderError),
            reraise=True,
        )

    async def extract(self, messages: List[Dict[str, str]]) -> str:
        """Llama al motor IA y devuelve el `content` del primer choice."""
        async for attempt in self._retrying(settings.ai_max_transport_attempts):
            with attempt:
                try:
                    response = await self._http.post(
                        "/v1/ai/extract", json={"messages": messages}, timeout=settings.ai_timeout
                    )
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    raise TransientProviderError(f"AI transport error: {exc!r}") from exc
                _raise_for_status(response, "AI extract")
                try:
                    body = response.json()
                    content = body["choices"][0]["message"]["content"]
                except (ValueError, KeyError, IndexError, TypeError):
                    # Respuesta con estructura inesperada: se trata como vacía y
                    # los guardrails decidirán si se vuelve a preguntar.
                    logger.warning("AI response with unexpected structure: %.200s", response.text)
                    return ""
                return content if isinstance(content, str) else ""
        raise AssertionError("unreachable")

    async def notify(
        self,
        notification: Notification,
        trace_id: str,
        on_attempt: Optional[Callable[[], None]] = None,
    ) -> dict:
        async for attempt in self._retrying(settings.notify_max_attempts):
            with attempt:
                # Cada intento consume un hueco del rate limit local, así no
                # malgastamos peticiones que el provider rechazaría con 429.
                await self._notify_limiter.acquire()
                if on_attempt:
                    on_attempt()
                try:
                    response = await self._http.post(
                        "/v1/notify",
                        json=notification.model_dump(),
                        params={"trace_id": trace_id},
                        timeout=settings.notify_timeout,
                    )
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    raise TransientProviderError(f"notify transport error: {exc!r}") from exc
                _raise_for_status(response, "notify")
                # La notificación ya se entregó: un cuerpo inesperado no debe
                # convertirla en fallo.
                try:
                    body = response.json()
                except ValueError:
                    return {}
                return body if isinstance(body, dict) else {}
        raise AssertionError("unreachable")

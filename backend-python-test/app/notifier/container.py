"""Composición de dependencias (*composition root*).

Un único sitio donde se construye el grafo de objetos. Sustituir el repositorio en memoria
por uno persistente, o el rate limiter local por uno distribuido en Redis, es cambiar una
línea aquí y nada más.
"""

from __future__ import annotations

import logging

import httpx

from notifier.metrics import Metrics
from notifier.pipeline import Dispatcher
from notifier.provider_client import ProviderClient
from notifier.repository import InMemoryNotificationRepository
from notifier.resilience import CircuitBreaker, SlidingWindowRateLimiter
from notifier.settings import Settings

logger = logging.getLogger(__name__)


class Container:
    """Mantiene los componentes vivos y gestiona su arranque y parada."""

    __slots__ = ("settings", "metrics", "repository", "rate_limiter", "breaker", "provider", "dispatcher")

    def __init__(
        self, settings: Settings | None = None, http_client: httpx.AsyncClient | None = None
    ) -> None:
        # `http_client` permite inyectar un transporte falso en los tests sin tocar red.
        self.settings = settings or Settings()
        self.metrics = Metrics()
        self.repository = InMemoryNotificationRepository(max_items=self.settings.repository_max_items)
        self.rate_limiter = SlidingWindowRateLimiter(
            max_events=self.settings.rate_limit_max_events,
            window_seconds=self.settings.rate_limit_window_seconds,
        )
        self.breaker = CircuitBreaker(
            failure_threshold=self.settings.breaker_failure_threshold,
            reset_seconds=self.settings.breaker_reset_seconds,
            enabled=self.settings.breaker_enabled,
        )
        self.provider = ProviderClient(
            settings=self.settings,
            rate_limiter=self.rate_limiter,
            breaker=self.breaker,
            metrics=self.metrics,
            client=http_client,
        )
        self.dispatcher = Dispatcher(
            repository=self.repository,
            provider=self.provider,
            settings=self.settings,
            metrics=self.metrics,
        )

    async def start(self) -> None:
        await self.provider.start()
        await self.dispatcher.start()
        logger.info(
            "notification service ready | provider=%s workers=%d rate_limit=%d/%.0fs queue_max=%d",
            self.settings.notify_url,
            self.settings.worker_count,
            self.settings.rate_limit_max_events,
            self.settings.rate_limit_window_seconds,
            self.settings.queue_max_size,
        )

    async def stop(self) -> None:
        # Orden importante: primero se para de consumir, luego se cierra el cliente HTTP.
        # Al revés, los workers en vuelo fallarían contra un cliente ya cerrado.
        await self.dispatcher.stop()
        await self.provider.aclose()

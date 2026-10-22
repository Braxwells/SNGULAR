"""Fixtures compartidas.

Principio: **ningún test toca la red.** El provider se sustituye por un `httpx.MockTransport`
programable, así los tests son deterministas y rápidos, y se pueden forzar escenarios
(100% de 500, 429 permanente, timeouts) que con el provider real serían azarosos.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import httpx
import pytest
from fastapi.testclient import TestClient

from notifier.app_factory import create_app
from notifier.container import Container
from notifier.settings import Settings


@dataclass
class FakeProvider:
    """Provider simulado y programable.

    `responses` es una lista de códigos a devolver en orden; el último se repite al agotarse.
    """

    responses: list[int] = field(default_factory=lambda: [200])
    latency: float = 0.0
    calls: list[dict] = field(default_factory=list)
    _index: int = 0

    def transport(self) -> httpx.MockTransport:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.calls.append(
                {
                    "url": str(request.url),
                    "api_key": request.headers.get("X-API-Key"),
                    "body": json.loads(request.content or b"{}"),
                }
            )
            if self.latency:
                import anyio

                await anyio.sleep(self.latency)
            code = self.responses[min(self._index, len(self.responses) - 1)]
            self._index += 1
            if code == 200:
                return httpx.Response(200, json={"status": "delivered", "provider_id": "p-1234"})
            if code == 599:  # código centinela: simula un fallo de transporte
                raise httpx.ConnectError("simulated connection failure", request=request)
            if code == 598:  # código centinela: simula un timeout de lectura
                raise httpx.ReadTimeout("simulated read timeout", request=request)
            return httpx.Response(code, json={"error": f"simulated {code}"})

        return httpx.MockTransport(handler)

    @property
    def call_count(self) -> int:
        return len(self.calls)


def build_settings(**overrides) -> Settings:
    """Settings con valores rápidos y deterministas para test."""
    defaults = {
        "worker_count": 4,
        "provider_max_concurrency": 4,
        "rate_limit_max_events": 1000,       # desactivado salvo que el test lo ajuste
        "rate_limit_window_seconds": 1.0,
        "retry_max_attempts": 3,
        "retry_base_delay": 0.001,
        "retry_max_delay": 0.01,
        "breaker_failure_threshold": 3,
        "breaker_reset_seconds": 0.05,
        "queue_max_size": 1000,
        "shutdown_drain_seconds": 2.0,
        "log_level": "WARNING",
    }
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture
def fake_provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def make_client(fake_provider: FakeProvider):
    """Devuelve una factoría de `TestClient` con el provider falso ya inyectado.

    Se usa `TestClient` como gestor de contexto para que corra el `lifespan`: así los
    workers del dispatcher existen de verdad durante el test.
    """
    created: list[TestClient] = []

    def _make(**setting_overrides) -> TestClient:
        settings = build_settings(**setting_overrides)
        http_client = httpx.AsyncClient(transport=fake_provider.transport())
        container = Container(settings=settings, http_client=http_client)
        client = TestClient(create_app(container=container))
        client.__enter__()
        created.append(client)
        return client

    yield _make

    for client in reversed(created):
        client.__exit__(None, None, None)


def wait_for_status(client: TestClient, request_id: str, expected: set[str], timeout: float = 5.0) -> str:
    """Hace polling del estado hasta que entra en `expected` o se agota el tiempo.

    Es la forma honesta de testear un pipeline asíncrono: no se asume un tiempo fijo de
    procesamiento, se espera al resultado observable por la API pública.
    """
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        response = client.get(f"/v1/requests/{request_id}")
        assert response.status_code == 200
        last = response.json()["status"]
        if last in expected:
            return last
        time.sleep(0.02)
    raise AssertionError(f"status {last!r} nunca alcanzó {expected} en {timeout}s")

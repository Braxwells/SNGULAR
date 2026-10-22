"""Fábrica de la aplicación FastAPI: lifespan, manejadores de error y wiring de routers."""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from notifier.api import internal_router, public_router
from notifier.container import Container
from notifier.settings import Settings

logger = logging.getLogger(__name__)


def configure_logging(level: str) -> None:
    """Logging a stdout, idempotente.

    Nota de rendimiento: a ~133 req/s, un log por petición cuesta syscalls de escritura
    dentro del event loop y ensancha el p99. Por eso el nivel por omisión es INFO (que aquí
    sólo registra arranque/parada e incidencias) y el access log de uvicorn se desactiva en
    el Dockerfile.
    """
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s %(name)s | %(message)s")
        )
        root.addHandler(handler)
    root.setLevel(getattr(logging, level, logging.INFO))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Arranca el pipeline al levantar el proceso y lo drena al apagarlo.

    Se usa `lifespan` y no `@app.on_event` (deprecado) para garantizar que el
    `httpx.AsyncClient` y las tareas de los workers se crean **dentro** del event loop que
    los va a usar.
    """
    container: Container = app.state.container
    await container.start()
    try:
        yield
    finally:
        await container.stop()


def create_app(settings: Settings | None = None, container: Container | None = None) -> FastAPI:
    """Construye la app. `container` inyectable para que los tests usen dobles de prueba."""
    resolved = container or Container(settings)
    configure_logging(resolved.settings.log_level)

    app = FastAPI(
        title="Notification Service (Technical Test)",
        version="1.0.0",
        description=(
            "Mediador entre clientes y un proveedor externo de notificaciones. "
            "Acepta y encola de forma asíncrona; entrega con rate limiting, reintentos "
            "con backoff exponencial y circuit breaker."
        ),
        lifespan=lifespan,
    )
    app.state.container = resolved
    app.include_router(public_router)
    app.include_router(internal_router)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """Red de seguridad: cualquier error no previsto sale como 500 JSON, no como una
        conexión cortada. "Robustez frente a errores inesperados" empieza por aquí.
        """
        resolved.metrics.inc("unhandled_exceptions")
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "internal server error"})

    return app

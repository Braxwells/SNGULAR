"""Routers HTTP.

Regla de oro de este módulo: **ningún handler del contrato público hace I/O ni `await` sobre
algo que pueda bloquear.** Todo lo que puede tardar vive en el pipeline. Es lo que mantiene
la media y el p99 de `/process` planos.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from notifier.container import Container
from notifier.pipeline import SubmitOutcome
from notifier.schemas import (
    CreateRequestBody,
    CreateRequestResponse,
    ErrorResponse,
    RequestDetailResponse,
    RequestStatusResponse,
)

logger = logging.getLogger(__name__)


def get_container(request: Request) -> Container:
    return request.app.state.container


public_router = APIRouter(prefix="/v1", tags=["requests"])
internal_router = APIRouter(tags=["internal"])


@public_router.post(
    "/requests",
    status_code=status.HTTP_201_CREATED,
    response_model=CreateRequestResponse,
    summary="Registrar una solicitud de notificación",
)
async def create_request(
    body: CreateRequestBody, container: Container = Depends(get_container)
) -> CreateRequestResponse:
    """Registra la solicitud en estado `queued`. **No** la envía todavía.

    El envío se dispara con `POST /v1/requests/{id}/process`, tal y como separa el contrato.
    """
    request_obj = container.repository.create(body.to, body.message, body.type)
    container.metrics.inc("requests_created")
    return CreateRequestResponse(id=request_obj.id)


@public_router.post(
    "/requests/{request_id}/process",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RequestStatusResponse,
    responses={404: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
    summary="Lanzar el envío al proveedor externo",
)
async def process_request(
    request_id: str, response: Response, container: Container = Depends(get_container)
) -> RequestStatusResponse:
    """Encola el envío y devuelve `202` de inmediato (idempotente).

    Operación de coste constante: reclama la solicitud y hace un `put_nowait` en la cola.
    La llamada real al proveedor la hace un worker en background.
    """
    outcome = container.dispatcher.submit(request_id)

    if outcome is SubmitOutcome.NOT_FOUND:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request not found")

    if outcome is SubmitOutcome.OVERFLOW and container.settings.queue_overflow_policy == "reject":
        # Semántica HTTP purista: se rechaza con 503 + Retry-After y el cliente reintenta.
        # No es el modo por omisión; ver decisión D4 en documentacion.md.
        response.headers["Retry-After"] = "1"
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="queue is full, retry later"
        )

    stored = container.repository.get(request_id)
    current_status = stored.status if stored is not None else None
    if current_status is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request not found")
    return RequestStatusResponse(id=request_id, status=current_status)


@public_router.get(
    "/requests/{request_id}",
    response_model=RequestStatusResponse,
    responses={404: {"model": ErrorResponse}},
    summary="Consultar el estado de una solicitud",
)
async def get_request(
    request_id: str, container: Container = Depends(get_container)
) -> RequestStatusResponse:
    """Lectura O(1) en memoria. Devuelve exactamente los dos campos del contrato."""
    stored = container.repository.get(request_id)
    if stored is None:
        container.metrics.inc("status_not_found")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request not found")
    return RequestStatusResponse(id=stored.id, status=stored.status)


# --------------------------------------------------------------------------------------
# Endpoints internos. Fuera del contrato del reto, pero imprescindibles para operar y para
# poder justificar con datos las decisiones de diseño.
# --------------------------------------------------------------------------------------


@internal_router.get("/health", summary="Liveness/readiness probe")
async def health(container: Container = Depends(get_container)) -> dict[str, object]:
    """Lo usa el healthcheck de `docker-compose` para que k6 no arranque antes de tiempo.

    Devuelve `200` incluso con el circuito abierto: el provider caído no significa que
    *nosotros* estemos caídos — seguimos aceptando y encolando correctamente. Si devolviese
    `503`, el orquestador nos reiniciaría y perderíamos la cola, empeorando el incidente.
    """
    pipeline_health = container.dispatcher.health()
    return {
        "status": "ok" if pipeline_health["workers_alive"] else "degraded",
        "pipeline": pipeline_health,
        "provider_circuit": container.breaker.state.value,
    }


@internal_router.get("/internal/metrics", summary="Contadores del servicio")
async def metrics(container: Container = Depends(get_container)) -> dict[str, object]:
    snapshot = container.metrics.snapshot()
    snapshot["pipeline"] = container.dispatcher.health()
    snapshot["provider_circuit"] = container.breaker.state.value
    snapshot["provider_circuit_transitions"] = container.breaker.transitions
    snapshot["rate_limiter_in_window"] = container.rate_limiter.in_window
    snapshot["repository_size"] = len(container.repository)
    snapshot["repository_evicted"] = container.repository.evicted
    snapshot["requests_by_status"] = container.repository.count_by_status()
    return snapshot


@internal_router.get(
    "/internal/requests/{request_id}",
    response_model=RequestDetailResponse,
    responses={404: {"model": ErrorResponse}},
    summary="Detalle completo de una solicitud (diagnóstico)",
)
async def request_detail(
    request_id: str, container: Container = Depends(get_container)
) -> RequestDetailResponse:
    stored = container.repository.get(request_id)
    if stored is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request not found")
    return RequestDetailResponse(
        id=stored.id,
        status=stored.status,
        to=stored.to,
        type=stored.type,
        attempts=stored.attempts,
        provider_id=stored.provider_id,
        last_error=stored.last_error,
        dispatched=stored.dispatched,
        created_at=stored.created_at,
        updated_at=stored.updated_at,
    )


@internal_router.get("/internal/dead-letters", summary="Últimas entregas fallidas")
async def dead_letters(
    limit: int = 50, container: Container = Depends(get_container)
) -> dict[str, object]:
    items = container.dispatcher.dead_letters(limit=max(1, min(limit, 500)))
    return {"count": len(items), "items": items}

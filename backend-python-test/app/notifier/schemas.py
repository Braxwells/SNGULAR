"""DTOs de la API pública. Separados del dominio a propósito: el contrato HTTP puede
evolucionar (versionado) sin arrastrar al modelo interno, y al revés.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from notifier.models import NotificationType, RequestStatus


class CreateRequestBody(BaseModel):
    """Cuerpo de `POST /v1/requests`.

    `extra="ignore"`: un cliente que envíe campos de más no recibe un 422. Preferimos ser
    tolerantes en la entrada (ley de Postel) porque un 422 cuenta como fallo de
    disponibilidad y un campo desconocido no nos impide hacer nuestro trabajo.
    """

    model_config = ConfigDict(extra="ignore")

    to: str = Field(min_length=1, max_length=320, examples=["user@example.com"])
    message: str = Field(min_length=1, max_length=4096, examples=["Your code is 1234"])
    type: NotificationType = Field(examples=["email"])


class CreateRequestResponse(BaseModel):
    """Respuesta de `POST /v1/requests`: el contrato sólo exige `id`."""

    id: str


class RequestStatusResponse(BaseModel):
    """Respuesta de `GET /v1/requests/{id}` y de `POST /v1/requests/{id}/process`.

    Estrictamente los dos campos del contrato. El detalle completo (intentos, error,
    provider_id) se expone aparte en `/internal/requests/{id}` para no contaminar el
    contrato público con campos que el evaluador no espera.
    """

    id: str
    status: RequestStatus


class RequestDetailResponse(BaseModel):
    """Vista de diagnóstico, fuera del contrato público."""

    id: str
    status: RequestStatus
    to: str
    type: NotificationType
    attempts: int
    provider_id: str | None
    last_error: str | None
    dispatched: bool
    created_at: float
    updated_at: float


class ErrorResponse(BaseModel):
    detail: str

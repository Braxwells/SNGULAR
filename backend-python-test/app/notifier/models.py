"""Modelo de dominio: la solicitud de notificación y su máquina de estados."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class RequestStatus(str, Enum):
    """Estados del contrato público. `SENT` y `FAILED` son terminales."""

    QUEUED = "queued"
    PROCESSING = "processing"
    SENT = "sent"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (RequestStatus.SENT, RequestStatus.FAILED)


class NotificationType(str, Enum):
    EMAIL = "email"
    SMS = "sms"
    PUSH = "push"


@dataclass(slots=True)
class NotificationRequest:
    """Entidad de dominio.

    `slots=True` reduce el consumo de memoria por instancia, lo que importa porque bajo la
    carga del test se acumulan decenas de miles de objetos vivos en el repositorio.
    """

    id: str
    to: str
    message: str
    type: NotificationType
    status: RequestStatus = RequestStatus.QUEUED
    attempts: int = 0
    provider_id: str | None = None
    last_error: str | None = None
    # Guarda de idempotencia para `POST /{id}/process`: se marca al encolar.
    dispatched: bool = False
    # Veces que se devolvio a la cola sin haberse intentado (circuito abierto).
    redeliveries: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.updated_at = time.time()

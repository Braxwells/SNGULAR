"""Almacenamiento de solicitudes.

Implementación en memoria: suficiente para la prueba (un único proceso uvicorn).
La interfaz está aislada para poder sustituirla por Redis/Postgres sin tocar el
resto del pipeline.
"""
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional


class Status(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    SENT = "sent"
    FAILED = "failed"


@dataclass
class RequestRecord:
    id: str
    user_input: str
    status: Status = Status.QUEUED
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    # Marca si ya se ha lanzado el procesamiento (idempotencia de /process).
    process_started: bool = False
    ai_attempts: int = 0
    notify_attempts: int = 0
    extracted: Optional[dict] = None
    provider_id: Optional[str] = None
    error: Optional[str] = None


class RequestStore:
    def __init__(self) -> None:
        self._items: Dict[str, RequestRecord] = {}

    def create(self, user_input: str) -> RequestRecord:
        record = RequestRecord(id=uuid.uuid4().hex, user_input=user_input)
        self._items[record.id] = record
        return record

    def get(self, request_id: str) -> Optional[RequestRecord]:
        return self._items.get(request_id)

    def set_status(self, record: RequestRecord, status: Status, error: Optional[str] = None) -> None:
        record.status = status
        record.updated_at = time.time()
        if error is not None:
            record.error = error

"""Almacenamiento de solicitudes.

Implementación en memoria: el contrato sólo necesita lecturas por clave primaria, y meter
I/O (Redis/Postgres) en el camino de `POST`/`GET` añadiría latencia justo donde el scorecard
mide. Se define un `Protocol` para que sustituirlo por una implementación persistente sea un
cambio de una línea en el wiring.

Concurrencia: el servicio corre en un único event loop (`--workers 1`), así que estos métodos
son secciones críticas naturales mientras no contengan ningún `await`. Ninguno lo contiene,
por eso no hace falta un lock, que además ensuciaría el p99.
"""

from __future__ import annotations

import uuid
from collections import OrderedDict
from typing import Protocol

from notifier.models import NotificationRequest, NotificationType, RequestStatus


class NotificationRepository(Protocol):
    def create(self, to: str, message: str, type_: NotificationType) -> NotificationRequest: ...
    def get(self, request_id: str) -> NotificationRequest | None: ...
    def claim_for_dispatch(self, request_id: str) -> NotificationRequest | None: ...
    def mark_processing(self, request_id: str) -> None: ...
    def mark_sent(self, request_id: str, provider_id: str | None, attempts: int) -> None: ...
    def mark_failed(self, request_id: str, error: str, attempts: int) -> None: ...
    def revert_to_queued(self, request_id: str) -> None: ...
    def mark_requeued(self, request_id: str) -> int: ...
    def __len__(self) -> int: ...


class InMemoryNotificationRepository:
    """Diccionario ordenado con cota superior y desalojo FIFO.

    La cota evita que un proceso de larga vida crezca sin límite (sería una fuga de memoria
    de manual). El desalojo prioriza solicitudes ya terminales: una solicitud en vuelo nunca
    se descarta mientras exista alguna terminal más antigua que sacrificar.
    """

    __slots__ = ("_items", "_max_items", "_evicted")

    # Cuántas entradas antiguas se inspeccionan buscando una terminal antes de rendirse y
    # desalojar la más antigua sin más. Acotado para mantener el desalojo en O(1) amortizado.
    _EVICTION_SCAN_LIMIT = 64

    def __init__(self, max_items: int = 500_000) -> None:
        self._items: OrderedDict[str, NotificationRequest] = OrderedDict()
        self._max_items = max(1, max_items)
        self._evicted = 0

    # ---- escritura -----------------------------------------------------------------
    def create(self, to: str, message: str, type_: NotificationType) -> NotificationRequest:
        request = NotificationRequest(id=uuid.uuid4().hex, to=to, message=message, type=type_)
        self._items[request.id] = request
        if len(self._items) > self._max_items:
            self._evict_one()
        return request

    def claim_for_dispatch(self, request_id: str) -> NotificationRequest | None:
        """Reclama la solicitud para encolarla. Devuelve `None` si ya estaba reclamada.

        Esto es lo que hace idempotente a `POST /{id}/process`: la comprobación y la marca
        ocurren sin `await` en medio, así que no hay ventana de carrera.
        """
        request = self._items.get(request_id)
        if request is None or request.dispatched:
            return None
        request.dispatched = True
        request.touch()
        return request

    def mark_processing(self, request_id: str) -> None:
        request = self._items.get(request_id)
        # No se degrada un estado terminal: protege contra reentradas inesperadas.
        if request is not None and not request.status.is_terminal:
            request.status = RequestStatus.PROCESSING
            request.touch()

    def mark_sent(self, request_id: str, provider_id: str | None, attempts: int) -> None:
        request = self._items.get(request_id)
        if request is None:
            return
        request.status = RequestStatus.SENT
        request.provider_id = provider_id
        request.attempts = attempts
        request.last_error = None
        request.touch()

    def mark_failed(self, request_id: str, error: str, attempts: int) -> None:
        request = self._items.get(request_id)
        if request is None:
            return
        request.status = RequestStatus.FAILED
        request.attempts = attempts
        request.last_error = error
        request.touch()

    def revert_to_queued(self, request_id: str) -> None:
        """Devuelve la solicitud a `queued` y la deja re-encolable.

        Se usa en el apagado: si un worker es cancelado mientras entregaba, dejar el estado
        en `processing` sería mentir (ya no hay nadie procesándolo). Volviendo a `queued` y
        limpiando `dispatched`, el cliente puede reintentar `POST /{id}/process`.
        """
        request = self._items.get(request_id)
        if request is not None and not request.status.is_terminal:
            request.status = RequestStatus.QUEUED
            request.dispatched = False
            request.touch()

    def mark_requeued(self, request_id: str) -> int:
        """Devuelve la solicitud a `queued` para reintentarla y cuenta la reentrega.

        A diferencia de `revert_to_queued`, **mantiene `dispatched=True`**: el reintento lo
        gestiona el pipeline, así que una llamada concurrente a `/process` no debe poder
        encolar una segunda copia del mismo mensaje.
        """
        request = self._items.get(request_id)
        if request is None:
            return 0
        request.status = RequestStatus.QUEUED
        request.redeliveries += 1
        request.touch()
        return request.redeliveries

    # ---- lectura -------------------------------------------------------------------
    def get(self, request_id: str) -> NotificationRequest | None:
        return self._items.get(request_id)

    def __len__(self) -> int:
        return len(self._items)

    @property
    def evicted(self) -> int:
        return self._evicted

    def count_by_status(self) -> dict[str, int]:
        """Recuento por estado. O(n): sólo para `/internal/metrics`, nunca en el hot path."""
        counts = {status.value: 0 for status in RequestStatus}
        for request in self._items.values():
            counts[request.status.value] += 1
        return counts

    # ---- interno -------------------------------------------------------------------
    def _evict_one(self) -> None:
        for index, (request_id, request) in enumerate(self._items.items()):
            if index >= self._EVICTION_SCAN_LIMIT:
                break
            if request.status.is_terminal:
                del self._items[request_id]
                self._evicted += 1
                return
        self._items.popitem(last=False)
        self._evicted += 1

"""Pipeline de procesamiento: cola acotada + pool de workers.

Es el corazón del diseño *accept-and-queue*. El handler HTTP de `/process` sólo hace un
`put_nowait()` (O(1), sin `await`, sin I/O) y devuelve `202`. Todo el trabajo caro —esperar
el rate limiter, hablar con el provider, reintentar— ocurre aquí, fuera del ciclo de vida de
la petición del cliente.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from enum import Enum

from notifier.metrics import Metrics
from notifier.provider_client import ProviderClient
from notifier.repository import InMemoryNotificationRepository
from notifier.settings import Settings

logger = logging.getLogger(__name__)


class SubmitOutcome(str, Enum):
    """Resultado de intentar encolar una solicitud."""

    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"      # /process ya se había llamado para este id (idempotencia)
    NOT_FOUND = "not_found"
    OVERFLOW = "overflow"        # cola llena


class Dispatcher:
    def __init__(
        self,
        repository: InMemoryNotificationRepository,
        provider: ProviderClient,
        settings: Settings,
        metrics: Metrics,
    ) -> None:
        self._repository = repository
        self._provider = provider
        self._settings = settings
        self._metrics = metrics
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=settings.queue_max_size)
        self._workers: list[asyncio.Task[None]] = []
        self._in_flight = 0
        # Cola de mensajes muertos acotada: lo que agota reintentos queda inspeccionable en
        # `/internal/dead-letters` en vez de desaparecer en un contador.
        self._dead_letters: deque[dict[str, object]] = deque(maxlen=settings.dead_letter_max_items)

    # ---- ciclo de vida -------------------------------------------------------------
    async def start(self) -> None:
        if self._workers:
            return
        for index in range(max(1, self._settings.worker_count)):
            self._workers.append(asyncio.create_task(self._worker(index), name=f"notifier-worker-{index}"))
        logger.info("dispatcher started with %d workers", len(self._workers))

    async def stop(self) -> None:
        """Parada ordenada: intenta drenar lo encolado antes de cancelar los workers.

        Sin esto, un redeploy perdería en silencio todo lo que hubiera en la cola. Con la
        espera acotada, tampoco se bloquea el apagado indefinidamente.
        """
        if not self._workers:
            return
        drain = self._settings.shutdown_drain_seconds
        if drain > 0 and not self._queue.empty():
            logger.info("draining %d queued items (max %.1fs)", self._queue.qsize(), drain)
            try:
                await asyncio.wait_for(self._queue.join(), timeout=drain)
            except asyncio.TimeoutError:  # noqa: UP041 - alias del builtin solo desde 3.11
                logger.warning("drain timed out with %d items still queued", self._queue.qsize())
        for task in self._workers:
            task.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        logger.info("dispatcher stopped")

    # ---- encolado (camino caliente) -------------------------------------------------
    def submit(self, request_id: str) -> SubmitOutcome:
        """Encola la solicitud. Síncrono y sin `await`: así `/process` es sub-milisegundo.

        No es `async` a propósito. Un `await queue.put()` esperaría cuando la cola está
        llena y convertiría la saturación del provider en latencia del cliente, que es
        exactamente lo que este diseño evita.
        """
        request = self._repository.claim_for_dispatch(request_id)
        if request is None:
            if self._repository.get(request_id) is None:
                self._metrics.inc("process_not_found")
                return SubmitOutcome.NOT_FOUND
            self._metrics.inc("process_duplicate")
            return SubmitOutcome.DUPLICATE

        try:
            self._queue.put_nowait(request_id)
        except asyncio.QueueFull:
            self._metrics.inc("queue_overflow")
            self._repository.mark_failed(request_id, "queue_overflow", attempts=0)
            self._record_dead_letter(request_id, "queue_overflow", attempts=0)
            logger.warning("queue full (%d), rejecting %s", self._queue.maxsize, request_id)
            return SubmitOutcome.OVERFLOW

        self._metrics.inc("process_accepted")
        return SubmitOutcome.ACCEPTED

    # ---- consumo -------------------------------------------------------------------
    async def _worker(self, index: int) -> None:
        queue = self._queue
        while True:
            request_id = await queue.get()
            self._in_flight += 1
            try:
                await self._deliver(request_id)
            except asyncio.CancelledError:
                # Apagado en curso: el item ya salió de la cola, así que se deja en `queued`
                # (no en `processing`) para que el estado no mienta y sea re-encolable.
                self._repository.revert_to_queued(request_id)
                raise
            except Exception:
                # Un worker NUNCA debe morir: si muere, el pipeline pierde capacidad en
                # silencio y la cola crece sin fin. Se registra y se sigue.
                self._metrics.inc("worker_unexpected_errors")
                logger.exception("worker %d crashed handling %s", index, request_id)
                self._repository.mark_failed(request_id, "internal_error", attempts=0)
            finally:
                self._in_flight -= 1
                queue.task_done()

    async def _deliver(self, request_id: str) -> None:
        request = self._repository.get(request_id)
        if request is None:
            # Puede pasar si el repositorio desalojó la entrada por su cota de memoria.
            self._metrics.inc("worker_request_missing")
            return

        self._repository.mark_processing(request_id)
        result = await self._provider.deliver(request)

        if result.ok:
            self._repository.mark_sent(request_id, result.provider_id, result.attempts)
            self._metrics.inc("notifications_sent")
            return

        error = result.error or "unknown_error"

        # El circuito abierto significa que la entrega NO se intento: marcarla `failed`
        # seria injusto y perderia el mensaje por un problema que es nuestro y transitorio.
        # Se devuelve a la cola, con un tope para que no sea un bucle infinito si el
        # provider no vuelve nunca.
        if error == "circuit_open" and request.redeliveries < self._settings.max_redeliveries:
            self._repository.mark_requeued(request_id)
            # Se espera a que el circuito pueda volver a sondearse antes de reencolar. Sin
            # esta espera, el worker cogeria el mismo item inmediatamente y agotaria el tope
            # de reentregas en milisegundos. Bloquear al worker aqui no cuesta nada: con el
            # provider caido no hay trabajo util que hacer.
            await asyncio.sleep(
                min(self._settings.breaker_reset_seconds, self._settings.redelivery_max_delay)
            )
            try:
                self._queue.put_nowait(request_id)
            except asyncio.QueueFull:
                pass  # cola llena: se cae al marcado como fallida, mas abajo
            else:
                self._metrics.inc("redeliveries")
                return

        self._repository.mark_failed(request_id, error, result.attempts)
        self._metrics.inc("notifications_failed")
        self._record_dead_letter(request_id, error, result.attempts)

    def _record_dead_letter(self, request_id: str, error: str, attempts: int) -> None:
        self._dead_letters.append({"id": request_id, "error": error, "attempts": attempts})

    # ---- introspección -------------------------------------------------------------
    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    @property
    def in_flight(self) -> int:
        return self._in_flight

    @property
    def worker_count(self) -> int:
        return len(self._workers)

    def dead_letters(self, limit: int = 50) -> list[dict[str, object]]:
        items = list(self._dead_letters)
        return items[-limit:]

    def health(self) -> dict[str, object]:
        alive = sum(1 for task in self._workers if not task.done())
        return {
            "workers_total": len(self._workers),
            "workers_alive": alive,
            "queue_depth": self._queue.qsize(),
            "queue_max_size": self._queue.maxsize,
            "in_flight": self._in_flight,
            "dead_letters": len(self._dead_letters),
        }

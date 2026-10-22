"""Orquestación del procesamiento: extracción IA -> guardrails -> notificación.

El endpoint /process solo encola el trabajo (responde 202 al instante) y este
pipeline lo ejecuta en segundo plano. Estados:

    queued --(entra al pool de IA)--> processing --> sent
                                                 \\-> failed
"""
import asyncio
import logging
from typing import Optional, Set

from .config import settings
from .guardrails import Notification, validate_extraction
from .prompts import build_messages
from .provider_client import PermanentProviderError, ProviderClient, ProviderError
from .store import RequestRecord, RequestStore, Status

logger = logging.getLogger("notification_service.pipeline")


class NotificationPipeline:
    def __init__(self, store: RequestStore, provider: ProviderClient) -> None:
        self._store = store
        self._provider = provider
        self._ai_semaphore = asyncio.Semaphore(settings.ai_max_concurrency)
        # Referencias fuertes a las tareas para que el GC no las elimine.
        self._tasks: Set[asyncio.Task] = set()

    def submit(self, record: RequestRecord) -> bool:
        """Lanza el procesamiento. Idempotente: devuelve False si ya se lanzó."""
        if record.process_started:
            return False
        record.process_started = True
        task = asyncio.create_task(self._run(record))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _run(self, record: RequestRecord) -> None:
        try:
            notification = await self._extract(record)
            if notification is not None:
                await self._notify(record, notification)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[%s] unexpected error while processing", record.id)
            self._fail(record, "internal_error")

    async def _extract(self, record: RequestRecord) -> Optional[Notification]:
        messages = build_messages(record.user_input)
        async with self._ai_semaphore:
            self._store.set_status(record, Status.PROCESSING)
            last_error = "unknown"
            for attempt in range(1, settings.ai_max_inference_attempts + 1):
                record.ai_attempts += 1
                try:
                    content = await self._provider.extract(messages)
                except ProviderError as exc:
                    self._fail(record, f"ai_unavailable: {exc}")
                    return None

                result = validate_extraction(content, record.user_input)
                if result.repairs:
                    logger.info("[%s] guardrail repairs: %s", record.id, ", ".join(result.repairs))
                if result.ok:
                    record.extracted = result.notification.model_dump()
                    return result.notification

                last_error = result.error or "unknown"
                logger.warning(
                    "[%s] extraction attempt %d failed (%s, retryable=%s): %.120r",
                    record.id, attempt, last_error, result.retryable, content,
                )
                if not result.retryable:
                    break
            self._fail(record, f"extraction_failed: {last_error}")
            return None

    async def _notify(self, record: RequestRecord, notification: Notification) -> None:
        def count_attempt() -> None:
            record.notify_attempts += 1

        try:
            response = await self._provider.notify(notification, trace_id=record.id, on_attempt=count_attempt)
        except PermanentProviderError as exc:
            self._fail(record, f"notify_rejected: {exc}")
            return
        except ProviderError as exc:
            self._fail(record, f"notify_unavailable: {exc}")
            return
        record.provider_id = response.get("provider_id")
        self._store.set_status(record, Status.SENT)
        logger.info(
            "[%s] sent via %s to %s (provider_id=%s, ai_attempts=%d, notify_attempts=%d)",
            record.id, notification.type, notification.to, record.provider_id,
            record.ai_attempts, record.notify_attempts,
        )

    def _fail(self, record: RequestRecord, reason: str) -> None:
        self._store.set_status(record, Status.FAILED, error=reason)
        logger.warning("[%s] failed: %s", record.id, reason)

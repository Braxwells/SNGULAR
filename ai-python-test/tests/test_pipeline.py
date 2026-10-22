import asyncio
import json

import httpx
import pytest

from service import provider_client
from service.config import settings
from service.pipeline import NotificationPipeline
from service.provider_client import ProviderClient
from service.store import RequestStore, Status

USER_INPUT = "Manda un mail a feda@test.com diciendo hola"
VALID = '{"to": "feda@test.com", "message": "hola", "type": "email"}'
REFUSAL = "Refused: Content analysis flagged sensitive information."


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(provider_client, "_backoff", lambda retry_state: 0)


def ai_response(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}}]})


def run_pipeline(ai_contents, notify_statuses):
    """Ejecuta el pipeline contra un provider simulado y devuelve (record, calls)."""
    ai_queue, notify_queue = list(ai_contents), list(notify_statuses)
    calls = {"ai": 0, "notify": 0, "notify_payloads": []}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-API-Key"] == "test-dev-2026"
        if request.url.path == "/v1/ai/extract":
            calls["ai"] += 1
            return ai_response(ai_queue.pop(0))
        calls["notify"] += 1
        calls["notify_payloads"].append(json.loads(request.content))
        code = notify_queue.pop(0)
        if code == 200:
            return httpx.Response(200, json={"status": "delivered", "provider_id": "p-1"})
        return httpx.Response(code, json={"detail": "error"})

    async def main():
        async with httpx.AsyncClient(
            base_url="http://provider", headers={"X-API-Key": "test-dev-2026"},
            transport=httpx.MockTransport(handler),
        ) as http:
            store = RequestStore()
            pipeline = NotificationPipeline(store, ProviderClient(http))
            record = store.create(USER_INPUT)
            assert pipeline.submit(record)
            assert not pipeline.submit(record)  # idempotente
            while pipeline._tasks:
                await asyncio.sleep(0.01)
            return record

    return asyncio.run(main()), calls


def test_happy_path():
    record, calls = run_pipeline([VALID], [200])
    assert record.status == Status.SENT
    assert record.provider_id == "p-1"
    assert calls["notify_payloads"] == [{"to": "feda@test.com", "message": "hola", "type": "email"}]


def test_refusal_is_retried_once_then_succeeds():
    record, calls = run_pipeline([REFUSAL, VALID], [200])
    assert record.status == Status.SENT
    assert calls["ai"] == 2


def test_refusal_is_retried_until_success():
    record, calls = run_pipeline([REFUSAL] * (settings.ai_max_inference_attempts - 1) + [VALID], [200])
    assert record.status == Status.SENT
    assert calls["ai"] == settings.ai_max_inference_attempts


def test_persistent_refusal_fails_without_notifying():
    record, calls = run_pipeline([REFUSAL] * settings.ai_max_inference_attempts, [])
    assert record.status == Status.FAILED
    assert calls["ai"] == settings.ai_max_inference_attempts
    assert calls["notify"] == 0


def test_insufficient_info_is_not_retried():
    record, calls = run_pipeline(["I was unable to identify the destination (email/phone) in your prompt."], [])
    assert record.status == Status.FAILED
    assert calls["ai"] == 1


def test_notify_retries_transient_errors():
    record, calls = run_pipeline([VALID], [429, 500, 200])
    assert record.status == Status.SENT
    assert calls["notify"] == 3


def test_notify_does_not_retry_permanent_errors():
    record, calls = run_pipeline([VALID], [422])
    assert record.status == Status.FAILED
    assert calls["notify"] == 1
    assert record.error.startswith("notify_rejected")

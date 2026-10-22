import time

import httpx
import pytest
from fastapi.testclient import TestClient

import main

USER_INPUT = "Manda un mail a feda@test.com diciendo hola"
VALID = '{"to": "feda@test.com", "message": "hola", "type": "email"}'


@pytest.fixture
def api(monkeypatch):
    """TestClient con el provider simulado; devuelve (client, calls)."""
    calls = {"ai": 0, "notify": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/ai/extract":
            calls["ai"] += 1
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": VALID}}]})
        calls["notify"] += 1
        return httpx.Response(200, json={"status": "delivered", "provider_id": "p-1"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        main.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)
    )
    with TestClient(main.app) as client:
        yield client, calls


def wait_for_status(client, request_id, expected, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = client.get(f"/v1/requests/{request_id}").json()["status"]
        if current in expected:
            return current
        time.sleep(0.02)
    return current


def test_create_returns_201_with_id(api):
    client, _ = api
    response = client.post("/v1/requests", json={"user_input": USER_INPUT})
    assert response.status_code == 201
    request_id = response.json()["id"]
    assert client.get(f"/v1/requests/{request_id}").json() == {"id": request_id, "status": "queued"}


def test_process_returns_202_and_is_idempotent(api):
    client, calls = api
    request_id = client.post("/v1/requests", json={"user_input": USER_INPUT}).json()["id"]
    first = client.post(f"/v1/requests/{request_id}/process")
    second = client.post(f"/v1/requests/{request_id}/process")
    assert first.status_code == 202 and second.status_code == 202
    assert wait_for_status(client, request_id, {"sent", "failed"}) == "sent"
    # La segunda llamada no relanza el procesamiento.
    assert calls == {"ai": 1, "notify": 1}


@pytest.mark.parametrize("method, path", [("get", "/v1/requests/nope"), ("post", "/v1/requests/nope/process")])
def test_unknown_request_returns_404(api, method, path):
    client, _ = api
    assert getattr(client, method)(path).status_code == 404


@pytest.mark.parametrize("body", [{}, {"user_input": ""}, {"user_input": "   "}, {"user_input": "x" * 2001}])
def test_invalid_body_returns_422(api, body):
    client, _ = api
    assert client.post("/v1/requests", json=body).status_code == 422


def test_health(api):
    client, _ = api
    assert client.get("/health").json() == {"status": "ok"}

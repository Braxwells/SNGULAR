"""Tests del contrato público: exactamente lo que valida la suite k6, más los bordes."""

from __future__ import annotations

from tests.conftest import wait_for_status

VALID_STATUSES = {"queued", "processing", "sent", "failed"}

PAYLOAD = {"to": "user@example.com", "message": "Test notification", "type": "email"}


def test_create_returns_201_with_id(make_client):
    client = make_client()
    response = client.post("/v1/requests", json=PAYLOAD)
    assert response.status_code == 201
    body = response.json()
    assert isinstance(body.get("id"), str) and body["id"]


def test_create_leaves_request_queued_until_processed(make_client):
    client = make_client()
    request_id = client.post("/v1/requests", json=PAYLOAD).json()["id"]
    # El contrato separa registro y envío: sin /process no debe salir nada al provider.
    assert client.get(f"/v1/requests/{request_id}").json()["status"] == "queued"


def test_process_returns_202(make_client):
    client = make_client()
    request_id = client.post("/v1/requests", json=PAYLOAD).json()["id"]
    response = client.post(f"/v1/requests/{request_id}/process")
    assert response.status_code == 202
    assert response.json()["status"] in VALID_STATUSES


def test_full_happy_path_reaches_sent(make_client, fake_provider):
    client = make_client()
    request_id = client.post("/v1/requests", json=PAYLOAD).json()["id"]
    client.post(f"/v1/requests/{request_id}/process")
    assert wait_for_status(client, request_id, {"sent"}) == "sent"

    assert fake_provider.call_count == 1
    call = fake_provider.calls[0]
    assert call["api_key"] == "test-dev-2026"
    assert call["body"] == PAYLOAD
    # El id propio viaja como trace_id: permite correlacionar nuestros logs con los del provider.
    assert f"trace_id={request_id}" in call["url"]


def test_status_endpoint_returns_only_contract_fields(make_client):
    client = make_client()
    request_id = client.post("/v1/requests", json=PAYLOAD).json()["id"]
    body = client.get(f"/v1/requests/{request_id}").json()
    assert set(body) == {"id", "status"}
    assert body["id"] == request_id
    assert body["status"] in VALID_STATUSES


def test_process_is_idempotent(make_client, fake_provider):
    client = make_client()
    request_id = client.post("/v1/requests", json=PAYLOAD).json()["id"]
    first = client.post(f"/v1/requests/{request_id}/process")
    second = client.post(f"/v1/requests/{request_id}/process")
    assert first.status_code == 202 and second.status_code == 202
    wait_for_status(client, request_id, {"sent"})
    # Lo importante: una segunda llamada NO duplica el envío al provider.
    assert fake_provider.call_count == 1
    counters = client.get("/internal/metrics").json()["counters"]
    assert counters["process_duplicate"] == 1


def test_unknown_id_returns_404(make_client):
    client = make_client()
    assert client.get("/v1/requests/does-not-exist").status_code == 404
    assert client.post("/v1/requests/does-not-exist/process").status_code == 404


def test_invalid_payload_is_rejected_with_422(make_client):
    client = make_client()
    assert client.post("/v1/requests", json={"to": "a", "message": "b", "type": "fax"}).status_code == 422
    assert client.post("/v1/requests", json={"to": "a"}).status_code == 422
    assert client.post("/v1/requests", json={"to": "", "message": "b", "type": "sms"}).status_code == 422


def test_unknown_extra_fields_are_tolerated(make_client):
    client = make_client()
    payload = {**PAYLOAD, "callback_url": "https://example.com", "priority": 9}
    assert client.post("/v1/requests", json=payload).status_code == 201


def test_all_notification_types_are_accepted(make_client):
    client = make_client()
    for notification_type in ("email", "sms", "push"):
        response = client.post("/v1/requests", json={**PAYLOAD, "type": notification_type})
        assert response.status_code == 201


def test_health_endpoint_reports_live_workers(make_client):
    client = make_client(worker_count=3)
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["pipeline"]["workers_alive"] == 3

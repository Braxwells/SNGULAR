"""Tests del pipeline: backpressure, aislamiento de fallos y la latencia que mide el scorecard."""

from __future__ import annotations

import statistics
import time

from tests.conftest import wait_for_status

PAYLOAD = {"to": "user@example.com", "message": "m", "type": "email"}


def test_process_stays_fast_even_with_a_slow_provider(make_client, fake_provider):
    """**El test que refleja el panel "INDICADOR DE ARQUITECTURA".**

    Con el provider tardando 500 ms (su peor caso) y un rate limiter que sólo deja pasar
    2 envíos por segundo, `/process` tiene que seguir respondiendo en milisegundos.
    Si alguien reintrodujera la llamada al provider en el handler, este test se cae.
    """
    fake_provider.latency = 0.5
    client = make_client(worker_count=2, rate_limit_max_events=2, rate_limit_window_seconds=1.0)

    ids = [client.post("/v1/requests", json=PAYLOAD).json()["id"] for _ in range(30)]

    durations = []
    for request_id in ids:
        started = time.perf_counter()
        response = client.post(f"/v1/requests/{request_id}/process")
        durations.append((time.perf_counter() - started) * 1000)
        assert response.status_code == 202

    mean_ms = statistics.mean(durations)
    p95_ms = sorted(durations)[int(len(durations) * 0.95) - 1]
    # Umbral del scorecard: 500 ms. Se exige 50 ms para que el test detecte una regresión
    # mucho antes de que el scorecard se ponga en rojo.
    assert mean_ms < 50, f"media de /process = {mean_ms:.1f} ms"
    assert p95_ms < 100, f"p95 de /process = {p95_ms:.1f} ms"


def test_status_remains_a_valid_contract_value_while_the_backlog_drains(make_client, fake_provider):
    """Con la cola congestionada, el estado nunca debe salirse del enum del contrato."""
    fake_provider.latency = 0.2
    client = make_client(worker_count=1, rate_limit_max_events=1, rate_limit_window_seconds=1.0)

    ids = []
    for _ in range(25):
        request_id = client.post("/v1/requests", json=PAYLOAD).json()["id"]
        client.post(f"/v1/requests/{request_id}/process")
        ids.append(request_id)

    for request_id in ids:
        response = client.get(f"/v1/requests/{request_id}")
        assert response.status_code == 200
        assert response.json()["status"] in {"queued", "processing", "sent", "failed"}


def test_queue_overflow_accepts_and_marks_failed_by_default(make_client, fake_provider):
    """Política por omisión: preservar la disponibilidad HTTP, no devolver 503."""
    fake_provider.latency = 5.0  # los workers se quedan ocupados y la cola se llena
    client = make_client(worker_count=1, queue_max_size=2)

    statuses, codes = [], []
    for _ in range(12):
        request_id = client.post("/v1/requests", json=PAYLOAD).json()["id"]
        response = client.post(f"/v1/requests/{request_id}/process")
        codes.append(response.status_code)
        statuses.append(response.json()["status"])

    assert set(codes) == {202}, "bajo saturación nunca se devuelve un código de error"
    assert "failed" in statuses, "las que desbordan quedan en un estado terminal honesto"
    assert client.get("/internal/metrics").json()["counters"]["queue_overflow"] > 0


def test_queue_overflow_can_be_configured_to_reject_with_503(make_client, fake_provider):
    """Modo alternativo: semántica HTTP purista (ver decisión D4 en documentacion.md)."""
    fake_provider.latency = 5.0
    client = make_client(worker_count=1, queue_max_size=2, queue_overflow_policy="reject")

    codes = []
    for _ in range(12):
        request_id = client.post("/v1/requests", json=PAYLOAD).json()["id"]
        codes.append(client.post(f"/v1/requests/{request_id}/process").status_code)

    assert 503 in codes


def test_a_crashing_worker_does_not_kill_the_pipeline(make_client, fake_provider):
    """Un error inesperado en un envío no debe dejar el pipeline sin capacidad."""
    client = make_client(worker_count=1)
    container = client.app.state.container
    original_deliver = container.provider.deliver
    calls = {"n": 0}

    async def exploding_deliver(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom inesperado")
        return await original_deliver(request)

    container.provider.deliver = exploding_deliver  # type: ignore[method-assign]

    first = client.post("/v1/requests", json=PAYLOAD).json()["id"]
    client.post(f"/v1/requests/{first}/process")
    assert wait_for_status(client, first, {"failed"}) == "failed"
    assert client.get(f"/internal/requests/{first}").json()["last_error"] == "internal_error"

    # El worker sigue vivo y procesa la siguiente notificación con normalidad.
    second = client.post("/v1/requests", json=PAYLOAD).json()["id"]
    client.post(f"/v1/requests/{second}/process")
    assert wait_for_status(client, second, {"sent"}) == "sent"

    metrics = client.get("/internal/metrics").json()
    assert metrics["counters"]["worker_unexpected_errors"] == 1
    assert metrics["pipeline"]["workers_alive"] == 1


def test_metrics_endpoint_exposes_the_full_picture(make_client):
    client = make_client()
    request_id = client.post("/v1/requests", json=PAYLOAD).json()["id"]
    client.post(f"/v1/requests/{request_id}/process")
    wait_for_status(client, request_id, {"sent"})

    body = client.get("/internal/metrics").json()
    assert body["counters"]["requests_created"] == 1
    assert body["counters"]["process_accepted"] == 1
    assert body["counters"]["notifications_sent"] == 1
    assert body["requests_by_status"]["sent"] == 1
    assert body["provider_call_count"] == 1
    assert body["repository_size"] == 1

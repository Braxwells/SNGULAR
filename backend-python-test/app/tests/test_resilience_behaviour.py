"""Tests del comportamiento frente a fallos del provider, vía la API pública."""

from __future__ import annotations

import time

from tests.conftest import wait_for_status

PAYLOAD = {"to": "user@example.com", "message": "m", "type": "email"}


def _submit(client) -> str:
    request_id = client.post("/v1/requests", json=PAYLOAD).json()["id"]
    assert client.post(f"/v1/requests/{request_id}/process").status_code == 202
    return request_id


def test_transient_500_is_retried_until_success(make_client, fake_provider):
    fake_provider.responses = [500, 500, 200]
    client = make_client(retry_max_attempts=3)
    request_id = _submit(client)
    assert wait_for_status(client, request_id, {"sent"}) == "sent"
    assert fake_provider.call_count == 3


def test_429_is_retried_and_does_not_open_the_circuit(make_client, fake_provider):
    fake_provider.responses = [429, 429, 200]
    client = make_client(retry_max_attempts=3, breaker_failure_threshold=2)
    request_id = _submit(client)
    assert wait_for_status(client, request_id, {"sent"}) == "sent"
    metrics = client.get("/internal/metrics").json()
    assert metrics["counters"]["provider_rate_limited"] == 2
    # Un 429 es backpressure, no avería: el circuito debe seguir cerrado.
    assert metrics["provider_circuit"] == "closed"
    assert metrics["provider_circuit_transitions"] == 0


def test_permanent_failure_ends_in_failed_after_exhausting_retries(make_client, fake_provider):
    fake_provider.responses = [500]
    client = make_client(retry_max_attempts=3, breaker_enabled=False)
    request_id = _submit(client)
    assert wait_for_status(client, request_id, {"failed"}) == "failed"
    assert fake_provider.call_count == 3
    detail = client.get(f"/internal/requests/{request_id}").json()
    assert detail["attempts"] == 3
    assert "500" in detail["last_error"]


def test_transport_error_is_retried(make_client, fake_provider):
    fake_provider.responses = [599, 200]  # 599 = fallo de conexión simulado
    client = make_client(retry_max_attempts=3)
    request_id = _submit(client)
    assert wait_for_status(client, request_id, {"sent"}) == "sent"
    assert client.get("/internal/metrics").json()["counters"]["provider_transport_errors"] == 1


def test_timeout_is_retried(make_client, fake_provider):
    fake_provider.responses = [598, 200]  # 598 = read timeout simulado
    client = make_client(retry_max_attempts=3)
    request_id = _submit(client)
    assert wait_for_status(client, request_id, {"sent"}) == "sent"
    assert client.get("/internal/metrics").json()["counters"]["provider_timeouts"] == 1


def test_401_is_not_retried(make_client, fake_provider):
    fake_provider.responses = [401]
    client = make_client(retry_max_attempts=4)
    request_id = _submit(client)
    assert wait_for_status(client, request_id, {"failed"}) == "failed"
    # Reintentar una clave inválida sólo gasta cuota del provider: un único intento.
    assert fake_provider.call_count == 1


def test_circuit_opens_after_consecutive_failures(make_client, fake_provider):
    fake_provider.responses = [599]  # provider completamente caído
    # max_redeliveries=0 aisla el comportamiento del breaker de la politica de reentrega
    # (que se testea aparte): aqui lo que se comprueba es que se falla rapido sin llamar.
    client = make_client(
        retry_max_attempts=2,
        breaker_failure_threshold=2,
        breaker_reset_seconds=60,
        max_redeliveries=0,
    )
    first = _submit(client)
    wait_for_status(client, first, {"failed"})
    calls_after_first = fake_provider.call_count

    second = _submit(client)
    wait_for_status(client, second, {"failed"})

    metrics = client.get("/internal/metrics").json()
    assert metrics["provider_circuit"] == "open"
    # Con el circuito abierto no se vuelve a llamar al provider: se falla rápido.
    assert fake_provider.call_count == calls_after_first
    assert metrics["counters"]["provider_skipped_circuit_open"] > 0


def test_circuit_closes_again_once_the_provider_recovers(make_client, fake_provider):
    fake_provider.responses = [599, 599, 200]
    client = make_client(retry_max_attempts=2, breaker_failure_threshold=2, breaker_reset_seconds=0.05)
    first = _submit(client)
    wait_for_status(client, first, {"failed"})
    assert client.get("/internal/metrics").json()["provider_circuit"] in {"open", "half_open"}

    # Se espera a que expire la ventana abierta: el presupuesto de reintentos de una sola
    # notificación (2 intentos x 10 ms) no puede ni debe absorber un circuito abierto 50 ms.
    time.sleep(0.08)

    second = _submit(client)
    assert wait_for_status(client, second, {"sent"}) == "sent"
    assert client.get("/internal/metrics").json()["provider_circuit"] == "closed"


def test_failed_deliveries_land_in_the_dead_letter_queue(make_client, fake_provider):
    fake_provider.responses = [500]
    client = make_client(retry_max_attempts=2, breaker_enabled=False)
    request_id = _submit(client)
    wait_for_status(client, request_id, {"failed"})
    dead = client.get("/internal/dead-letters").json()
    assert dead["count"] == 1
    assert dead["items"][0]["id"] == request_id


def test_items_not_attempted_due_to_an_open_circuit_are_requeued(make_client, fake_provider):
    """Si el circuito estaba abierto, la entrega no se intentó: no se puede dar por fallida.

    El mensaje vuelve a la cola (con tope) y se entrega en cuanto el provider se recupera.
    """
    fake_provider.responses = [599, 599, 200]
    client = make_client(
        worker_count=1,
        retry_max_attempts=1,
        retry_max_delay=0.01,
        breaker_failure_threshold=2,
        breaker_reset_seconds=0.15,
        max_redeliveries=5,
    )
    # Dos entregas tumban el circuito (threshold=2, 1 intento cada una).
    for _ in range(2):
        wait_for_status(client, _submit(client), {"failed"})
    assert client.get("/internal/metrics").json()["provider_circuit"] == "open"

    # Esta no llega a intentarse: debe acabar enviada, no fallida.
    request_id = _submit(client)
    assert wait_for_status(client, request_id, {"sent"}, timeout=8.0) == "sent"
    metrics = client.get("/internal/metrics").json()
    assert metrics["counters"]["redeliveries"] >= 1


def test_redeliveries_are_capped_so_a_dead_provider_does_not_loop_forever(make_client, fake_provider):
    fake_provider.responses = [599]  # el provider no vuelve nunca
    client = make_client(
        worker_count=1,
        retry_max_attempts=1,
        retry_max_delay=0.01,
        breaker_failure_threshold=1,
        breaker_reset_seconds=30,
        redelivery_max_delay=0.02,
        max_redeliveries=2,
    )
    wait_for_status(client, _submit(client), {"failed"})  # abre el circuito

    request_id = _submit(client)
    assert wait_for_status(client, request_id, {"failed"}, timeout=8.0) == "failed"
    detail = client.get(f"/internal/requests/{request_id}").json()
    assert detail["last_error"] == "circuit_open"
    assert client.get("/internal/metrics").json()["counters"]["redeliveries"] == 2

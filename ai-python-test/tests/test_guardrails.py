import pytest

from service.guardrails import contacts_in_text, validate_extraction
from service.prompts import build_messages

EMAIL_INPUT = "Manda un correo a marta@app.com: Tienes un nuevo mensaje."
SMS_INPUT = "Avisar por SMS al 600-111-222 que la reserva ha sido confirmada."

T, M, K = "marta@app.com", "tienes un nuevo mensaje.", "email"

# Todas las variantes que produce provider/responses.py
RECOVERABLE_EMAIL_RESPONSES = [
    f'{{"to": "{T}", "message": "{M}", "type": "{K}"}}',
    f'{{"Recipient": "{T}", "body": "{M}", "channel": "{K}"}}',
    f'{{"To": "{T}", "Message": "{M}", "Type": "{K}"}}',
    f'{{"destination": "{T}", "text": "{M}", "method": "{K}"}}',
    f'{{"to": "{T}", "message": "{M}", "type": "{K}", "confidence": 0.99, "latency_ms": 120}}',
    f'{{"to": "{T}", "message": "{M}"}}',
    f'{{"message": "{M}", "type": "{K}"}}',
    f'He extraído la información correctamente:\n```json\n{{"to": "{T}", "message": "{M}", "type": "{K}"}}\n```',
    f'Output:\n```\n{{"to": "{T}", "message": "{M}", "type": "{K}"}}\n```',
    f'Claro, el destino es {T} y enviaré {M} por {K}. En formato JSON: {{"to": "{T}", "message": "{M}", "type": "{K}"}}',
    f'{{"to": "{T}", "message": "{M}", "type": "{K}" ...',
    f"{{'to': '{T}', 'message': '{M}', 'type': '{K}'}}",
    f'{{to: "{T}", message: "{M}", type: "{K}"}}',
]


@pytest.mark.parametrize("content", RECOVERABLE_EMAIL_RESPONSES)
def test_recovers_all_noisy_variants(content):
    result = validate_extraction(content, EMAIL_INPUT)
    assert result.ok, result.error
    assert result.notification.model_dump() == {"to": T, "message": M, "type": "email"}


@pytest.mark.parametrize(
    "content",
    [
        "Lo siento, como IA no tengo permitido procesar datos de contacto personales.",
        "Error: El contenido del mensaje viola las políticas de seguridad (Potential Spam).",
        "Refused: Content analysis flagged sensitive information.",
        "",
    ],
)
def test_refusals_are_retryable(content):
    result = validate_extraction(content, EMAIL_INPUT)
    assert not result.ok
    assert result.retryable


def test_insufficient_info_text_is_not_retryable():
    content = "I cannot process the request. I was unable to identify the destination (email/phone) in your prompt."
    result = validate_extraction(content, "Dile a Juan que llego tarde")
    assert not result.ok
    assert not result.retryable


def test_structured_error_from_prompt_protocol_is_not_retryable():
    result = validate_extraction('{"error": "No recipient found"}', "Dile a Juan que llego tarde")
    assert not result.ok
    assert not result.retryable


def test_sms_phone_is_normalized_and_type_kept():
    content = '{"to": "600-111-222", "message": "la reserva ha sido confirmada", "type": "sms"}'
    result = validate_extraction(content, SMS_INPUT)
    assert result.ok
    assert result.notification.to == "600111222"
    assert result.notification.type == "sms"


def test_type_is_corrected_from_recipient_format():
    content = '{"to": "600111222", "message": "hola", "type": "email"}'
    result = validate_extraction(content, SMS_INPUT)
    assert result.ok
    assert result.notification.type == "sms"
    assert any(r.startswith("type_corrected") for r in result.repairs)


def test_hallucinated_recipient_is_replaced_by_the_one_in_input():
    content = '{"to": "otro@fake.com", "message": "hola", "type": "email"}'
    result = validate_extraction(content, EMAIL_INPUT)
    assert result.ok
    assert result.notification.to == T
    assert "hallucinated_recipient_discarded" in result.repairs


def test_truncated_string_value_is_dropped_not_sent_cut():
    content = '{"to": "marta@app.com", "type": "email", "message": "tienes un nu'
    result = validate_extraction(content, EMAIL_INPUT)
    assert not result.ok
    assert result.error == "missing_message"
    assert result.retryable


def test_missing_recipient_with_no_contact_in_input_is_final():
    result = validate_extraction('{"message": "hola", "type": "email"}', "Dile hola a Juan")
    assert not result.ok
    assert not result.retryable


def test_ambiguous_recipient_is_not_guessed():
    text = "Manda un correo a a@x.com y a b@y.com diciendo hola"
    result = validate_extraction('{"message": "hola", "type": "email"}', text)
    assert not result.ok
    assert result.error == "ambiguous_recipient"


def test_nested_object_is_unwrapped():
    content = '{"notification": {"to": "marta@app.com", "message": "hola", "type": "email"}}'
    assert validate_extraction(content, EMAIL_INPUT).ok


def test_contacts_in_text():
    assert contacts_in_text("Recordatorio por teléfono al 699888777: cita a las 10:00") == ["699888777"]
    assert contacts_in_text("SMS al +34 611-222-333") == ["+34611222333"]
    assert contacts_in_text(EMAIL_INPUT) == [T]


def test_user_input_is_sent_verbatim_as_last_message():
    messages = build_messages(EMAIL_INPUT)
    assert messages[0]["role"] == "system"
    assert messages[-1] == {"role": "user", "content": EMAIL_INPUT}

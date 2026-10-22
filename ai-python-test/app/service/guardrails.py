"""Guardrails sobre la salida del LLM.

Pipeline de limpieza y validación (en este orden):
1. Localizar el JSON dentro de la respuesta (bloques Markdown, texto alrededor).
2. Parsear: primero JSON estricto; si falla, un parser tolerante que soporta
   comillas simples, claves sin comillas, comas sobrantes y JSON truncado.
3. Normalizar claves (alias y mayúsculas: Recipient/destination -> to, ...).
4. Validar y reconciliar con la entrada original del usuario:
   - el destinatario debe aparecer en el texto del usuario (anti-alucinación);
   - si falta, se recupera del texto si hay un único candidato inequívoco;
   - el tipo se deriva del formato del destinatario si falta o contradice.
5. Clasificar el fallo como reintentable (vale la pena volver a preguntar al
   LLM) o definitivo (el texto del usuario no contiene la información).
"""
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, ValidationError, field_validator

from .config import settings

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Teléfonos de 9 a 15 dígitos con separadores opcionales (espacio, punto, guion).
PHONE_IN_TEXT_RE = re.compile(r"(?<![\w+])\+?\d(?:[\s.-]?\d){8,14}(?![\w])")
PHONE_RE = re.compile(r"^\+?\d{9,15}$")

KEY_ALIASES = {
    "to": "to", "recipient": "to", "destination": "to", "destinatario": "to",
    "target": "to", "receiver": "to", "address": "to",
    "message": "message", "body": "message", "text": "message", "content": "message",
    "mensaje": "message", "msg": "message", "texto": "message",
    "type": "type", "channel": "type", "method": "type", "tipo": "type",
    "canal": "type", "medium": "type",
}

TYPE_ALIASES = {
    "email": "email", "e-mail": "email", "mail": "email", "correo": "email",
    "sms": "sms", "text": "sms", "phone": "sms", "teléfono": "sms", "telefono": "sms",
}

# Frases con las que el modelo indica que el input no contiene la información:
# reintentar no va a cambiar el resultado.
INSUFFICIENT_INFO_RE = re.compile(
    r"unable to identify|cannot identify|could not identify|no recipient|missing (recipient|destination)",
    re.IGNORECASE,
)


class Notification(BaseModel):
    to: str
    message: str
    type: Literal["email", "sms"]

    @field_validator("message")
    @classmethod
    def _message_not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message is empty")
        if len(value) > settings.max_message_length:
            raise ValueError("message too long")
        return value


@dataclass
class ExtractionResult:
    notification: Optional[Notification] = None
    error: Optional[str] = None
    retryable: bool = False
    repairs: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.notification is not None


# --------------------------------------------------------------------------- #
# 1-2. Localización y parseo tolerante
# --------------------------------------------------------------------------- #

class _Truncated(Exception):
    pass


class _LenientParser:
    """Parser recursivo descendente que acepta "casi JSON".

    Tolera: comillas simples, claves sin comillas, comas sobrantes, literales de
    Python (True/False/None) y objetos sin cerrar. Un string sin cerrar se
    descarta (preferimos perder un campo a enviar un mensaje cortado).
    """

    def __init__(self, text: str) -> None:
        self.s = text
        self.i = 0
        self.repairs: List[str] = []

    def parse(self) -> Any:
        self._skip_ws()
        return self._value()

    def _eof(self) -> bool:
        return self.i >= len(self.s)

    def _skip_ws(self) -> None:
        while not self._eof() and self.s[self.i].isspace():
            self.i += 1

    def _value(self) -> Any:
        if self._eof():
            raise _Truncated()
        c = self.s[self.i]
        if c == "{":
            return self._object()
        if c == "[":
            return self._array()
        if c in "\"'":
            if c == "'":
                self._note("single_quotes")
            return self._string(c)
        if c == "-" or c.isdigit():
            return self._number()
        return self._bareword()

    def _object(self) -> Dict[str, Any]:
        self.i += 1
        obj: Dict[str, Any] = {}
        while True:
            self._skip_ws()
            if self._eof():
                self._note("unclosed_object")
                return obj
            c = self.s[self.i]
            if c == "}":
                self.i += 1
                return obj
            if c == ",":
                self.i += 1
                continue
            try:
                if c in "\"'":
                    if c == "'":
                        self._note("single_quotes")
                    key = self._string(c)
                elif c.isalpha() or c == "_":
                    key = self._identifier()
                    self._note("unquoted_keys")
                else:
                    # Basura (p.ej. "..." de una respuesta truncada): cerramos aquí.
                    self._note("trailing_garbage")
                    return obj
                self._skip_ws()
                if self._eof() or self.s[self.i] != ":":
                    self._note("missing_colon")
                    return obj
                self.i += 1
                self._skip_ws()
                obj[key] = self._value()
            except _Truncated:
                self._note("truncated_value")
                return obj

    def _array(self) -> List[Any]:
        self.i += 1
        items: List[Any] = []
        while True:
            self._skip_ws()
            if self._eof():
                self._note("unclosed_array")
                return items
            c = self.s[self.i]
            if c == "]":
                self.i += 1
                return items
            if c == ",":
                self.i += 1
                continue
            try:
                items.append(self._value())
            except _Truncated:
                self._note("truncated_value")
                return items

    def _string(self, quote: str) -> str:
        self.i += 1
        out: List[str] = []
        escapes = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "/": "/"}
        while not self._eof():
            c = self.s[self.i]
            if c == "\\" and self.i + 1 < len(self.s):
                nxt = self.s[self.i + 1]
                if nxt == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", self.s[self.i + 2:self.i + 6]):
                    out.append(chr(int(self.s[self.i + 2:self.i + 6], 16)))
                    self.i += 6
                    continue
                out.append(escapes.get(nxt, nxt))
                self.i += 2
                continue
            if c == quote:
                self.i += 1
                return "".join(out)
            out.append(c)
            self.i += 1
        raise _Truncated()

    def _identifier(self) -> str:
        start = self.i
        while not self._eof() and (self.s[self.i].isalnum() or self.s[self.i] in "_-"):
            self.i += 1
        return self.s[start:self.i]

    def _number(self) -> Any:
        match = re.compile(r"-?\d+(\.\d+)?([eE][+-]?\d+)?").match(self.s, self.i)
        if not match:
            raise _Truncated()
        self.i = match.end()
        text = match.group(0)
        return float(text) if any(ch in text for ch in ".eE") else int(text)

    def _bareword(self) -> Any:
        match = re.compile(r"[^\s,}\]:]+").match(self.s, self.i)
        if not match:
            raise _Truncated()
        self.i = match.end()
        word = match.group(0)
        literals = {"true": True, "false": False, "null": None, "none": None}
        if word.lower() in literals:
            return literals[word.lower()]
        self._note("unquoted_value")
        return word

    def _note(self, repair: str) -> None:
        if repair not in self.repairs:
            self.repairs.append(repair)


def extract_json_candidate(text: str) -> Optional[str]:
    """Devuelve el texto a partir del primer '{', priorizando bloques ``` ```."""
    fence = re.search(r"```[a-zA-Z]*\s*(.*?)(?:```|$)", text, re.DOTALL)
    if fence and "{" in fence.group(1):
        text = fence.group(1)
    start = text.find("{")
    if start == -1:
        return None
    return text[start:]


def parse_llm_json(text: str) -> Tuple[Optional[Any], List[str]]:
    repairs: List[str] = []
    if "```" in text:
        repairs.append("markdown_fence")
    candidate = extract_json_candidate(text)
    if candidate is None:
        return None, repairs
    if candidate != text.strip() and "markdown_fence" not in repairs:
        repairs.append("surrounding_text")
    try:
        # raw_decode ignora lo que venga después del objeto.
        obj, _ = json.JSONDecoder().raw_decode(candidate)
        return obj, repairs
    except json.JSONDecodeError:
        pass
    parser = _LenientParser(candidate)
    try:
        obj = parser.parse()
    except _Truncated:
        return None, repairs + parser.repairs + ["unparseable"]
    return obj, repairs + parser.repairs


# --------------------------------------------------------------------------- #
# 3. Normalización
# --------------------------------------------------------------------------- #

def normalize_keys(obj: Dict[str, Any], repairs: List[str]) -> Dict[str, Any]:
    # Desenvolver un nivel si el modelo anidó el resultado ({"notification": {...}}).
    if not any(str(k).strip().lower() in KEY_ALIASES for k in obj):
        nested = [v for v in obj.values() if isinstance(v, dict)]
        if len(nested) == 1:
            obj = nested[0]
            repairs.append("unwrapped_nested_object")
    normalized: Dict[str, Any] = {}
    for key, value in obj.items():
        raw = str(key).strip()
        canonical = KEY_ALIASES.get(raw.lower())
        if canonical is None:
            repairs.append(f"dropped_key:{raw}")
            continue
        if raw != canonical:
            repairs.append(f"renamed_key:{raw}->{canonical}")
        normalized.setdefault(canonical, value)
    return normalized


def normalize_phone(value: str) -> str:
    value = value.strip()
    prefix = "+" if value.startswith("+") else ""
    return prefix + re.sub(r"\D", "", value)


def classify_recipient(value: str) -> Optional[str]:
    """Devuelve 'email' o 'sms' según el formato, o None si no es válido."""
    if EMAIL_RE.fullmatch(value):
        return "email"
    if PHONE_RE.fullmatch(value):
        return "sms"
    return None


def normalize_recipient(value: Any) -> Optional[str]:
    if not isinstance(value, (str, int)):
        return None
    text = str(value).strip().strip("<>").strip()
    if "@" in text:
        return text.lower()
    return normalize_phone(text)


def contacts_in_text(text: str) -> List[str]:
    """Destinatarios presentes en el texto del usuario, normalizados y sin duplicados."""
    found: List[str] = []
    for email in EMAIL_RE.findall(text):
        if email.lower() not in found:
            found.append(email.lower())
    for phone in PHONE_IN_TEXT_RE.findall(text):
        normalized = normalize_phone(phone)
        if normalized not in found:
            found.append(normalized)
    return found


def recipient_in_text(recipient: str, user_input: str) -> bool:
    if "@" in recipient:
        return recipient.lower() in user_input.lower()
    digits = re.sub(r"\D", "", recipient)
    return len(digits) >= 9 and digits in re.sub(r"\D", "", user_input)


# --------------------------------------------------------------------------- #
# 4-5. Validación, reconciliación y clasificación del error
# --------------------------------------------------------------------------- #

def validate_extraction(raw_content: str, user_input: str) -> ExtractionResult:
    content = (raw_content or "").strip()
    if not content:
        return ExtractionResult(error="empty_ai_response", retryable=True)

    obj, repairs = parse_llm_json(content)
    if obj is None:
        if INSUFFICIENT_INFO_RE.search(content):
            return ExtractionResult(error="insufficient_info", retryable=False, repairs=repairs)
        # Negativas, avisos de políticas, texto libre... un nuevo intento suele funcionar.
        return ExtractionResult(error="no_json_in_response", retryable=True, repairs=repairs)
    if not isinstance(obj, dict):
        return ExtractionResult(error="json_is_not_an_object", retryable=True, repairs=repairs)

    if "error" in {str(k).lower() for k in obj} and not ({"to", "message"} & {str(k).lower() for k in obj}):
        # El modelo siguió el protocolo de error del prompt: falta información.
        return ExtractionResult(error="insufficient_info", retryable=False, repairs=repairs)

    data = normalize_keys(obj, repairs)

    # --- Destinatario ---
    candidates = contacts_in_text(user_input)
    to = normalize_recipient(data.get("to"))
    if to is not None and classify_recipient(to) is None:
        repairs.append("invalid_recipient_discarded")
        to = None
    if to is not None and not recipient_in_text(to, user_input):
        repairs.append("hallucinated_recipient_discarded")
        to = None

    declared_type = TYPE_ALIASES.get(str(data.get("type", "")).strip().lower())
    if to is None:
        compatible = [c for c in candidates if declared_type in (None, classify_recipient(c))]
        if len(compatible) == 1:
            to = compatible[0]
            repairs.append("recipient_recovered_from_input")
        elif not candidates:
            return ExtractionResult(error="no_recipient_in_input", retryable=False, repairs=repairs)
        else:
            return ExtractionResult(error="ambiguous_recipient", retryable=True, repairs=repairs)

    # --- Tipo: el formato del destinatario manda ---
    derived_type = classify_recipient(to)
    if declared_type is None:
        repairs.append("type_inferred_from_recipient")
    elif declared_type != derived_type:
        repairs.append(f"type_corrected:{declared_type}->{derived_type}")

    # --- Mensaje ---
    message = data.get("message")
    if isinstance(message, (int, float)) and not isinstance(message, bool):
        message = str(message)
    if not isinstance(message, str) or not message.strip():
        return ExtractionResult(error="missing_message", retryable=True, repairs=repairs)

    try:
        notification = Notification(to=to, message=message, type=derived_type)
    except ValidationError as exc:
        return ExtractionResult(error=f"validation_error: {exc.errors()[0]['msg']}", retryable=True, repairs=repairs)
    return ExtractionResult(notification=notification, repairs=repairs)

"""Configuración centralizada del servicio.

Todos los valores se pueden sobrescribir con variables de entorno para poder
ajustar el comportamiento sin reconstruir la imagen (útil en el docker-compose).
"""
import os


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


class Settings:
    # Provider (motor IA + notificaciones)
    provider_base_url: str = os.getenv("PROVIDER_BASE_URL", "http://localhost:3001")
    provider_api_key: str = os.getenv("PROVIDER_API_KEY", "test-dev-2026")

    # Timeouts (segundos). La IA del mock tarda 1.5-3s, damos margen.
    ai_timeout: float = _env_float("AI_TIMEOUT", 10.0)
    notify_timeout: float = _env_float("NOTIFY_TIMEOUT", 5.0)

    # Reintentos
    # Intentos de inferencia cuando la respuesta del LLM no se puede recuperar
    # (negativas, texto sin JSON, campos imposibles de reparar...). Con un 10%
    # de negativas, 3 intentos dejan ~0.1% de fallos con un ratio de ~1.11
    # llamadas por solicitud (el dashboard marca verde por debajo de 1.5).
    ai_max_inference_attempts: int = _env_int("AI_MAX_INFERENCE_ATTEMPTS", 3)
    # Reintentos de transporte (timeouts, 5xx, 429) contra el endpoint de IA.
    ai_max_transport_attempts: int = _env_int("AI_MAX_TRANSPORT_ATTEMPTS", 3)
    notify_max_attempts: int = _env_int("NOTIFY_MAX_ATTEMPTS", 5)
    # Tiempo máximo reintentando ante 429 (en lugar de un número de intentos).
    rate_limited_retry_budget: float = _env_float("RATE_LIMITED_RETRY_BUDGET", 120.0)

    # Concurrencia
    # Máximo de llamadas simultáneas a la IA (protege al provider y a nuestro pool).
    ai_max_concurrency: int = _env_int("AI_MAX_CONCURRENCY", 200)
    http_max_connections: int = _env_int("HTTP_MAX_CONNECTIONS", 300)

    # Rate limit de /v1/notify. El provider permite 50 peticiones por ventana
    # deslizante de 10s; nos quedamos un poco por debajo para absorber jitter.
    notify_rate_limit: int = _env_int("NOTIFY_RATE_LIMIT", 45)
    notify_rate_window: float = _env_float("NOTIFY_RATE_WINDOW", 10.0)

    # Validación de entrada
    max_user_input_length: int = _env_int("MAX_USER_INPUT_LENGTH", 2000)
    max_message_length: int = _env_int("MAX_MESSAGE_LENGTH", 1000)


settings = Settings()

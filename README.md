# Prueba técnica Singular

Este repositorio contiene las soluciones a los dos retos de la prueba técnica. Ambos implementan un servicio de notificaciones en **FastAPI** que media entre un cliente y un proveedor externo simulado. Cada solución se valida con el test de carga **k6** y el scorecard de **Grafana** que vienen con su reto.

| Reto | Carpeta | Qué resuelve |
|---|---|---|
| **AI Engineer** | [`ai-python-test/`](ai-python-test/) | Recibe peticiones en lenguaje natural, extrae `{to, message, type}` con un LLM, limpia y valida la salida, y envía la notificación. |
| **Backend** | [`backend-python-test/`](backend-python-test/) | Recibe peticiones estructuradas y las entrega a un proveedor lento, con rate limit y errores aleatorios, sin degradar al cliente. |

La documentación completa de cada reto está en su carpeta `docs/`: análisis previo, decisiones de diseño, problemas encontrados y alternativas valoradas.

- [`ai-python-test/docs/`](ai-python-test/docs/)
- [`backend-python-test/docs/`](backend-python-test/docs/)

---

## El problema de fondo (común a los dos retos)

El proveedor de notificaciones acepta **50 peticiones por cada 10 s de ventana deslizante**, es decir, unas 5 por segundo. En cambio, k6 llega a generar **más de 60 solicitudes por segundo** con 200 usuarios virtuales. Entrar unas 13 veces más rápido de lo que se puede salir no tiene arreglo en el lado cliente. El servicio tiene que:

1. **Aceptar rápido.** `POST /process` encola y responde `202` en milisegundos, sin esperar al proveedor ni a la IA.
2. **Drenar al ritmo que el proveedor tolera**, con un rate limiter local que sigue el mismo algoritmo que el proveedor (45/10 s) para **no provocar ningún 429**.
3. **No devolver nunca errores al cliente** por problemas del proveedor. Lo que aún no se ha enviado queda en `queued` o `processing`, que son estados válidos del contrato.

Las dos soluciones comparten esa base y se diferencian en lo que añade cada reto.

### AI Engineer: robustez frente al LLM

- **System prompt** con esquema de salida estricto, reglas por campo, protocolo de error en JSON (`{"error": ...}`) y defensa frente a prompt injection. Incluye **few-shot** como turnos `user`/`assistant`.
- **Guardrails** con parser JSON tolerante propio. Reparan en local el ruido del modelo sin volver a llamarlo: bloques Markdown, texto alrededor, comillas simples, claves sin comillas, JSON truncado, claves renombradas y campos que faltan.
- **Validación cruzada con la entrada del usuario.** Se descartan destinatarios inventados, se recupera el destinatario si falta y el tipo (`email`/`sms`) se deriva del formato del destinatario.
- **Reintentos selectivos.** Solo se reintentan las negativas y las respuestas irrecuperables, con un máximo de 3 intentos de inferencia. La falta de información no se reintenta.

### Backend: resiliencia frente al proveedor

- **Cola acotada y pool de workers** (patrón *accept-and-queue*). Los workers están blindados frente a excepciones inesperadas.
- **Reintentos con backoff exponencial y *full jitter***, con política distinta por tipo de respuesta: `429` y `5xx` se reintentan, `401` y `422` no.
- **Circuit breaker.** Un `429` no cuenta como fallo. Las entregas que no se intentaron por tener el circuito abierto vuelven a la cola, con un tope.
- **Observabilidad.** `/health`, `/internal/metrics`, `/internal/requests/{id}` y una *dead-letter queue*.

---

## Cómo ejecutar

Cada reto es independiente y se lanza desde su propia carpeta.

```bash
cd ai-python-test        # o backend-python-test

docker-compose up -d provider influxdb grafana   # infraestructura
docker-compose up -d --build app                 # la solución
docker-compose run --rm load-test                # test de carga k6
```

Scorecards de Grafana (`localhost:3000`):

- AI Engineer: <http://localhost:3000/d/ia-performance-scorecard/>
- Backend: <http://localhost:3000/d/backend-performance-scorecard/>

### Tests

```bash
# AI Engineer
cd ai-python-test
pip install -r requirements-dev.txt
pytest tests -q

# Backend
cd backend-python-test/app
pip install -r requirements-dev.txt
pytest
ruff check .
```

Ningún test toca la red: el proveedor se sustituye por un `httpx.MockTransport`.

---

## Resultados

Medidos con el `test.js` de k6 original, sin modificar, contra el provider mock original.

| Métrica | AI Engineer | Backend | Umbral del scorecard |
|---|---|---|---|
| Checks de k6 | 100 % | 100 % | — |
| Peticiones fallidas (`http_req_failed`) | 0 % | 0 % | Disponibilidad ≥ 99 % |
| Latencia media de `/process` | ~1 ms | ~1–2 ms | < 500 ms (ASYNC) |
| p99 de `/process` | < 10 ms | < 10 ms | — |
| Respuestas 429 del proveedor | 0 | 0 | — |
| Específico del reto | Ratio intentos IA / solicitud ≈ **1,10** (verde < 1,5) | Errores 500 del proveedor recuperados con reintentos: **100 %** | — |

Los detalles (métricas internas, prueba de caos con el proveedor caído a mitad del test, fallos residuales esperados por las negativas aleatorias del mock) están en la documentación de cada reto.

---

## Limitaciones conocidas

Son decisiones deliberadas para el alcance de la prueba, explicadas en `docs/` junto con cómo evolucionarían a producción:

- **Estado y cola en memoria, con un solo proceso.** En producción se usaría Redis o Postgres para el estado, una cola duradera (Redis Streams, SQS o RabbitMQ) y un rate limiter distribuido para poder escalar horizontalmente.
- **Sin autenticación** en la API propia ni deduplicación por `Idempotency-Key`. `/process` sí es idempotente.
- **Con un LLM real** se añadirían *structured outputs* o *tool calling*, `temperature=0`, versionado de prompts y un dataset de evaluación.

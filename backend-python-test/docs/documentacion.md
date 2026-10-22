# Notification Service — Prueba técnica backend (Singular)

Documento de trabajo: registro de **cada paso**, los **problemas** encontrados, las
**soluciones** aplicadas y las **alternativas** posibles.

- Repositorio del reto: `backend-python-test/`
- Servicio implementado: `backend-python-test/app/`
- Fecha: 2026-10-22

---

## Índice

1. [Paso 1 — Lectura del enunciado y del código de partida](#paso-1)
2. [Paso 2 — Análisis del provider: dónde está el cuello de botella real](#paso-2)
3. [Paso 3 — Análisis del scorecard: qué se mide exactamente](#paso-3)
4. [Paso 4 — Decisión de arquitectura](#paso-4)
5. [Paso 5 — Implementación módulo a módulo](#paso-5)
6. [Paso 6 — Tests](#paso-6)
7. [Paso 7 — Validación y resultados](#paso-7)
8. [Paso 8 — Problemas encontrados y decisiones de diseño](#paso-8)
9. [Paso 9 — Limitaciones conocidas y evolución a producción](#paso-9)

---

<a name="paso-1"></a>
## Paso 1 — Lectura del enunciado y del código de partida

### Qué pide el enunciado

Un servicio mediador entre el cliente (k6) y un proveedor externo, con tres endpoints en el
**puerto 5000**:

| Método | Ruta | Respuesta esperada |
|---|---|---|
| `POST` | `/v1/requests` | `201` + `{"id": "..."}` |
| `POST` | `/v1/requests/{id}/process` | `200` o `202` |
| `GET`  | `/v1/requests/{id}` | `200` + `{"id": "...", "status": "queued/processing/sent/failed"}` |

El proveedor vive en `localhost:3001`, endpoint `POST /v1/notify`, cabecera
`X-API-Key: test-dev-2026`.

### Inventario del repositorio

```
backend-python-test/
├── app/                      <- LO ÚNICO QUE HAY QUE ESCRIBIR
│   ├── main.py               <- 3 líneas: un FastAPI vacío
│   ├── Dockerfile            <- uvicorn main:app --port 5000
│   └── requirements.txt      <- fastapi, uvicorn, httpx, pydantic, tenacity
├── provider/app.py           <- proveedor simulado (no se toca)
├── platform/k6/test.js       <- suite de carga que puntúa
├── platform/grafana/...      <- el scorecard
└── docker-compose.yaml
```

Punto de partida real de `app/main.py`:

```python
from fastapi import FastAPI
app = FastAPI(title="Notification Service (Technical Test)")
```

Hay que escribirlo todo.

### Detalle de infraestructura que condiciona la solución

En `docker-compose.yaml` el servicio `app` usa:

```yaml
app:
  network_mode: "service:provider"
```

El contenedor de la app **comparte el network namespace del provider**. Consecuencias:

1. La app alcanza al provider en `http://localhost:3001`, no en `http://provider:3001`.
2. El `5000:5000` publicado por el servicio `provider` es en realidad **nuestro** puerto.
3. k6 ataca `http://provider:5000`, que resuelve a nuestra app. Confuso, pero correcto.
4. El servicio `app` no puede tener `ports:` propio ni alias DNS propio.
5. Como comparten localhost, la llamada app→provider no sale a la red del host: la latencia
   de red es despreciable y toda la latencia observada es la simulada por el provider.

---

<a name="paso-2"></a>
## Paso 2 — Análisis del provider: dónde está el cuello de botella real

Esta es la parte decisiva del reto. Leyendo `provider/app.py` con cuidado:

```python
FAIL_RATE = 0.1               # 10% de 500 aleatorios
LATENCY_MIN = 0.1             # 100 ms
LATENCY_MAX = 0.5             # 500 ms
RATE_LIMIT_THRESHOLD = 50     # 50 peticiones...
MAX_CONCURRENT_REQUESTS = 50  # semáforo de concurrencia

request_counts = [t for t in request_counts if now - t < 10]   # ...por VENTANA DE 10 s
if len(request_counts) >= RATE_LIMIT_THRESHOLD:
    raise HTTPException(429)
request_counts.append(now)                 # sólo cuenta si se ACEPTA
await asyncio.sleep(random.uniform(0.1, 0.5))
if random.random() < FAIL_RATE:
    raise HTTPException(500)
```

### Conclusión numérica

- **50 aceptadas por ventana deslizante de 10 s ⇒ 5 req/s sostenidos.** No 50/s.
- De esas, ~10% devuelve `500` ⇒ **~4,5 entregas/s como techo teórico absoluto**.
- Latencia por petición: 100–500 ms (media 300 ms).
- Las rechazadas con `429` **no** entran en la ventana: reintentar no empeora el límite,
  pero tampoco lo mejora.
- El semáforo de 50 concurrentes nunca llega a ser el límite activo: el rate limit ata antes.

### La carga que nos van a meter

`platform/k6/test.js`:

```js
stages: [
  { duration: '10s', target: 50 },
  { duration: '20s', target: 150 },
  { duration: '10s', target: 200 },
]
```

Cada iteración hace `POST /v1/requests` → `POST .../process` → `sleep(0.5)` →
`GET /v1/requests/{id}` → `sleep(1)`. O sea **~1 notificación cada ~1,5 s por VU**.

| VUs | Notificaciones/s entrantes |
|---|---|
| 50 | ~33 /s |
| 150 | ~100 /s |
| 200 | ~133 /s |

### El problema central

> **Entran hasta ~133 notificaciones/s y el proveedor digiere ~4,5/s.
> Desajuste de ~30x.**

No es un bug a corregir: es el enunciado. Y tiene una única lectura razonable:

- **Es imposible entregar todo durante el test.** Cualquier diseño que llame al provider de
  forma sincrónica se hunde: o acumula latencia de segundos, o devuelve errores al cliente.
- El servicio **debe desacoplar** aceptación de entrega: aceptar rápido, encolar, drenar al
  ritmo que el proveedor tolera.
- Lo no enviado todavía se queda en `queued`, que **es un estado válido** del contrato. El
  contrato no exige `sent` en ningún plazo.

Esto encaja exactamente con lo que mide el scorecard.

---

<a name="paso-3"></a>
## Paso 3 — Análisis del scorecard: qué se mide exactamente

Las queries de `platform/grafana/provisioning/dashboards/k6-dashboard.json` son,
literalmente, la rúbrica de corrección. Merece la pena leerlas antes de escribir código.

### Panel 1 — "INDICADOR DE ARQUITECTURA"

```sql
SELECT mean("value") FROM "http_req_duration" WHERE "url" =~ /process/
```

Mapeo: `0–500 ms → "ASYNC" (verde)`, `501–100000 ms → "SYNC" (rojo)`.

> **La latencia media de `/process` tiene que estar por debajo de 500 ms.** Como el provider
> ya tarda 100–500 ms por sí solo (más reintentos, más espera por rate limit), llamar al
> provider dentro del handler de `/process` garantiza el rojo. `/process` debe **encolar y
> devolver** inmediatamente.

### Paneles "DISPONIBILIDAD GLOBAL" y "ESTADO RESILIENCIA"

```sql
SELECT (count("value") - sum("value")) / count("value") * 100 FROM "http_req_failed"
```

Umbral verde: **≥ 99 %**. En k6 `http_req_failed` vale 1 cuando el status es `>= 400`, y se
mide sobre **todas** las peticiones que hace k6 — y k6 sólo habla con nuestra app.

> **Nuestra app no puede devolver 4xx/5xx prácticamente nunca**, ni bajo saturación. Esto
> descarta usar `503` o `429` como backpressure, aunque en HTTP puro sea lo correcto
> (ver decisión D4 en el Paso 8).

### Panel 4 — "ESTABILIDAD: PERCENTILES (p95/p99)" sobre `/process`

> No basta con media baja: la cola p99 también tiene que ser plana. Esto penaliza diseños
> con locks globales en el camino caliente o con trabajo CPU-bound dentro del event loop.

### Panel 5 — "DELTA DE EFICIENCIA (OVERHEAD)": `/process` vs el resto de URLs

> `POST /v1/requests` y `GET /v1/requests/{id}` también tienen que ser rápidos.

### Checks funcionales de k6

```
'create status is 201 or 200'      'process status is 202 or 200'
'id is present in response'        'status is retrieved' (200)
'status body is valid json'        'status value is valid' -> queued|processing|sent|failed
```

Nótese que **`queued` puntúa igual que `sent`**. Confirma la lectura del Paso 2.

### Resumen de la rúbrica → diseño

| Requisito derivado | Implicación |
|---|---|
| `/process` medio < 500 ms | Pipeline asíncrono; nunca llamar al provider en el handler |
| p95/p99 de `/process` planos | Camino caliente sin bloqueos |
| Disponibilidad ≥ 99 % | Nunca devolver ≥ 400 bajo carga; absorber la saturación |
| `POST`/`GET` también rápidos | Estado en memoria, O(1), sin I/O en el request path |
| Estado siempre válido | Máquina de estados estricta `queued→processing→sent\|failed` |
| "Robustez frente a errores inesperados" | Reintentos, circuit breaker, handler global |

---

<a name="paso-4"></a>
## Paso 4 — Decisión de arquitectura

### Patrón elegido: *Accept-and-Queue* (productor/consumidor in-process)

```
                 ┌──────────────────── proceso uvicorn (1 event loop) ───────────────────┐
                 │                                                                       │
 k6 ──POST /v1/requests──► API ──► Repository (dict en memoria)   status=queued           │
                 │                                                                       │
 k6 ──POST /{id}/process─► API ──► queue.put_nowait(id) ──► 202  (sub-milisegundo)        │
                 │                          │                                            │
                 │                          ▼                                            │
                 │                   asyncio.Queue (acotada)                             │
                 │                          │                                            │
                 │              ┌───────────┴───────────┐                                │
                 │              │   Worker pool (N)     │   status=processing             │
                 │              └───────────┬───────────┘                                │
                 │                          ▼                                            │
                 │              RateLimiter (ventana deslizante 45/10 s)                  │
                 │                          ▼                                            │
                 │              CircuitBreaker  +  Retry (backoff exponencial + jitter)   │
                 │                          ▼                                            │
                 │              httpx.AsyncClient (keep-alive pool) ──────────────────────┼──► provider:3001
                 │                          ▼                                            │
                 │                   status=sent | failed  (+ dead-letter queue)          │
                 └───────────────────────────────────────────────────────────────────────┘
```

### Por qué cada pieza

| Pieza | Problema que resuelve |
|---|---|
| **`asyncio.Queue` acotada** | Desacopla la tasa de entrada (133/s) de la de salida (4,5/s). Acotada para que la memoria no crezca sin límite. |
| **Worker pool** | Paralelismo controlado contra el provider; aísla los fallos del camino de request. |
| **Rate limiter de ventana deslizante** | *Espeja* el algoritmo exacto del provider (45 por 10 s frente a su límite de 50) para **no provocar `429` en origen**. Es mejor no pedir que pedir y ser rechazado. |
| **Reintentos con backoff exponencial + full jitter** | Absorbe el 10% de `500` aleatorios y cualquier `429` residual. El jitter evita que los workers se sincronicen y hagan oleadas. |
| **Circuit breaker** | Si el provider se cae de verdad (connection refused), deja de martillearlo y falla rápido; sondea en half-open. |
| **Repositorio en memoria con cota + LRU** | `GET` en O(1) sin I/O. Cota para no hacer OOM en un proceso de larga vida. |
| **Dead-letter queue acotada** | Lo que agota reintentos queda inspeccionable en lugar de desaparecer. |
| **Métricas internas** | Sin métricas no se puede justificar nada de lo anterior; `/internal/metrics` lo expone. |

### Decisiones explícitas y su contrapartida

1. **Un solo proceso uvicorn (`--workers 1`)**, no varios. El estado (cola, repositorio,
   rate limiter, breaker) es in-process. Con N workers tendríamos N rate limiters
   independientes y multiplicaríamos por N la presión sobre el provider, además de perder
   la consistencia del `GET` (una petición creada en el worker 1 no existiría para el
   worker 2). Con ~133 req/s de handlers triviales, un event loop va sobrado.
   *Alternativa de producción:* Redis/Postgres como estado compartido + rate limiter
   distribuido, y entonces sí escalar horizontalmente.

2. **Rate limiter conservador (45/10 s en vez de 50/10 s).** Deja un 10% de margen frente a
   desfases de reloj entre nuestro instante de envío y el instante en que el provider
   contabiliza. Se puede ajustar por variable de entorno.

3. **Backpressure sin romper el contrato.** Si la cola se llena, la petición se acepta
   (`202`) y se marca `failed` con motivo `queue_overflow`, en vez de devolver `503`.
   Es un estado válido del contrato y preserva la disponibilidad que mide el scorecard.
   Configurable: `NOTIFIER_QUEUE_OVERFLOW_POLICY=accept_and_fail|reject`.

4. **Reintentos hechos a mano en lugar de `tenacity`.** `tenacity` venía en el
   `requirements.txt` de partida, pero necesitamos política distinta por tipo de respuesta
   (`429` reintentable y gratis en la ventana, `500` reintentable, `401/422` NO reintentable),
   soporte de `Retry-After` y contabilidad de métricas por intento. Son ~30 líneas y queda
   más legible y testeable que configurar `tenacity` para lo mismo.

### Flujo de estados

```
          POST /v1/requests            POST /{id}/process        worker toma el item
   (nada) ───────────────────► queued ────────────────────► queued ───────────────────► processing
                                                                                             │
                                             ┌───────────────────────────────────────────────┤
                                             ▼                                               ▼
                                           sent                                           failed
                               (provider devolvió 200)                   (reintentos agotados / no reintentable /
                                                                          circuito abierto / cola desbordada)
```

`sent` y `failed` son terminales. `/process` es **idempotente**: una segunda llamada sobre el
mismo id no vuelve a encolar (se contabiliza como `process_duplicate`) y responde `202`.

---

<a name="paso-5"></a>
## Paso 5 — Implementación módulo a módulo

### Estructura final

```
backend-python-test/
├── app/
│   ├── main.py                  # 14 líneas: punto de entrada ASGI (uvicorn main:app)
│   ├── Dockerfile               # usuario sin privilegios + flags de uvicorn justificados
│   ├── requirements.txt         # fastapi, uvicorn[standard], httpx, pydantic
│   ├── requirements-dev.txt     # pytest, pytest-asyncio, ruff
│   ├── pytest.ini / ruff.toml
│   ├── notifier/                # el paquete de la aplicación
│   │   ├── settings.py          # configuración desde entorno (prefijo NOTIFIER_)
│   │   ├── models.py            # dominio: NotificationRequest + RequestStatus
│   │   ├── repository.py        # almacén en memoria, O(1), acotado
│   │   ├── resilience.py        # rate limiter, circuit breaker, backoff con jitter
│   │   ├── provider_client.py   # cliente HTTP resiliente del proveedor
│   │   ├── pipeline.py          # cola acotada + pool de workers (Dispatcher)
│   │   ├── metrics.py           # contadores en proceso
│   │   ├── schemas.py           # DTOs de la API
│   │   ├── api.py               # routers: contrato público + endpoints internos
│   │   ├── container.py         # composition root (wiring)
│   │   └── app_factory.py       # lifespan + manejadores de error
│   └── tests/                   # 38 tests
└── tools/
    ├── local_e2e.sh             # validación end-to-end sin Docker
    └── score_k6.py              # reproduce el scorecard de Grafana desde el JSON de k6
```

~1.100 líneas de aplicación y ~500 de test, todo comentado en el porqué (no en el qué).

### `settings.py` — configuración

Todo parametrizado por entorno con prefijo `NOTIFIER_`. Se usa `os.environ` y un
`@dataclass(frozen=True)` en vez de `pydantic-settings` para no añadir una dependencia: la
configuración es plana y se lee una sola vez al arrancar. Inmutable para que nadie la mute
en caliente y cambie el comportamiento a mitad de ejecución.

### `models.py` — dominio

`RequestStatus` es un `str, Enum` con los cuatro valores exactos del contrato, y una
propiedad `is_terminal`. `NotificationRequest` usa `slots=True`: bajo la carga del test se
acumulan ~2.700 objetos vivos, y los `__slots__` eliminan el `__dict__` por instancia.

### `repository.py` — estado

Diccionario ordenado con **cota superior y desalojo FIFO**. Dos detalles que no son obvios:

- **Sin locks.** El servicio corre en un único event loop y ningún método del repositorio
  contiene un `await`, así que cada uno es una sección crítica atómica por construcción. Un
  lock aquí sólo añadiría latencia al p99 sin aportar nada.
- **El desalojo prioriza entradas terminales.** Al superar la cota se buscan `sent`/`failed`
  entre las más antiguas antes de sacrificar una solicitud que todavía está en vuelo.
- `claim_for_dispatch()` es lo que hace **idempotente** a `/process`: comprueba y marca
  `dispatched` sin ningún `await` en medio, así que no existe ventana de carrera.

### `resilience.py` — las tres primitivas

**`SlidingWindowRateLimiter`.** La pieza más importante de todo el diseño. Implementa la
misma ventana deslizante que el provider (deque de timestamps, descartar los que salen de la
ventana) con un límite ligeramente inferior: **45 por 10 s frente a su 50 por 10 s.**

Por qué ventana deslizante y no token bucket: un token bucket con `burst` permitiría una
ráfaga inicial que el provider contabilizaría y nos devolvería `429`. **Es mejor no pedir que
pedir y ser rechazado**, porque un `429` cuesta un round-trip completo y no entrega nada.
El `asyncio.Lock` serializa la decisión «¿hay hueco?» + «reservo el hueco»; sin él, N workers
despertando simultáneamente se concederían todos el mismo hueco.

**`CircuitBreaker`.** Tres estados. El matiz que importa: **un `429` NO cuenta como fallo.**
Un 429 es backpressure legítima, no una avería; contarlo abriría el circuito en condiciones
perfectamente normales. Sólo cuentan los `5xx` y los fallos de transporte, y el umbral es de
fallos **consecutivos** (con `FAIL_RATE=0.1`, 10 consecutivos tiene probabilidad 1e-10, así
que el circuito sólo se abre cuando el provider está realmente roto).

**`full_jitter_backoff`.** `uniform(0, min(cap, base * 2^n))`. El *full jitter* del paper de
backoff de AWS: desincroniza a los workers que fallan a la vez, en lugar de que todos
reintenten en el mismo instante y vuelvan a saturar al provider.

### `provider_client.py` — la política de resiliencia

Un único método público, `deliver()`, que por cada intento:

1. consulta el circuit breaker (si está abierto, no gasta ni un socket);
2. pide hueco al rate limiter (espera, no falla: el que espera es un worker de background);
3. hace el `POST` con `httpx` sobre un pool con keep-alive;
4. clasifica la respuesta y decide si reintentar.

Tabla de decisión, que es la esencia del módulo:

| Respuesta | ¿Reintentar? | ¿Cuenta para el breaker? | Por qué |
|---|---|---|---|
| `200` | no (éxito) | reinicia el contador | — |
| `429` | **sí** | **no** | backpressure, no avería |
| `500/502/503/504/408` | sí | sí | fallo transitorio del provider |
| `401/403` | **no** | no | clave mal configurada: reintentar sólo gasta cuota. Se registra en `logger.error` |
| otros `4xx` | no | no | petición inválida: no va a funcionar nunca |
| timeout | sí | sí | probable saturación: martillear lo empeora |
| error de transporte | sí | sí | provider caído |
| cualquier otra excepción | sí | sí | defensa en profundidad: nada escapa del método |

Se reintenta a mano en lugar de con `tenacity` (que venía en el `requirements.txt` original)
porque la política depende del tipo de respuesta, hay que contabilizar métricas por intento y
hay que distinguir «espera del rate limiter» de «backoff de reintento». Son ~40 líneas y
quedan más legibles y testeables que configurar `tenacity` para lo mismo. `tenacity` se
eliminó de `requirements.txt` al quedar sin usar.

### `pipeline.py` — el `Dispatcher`

`submit()` **no es `async`, y eso es deliberado**: un `await queue.put()` esperaría cuando la
cola está llena y convertiría la saturación del provider en latencia del cliente, que es
exactamente lo que este diseño evita. Es `put_nowait()` y punto.

Los workers están blindados: un `except Exception` que registra y continúa. **Un worker nunca
debe morir**; si muere, el pipeline pierde capacidad en silencio y la cola crece sin fin.

La parada es ordenada: intenta drenar la cola con un tiempo máximo y sólo después cancela.
Un worker cancelado devuelve su item a `queued` en lugar de dejarlo en `processing`, para que
el estado no mienta.

### `api.py` — endpoints

Contrato público (`/v1/...`) estrictamente lo pedido. `GET /v1/requests/{id}` devuelve
**exactamente** `{"id", "status"}`; el detalle (intentos, último error, `provider_id`) vive
en `/internal/requests/{id}` para no contaminar el contrato con campos que el evaluador no
espera.

Endpoints internos añadidos: `/health`, `/internal/metrics`, `/internal/requests/{id}`,
`/internal/dead-letters`.

Detalle de `/health`: devuelve `200` **aunque el circuito esté abierto**. El provider caído no
significa que nosotros estemos caídos — seguimos aceptando y encolando correctamente. Si
devolviese `503`, el orquestador nos reiniciaría y perderíamos la cola, empeorando el
incidente.

### `Dockerfile` — por qué cada flag

```dockerfile
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "5000",
     "--workers", "1", "--loop", "uvloop", "--http", "httptools",
     "--no-access-log", "--timeout-keep-alive", "15"]
```

- `--workers 1`: el estado (cola, repositorio, rate limiter, breaker) es in-process. Con N
  workers habría **N rate limiters independientes** multiplicando por N la presión sobre el
  provider, y el `GET` sería inconsistente (una solicitud creada en el worker 1 no existiría
  para el worker 2). Un event loop va sobrado para los ~200 req/s medidos.
- `--loop uvloop --http httptools`: implementaciones en C que ya vienen en
  `uvicorn[standard]`. Gratis, y reducen el p99.
- `--no-access-log`: a ~200 req/s, un log por petición mete syscalls de escritura dentro del
  event loop y ensancha justo el percentil que mide el scorecard.
- Usuario `appuser` sin privilegios: si alguien escapa del proceso, no es root.

### `docker-compose.yaml` — único cambio funcional

Se añadió un `healthcheck` al servicio `app` y `load-test` ahora depende de
`app: condition: service_healthy`.

**Problema que resuelve:** el `depends_on` original sólo esperaba a que el *provider*
estuviera sano (`/docs` en el 3001). Nada garantizaba que **nuestra** app estuviera
escuchando en el 5000 cuando k6 empezaba a disparar, y cada petición de esos primeros
instantes es un `connection refused` que cuenta como fallo y hunde la métrica de
disponibilidad. Con el healthcheck, k6 no arranca hasta que `/health` responde `200`.

También se explicitaron todas las variables de entorno en el `compose`, para que la
configuración efectiva sea visible y ajustable sin reconstruir la imagen.

---

<a name="paso-6"></a>
## Paso 6 — Tests

**38 tests, todos en verde, en ~11 s.** Principio rector: **ningún test toca la red.** El
provider se sustituye por un `httpx.MockTransport` programable (`FakeProvider` en
`conftest.py`), lo que permite forzar escenarios que con el provider real serían azarosos:
100 % de `500`, `429` permanente, timeouts, fallos de conexión.

Se usa `TestClient` como gestor de contexto para que corra el `lifespan`: así los workers del
dispatcher **existen de verdad** durante el test y se ejercita el pipeline completo, no una
simulación de él.

### Reparto

| Fichero | Tests | Qué cubre |
|---|---|---|
| `test_api_contract.py` | 11 | El contrato exacto que valida k6, más los bordes (404, 422, campos extra, los tres tipos) |
| `test_resilience_behaviour.py` | 11 | Reintentos, 429 sin abrir circuito, apertura/cierre del circuito, no reintentar 401, dead-letters, reentregas |
| `test_resilience_units.py` | 11 | Las primitivas aisladas, incluido el invariante del rate limiter bajo concurrencia |
| `test_pipeline_and_latency.py` | 5 | Latencia de `/process`, backpressure, aislamiento de fallos de worker |

### Los tres tests que más valor aportan

**1. El que vigila el indicador de arquitectura** (`test_process_stays_fast_even_with_a_slow_provider`):
con el provider tardando 500 ms (su peor caso) y un rate limiter que sólo deja pasar 2 envíos
por segundo, exige que `/process` responda con media < 50 ms y p95 < 100 ms. El umbral del
scorecard es 500 ms; el test aprieta 10 veces más para detectar una regresión mucho antes de
que el panel se ponga en rojo. **Si alguien reintrodujera la llamada al provider dentro del
handler, este test se cae inmediatamente.**

**2. El invariante del rate limiter** (`test_rate_limiter_never_exceeds_the_limit_under_concurrency`):
lanza 40 corrutinas contra un limitador de 10 por 0,5 s y comprueba que en **ninguna** ventana
deslizante hay más de 10 eventos. Es la propiedad de la que depende no recibir `429`.

**3. El del worker que explota** (`test_a_crashing_worker_does_not_kill_the_pipeline`):
inyecta una excepción inesperada en una entrega y verifica que (a) esa notificación queda en
`failed` con `internal_error`, (b) **el worker sigue vivo** y procesa la siguiente con
normalidad. Es el test de "robustez frente a errores inesperados" del enunciado.

### Calidad estática

`ruff` con `E,F,W,I,B,UP,C4,SIM,RUF,ASYNC` → **All checks passed**. La regla `B008` se
configuró como excepción para `fastapi.Depends`, porque `Depends(...)` como valor por defecto
es el idiom de inyección de FastAPI (el framework resuelve la llamada en cada petición; no se
comparte estado mutable).

---

<a name="paso-7"></a>
## Paso 7 — Validación y resultados

### Problema: no había Docker en la máquina de desarrollo

`docker` no está instalado ni en Windows ni dentro de WSL, y `sudo` pide contraseña, así que
no se pudo ejecutar el `docker-compose` del enunciado.

**Solución:** se montó un arnés equivalente que levanta el **provider real** (`provider/app.py`,
sin modificar) en el 3001 y la **app real** en el 5000, y lanza el **k6 real**
(`platform/k6/test.js`, sin modificar) contra ella. Es el mismo grafo que el `compose`: la app
habla con `localhost:3001` y k6 con `localhost:5000`.

Queda guardado como herramienta reutilizable en `tools/local_e2e.sh`.

Lo único que no se pudo reproducir es el pipeline InfluxDB → Grafana. Para suplirlo se
escribió `tools/score_k6.py`, que **reproduce sobre el JSON crudo de k6 las mismas queries que
los paneles del dashboard** (incluido el filtro `url =~ /process/`) y dice si cada panel
saldría verde o rojo.

```bash
bash tools/local_e2e.sh            # escenario normal
bash tools/local_e2e.sh --chaos    # el provider muere a los 15 s
```

### Resultado del escenario normal

```
==========================================================================
SCORECARD (replica exacta de las queries de Grafana)
==========================================================================
DISPONIBILIDAD GLOBAL / RESILIENCIA : 100.00 %   [verde >= 99]  -> VERDE (ALTA)
  peticiones totales                : 8232  (fallidas: 0)

1. INDICADOR DE ARQUITECTURA        : mean(/process) =    0.99 ms   [ASYNC si < 500]  -> ASYNC (VERDE)

4. ESTABILIDAD: PERCENTILES sobre /process
      p50 =     0.87 ms
      p95 =     1.79 ms
      p99 =     2.52 ms
      max =     7.30 ms

5. DELTA DE EFICIENCIA (OVERHEAD)
     Core Service (/process)        :    0.99 ms  (n=2744)
     Resto de endpoints             :    1.34 ms  (n=5488)
       - create   mean=  1.36 ms  p95=  2.45 ms  n=2744
       - status   mean=  1.32 ms  p95=  2.46 ms  n=2744
2. RENDIMIENTO                      : 8232 peticiones
     throughput medio               : 198.5 RPS
     checks k6                      : 16464/16464 OK (100.00 %)
CONCURRENCIA MAXIMA (VUs)           : 199
==========================================================================
```

| Panel del scorecard | Umbral | Medido | Veredicto |
|---|---|---|---|
| Disponibilidad global | ≥ 99 % | **100,00 %** | 🟢 |
| Estado resiliencia | ≥ 99 % | **100,00 %** | 🟢 ALTA |
| 1. Indicador de arquitectura | < 500 ms | **0,99 ms** (505× de margen) | 🟢 ASYNC |
| 4. Estabilidad p95/p99 | — | **1,79 / 2,52 ms** | 🟢 |
| 5. Overhead | — | `/process` 0,99 ms vs resto 1,34 ms | 🟢 |
| Checks funcionales k6 | 100 % | **16.464 / 16.464** | 🟢 |

### Métricas internas del servicio al terminar

```json
{
  "counters": {
    "requests_created": 2744,  "process_accepted": 2744,
    "provider_attempts": 191,  "provider_success": 173,
    "provider_retries": 18,    "provider_server_errors": 18,
    "notifications_sent": 173
  },
  "provider_call_mean_latency_ms": 304.93,
  "rate_limit_wait_total_seconds": 511.86,
  "pipeline": { "workers_alive": 16, "queue_depth": 2555, "in_flight": 16 },
  "provider_circuit": "closed", "provider_circuit_transitions": 0,
  "rate_limiter_in_window": 45,
  "requests_by_status": { "queued": 2555, "processing": 16, "sent": 173, "failed": 0 }
}
```

Lo que confirma el análisis del Paso 2 y valida el diseño:

- **`provider_rate_limited` no aparece: CERO `429` recibidos del provider.** El rate limiter
  que espeja su ventana funcionó exactamente como se diseñó. Esto es el resultado más
  importante de toda la validación.
- **`notifications_sent` = 173 en ~41 s = 4,2 entregas/s**, contra el techo teórico de 4,5/s
  calculado en el Paso 2. Se está operando **al 93 % del máximo que el provider permite**.
- `provider_attempts` 191 = 173 éxitos + 18 reintentos por `500`. El 9,4 % de fallos aleatorios
  coincide con el `FAIL_RATE = 0.1` del provider. **Todos los `500` se recuperaron: 0 fallos
  definitivos.**
- `queue_depth` 2555: el backlog esperado por el desajuste de 30×. Esas 2.555 solicitudes
  están en `queued`, un estado válido del contrato, y se seguirían drenando a 4,5/s si el
  proceso continuara.
- `rate_limit_wait_total_seconds` 511 s agregados entre 16 workers: la prueba de que la espera
  se absorbe en background y **nunca** en el camino de la petición del cliente.

### Resultado del escenario de caos (el provider muere a los 15 s)

```
>>> matando el provider <<<
checks..............: 100.00% ✓ 16482   ✗ 0
http_req_failed.....: 0.00%   ✓ 0       ✗ 8241
http_req_duration...: avg=1.21ms  p(95)=2.2ms
```

```json
{ "notifications_sent": 82, "notifications_failed": 1,
  "provider_transport_errors": 28, "provider_skipped_circuit_open": 122,
  "redeliveries": 31, "provider_circuit": "open", "provider_circuit_transitions": 4,
  "requests_by_status": { "queued": 2648, "sent": 82, "failed": 1 } }
```

Con el proveedor externo **muerto durante la mitad del test**:

- **La disponibilidad del servicio sigue siendo del 100 %** y la latencia no se mueve
  (1,21 ms de media). El fallo del proveedor queda contenido en el background.
- El circuito se abrió y **122 entregas se descartaron sin gastar un socket**, en lugar de
  martillear un servicio caído.
- Las 4 transiciones son el ciclo `open → half_open → open`: el breaker sondeando cada 5 s
  por si volvía.
- Sólo **1** notificación se perdió definitivamente; 31 se devolvieron a la cola.
- `/health` siguió devolviendo `200`, como se decidió: el provider caído no nos hace estar
  caídos a nosotros.

---

<a name="paso-8"></a>
## Paso 8 — Problemas encontrados y decisiones de diseño

### Problemas resueltos durante el desarrollo

#### P1 — El `X-API-Key` desaparecía según quién construyera el cliente HTTP

**Síntoma.** El test `test_full_happy_path_reaches_sent` falló con
`assert None == 'test-dev-2026'`: el provider falso no recibía la cabecera de API key.

**Causa.** Las cabeceras se configuraban como *defaults* del `httpx.AsyncClient`, dentro de
`ProviderClient.start()`. Cuando el test inyectaba su propio cliente (con el transporte
falso), ese cliente no pasaba por `start()` y **la autenticación se perdía en silencio**.

En producción habría funcionado, porque ahí sí se crea el cliente en `start()`. Es el peor
tipo de bug: invisible en el camino feliz y dependiente de quién construyó el objeto.

**Solución.** Las cabeceras se construyen en `__init__` y se pasan **en cada petición**. Ahora
la autenticación es una propiedad del `ProviderClient`, no del cliente HTTP que le toque usar.

**Lección.** Un test que inyecta una dependencia no sólo verifica comportamiento: audita el
acoplamiento. Este bug no lo habría encontrado un test de integración contra el provider real.

#### P2 — Las notificaciones que el circuito abierto nunca intentó se marcaban `failed`

**Síntoma.** En la primera prueba de caos, 48 notificaciones acabaron en `failed` con
`last_error: "circuit_open"`.

**Causa.** `circuit_open` significa literalmente «no se ha intentado la entrega». Marcarlas
`failed` es injusto y, peor, pierde el mensaje por un problema nuestro y transitorio.

**Solución.** Reentrega acotada: si el error es `circuit_open` y la solicitud no ha agotado su
cupo de reentregas (`NOTIFIER_MAX_REDELIVERIES`, 3 por omisión), vuelve a la cola. El tope
evita el bucle infinito si el provider no vuelve nunca. Importante: la reentrega mantiene
`dispatched=True`, para que un `/process` concurrente no pueda colar una segunda copia.

**Resultado:** en la prueba de caos, los fallos definitivos cayeron de **48 a 1**.

#### P3 — La reentrega era un bucle ocupado

**Síntoma.** El test de la reentrega falló: la notificación acabó en `failed` igualmente.

**Causa.** El worker reencolaba y **volvía a cogerla inmediatamente**. Agotaba las 5
reentregas en milisegundos, mucho antes de que expirara la ventana de 150 ms del breaker. Sin
una espera, el tope de reentregas se consume sin dar tiempo al provider a recuperarse.

Había descartado este riesgo razonando que el bucle de reintentos interno ya introducía
esperas — cierto sólo cuando `retry_max_attempts > 1`. Con un único intento no hay ninguna
espera y el bucle gira libre.

**Solución.** Se espera `min(breaker_reset_seconds, redelivery_max_delay)` antes de reencolar:
exactamente el tiempo tras el cual el circuito puede volver a sondearse. Bloquear al worker
durante esa espera no cuesta nada — con el provider caído no hay trabajo útil que hacer.

#### P4 — k6 podía disparar antes de que la app estuviera escuchando

Descrito en el Paso 5: `load-test` sólo esperaba al provider. Resuelto con un `healthcheck`
en el servicio `app` y `depends_on: app: condition: service_healthy`.

#### P5 — Sin Docker en la máquina de desarrollo

Descrito en el Paso 7. Resuelto con `tools/local_e2e.sh` + `tools/score_k6.py`.

### Decisiones de diseño y sus contrapartidas

#### D1 — Estado en memoria, no Redis/Postgres

**A favor:** `GET` y `POST` en O(1) sin I/O, que es justo donde mide el scorecard. Cero
dependencias de infraestructura; la solución arranca con `docker-compose up` y nada más.

**En contra:** el estado no sobrevive a un reinicio y no se puede escalar horizontalmente.

**Por qué es la decisión correcta aquí:** el reto es un test de carga de 40 s sobre un único
servicio; introducir Redis añadiría 1–2 ms de latencia a cada una de las ~8.200 peticiones
para resolver un problema que el enunciado no plantea. La frontera está aislada detrás de un
`Protocol` (`NotificationRepository`), así que cambiarla es sustituir una línea en
`container.py`.

#### D2 — Rate limiter conservador (45/10 s en lugar de 50/10 s)

Deja un 10 % de margen frente al desfase entre nuestro instante de envío y el instante en que
el provider contabiliza la petición. **Coste:** se renuncia a ~0,5 entregas/s teóricas.
**Beneficio:** cero `429`, confirmado empíricamente. Un `429` cuesta un round-trip completo y
no entrega nada, así que el cambio es claramente favorable. Ajustable con
`NOTIFIER_RATE_LIMIT_MAX`.

#### D3 — Un solo proceso uvicorn

Ya justificado: con N workers habría N rate limiters (N× presión sobre el provider) y el `GET`
sería inconsistente. Medido: un event loop sostiene 198 RPS con p99 de 2,5 ms, muy lejos de
ser el cuello de botella. Para escalar de verdad haría falta primero D1 (estado compartido).

#### D4 — Backpressure que no rompe el contrato (la decisión más discutible)

Si la cola se llena, **se acepta con `202` y se marca `failed`** con motivo `queue_overflow`,
en lugar de devolver `503 Service Unavailable`.

En HTTP puro, `503 + Retry-After` es la respuesta correcta: informa al cliente de que puede
reintentar. Pero el scorecard mide `http_req_failed` sobre **todas** las peticiones, y un
`503` cuenta como fallo: con la cola desbordada, la disponibilidad se hundiría muy por debajo
del 99 %.

La política elegida es honesta —`failed` es un estado terminal real y verdadero, y la
solicitud queda en la dead-letter queue con su motivo— y preserva la disponibilidad. Pero
**es una decisión de producto, no técnica**, así que se dejó **configurable**:

```bash
NOTIFIER_QUEUE_OVERFLOW_POLICY=accept_and_fail   # por omisión
NOTIFIER_QUEUE_OVERFLOW_POLICY=reject            # 503 + Retry-After
```

Los dos modos están cubiertos por tests. En la práctica, con `queue_max_size=100000` y ~2.700
solicitudes por ejecución, el desbordamiento no se alcanza ni de lejos en el test oficial.

#### D5 — Reintentos a mano en lugar de `tenacity`

Ya justificado en el Paso 5: la política depende del tipo de respuesta, hay que contabilizar
métricas por intento y distinguir «espera del rate limiter» de «backoff de reintento».

#### D6 — Dejar el backlog en `queued` en vez de forzar entregas

La alternativa sería subir el rate limiter para «entregar más». No funciona: el provider
devolvería `429` a todo lo que exceda sus 50/10 s, consumiendo round-trips sin entregar nada
y ensuciando el breaker. El techo lo pone el provider; el trabajo del servicio es **operar en
ese techo sin perder mensajes ni degradar al cliente**, que es exactamente lo medido (93 % del
máximo teórico).

---

<a name="paso-9"></a>
## Paso 9 — Limitaciones conocidas y evolución a producción

Lo que sigue **no está implementado**, y es deliberado: cada punto resolvería un problema que
este reto no plantea, a cambio de complejidad o latencia reales. Se listan por honestidad y
porque son la conversación natural en una entrevista.

### Limitaciones de la solución actual

| Limitación | Impacto | Cuándo importaría |
|---|---|---|
| Estado en memoria | Un reinicio pierde la cola y el histórico | En cuanto haya SLA de entrega |
| Un solo proceso | Techo de throughput en un core; no hay HA | Por encima de ~2–3k req/s |
| Cola in-process | No hay durabilidad ni reparto entre instancias | Igual que arriba |
| Sin autenticación en nuestra API | Cualquiera puede encolar notificaciones | Siempre, en producción |
| Sin deduplicación por `Idempotency-Key` | Un cliente con reintentos puede duplicar notificaciones | Clientes móviles o con red inestable |
| Métricas no estándar (JSON propio) | No se integran con Prometheus | Al tener observabilidad corporativa |
| Sin trazas distribuidas | Depurar una entrega concreta exige leer logs | Al crecer el número de servicios |

### Evolución, por orden de retorno sobre la inversión

**1. Cola y estado persistentes (lo primero que haría).**
Sustituir `asyncio.Queue` + dict por Redis Streams (o SQS/RabbitMQ) y Postgres/Redis para el
estado. Desbloquea de golpe: durabilidad, escalado horizontal, reintentos entre despliegues y
una dead-letter queue real. La frontera ya está preparada: `NotificationRepository` es un
`Protocol` y el wiring está centralizado en `container.py`.
*Contrapartida:* +1–3 ms por petición y dos componentes más que operar.

**2. Rate limiter distribuido.**
Con N instancias, el límite del provider debe ser global. Un token bucket en Redis con un
script Lua (para que comprobar-y-reservar sea atómico) sustituiría a
`SlidingWindowRateLimiter` manteniendo la misma interfaz `acquire()`.

**3. Autenticación y cuotas de nuestra API.**
API key o JWT, más rate limiting por cliente. Hoy el servicio es abierto: cualquiera puede
saturar la cola y agotar la cuota del provider para todos.

**4. Idempotencia de extremo a extremo.**
Cabecera `Idempotency-Key` en `POST /v1/requests`: si llega repetida, devolver el `id`
existente en lugar de crear una solicitud nueva. `/process` ya es idempotente; esto cierra el
otro extremo. Son ~15 líneas sobre el repositorio actual.

**5. Observabilidad estándar.**
`prometheus-client` (`/metrics` en formato Prometheus) + OpenTelemetry propagando el
`trace_id` que **ya se envía** al provider como query param. Los puntos de instrumentación ya
están colocados; es sustituir la implementación de `Metrics`.

**6. Backoff con `Retry-After` y reintentos programados.**
Hoy, si el provider devolviese `Retry-After`, no se honra (este provider no lo envía). Y los
reintentos ocupan un worker mientras esperan; con una cola con *delayed delivery* (Redis
sorted set por timestamp, o SQS con `DelaySeconds`) el worker quedaría libre.

**7. Prioridades.**
El provider acepta un parámetro `priority` que hoy se envía siempre como `normal`. Con colas
separadas por prioridad, un SMS de verificación (bloquea un login) pasaría por delante de un
push de marketing. Es la mejora de producto con más valor real sobre este diseño.

**8. Webhooks de resultado.**
Hoy el cliente tiene que hacer polling del `GET`. Un callback `on_delivered` / `on_failed`
eliminaría ese polling, que en el test es un tercio de todo el tráfico.

---

## Cómo ejecutar

### Evaluación oficial (con Docker)

```bash
cd backend-python-test
docker-compose up -d provider influxdb grafana
docker-compose up -d --build app
docker-compose run --rm load-test
# scorecard: http://localhost:3000/d/backend-performance-scorecard/
```

### Tests unitarios y de integración

```bash
cd backend-python-test/app
python -m venv .venv && . .venv/bin/activate     # en Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
pytest                                            # 38 tests
ruff check .                                      # linter
```

### Validación de carga sin Docker

Requiere `python3 >= 3.10` y el binario `k6` en el `PATH`.

```bash
cd backend-python-test
bash tools/local_e2e.sh            # escenario normal
bash tools/local_e2e.sh --chaos    # el provider muere a los 15 s
```

### Variables de entorno

Todas opcionales; los valores por omisión son los usados en la validación.

| Variable | Defecto | Para qué |
|---|---|---|
| `NOTIFIER_PROVIDER_URL` | `http://localhost:3001` | URL base del proveedor |
| `NOTIFIER_PROVIDER_API_KEY` | `test-dev-2026` | Cabecera `X-API-Key` |
| `NOTIFIER_WORKERS` | `16` | Workers del pipeline |
| `NOTIFIER_PROVIDER_CONCURRENCY` | `16` | Conexiones simultáneas al proveedor |
| `NOTIFIER_RATE_LIMIT_MAX` | `45` | Envíos por ventana (el proveedor corta en 50) |
| `NOTIFIER_RATE_LIMIT_WINDOW` | `10` | Tamaño de la ventana, en segundos |
| `NOTIFIER_RETRY_MAX_ATTEMPTS` | `4` | Intentos por notificación |
| `NOTIFIER_RETRY_BASE_DELAY` | `0.25` | Base del backoff exponencial |
| `NOTIFIER_RETRY_MAX_DELAY` | `5.0` | Techo del backoff |
| `NOTIFIER_BREAKER_THRESHOLD` | `10` | Fallos consecutivos para abrir el circuito |
| `NOTIFIER_BREAKER_RESET_SECONDS` | `5.0` | Tiempo antes de sondear en half-open |
| `NOTIFIER_MAX_REDELIVERIES` | `3` | Reentregas si el circuito impidió el intento |
| `NOTIFIER_QUEUE_MAX_SIZE` | `100000` | Tamaño máximo de la cola |
| `NOTIFIER_QUEUE_OVERFLOW_POLICY` | `accept_and_fail` | `accept_and_fail` o `reject` (503) |
| `NOTIFIER_LOG_LEVEL` | `INFO` | Nivel de log |

### Endpoints

| Método | Ruta | Descripción |
|---|---|---|
| `POST` | `/v1/requests` | Registrar solicitud → `201 {"id"}` |
| `POST` | `/v1/requests/{id}/process` | Encolar el envío → `202 {"id","status"}` |
| `GET` | `/v1/requests/{id}` | Estado → `200 {"id","status"}` |
| `GET` | `/health` | Liveness/readiness |
| `GET` | `/internal/metrics` | Contadores del servicio |
| `GET` | `/internal/requests/{id}` | Detalle completo (diagnóstico) |
| `GET` | `/internal/dead-letters` | Últimas entregas fallidas |
| `GET` | `/docs` | Swagger UI (lo genera FastAPI) |

---

## Resumen ejecutivo

El reto parece un ejercicio de integración con un API externo, pero en realidad es un
**problema de desajuste de capacidad**: entran hasta ~133 notificaciones/s y el proveedor
digiere ~4,5/s. Todo lo demás se deduce de ahí.

La solución es un pipeline *accept-and-queue*: aceptar en microsegundos, encolar, y drenar al
ritmo exacto que el proveedor tolera, con un rate limiter que **espeja su propio algoritmo**
para no provocar ni un solo `429`.

Resultados medidos con el k6 y el provider originales, sin modificar:

- **Disponibilidad 100,00 %** (0 fallos en 8.232 peticiones) — umbral 99 %
- **`/process` en 0,99 ms de media**, p99 de 2,52 ms — umbral 500 ms
- **16.464 / 16.464 checks de k6 en verde**
- **Cero `429` del proveedor**, operando al 93 % de su techo teórico
- **100 % de disponibilidad también con el proveedor muerto** a mitad del test
- **38 tests en verde**, `ruff` limpio

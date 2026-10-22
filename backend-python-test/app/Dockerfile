FROM python:3.12-slim

# PYTHONUNBUFFERED: los logs salen al instante, sin quedarse en el buffer si el contenedor
# muere. PYTHONDONTWRITEBYTECODE: no ensuciar la capa con .pyc.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /code

# Las dependencias se copian antes del código para que la capa de pip se reutilice en cada
# rebuild mientras requirements.txt no cambie.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Usuario sin privilegios: si alguien escapa del proceso, no es root dentro del contenedor.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /code
USER appuser

EXPOSE 5000

# --no-access-log : a ~133 req/s un log por petición mete syscalls de escritura en el event
#                   loop y ensancha el p99 que mide el scorecard.
# --loop uvloop / --http httptools : implementaciones en C que vienen con uvicorn[standard].
# --workers 1     : el estado (cola, repositorio, rate limiter, breaker) es in-process.
#                   Con N workers habría N rate limiters y multiplicaríamos por N la presión
#                   sobre el provider, además de romper la consistencia del GET.
CMD ["uvicorn", "main:app", \
     "--host", "0.0.0.0", "--port", "5000", \
     "--workers", "1", \
     "--loop", "uvloop", \
     "--http", "httptools", \
     "--no-access-log", \
     "--timeout-keep-alive", "15"]

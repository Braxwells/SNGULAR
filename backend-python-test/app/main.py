"""Punto de entrada ASGI: `uvicorn main:app`.

Se mantiene mínimo a propósito; toda la lógica vive en el paquete `notifier`.
"""

from notifier.app_factory import create_app

app = create_app()


if __name__ == "__main__":  # pragma: no cover - sólo para ejecución local sin Docker
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=5000, access_log=False)

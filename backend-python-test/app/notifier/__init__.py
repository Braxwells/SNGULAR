"""Notification Service: mediador resiliente entre clientes y el proveedor externo."""

__all__ = ["create_app"]


def __getattr__(name: str):  # pragma: no cover - sólo azúcar de importación
    if name == "create_app":
        from notifier.app_factory import create_app

        return create_app
    raise AttributeError(name)

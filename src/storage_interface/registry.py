"""Backend registry: look backends up by name, and plug in new ones.

Built-in backends are imported lazily, so ``import storage_interface`` never requires
``google-api-python-client`` or ``telethon`` unless you actually use them.

Third-party packages can ship a backend by declaring an entry point::

    [project.entry-points."storage_interface.backends"]
    dropbox = "my_pkg.dropbox:DropboxBackend"
"""

from __future__ import annotations

import importlib
from importlib import metadata
from typing import Any, Union

from .base import StorageBackend
from .errors import UnknownBackendError

ENTRY_POINT_GROUP = "storage_interface.backends"

_BUILTIN: dict[str, str] = {
    "local": "storage_interface.backends.local:LocalBackend",
    "gdrive": "storage_interface.backends.gdrive:GoogleDriveBackend",
    "telegram": "storage_interface.backends.telegram:TelegramBackend",
}
_registered: dict[str, type[StorageBackend]] = {}


def register_backend(
    name: str, backend: type[StorageBackend], *, replace: bool = False
) -> None:
    """Register ``backend`` under ``name`` for use with :func:`create_backend`."""
    if not (isinstance(backend, type) and issubclass(backend, StorageBackend)):
        raise TypeError("backend must be a StorageBackend subclass")
    if not replace and (name in _registered or name in _BUILTIN):
        raise ValueError(f"backend {name!r} is already registered")
    _registered[name] = backend


def _entry_points() -> dict[str, Any]:
    try:
        return {ep.name: ep for ep in metadata.entry_points(group=ENTRY_POINT_GROUP)}
    except Exception:  # pragma: no cover - defensive against odd environments
        return {}


def available_backends() -> list[str]:
    return sorted(set(_BUILTIN) | set(_registered) | set(_entry_points()))


def get_backend_class(name: str) -> type[StorageBackend]:
    if name in _registered:
        return _registered[name]
    target: Union[str, None] = _BUILTIN.get(name)
    if target is not None:
        module_name, _, attr = target.partition(":")
        return getattr(importlib.import_module(module_name), attr)
    entry_point = _entry_points().get(name)
    if entry_point is not None:
        cls = entry_point.load()
        if not (isinstance(cls, type) and issubclass(cls, StorageBackend)):
            raise TypeError(f"entry point {name!r} is not a StorageBackend subclass")
        return cls
    raise UnknownBackendError(
        f"unknown backend {name!r}; available: {', '.join(available_backends())}"
    )


def create_backend(name: str, **config: Any) -> StorageBackend:
    """Instantiate a backend by name.  Use it as ``async with create_backend(...)``."""
    return get_backend_class(name)(**config)

"""The backend-neutral interface.

Every storage service is exposed through :class:`StorageBackend`.  Paths are
always POSIX-style, absolute *within the backend* (``/photos/2026/a.jpg``),
regardless of how the underlying service models folders.
"""

from __future__ import annotations

import abc
import asyncio
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import AsyncIterator, Callable, ClassVar, Optional, Union

from .errors import InvalidPathError, NotFoundError, NotSupportedError

PathLike = Union[str, Path]

#: ``progress(bytes_done, bytes_total)``.  Depending on the backend this may be
#: invoked from a worker thread, so keep callbacks quick and thread-safe.
ProgressCallback = Callable[[int, int], None]


class Capability(str, Enum):
    """Optional features a backend may or may not offer."""

    MOVE = "move"
    COPY = "copy"
    TRASH = "trash"  # delete() is recoverable (e.g. Drive trash)


@dataclass(frozen=True)
class FileInfo:
    """Metadata about a file or folder."""

    path: str
    name: str
    is_dir: bool
    size: Optional[int] = None
    modified: Optional[datetime] = None
    mime_type: Optional[str] = None
    #: Provider-native identifier (Drive file id, Telegram message id, ...).
    backend_id: Optional[str] = None
    #: Provider-specific extras (checksums, ...). Not part of equality.
    extra: dict = field(default_factory=dict, compare=False)


# --------------------------------------------------------------------------- #
# Path helpers
# --------------------------------------------------------------------------- #
def normalize_path(path: str) -> str:
    """Return ``path`` as a canonical absolute POSIX path (``/a/b``).

    Empty segments and ``.`` are dropped.  ``..`` is rejected rather than
    resolved, so a path can never climb out of a backend's root.
    """
    if not isinstance(path, str):
        raise InvalidPathError(f"path must be str, got {type(path).__name__}")
    if "\x00" in path:
        raise InvalidPathError("path contains a NUL byte")
    parts = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise InvalidPathError("'..' is not allowed in paths")
        parts.append(part)
    return "/" + "/".join(parts)


def split_path(path: str) -> tuple[str, str]:
    """Split a normalized path into ``(parent, name)``. Root gives ``("/", "")``."""
    path = normalize_path(path)
    if path == "/":
        return "/", ""
    parent, _, name = path.rpartition("/")
    return (parent or "/"), name


def join_path(parent: str, name: str) -> str:
    return normalize_path(parent.rstrip("/") + "/" + name)


# --------------------------------------------------------------------------- #
# The interface
# --------------------------------------------------------------------------- #
class StorageBackend(abc.ABC):
    """Abstract base class implemented by every storage service."""

    #: Registry name, e.g. ``"gdrive"``.
    name: ClassVar[str] = ""
    capabilities: ClassVar[frozenset[Capability]] = frozenset()

    # -- lifecycle ---------------------------------------------------------- #
    async def connect(self) -> None:
        """Authenticate / open connections. Safe to skip for simple backends."""

    async def close(self) -> None:
        """Release connections and file handles."""

    async def __aenter__(self) -> "StorageBackend":
        await self.connect()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    # -- required operations ------------------------------------------------ #
    @abc.abstractmethod
    async def stat(self, path: str) -> FileInfo:
        """Return metadata for ``path`` or raise :class:`NotFoundError`."""

    @abc.abstractmethod
    def iterdir(self, path: str = "/") -> AsyncIterator[FileInfo]:
        """Yield the direct children of a folder (implement as an async generator)."""

    @abc.abstractmethod
    async def mkdir(
        self, path: str, *, parents: bool = True, exist_ok: bool = True
    ) -> FileInfo:
        """Create a folder."""

    @abc.abstractmethod
    async def upload_file(
        self,
        local_path: PathLike,
        remote_path: str,
        *,
        overwrite: bool = False,
        progress: Optional[ProgressCallback] = None,
    ) -> FileInfo:
        """Upload a local file. Missing parent folders are created."""

    @abc.abstractmethod
    async def download_file(
        self,
        remote_path: str,
        local_path: PathLike,
        *,
        overwrite: bool = False,
        progress: Optional[ProgressCallback] = None,
    ) -> Path:
        """Download a remote file to ``local_path`` and return that path."""

    @abc.abstractmethod
    async def delete(self, path: str, *, recursive: bool = False) -> None:
        """Delete a file, or a folder (non-empty folders need ``recursive``)."""

    # -- optional operations ------------------------------------------------ #
    async def move(self, src: str, dst: str, *, overwrite: bool = False) -> FileInfo:
        raise NotSupportedError(f"{self.name or type(self).__name__} cannot move")

    async def copy(self, src: str, dst: str, *, overwrite: bool = False) -> FileInfo:
        raise NotSupportedError(f"{self.name or type(self).__name__} cannot copy")

    # -- conveniences built on the primitives ------------------------------- #
    async def exists(self, path: str) -> bool:
        try:
            await self.stat(path)
        except NotFoundError:
            return False
        return True

    async def listdir(self, path: str = "/") -> list[FileInfo]:
        return [info async for info in self.iterdir(path)]

    async def walk(self, path: str = "/") -> AsyncIterator[FileInfo]:
        """Recursively yield every descendant of ``path`` (folders before contents)."""
        stack = [normalize_path(path)]
        while stack:
            current = stack.pop()
            async for info in self.iterdir(current):
                yield info
                if info.is_dir:
                    stack.append(info.path)

    async def read_bytes(self, path: str) -> bytes:
        """Download a file into memory. Prefer :meth:`download_file` for big files."""
        with tempfile.TemporaryDirectory(prefix="storage_interface-") as tmp:
            target = Path(tmp) / "blob"
            await self.download_file(path, target)
            return await asyncio.to_thread(target.read_bytes)

    async def write_bytes(
        self,
        path: str,
        data: bytes,
        *,
        overwrite: bool = False,
        progress: Optional[ProgressCallback] = None,
    ) -> FileInfo:
        """Upload an in-memory blob."""
        with tempfile.TemporaryDirectory(prefix="storage_interface-") as tmp:
            # Neutral staging name: backends take the file name from ``path``,
            # and a remote name may be illegal or too long for the local disk.
            source = Path(tmp) / "payload"
            await asyncio.to_thread(source.write_bytes, data)
            return await self.upload_file(
                source, path, overwrite=overwrite, progress=progress
            )

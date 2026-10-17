"""Local-disk backend.

Mostly useful as a reference implementation, for tests, and for staging
between two cloud services, but it is a fully working backend.
"""

from __future__ import annotations

import asyncio
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Optional

from ..base import (
    Capability,
    FileInfo,
    PathLike,
    ProgressCallback,
    StorageBackend,
    normalize_path,
)
from ..errors import (
    AlreadyExistsError,
    DirectoryNotEmptyError,
    InvalidPathError,
    NotAFileError,
    NotAFolderError,
    NotFoundError,
)


class LocalBackend(StorageBackend):
    """Expose a directory on local disk as a storage backend."""

    name = "local"
    capabilities = frozenset({Capability.MOVE, Capability.COPY})

    def __init__(self, root: PathLike) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # -- helpers ------------------------------------------------------------ #
    def _fs(self, path: str) -> Path:
        norm = normalize_path(path)
        real = (self.root / norm.lstrip("/")).resolve()
        if real != self.root and self.root not in real.parents:
            raise InvalidPathError(f"{path!r} escapes the backend root")
        return real

    def _virtual(self, real: Path) -> str:
        rel = real.relative_to(self.root).as_posix()
        return "/" if rel == "." else "/" + rel

    def _info(self, real: Path) -> FileInfo:
        st = real.stat()
        is_dir = real.is_dir()
        return FileInfo(
            path=self._virtual(real),
            name=real.name,
            is_dir=is_dir,
            size=None if is_dir else st.st_size,
            modified=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
        )

    # -- required operations ------------------------------------------------ #
    async def stat(self, path: str) -> FileInfo:
        real = self._fs(path)
        if not real.exists():
            raise NotFoundError(normalize_path(path))
        return await asyncio.to_thread(self._info, real)

    async def iterdir(self, path: str = "/") -> AsyncIterator[FileInfo]:
        real = self._fs(path)
        if not real.exists():
            raise NotFoundError(normalize_path(path))
        if not real.is_dir():
            raise NotAFolderError(normalize_path(path))
        children = await asyncio.to_thread(lambda: sorted(real.iterdir()))
        for child in children:
            yield await asyncio.to_thread(self._info, child)

    async def mkdir(
        self, path: str, *, parents: bool = True, exist_ok: bool = True
    ) -> FileInfo:
        real = self._fs(path)
        if real.exists() and not (real.is_dir() and exist_ok):
            raise AlreadyExistsError(normalize_path(path))
        if not parents and not real.parent.exists():
            raise NotFoundError(self._virtual(real.parent))
        await asyncio.to_thread(real.mkdir, parents=True, exist_ok=True)
        return self._info(real)

    async def upload_file(
        self,
        local_path: PathLike,
        remote_path: str,
        *,
        overwrite: bool = False,
        progress: Optional[ProgressCallback] = None,
    ) -> FileInfo:
        source = Path(local_path)
        if not source.is_file():
            raise NotAFileError(str(source))
        target = self._fs(remote_path)
        if target == self.root or target.is_dir():
            raise NotAFileError(normalize_path(remote_path))
        if target.exists() and not overwrite:
            raise AlreadyExistsError(normalize_path(remote_path))
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.copyfile, source, target)
        if progress:
            size = target.stat().st_size
            progress(size, size)
        return self._info(target)

    async def download_file(
        self,
        remote_path: str,
        local_path: PathLike,
        *,
        overwrite: bool = False,
        progress: Optional[ProgressCallback] = None,
    ) -> Path:
        source = self._fs(remote_path)
        if not source.exists():
            raise NotFoundError(normalize_path(remote_path))
        if source.is_dir():
            raise NotAFileError(normalize_path(remote_path))
        target = Path(local_path)
        if target.exists() and not overwrite:
            raise AlreadyExistsError(str(target))
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.copyfile, source, target)
        if progress:
            size = target.stat().st_size
            progress(size, size)
        return target

    async def delete(self, path: str, *, recursive: bool = False) -> None:
        real = self._fs(path)
        if real == self.root:
            raise InvalidPathError("cannot delete the backend root")
        if not real.exists():
            raise NotFoundError(normalize_path(path))
        if real.is_dir():
            if any(real.iterdir()) and not recursive:
                raise DirectoryNotEmptyError(normalize_path(path))
            await asyncio.to_thread(shutil.rmtree, real)
        else:
            await asyncio.to_thread(real.unlink)

    # -- optional operations ------------------------------------------------ #
    async def move(self, src: str, dst: str, *, overwrite: bool = False) -> FileInfo:
        source, target = self._fs(src), self._fs(dst)
        if not source.exists():
            raise NotFoundError(normalize_path(src))
        if source == self.root:
            raise InvalidPathError("cannot move the backend root")
        if target.exists():
            if not overwrite:
                raise AlreadyExistsError(normalize_path(dst))
            await self.delete(dst, recursive=True)
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.move, str(source), str(target))
        return self._info(target)

    async def copy(self, src: str, dst: str, *, overwrite: bool = False) -> FileInfo:
        source, target = self._fs(src), self._fs(dst)
        if not source.exists():
            raise NotFoundError(normalize_path(src))
        if target.exists():
            if not overwrite:
                raise AlreadyExistsError(normalize_path(dst))
            await self.delete(dst, recursive=True)
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        if source.is_dir():
            await asyncio.to_thread(shutil.copytree, source, target)
        else:
            await asyncio.to_thread(shutil.copy2, source, target)
        return self._info(target)

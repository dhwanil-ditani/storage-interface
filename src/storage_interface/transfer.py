"""Helpers for moving data *between* two backends (Drive -> Telegram, ...)."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Optional

from .base import (
    FileInfo,
    ProgressCallback,
    StorageBackend,
    join_path,
    normalize_path,
)


async def copy_between(
    src: StorageBackend,
    src_path: str,
    dst: StorageBackend,
    dst_path: str,
    *,
    overwrite: bool = False,
    progress: Optional[ProgressCallback] = None,
) -> FileInfo:
    """Copy one file from ``src`` to ``dst`` via a temporary local file.

    Only one file is held on disk at a time, so this is safe for large files.
    ``progress`` reports the *upload* leg.
    """
    with tempfile.TemporaryDirectory(prefix="storage_interface-") as tmp:
        # Neutral staging name: the destination backend names the file from
        # ``dst_path``, which may not be a legal name on the local disk.
        staged = Path(tmp) / "payload"
        await src.download_file(src_path, staged)
        return await dst.upload_file(
            staged, dst_path, overwrite=overwrite, progress=progress
        )


async def copy_tree(
    src: StorageBackend,
    src_dir: str,
    dst: StorageBackend,
    dst_dir: str,
    *,
    overwrite: bool = False,
) -> int:
    """Recursively copy a folder between backends. Returns the number of files copied."""
    src_dir = normalize_path(src_dir)
    dst_dir = normalize_path(dst_dir)
    await dst.mkdir(dst_dir, parents=True, exist_ok=True)
    copied = 0
    async for info in src.walk(src_dir):
        relative = info.path[len(src_dir):].lstrip("/") if src_dir != "/" else info.path.lstrip("/")
        target = join_path(dst_dir, relative)
        if info.is_dir:
            await dst.mkdir(target, parents=True, exist_ok=True)
        else:
            await copy_between(src, info.path, dst, target, overwrite=overwrite)
            copied += 1
    return copied

"""Telegram backend: use a chat or channel as file storage (MTProto via Telethon).

Telegram has no folders, listings or paths, only messages.  This backend turns
a chat into a filesystem like this:

* every file is a *document message* in one chat/channel (``channel=``;
  ``"me"`` is your Saved Messages);
* the virtual path is written into the message **caption**, and mirrored in a
  local SQLite **index** (``index_path=``) that provides ``stat``, ``iterdir``,
  folders and ``move``;
* if the index is lost, :meth:`TelegramBackend.rebuild_index` re-creates it
  from the captions (empty folders are not recoverable).

Limits worth knowing: 2000 MiB per file (4000 MiB with Telegram Premium),
empty files are rejected, and folders exist only in the index.

Authentication: Telethon needs an ``api_id``/``api_hash`` from
https://my.telegram.org.  Log in either as a user (the first ``connect()``
prompts for phone and code, then the session file is reused) or as a bot via
``bot_token=`` (add the bot to the channel as an admin).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from ..base import (
    Capability,
    FileInfo,
    PathLike,
    ProgressCallback,
    StorageBackend,
    normalize_path,
    split_path,
)
from ..errors import (
    AlreadyExistsError,
    BackendNotInstalledError,
    DirectoryNotEmptyError,
    InvalidPathError,
    NotAFileError,
    NotAFolderError,
    NotFoundError,
    NotSupportedError,
    StorageError,
)
from ._pathindex import IndexEntry, PathIndex

log = logging.getLogger(__name__)

MAX_FILE_SIZE = 2000 * 1024 * 1024  # 4000 MiB for Premium accounts
MAX_CAPTION = 1024  # Telegram's caption limit for non-Premium accounts


def _filename_attributes(name: str) -> Optional[list]:
    """Force the document's displayed file name (Telethon would use the temp name)."""
    try:
        from telethon.tl.types import DocumentAttributeFilename
    except ImportError:  # only reachable with an injected client and no telethon
        return None
    return [DocumentAttributeFilename(file_name=name)]


class TelegramBackend(StorageBackend):
    name = "telegram"
    capabilities = frozenset({Capability.MOVE})

    def __init__(
        self,
        *,
        api_id: Optional[int] = None,
        api_hash: Optional[str] = None,
        session: Any = "storage_interface_telegram",
        channel: Any = "me",
        index_path: PathLike = "storage_interface_telegram_index.sqlite",
        bot_token: Optional[str] = None,
        phone: Optional[str] = None,
        password: Optional[str] = None,
        max_file_size: int = MAX_FILE_SIZE,
        client: Any = None,
    ) -> None:
        """
        :param session: Telethon session name/path, or a ``StringSession``.
        :param channel: Where files live: ``"me"``, a username, or a numeric id.
        :param index_path: SQLite file for the path index (``":memory:"`` for tests).
        :param client: A ready-made, already-started ``TelegramClient``. The
            backend will not log in or disconnect it.
        """
        if client is None and (api_id is None or not api_hash):
            raise ValueError("api_id and api_hash are required (or pass client=)")
        self._api_id = api_id
        self._api_hash = api_hash
        self._session = session
        self._channel = channel
        self._bot_token = bot_token
        self._phone = phone
        self._password = password
        self._max_file_size = max_file_size
        self._client = client
        self._owns_client = client is None
        self._entity: Any = None
        self._index = PathIndex(str(index_path))

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def connect(self) -> None:
        if self._client is None:
            try:
                from telethon import TelegramClient
            except ImportError as exc:  # pragma: no cover - depends on environment
                raise BackendNotInstalledError(
                    "telethon is required for the Telegram backend: "
                    "pip install 'storage-interface[telegram]'"
                ) from exc
            self._client = TelegramClient(self._session, self._api_id, self._api_hash)
            await self._client.start(
                phone=self._phone, password=self._password, bot_token=self._bot_token
            )
        self._entity = await self._client.get_input_entity(self._channel)

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None and self._owns_client:
            await client.disconnect()
        self._entity = None
        self._index.close()

    def _tg(self) -> Any:
        if self._client is None or self._entity is None:
            raise StorageError("backend is not connected; use 'async with' or connect()")
        return self._client

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _info(entry: IndexEntry) -> FileInfo:
        modified = (
            datetime.fromtimestamp(entry.mtime, tz=timezone.utc) if entry.mtime else None
        )
        return FileInfo(
            path=entry.path,
            name=entry.name,
            is_dir=entry.is_dir,
            size=None if entry.is_dir else entry.size,
            modified=modified,
            mime_type=entry.mime,
            backend_id=None if entry.message_id is None else str(entry.message_id),
        )

    def _ensure_parent_dirs(self, path: str) -> None:
        """Create parent folder rows, refusing to build a folder under a file."""
        parent = split_path(path)[0]
        probe = ""
        for part in [p for p in parent.split("/") if p]:
            probe += "/" + part
            existing = self._index.get(probe)
            if existing is not None and not existing.is_dir:
                raise NotAFolderError(f"{probe} is a file")
        self._index.ensure_dirs(parent)

    async def _delete_messages(self, message_ids: list[int]) -> None:
        if message_ids:
            await self._tg().delete_messages(self._entity, message_ids)

    async def _clear_destination(self, dst: str, overwrite: bool) -> None:
        existing = self._index.get(dst)
        if existing is None:
            return
        if not overwrite:
            raise AlreadyExistsError(dst)
        await self.delete(dst, recursive=True)

    # ------------------------------------------------------------------ #
    # Required operations
    # ------------------------------------------------------------------ #
    async def stat(self, path: str) -> FileInfo:
        path = normalize_path(path)
        if path == "/":
            return FileInfo(path="/", name="", is_dir=True)
        entry = self._index.get(path)
        if entry is None:
            raise NotFoundError(path)
        return self._info(entry)

    async def iterdir(self, path: str = "/") -> AsyncIterator[FileInfo]:
        path = normalize_path(path)
        if path != "/":
            entry = self._index.get(path)
            if entry is None:
                raise NotFoundError(path)
            if not entry.is_dir:
                raise NotAFolderError(path)
        for child in self._index.children(path):
            yield self._info(child)

    async def mkdir(
        self, path: str, *, parents: bool = True, exist_ok: bool = True
    ) -> FileInfo:
        path = normalize_path(path)
        if path == "/":
            return FileInfo(path="/", name="", is_dir=True)
        existing = self._index.get(path)
        if existing is not None:
            if not existing.is_dir or not exist_ok:
                raise AlreadyExistsError(path)
            return self._info(existing)
        parent = split_path(path)[0]
        if not parents and parent != "/" and self._index.get(parent) is None:
            raise NotFoundError(parent)
        self._ensure_parent_dirs(path)
        self._index.ensure_dirs(path)
        return self._info(self._index.get(path))  # type: ignore[arg-type]

    async def upload_file(
        self,
        local_path: PathLike,
        remote_path: str,
        *,
        overwrite: bool = False,
        progress: Optional[ProgressCallback] = None,
    ) -> FileInfo:
        client = self._tg()
        source = Path(local_path)
        if not source.is_file():
            raise NotAFileError(str(source))
        path = normalize_path(remote_path)
        name = split_path(path)[1]
        if not name:
            raise InvalidPathError("cannot upload to the root folder itself")
        if len(path) > MAX_CAPTION:
            raise InvalidPathError(f"path longer than {MAX_CAPTION} characters")
        size = source.stat().st_size
        if size == 0:
            raise NotSupportedError("Telegram cannot store empty files")
        if size > self._max_file_size:
            raise StorageError(
                f"{size} bytes exceeds the {self._max_file_size}-byte Telegram limit"
            )

        existing = self._index.get(path)
        if existing is not None and (existing.is_dir or not overwrite):
            raise AlreadyExistsError(path)
        self._ensure_parent_dirs(path)

        kwargs: dict[str, Any] = {"caption": path, "force_document": True}
        attributes = _filename_attributes(name)
        if attributes:
            kwargs["attributes"] = attributes
        if progress:
            kwargs["progress_callback"] = progress
        message = await client.send_file(self._entity, str(source), **kwargs)

        # Only drop the previous version once the new one is safely stored.
        if existing is not None and existing.message_id is not None:
            await self._delete_messages([existing.message_id])

        file_meta = getattr(message, "file", None)
        date = getattr(message, "date", None)
        entry = IndexEntry(
            path=path,
            is_dir=False,
            size=size,
            mtime=date.timestamp() if date else datetime.now(timezone.utc).timestamp(),
            message_id=message.id,
            mime=getattr(file_meta, "mime_type", None),
        )
        self._index.put(entry)
        return self._info(entry)

    async def download_file(
        self,
        remote_path: str,
        local_path: PathLike,
        *,
        overwrite: bool = False,
        progress: Optional[ProgressCallback] = None,
    ) -> Path:
        client = self._tg()
        path = normalize_path(remote_path)
        entry = self._index.get(path)
        if entry is None:
            raise NotFoundError(path)
        if entry.is_dir:
            raise NotAFileError(path)
        target = Path(local_path)
        if target.exists() and not overwrite:
            raise AlreadyExistsError(str(target))

        message = await client.get_messages(self._entity, ids=entry.message_id)
        if message is None or not getattr(message, "file", None):
            raise NotFoundError(
                f"{path}: message {entry.message_id} no longer exists in the channel "
                "(index is stale; try rebuild_index())"
            )

        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")  # keeps an extension, see Telethon
        kwargs: dict[str, Any] = {"file": str(part)}
        if progress:
            kwargs["progress_callback"] = progress
        saved = None
        try:
            saved = await client.download_media(message, **kwargs)
            if not saved:
                raise NotFoundError(f"{path}: message has no downloadable media")
            os.replace(saved, target)
        except BaseException:
            for leftover in {str(part), str(saved) if saved else None}:
                if leftover:
                    Path(leftover).unlink(missing_ok=True)
            raise
        return target

    async def delete(self, path: str, *, recursive: bool = False) -> None:
        path = normalize_path(path)
        if path == "/":
            raise InvalidPathError("cannot delete the root folder")
        entry = self._index.get(path)
        if entry is None:
            raise NotFoundError(path)
        doomed = [entry]
        if entry.is_dir:
            below = self._index.descendants(path)
            if below and not recursive:
                raise DirectoryNotEmptyError(path)
            doomed += below
        await self._delete_messages(
            [e.message_id for e in doomed if e.message_id is not None]
        )
        self._index.delete(e.path for e in doomed)

    # ------------------------------------------------------------------ #
    # Optional operations
    # ------------------------------------------------------------------ #
    async def move(self, src: str, dst: str, *, overwrite: bool = False) -> FileInfo:
        """Rename/move a file or folder.

        This only rewrites the index plus a best-effort caption edit per file,
        so it is cheap and never re-uploads data.
        """
        client = self._tg()
        src, dst = normalize_path(src), normalize_path(dst)
        if src == "/" or dst == "/":
            raise InvalidPathError("cannot move the root folder")
        if dst == src:
            return await self.stat(src)
        if dst.startswith(src + "/"):
            raise InvalidPathError("cannot move a folder into itself")
        entry = self._index.get(src)
        if entry is None:
            raise NotFoundError(src)
        if len(dst) > MAX_CAPTION:
            raise InvalidPathError(f"path longer than {MAX_CAPTION} characters")

        await self._clear_destination(dst, overwrite)
        self._ensure_parent_dirs(dst)
        affected = [entry, *self._index.descendants(src)]
        self._index.rename_tree(src, dst)

        for old in affected:
            if old.message_id is None:
                continue
            new_path = dst + old.path[len(src):]
            try:
                await client.edit_message(self._entity, old.message_id, text=new_path)
            except Exception as exc:  # captions only matter for rebuild_index()
                log.warning("could not update caption of message %s: %s", old.message_id, exc)
        return self._info(self._index.get(dst))  # type: ignore[arg-type]

    # ------------------------------------------------------------------ #
    # Telegram-specific
    # ------------------------------------------------------------------ #
    async def rebuild_index(self, *, clear: bool = True) -> int:
        """Re-create the index by scanning the channel for path captions.

        Returns the number of files indexed.  Messages whose caption is not an
        absolute path (``/...``) are ignored, so the channel can hold other posts.
        """
        client = self._tg()
        if clear:
            self._index.clear()
        count = 0
        async for message in client.iter_messages(self._entity):
            caption = getattr(message, "message", None) or ""
            file_meta = getattr(message, "file", None)
            if file_meta is None or not caption.startswith("/"):
                continue
            try:
                path = normalize_path(caption)
            except InvalidPathError:
                continue
            if path == "/":
                continue
            if self._index.get(path) is not None:
                continue  # iter_messages is newest-first: keep the latest upload
            try:
                self._ensure_parent_dirs(path)
            except NotAFolderError:
                log.warning("skipping %s: a parent path is already a file", path)
                continue
            date = getattr(message, "date", None)
            self._index.put(
                IndexEntry(
                    path=path,
                    is_dir=False,
                    size=getattr(file_meta, "size", None),
                    mtime=date.timestamp() if date else None,
                    message_id=message.id,
                    mime=getattr(file_meta, "mime_type", None),
                )
            )
            count += 1
        return count

"""Google Drive backend (Drive API v3).

Drive is an ID-based store: folders can contain several items with the same
name, and a "path" is just a chain of name lookups.  This backend resolves
paths by walking that chain and caches folder ids.  If two items in one folder
share a name, the oldest one wins.

Authentication (pick one):

* ``service_account_file=`` - server-to-server; share the target folder with
  the service account's e-mail, or use ``subject=`` for domain-wide delegation.
* ``token_file=`` (+ ``client_secrets_file=`` for the first run) - OAuth for a
  normal user account.  The first run opens a browser; the refreshable token is
  saved to ``token_file`` afterwards.
* ``credentials=`` - any ``google.auth`` credentials object you already have.
* ``service=`` - a pre-built Drive v3 service (mainly for tests).
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Mapping, Optional, Sequence

from ..base import (
    Capability,
    FileInfo,
    PathLike,
    ProgressCallback,
    StorageBackend,
    join_path,
    normalize_path,
    split_path,
)
from ..errors import (
    AlreadyExistsError,
    AuthError,
    BackendNotInstalledError,
    DirectoryNotEmptyError,
    InvalidPathError,
    NotAFileError,
    NotAFolderError,
    NotFoundError,
    NotSupportedError,
    RateLimitError,
    StorageError,
)

log = logging.getLogger(__name__)

FOLDER_MIME = "application/vnd.google-apps.folder"
NATIVE_PREFIX = "application/vnd.google-apps."
SCOPE_FULL = "https://www.googleapis.com/auth/drive"
SCOPE_FILE = "https://www.googleapis.com/auth/drive.file"

_FIELDS = "id,name,mimeType,size,modifiedTime,md5Checksum,parents,trashed"

#: How Google-native documents are exported when downloaded.
DEFAULT_EXPORT_FORMATS: Mapping[str, str] = {
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    ),
    "application/vnd.google-apps.drawing": "application/pdf",
}


def _require(module: str, extra: str) -> Any:
    try:
        return __import__(module, fromlist=["_"])
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise BackendNotInstalledError(
            f"{module} is required for the Google Drive backend: "
            f"pip install 'storage-interface[{extra}]'"
        ) from exc


def _escape(value: str) -> str:
    """Escape a string for use inside a single-quoted Drive query literal."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class GoogleDriveBackend(StorageBackend):
    name = "gdrive"
    capabilities = frozenset({Capability.MOVE, Capability.COPY, Capability.TRASH})

    def __init__(
        self,
        *,
        credentials: Any = None,
        service_account_file: Optional[PathLike] = None,
        subject: Optional[str] = None,
        token_file: Optional[PathLike] = None,
        client_secrets_file: Optional[PathLike] = None,
        scopes: Optional[Sequence[str]] = None,
        service: Any = None,
        root_folder_id: str = "root",
        trash_on_delete: bool = True,
        supports_all_drives: bool = True,
        chunk_size: int = 8 * 1024 * 1024,
        retries: int = 3,
        export_formats: Optional[Mapping[str, str]] = None,
    ) -> None:
        if chunk_size <= 0 or chunk_size % (256 * 1024):
            raise ValueError("chunk_size must be a positive multiple of 256 KiB")
        self._credentials = credentials
        self._service_account_file = service_account_file
        self._subject = subject
        self._token_file = token_file
        self._client_secrets_file = client_secrets_file
        self._scopes = list(scopes or [SCOPE_FULL])
        self._service = service
        self._root_id = root_folder_id
        self._trash_on_delete = trash_on_delete
        self._chunk_size = chunk_size
        self._retries = retries
        self._export_formats = dict(export_formats or DEFAULT_EXPORT_FORMATS)
        self._kw: dict[str, Any] = {"supportsAllDrives": True} if supports_all_drives else {}
        self._list_kw: dict[str, Any] = (
            {"supportsAllDrives": True, "includeItemsFromAllDrives": True}
            if supports_all_drives
            else {}
        )
        # googleapiclient services wrap httplib2, which is not thread-safe, so
        # every API call is serialised through this lock.
        self._lock = threading.Lock()
        self._folder_ids: dict[str, str] = {}

    # ------------------------------------------------------------------ #
    # Lifecycle / auth
    # ------------------------------------------------------------------ #
    async def connect(self) -> None:
        if self._service is not None:
            return
        self._service = await asyncio.to_thread(self._build_service)

    async def close(self) -> None:
        service, self._service = self._service, None
        if service is not None and hasattr(service, "close"):
            await asyncio.to_thread(service.close)

    def clear_cache(self) -> None:
        """Forget cached folder ids (call after changing Drive from elsewhere)."""
        self._folder_ids.clear()

    def _build_service(self) -> Any:
        discovery = _require("googleapiclient.discovery", "gdrive")
        creds = self._credentials or self._load_credentials()
        return discovery.build("drive", "v3", credentials=creds, cache_discovery=False)

    def _load_credentials(self) -> Any:
        if self._service_account_file:
            sa = _require("google.oauth2.service_account", "gdrive")
            creds = sa.Credentials.from_service_account_file(
                str(self._service_account_file), scopes=self._scopes
            )
            return creds.with_subject(self._subject) if self._subject else creds

        if self._token_file or self._client_secrets_file:
            oauth_creds = _require("google.oauth2.credentials", "gdrive")
            requests = _require("google.auth.transport.requests", "gdrive")
            creds = None
            token_path = Path(self._token_file) if self._token_file else None
            if token_path and token_path.exists():
                creds = oauth_creds.Credentials.from_authorized_user_file(
                    str(token_path), self._scopes
                )
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(requests.Request())
            elif not creds or not creds.valid:
                if not self._client_secrets_file:
                    raise AuthError(
                        "no valid token; pass client_secrets_file to run the "
                        "OAuth consent flow"
                    )
                flow_mod = _require("google_auth_oauthlib.flow", "gdrive")
                flow = flow_mod.InstalledAppFlow.from_client_secrets_file(
                    str(self._client_secrets_file), self._scopes
                )
                creds = flow.run_local_server(port=0)
            if token_path:
                token_path.parent.mkdir(parents=True, exist_ok=True)
                token_path.write_text(creds.to_json())
                try:
                    os.chmod(token_path, 0o600)
                except OSError:  # pragma: no cover - e.g. Windows
                    pass
            return creds

        raise AuthError(
            "no credentials: pass credentials=, service_account_file=, or "
            "token_file=/client_secrets_file="
        )

    # ------------------------------------------------------------------ #
    # Low-level API plumbing
    # ------------------------------------------------------------------ #
    def _svc(self) -> Any:
        if self._service is None:
            raise StorageError("backend is not connected; use 'async with' or connect()")
        return self._service

    async def _run(self, fn, *args) -> Any:
        return await asyncio.to_thread(self._locked, fn, *args)

    def _locked(self, fn, *args) -> Any:
        with self._lock:
            try:
                return fn(*args)
            except Exception as exc:
                translated = self._translate(exc)
                if translated is exc:
                    raise
                raise translated from exc

    @staticmethod
    def _translate(exc: Exception) -> Exception:
        try:
            from googleapiclient.errors import HttpError
        except ImportError:  # pragma: no cover
            return exc
        if not isinstance(exc, HttpError):
            return exc
        status = int(getattr(exc.resp, "status", 0) or 0)
        # str(exc) only carries the reason when the payload has Google's exact
        # error shape, so also look at the raw response body.
        body = exc.content.decode("utf-8", "replace") if isinstance(exc.content, bytes) else ""
        text = f"{exc} {body}".strip()
        if status == 404:
            return NotFoundError(text)
        lowered = text.lower()
        # Drive reports throttling as 403 with reasons like rateLimitExceeded,
        # userRateLimitExceeded, sharingRateLimitExceeded, dailyLimitExceeded.
        throttled = any(s in lowered for s in ("ratelimit", "limitexceeded", "quota"))
        if status == 429 or (status == 403 and throttled):
            return RateLimitError(text)
        if status in (401, 403):
            return AuthError(text)
        return StorageError(text)

    def _execute(self, request: Any) -> Any:
        return request.execute(num_retries=self._retries)

    # ------------------------------------------------------------------ #
    # Metadata helpers
    # ------------------------------------------------------------------ #
    def _info(self, meta: Mapping[str, Any], path: str) -> FileInfo:
        is_dir = meta.get("mimeType") == FOLDER_MIME
        size = meta.get("size")
        extra = {k: meta[k] for k in ("md5Checksum", "parents") if k in meta}
        return FileInfo(
            path=path,
            name=meta.get("name") or split_path(path)[1],
            is_dir=is_dir,
            size=None if is_dir or size is None else int(size),
            modified=_parse_time(meta.get("modifiedTime")),
            mime_type=meta.get("mimeType"),
            backend_id=meta.get("id"),
            extra=extra,
        )

    def _root_meta(self) -> dict[str, Any]:
        return {"id": self._root_id, "name": "", "mimeType": FOLDER_MIME}

    async def _get_meta(self, file_id: str) -> dict[str, Any]:
        return await self._run(
            lambda: self._execute(
                self._svc().files().get(fileId=file_id, fields=_FIELDS, **self._kw)
            )
        )

    async def _find_child(self, parent_id: str, name: str) -> Optional[dict[str, Any]]:
        query = (
            f"'{_escape(parent_id)}' in parents and name = '{_escape(name)}' "
            "and trashed = false"
        )
        resp = await self._run(
            lambda: self._execute(
                self._svc().files().list(
                    q=query,
                    pageSize=10,
                    orderBy="createdTime",
                    fields=f"files({_FIELDS})",
                    spaces="drive",
                    **self._list_kw,
                )
            )
        )
        found = resp.get("files", [])
        if len(found) > 1:
            log.warning("%d items named %r in folder %s; using the oldest", len(found), name, parent_id)
        return found[0] if found else None

    async def _resolve(self, path: str) -> Optional[dict[str, Any]]:
        """Return Drive metadata for ``path`` or ``None`` if it doesn't exist."""
        path = normalize_path(path)
        if path == "/":
            return self._root_meta()

        cached_id = self._folder_ids.get(path)
        if cached_id:
            try:
                meta = await self._get_meta(cached_id)
                if not meta.get("trashed"):
                    return meta
            except NotFoundError:
                pass
            self._drop_cache(path)

        parent_path, name = split_path(path)
        parent = await self._resolve(parent_path)
        if parent is None or parent.get("mimeType") != FOLDER_MIME:
            return None
        meta = await self._find_child(parent["id"], name)
        if meta is not None and meta.get("mimeType") == FOLDER_MIME:
            self._folder_ids[path] = meta["id"]
        return meta

    def _drop_cache(self, path: str) -> None:
        prefix = path.rstrip("/") + "/"
        for key in [k for k in self._folder_ids if k == path or k.startswith(prefix)]:
            del self._folder_ids[key]

    async def _create_folder(self, parent_id: str, name: str) -> dict[str, Any]:
        body = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
        return await self._run(
            lambda: self._execute(
                self._svc().files().create(body=body, fields=_FIELDS, **self._kw)
            )
        )

    # ------------------------------------------------------------------ #
    # Required operations
    # ------------------------------------------------------------------ #
    async def stat(self, path: str) -> FileInfo:
        path = normalize_path(path)
        meta = await self._resolve(path)
        if meta is None:
            raise NotFoundError(path)
        return self._info(meta, path)

    async def iterdir(self, path: str = "/") -> AsyncIterator[FileInfo]:
        path = normalize_path(path)
        meta = await self._resolve(path)
        if meta is None:
            raise NotFoundError(path)
        if meta.get("mimeType") != FOLDER_MIME:
            raise NotAFolderError(path)
        query = f"'{_escape(meta['id'])}' in parents and trashed = false"
        page_token: Optional[str] = None
        while True:
            resp = await self._run(
                lambda token=page_token: self._execute(
                    self._svc().files().list(
                        q=query,
                        pageSize=1000,
                        pageToken=token,
                        orderBy="folder,name",
                        fields=f"nextPageToken,files({_FIELDS})",
                        spaces="drive",
                        **self._list_kw,
                    )
                )
            )
            for child in resp.get("files", []):
                yield self._info(child, join_path(path, child["name"]))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    async def mkdir(
        self, path: str, *, parents: bool = True, exist_ok: bool = True
    ) -> FileInfo:
        path = normalize_path(path)
        if path == "/":
            return self._info(self._root_meta(), "/")
        current = "/"
        current_meta = self._root_meta()
        parts = [p for p in path.split("/") if p]
        for index, part in enumerate(parts):
            is_last = index == len(parts) - 1
            current = join_path(current, part)
            child = await self._find_child(current_meta["id"], part)
            if child is None:
                if not is_last and not parents:
                    raise NotFoundError(current)
                child = await self._create_folder(current_meta["id"], part)
            elif child.get("mimeType") != FOLDER_MIME:
                raise AlreadyExistsError(f"{current} exists and is not a folder")
            elif is_last and not exist_ok:
                raise AlreadyExistsError(current)
            self._folder_ids[current] = child["id"]
            current_meta = child
        return self._info(current_meta, path)

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
        path = normalize_path(remote_path)
        parent_path, name = split_path(path)
        if not name:
            raise InvalidPathError("cannot upload to the root folder itself")

        await self.mkdir(parent_path, parents=True, exist_ok=True)
        parent = await self._resolve(parent_path)
        assert parent is not None  # just created
        existing = await self._find_child(parent["id"], name)
        if existing is not None:
            if existing.get("mimeType") == FOLDER_MIME:
                raise AlreadyExistsError(f"{path} is a folder")
            if not overwrite:
                raise AlreadyExistsError(path)

        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        size = source.stat().st_size
        meta = await self._run(
            self._upload_sync,
            source,
            name,
            parent["id"],
            existing["id"] if existing else None,
            mime,
            size,
            progress,
        )
        return self._info(meta, path)

    def _upload_sync(
        self,
        source: Path,
        name: str,
        parent_id: str,
        existing_id: Optional[str],
        mime: str,
        size: int,
        progress: Optional[ProgressCallback],
    ) -> dict[str, Any]:
        http = _require("googleapiclient.http", "gdrive")
        files = self._svc().files()
        media = http.MediaFileUpload(
            str(source),
            mimetype=mime,
            chunksize=self._chunk_size,
            resumable=size > 0,  # Drive rejects empty resumable uploads
        )
        if existing_id:
            request = files.update(
                fileId=existing_id, media_body=media, fields=_FIELDS, **self._kw
            )
        else:
            request = files.create(
                body={"name": name, "parents": [parent_id]},
                media_body=media,
                fields=_FIELDS,
                **self._kw,
            )
        if size == 0:
            response = request.execute(num_retries=self._retries)
        else:
            response = None
            while response is None:
                status, response = request.next_chunk(num_retries=self._retries)
                if status and progress:
                    progress(int(status.resumable_progress), size)
        if progress:
            progress(size, size)
        return response

    async def download_file(
        self,
        remote_path: str,
        local_path: PathLike,
        *,
        overwrite: bool = False,
        progress: Optional[ProgressCallback] = None,
    ) -> Path:
        path = normalize_path(remote_path)
        meta = await self._resolve(path)
        if meta is None:
            raise NotFoundError(path)
        mime = meta.get("mimeType", "")
        if mime == FOLDER_MIME:
            raise NotAFileError(path)
        target = Path(local_path)
        if target.exists() and not overwrite:
            raise AlreadyExistsError(str(target))

        files = self._svc().files()
        if mime.startswith(NATIVE_PREFIX):
            export_mime = self._export_formats.get(mime)
            if export_mime is None:
                raise NotSupportedError(
                    f"{path} is a Google-native file ({mime}) with no export format; "
                    "pass export_formats={...}"
                )
            request = files.export_media(fileId=meta["id"], mimeType=export_mime)
        else:
            request = files.get_media(fileId=meta["id"], **self._kw)

        total = int(meta.get("size") or 0)
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        try:
            await self._run(self._download_to_path, request, part, total, progress)
            os.replace(part, target)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        return target

    def _download_to_path(
        self,
        request: Any,
        part: Path,
        total: int,
        progress: Optional[ProgressCallback],
    ) -> None:
        http = _require("googleapiclient.http", "gdrive")
        with open(part, "wb") as handle:
            downloader = http.MediaIoBaseDownload(handle, request, chunksize=self._chunk_size)
            done = False
            while not done:
                status, done = downloader.next_chunk(num_retries=self._retries)
                if status and progress:
                    progress(int(status.resumable_progress), total or int(status.total_size or 0))
        if progress:
            size = part.stat().st_size
            progress(size, size)

    async def delete(self, path: str, *, recursive: bool = False) -> None:
        path = normalize_path(path)
        if path == "/":
            raise InvalidPathError("cannot delete the root folder")
        meta = await self._resolve(path)
        if meta is None:
            raise NotFoundError(path)
        if meta.get("mimeType") == FOLDER_MIME and not recursive:
            async for _ in self.iterdir(path):
                raise DirectoryNotEmptyError(path)
        files = self._svc().files()
        if self._trash_on_delete:
            call = lambda: self._execute(
                files.update(fileId=meta["id"], body={"trashed": True}, fields="id", **self._kw)
            )
        else:
            call = lambda: self._execute(files.delete(fileId=meta["id"], **self._kw))
        await self._run(call)
        self._drop_cache(path)

    # ------------------------------------------------------------------ #
    # Optional operations
    # ------------------------------------------------------------------ #
    async def move(self, src: str, dst: str, *, overwrite: bool = False) -> FileInfo:
        src, dst = normalize_path(src), normalize_path(dst)
        if src == "/" or dst == "/":
            raise InvalidPathError("cannot move the root folder")
        meta = await self._resolve(src)
        if meta is None:
            raise NotFoundError(src)
        new_parent_path, new_name = split_path(dst)
        await self._clear_destination(dst, overwrite)
        await self.mkdir(new_parent_path, parents=True, exist_ok=True)
        new_parent = await self._resolve(new_parent_path)
        assert new_parent is not None
        old_parents = ",".join(meta.get("parents", []))
        updated = await self._run(
            lambda: self._execute(
                self._svc().files().update(
                    fileId=meta["id"],
                    body={"name": new_name},
                    addParents=new_parent["id"],
                    removeParents=old_parents,
                    fields=_FIELDS,
                    **self._kw,
                )
            )
        )
        self._drop_cache(src)
        return self._info(updated, dst)

    async def copy(self, src: str, dst: str, *, overwrite: bool = False) -> FileInfo:
        src, dst = normalize_path(src), normalize_path(dst)
        meta = await self._resolve(src)
        if meta is None:
            raise NotFoundError(src)
        if meta.get("mimeType") == FOLDER_MIME:
            raise NotSupportedError("Drive cannot copy folders; use copy_tree()")
        new_parent_path, new_name = split_path(dst)
        if not new_name:
            raise InvalidPathError("destination must name a file")
        await self._clear_destination(dst, overwrite)
        await self.mkdir(new_parent_path, parents=True, exist_ok=True)
        new_parent = await self._resolve(new_parent_path)
        assert new_parent is not None
        copied = await self._run(
            lambda: self._execute(
                self._svc().files().copy(
                    fileId=meta["id"],
                    body={"name": new_name, "parents": [new_parent["id"]]},
                    fields=_FIELDS,
                    **self._kw,
                )
            )
        )
        return self._info(copied, dst)

    async def _clear_destination(self, dst: str, overwrite: bool) -> None:
        existing = await self._resolve(dst)
        if existing is None:
            return
        if not overwrite:
            raise AlreadyExistsError(dst)
        await self.delete(dst, recursive=True)

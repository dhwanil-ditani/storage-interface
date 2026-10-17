"""Google Drive backend tests against an in-memory fake of the Drive v3 service."""

import itertools
import re
from types import SimpleNamespace

import httplib2
import pytest
from googleapiclient.errors import HttpError

from storage_interface import (
    AlreadyExistsError,
    AuthError,
    DirectoryNotEmptyError,
    NotAFileError,
    NotAFolderError,
    NotFoundError,
    NotSupportedError,
    RateLimitError,
    StorageError,
)
from storage_interface.backends.gdrive import FOLDER_MIME, GoogleDriveBackend

DOC_MIME = "application/vnd.google-apps.document"


def http_error(status, body=b"{}"):
    return HttpError(httplib2.Response({"status": str(status)}), body)


class FakeRequest:
    def __init__(self, fn, chunked=False):
        self._fn, self._chunked, self._calls = fn, chunked, 0

    def execute(self, num_retries=0):
        return self._fn()

    def next_chunk(self, num_retries=0):
        self._calls += 1
        if self._chunked and self._calls == 1:
            return SimpleNamespace(resumable_progress=1), None
        return None, self._fn()


class FakeDrive:
    """The slice of Drive v3 that the backend touches."""

    def __init__(self):
        self.items = {}
        self._ids = itertools.count(1)
        self.page_cap = 1000
        self.list_calls = 0

    # -- test helpers --
    def add(self, name, parent="root", mime="text/plain", data=b"", **extra):
        item_id = f"id{next(self._ids)}"
        self.items[item_id] = {
            "id": item_id,
            "name": name,
            "mimeType": mime,
            "parents": [parent],
            "trashed": False,
            "size": str(len(data)),
            "modifiedTime": "2026-10-01T10:00:00.000Z",
            "md5Checksum": "abc",
            "_data": data,
            **extra,
        }
        return item_id

    def meta(self, item_id):
        return {k: v for k, v in self.items[item_id].items() if not k.startswith("_")}

    def files(self):
        return self

    # -- Drive API surface --
    def list(self, q="", pageSize=100, pageToken=None, orderBy=None, **_):
        def run():
            self.list_calls += 1
            parent = re.search(r"'([^']+)' in parents", q).group(1)
            name_match = re.search(r"name = '((?:[^'\\]|\\.)*)'", q)
            wanted = re.sub(r"\\(.)", r"\1", name_match.group(1)) if name_match else None
            rows = [
                i for i in self.items.values()
                if parent in i["parents"]
                and not i["trashed"]
                and (wanted is None or i["name"] == wanted)
            ]
            if orderBy and orderBy.startswith("folder"):
                rows.sort(key=lambda i: (i["mimeType"] != FOLDER_MIME, i["name"]))
            start = int(pageToken or 0)
            size = min(pageSize, self.page_cap)
            page = rows[start:start + size]
            out = {"files": [self._public(i) for i in page]}
            if start + size < len(rows):
                out["nextPageToken"] = str(start + size)
            return out
        return FakeRequest(run)

    def get(self, fileId, **_):
        def run():
            if fileId not in self.items:
                raise http_error(404)
            return self.meta(fileId)
        return FakeRequest(run)

    def create(self, body, media_body=None, **_):
        def run():
            new_id = self.add(
                body["name"], body["parents"][0], body.get("mimeType", "text/plain")
            )
            self._store(new_id, media_body)
            return self.meta(new_id)
        return FakeRequest(run, chunked=media_body is not None and media_body.resumable())

    def update(self, fileId, body=None, media_body=None, addParents=None, removeParents=None, **_):
        def run():
            if fileId not in self.items:
                raise http_error(404)
            item = self.items[fileId]
            item.update(body or {})
            if removeParents:
                item["parents"] = [p for p in item["parents"] if p not in removeParents.split(",")]
            if addParents:
                item["parents"].append(addParents)
            self._store(fileId, media_body)
            return self.meta(fileId)
        return FakeRequest(run, chunked=media_body is not None and media_body.resumable())

    def delete(self, fileId, **_):
        return FakeRequest(lambda: self.items.pop(fileId) and None)

    def copy(self, fileId, body, **_):
        def run():
            src = self.items[fileId]
            new_id = self.add(body["name"], body["parents"][0], src["mimeType"], src["_data"])
            return self.meta(new_id)
        return FakeRequest(run)

    def get_media(self, fileId, **_):
        return FakeRequest(lambda: self.items[fileId]["_data"])

    def export_media(self, fileId, mimeType):
        return FakeRequest(lambda: b"exported:" + mimeType.encode())

    # -- internals --
    def _public(self, item):
        return {k: v for k, v in item.items() if not k.startswith("_")}

    def _store(self, item_id, media_body):
        if media_body is not None:
            data = media_body.getbytes(0, media_body.size())
            self.items[item_id]["_data"] = data
            self.items[item_id]["size"] = str(len(data))


class FakeDriveBackend(GoogleDriveBackend):
    """Swap the real MediaIoBaseDownload (needs HTTP) for a direct payload copy."""

    def _download_to_path(self, request, part, total, progress):
        part.write_bytes(request.execute())
        if progress:
            progress(part.stat().st_size, part.stat().st_size)


@pytest.fixture
def drive():
    return FakeDrive()


@pytest.fixture
async def gd(drive):
    async with FakeDriveBackend(service=drive) as backend:
        yield backend


async def test_mkdir_and_stat(gd, drive):
    info = await gd.mkdir("/a/b/c")
    assert info.is_dir and info.path == "/a/b/c" and info.backend_id
    assert [i["name"] for i in drive.items.values()] == ["a", "b", "c"]
    await gd.mkdir("/a/b/c")  # idempotent
    assert len(drive.items) == 3
    with pytest.raises(AlreadyExistsError):
        await gd.mkdir("/a/b/c", exist_ok=False)
    with pytest.raises(NotFoundError):
        await gd.mkdir("/x/y", parents=False)
    assert (await gd.stat("/")).is_dir
    assert (await gd.stat("/a/b")).is_dir


async def test_upload_download_roundtrip(gd, drive, tmp_path):
    src = tmp_path / "notes.txt"
    src.write_bytes(b"hello drive")
    seen = []
    info = await gd.upload_file(src, "/docs/notes.txt", progress=lambda d, t: seen.append((d, t)))
    assert info.path == "/docs/notes.txt" and info.size == 11 and info.mime_type == "text/plain"
    assert seen[0] == (1, 11) and seen[-1] == (11, 11)

    out = await gd.download_file("/docs/notes.txt", tmp_path / "out" / "n.txt")
    assert out.read_bytes() == b"hello drive"
    assert await gd.read_bytes("/docs/notes.txt") == b"hello drive"
    assert not list((tmp_path / "out").glob("*.part"))


async def test_overwrite_updates_in_place(gd, drive, tmp_path):
    f = tmp_path / "a.txt"
    f.write_bytes(b"1")
    first = await gd.upload_file(f, "/a.txt")
    with pytest.raises(AlreadyExistsError):
        await gd.upload_file(f, "/a.txt")
    f.write_bytes(b"222")
    second = await gd.upload_file(f, "/a.txt", overwrite=True)
    assert second.backend_id == first.backend_id and second.size == 3
    assert len(drive.items) == 1


async def test_empty_file_upload(gd, tmp_path):
    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")
    info = await gd.upload_file(empty, "/empty.bin")
    assert info.size == 0


async def test_iterdir_paginates_and_orders(gd, drive):
    drive.page_cap = 2
    drive.add("zeta.txt")
    drive.add("alpha.txt")
    drive.add("Photos", mime=FOLDER_MIME)
    drive.add("mid.txt")
    drive.add("trashed.txt", trashed=True)
    listing = await gd.listdir("/")
    assert [i.name for i in listing] == ["Photos", "alpha.txt", "mid.txt", "zeta.txt"]
    assert listing[0].is_dir and listing[1].path == "/alpha.txt"
    assert drive.list_calls >= 2


async def test_names_with_quotes_are_escaped(gd, drive, tmp_path):
    f = tmp_path / "x"
    f.write_bytes(b"1")
    await gd.upload_file(f, "/it's mine/dad's file.txt")
    assert (await gd.stat("/it's mine/dad's file.txt")).size == 1


async def test_download_errors_and_native_docs(gd, drive, tmp_path):
    folder = drive.add("Folder", mime=FOLDER_MIME)
    drive.add("Report", mime=DOC_MIME)
    drive.add("Form", mime="application/vnd.google-apps.form")
    drive.add("child.txt", parent=folder)
    with pytest.raises(NotFoundError):
        await gd.download_file("/nope", tmp_path / "x")
    with pytest.raises(NotAFileError):
        await gd.download_file("/Folder", tmp_path / "x")
    with pytest.raises(NotAFolderError):
        await gd.listdir("/Report")
    with pytest.raises(NotSupportedError):
        await gd.download_file("/Form", tmp_path / "x")
    out = await gd.download_file("/Report", tmp_path / "report.docx")
    assert out.read_bytes().startswith(b"exported:application/vnd.openxmlformats")


async def test_delete_trashes_by_default(gd, drive):
    folder = drive.add("D", mime=FOLDER_MIME)
    child = drive.add("c.txt", parent=folder)
    with pytest.raises(DirectoryNotEmptyError):
        await gd.delete("/D")
    await gd.delete("/D", recursive=True)
    assert drive.items[folder]["trashed"] is True
    assert not await gd.exists("/D")
    assert child in drive.items  # Drive trashes the folder; recovery stays possible
    with pytest.raises(NotFoundError):
        await gd.delete("/D")


async def test_permanent_delete(drive):
    file_id = drive.add("gone.txt")
    async with FakeDriveBackend(service=drive, trash_on_delete=False) as gd:
        await gd.delete("/gone.txt")
    assert file_id not in drive.items


async def test_move_and_copy(gd, drive):
    file_id = drive.add("a.txt", data=b"data")
    moved = await gd.move("/a.txt", "/new/dir/b.txt")
    assert moved.backend_id == file_id and moved.path == "/new/dir/b.txt"
    assert not await gd.exists("/a.txt")
    assert await gd.read_bytes("/new/dir/b.txt") == b"data"

    copied = await gd.copy("/new/dir/b.txt", "/c.txt")
    assert copied.backend_id != file_id and await gd.exists("/new/dir/b.txt")
    with pytest.raises(AlreadyExistsError):
        await gd.copy("/new/dir/b.txt", "/c.txt")
    await gd.copy("/new/dir/b.txt", "/c.txt", overwrite=True)
    with pytest.raises(NotSupportedError):
        await gd.copy("/new", "/new2")


async def test_duplicate_names_prefer_oldest(gd, drive):
    first = drive.add("dup.txt")
    drive.add("dup.txt")
    assert (await gd.stat("/dup.txt")).backend_id == first


async def test_stale_folder_cache_recovers(gd, drive):
    await gd.mkdir("/cached")
    cached_id = (await gd.stat("/cached")).backend_id
    drive.items[cached_id]["trashed"] = True  # removed behind our back
    with pytest.raises(NotFoundError):
        await gd.stat("/cached")
    del drive.items[cached_id]
    gd.clear_cache()
    with pytest.raises(NotFoundError):
        await gd.stat("/cached")


@pytest.mark.parametrize(
    "status, body, expected",
    [
        (404, b"{}", NotFoundError),
        (401, b"{}", AuthError),
        (403, b'{"error":{"errors":[{"reason":"forbidden"}]}}', AuthError),
        (403, b'{"error":{"errors":[{"reason":"rateLimitExceeded"}]}}', RateLimitError),
        (403, b'{"reason":"userRateLimitExceeded"}', RateLimitError),  # odd payload shape
        (429, b"{}", RateLimitError),
        (500, b"{}", StorageError),
    ],
)
def test_http_error_translation(status, body, expected):
    assert type(GoogleDriveBackend._translate(http_error(status, body))) is expected


async def test_api_errors_surface_as_storage_interface_errors(gd, drive):
    drive.get = lambda fileId, **_: FakeRequest(lambda: (_ for _ in ()).throw(http_error(404)))
    gd._folder_ids["/x"] = "id-missing"
    with pytest.raises(NotFoundError):
        await gd.stat("/x")  # cache miss -> falls back to lookup -> not found


async def test_connection_and_config_errors(drive):
    with pytest.raises(ValueError):
        GoogleDriveBackend(service=drive, chunk_size=1000)
    unconnected = GoogleDriveBackend(service=None)
    with pytest.raises(StorageError, match="not connected"):
        await unconnected.stat("/x")
    with pytest.raises(AuthError, match="no credentials"):
        await unconnected.connect()

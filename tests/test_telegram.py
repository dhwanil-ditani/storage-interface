"""Telegram backend tests against an in-memory fake of the Telethon client."""

import itertools
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from storage_interface import (
    AlreadyExistsError,
    DirectoryNotEmptyError,
    InvalidPathError,
    NotAFolderError,
    NotFoundError,
    NotSupportedError,
    StorageError,
)
from storage_interface.backends.telegram import TelegramBackend


class FakeTelegramClient:
    """Implements just the Telethon calls the backend uses."""

    def __init__(self):
        self.messages = {}  # id -> SimpleNamespace
        self._ids = itertools.count(1)
        self.disconnected = False
        self.fail_edits = False
        self.send_calls = []

    async def get_input_entity(self, channel):
        return f"entity:{channel}"

    async def send_file(self, entity, file, **kwargs):
        self.send_calls.append(kwargs)
        data = Path(file).read_bytes()
        if kwargs.get("progress_callback"):
            kwargs["progress_callback"](len(data), len(data))
        msg = SimpleNamespace(
            id=next(self._ids),
            date=datetime.now(timezone.utc),
            message=kwargs.get("caption", ""),
            data=data,
            file=SimpleNamespace(size=len(data), mime_type="application/octet-stream"),
        )
        self.messages[msg.id] = msg
        return msg

    async def get_messages(self, entity, ids=None):
        return self.messages.get(ids)

    async def download_media(self, message, file=None, progress_callback=None):
        Path(file).write_bytes(message.data)
        if progress_callback:
            progress_callback(len(message.data), len(message.data))
        return file

    async def delete_messages(self, entity, ids):
        for i in ids:
            self.messages.pop(i, None)

    async def edit_message(self, entity, message_id, text=None):
        if self.fail_edits:
            raise RuntimeError("flood wait")
        self.messages[message_id].message = text

    async def iter_messages(self, entity):
        for i in sorted(self.messages, reverse=True):  # newest first
            yield self.messages[i]

    async def disconnect(self):
        self.disconnected = True


@pytest.fixture
def client():
    return FakeTelegramClient()


@pytest.fixture
async def tg(client):
    backend = TelegramBackend(client=client, index_path=":memory:")
    await backend.connect()
    yield backend
    await backend.close()


async def test_upload_list_download(tg, client, tmp_path):
    src = tmp_path / "report.pdf"
    src.write_bytes(b"pdf-bytes")
    seen = []
    info = await tg.upload_file(src, "/work/2026/report.pdf", progress=lambda d, t: seen.append(d))
    assert info.size == 9 and info.path == "/work/2026/report.pdf"
    assert seen == [9]

    # Caption carries the path; display name is forced to the remote name.
    msg = client.messages[int(info.backend_id)]
    assert msg.message == "/work/2026/report.pdf"
    attrs = client.send_calls[0]["attributes"]
    assert attrs[0].file_name == "report.pdf"

    assert [i.name for i in await tg.listdir("/")] == ["work"]
    assert [i.is_dir for i in await tg.listdir("/work")] == [True]
    assert [i.name for i in await tg.listdir("/work/2026")] == ["report.pdf"]

    out = await tg.download_file("/work/2026/report.pdf", tmp_path / "dl" / "r.pdf")
    assert out.read_bytes() == b"pdf-bytes"
    assert not list((tmp_path / "dl").glob("*.part"))
    assert await tg.read_bytes("/work/2026/report.pdf") == b"pdf-bytes"


async def test_overwrite_replaces_old_message(tg, client):
    first = await tg.write_bytes("/a.txt", b"v1")
    with pytest.raises(AlreadyExistsError):
        await tg.write_bytes("/a.txt", b"v2")
    assert len(client.messages) == 1  # failed attempt uploaded nothing
    second = await tg.write_bytes("/a.txt", b"v2-longer", overwrite=True)
    assert len(client.messages) == 1
    assert int(first.backend_id) not in client.messages
    assert await tg.read_bytes("/a.txt") == b"v2-longer"
    assert second.size == 9


async def test_limits_and_validation(client, tmp_path):
    small = TelegramBackend(client=client, index_path=":memory:", max_file_size=4)
    await small.connect()
    with pytest.raises(StorageError, match="exceeds"):
        await small.write_bytes("/big.bin", b"12345")
    with pytest.raises(NotSupportedError):
        await small.write_bytes("/empty.bin", b"")
    with pytest.raises(InvalidPathError):
        await small.write_bytes("/" + "x" * 2000, b"1")  # too long for a caption
    assert client.messages == {}


async def test_long_or_odd_remote_names_do_not_depend_on_local_fs(tg, client):
    # 300-char name exceeds typical local filename limits (255) but fits a caption.
    long_name = "/" + "n" * 300 + ".txt"
    await tg.write_bytes(long_name, b"1")
    await tg.write_bytes('/what?<is>:this*.txt', b"2")  # characters illegal on Windows
    assert await tg.read_bytes(long_name) == b"1"
    assert await tg.read_bytes('/what?<is>:this*.txt') == b"2"
    # The remote name is still what Telegram displays.
    assert client.send_calls[0]["attributes"][0].file_name == "n" * 300 + ".txt"


async def test_file_vs_folder_conflicts(tg):
    await tg.write_bytes("/f", b"1")
    with pytest.raises(NotAFolderError):
        await tg.write_bytes("/f/child", b"1")
    with pytest.raises(NotAFolderError):
        await tg.listdir("/f")
    await tg.mkdir("/d")
    with pytest.raises(AlreadyExistsError):
        await tg.write_bytes("/d", b"1")
    with pytest.raises(AlreadyExistsError):
        await tg.mkdir("/d", exist_ok=False)
    with pytest.raises(NotFoundError):
        await tg.mkdir("/p/q", parents=False)


async def test_delete(tg, client):
    await tg.write_bytes("/d/a", b"1")
    await tg.write_bytes("/d/sub/b", b"2")
    with pytest.raises(DirectoryNotEmptyError):
        await tg.delete("/d")
    await tg.delete("/d/a")
    assert len(client.messages) == 1
    await tg.delete("/d", recursive=True)
    assert client.messages == {} and not await tg.exists("/d/sub/b")
    with pytest.raises(NotFoundError):
        await tg.delete("/d")


async def test_move_file_and_folder(tg, client):
    info = await tg.write_bytes("/a/one.txt", b"1")
    await tg.write_bytes("/a/deep/two.txt", b"2")

    moved = await tg.move("/a/one.txt", "/b/renamed.txt")
    assert moved.path == "/b/renamed.txt" and moved.backend_id == info.backend_id
    assert client.messages[int(info.backend_id)].message == "/b/renamed.txt"

    await tg.move("/a", "/z")
    assert sorted([i.path async for i in tg.walk("/z")]) == ["/z/deep", "/z/deep/two.txt"]
    assert not await tg.exists("/a")
    assert await tg.read_bytes("/z/deep/two.txt") == b"2"

    with pytest.raises(InvalidPathError):
        await tg.move("/z", "/z/inside")
    with pytest.raises(AlreadyExistsError):
        await tg.move("/z/deep/two.txt", "/b/renamed.txt")
    await tg.move("/z/deep/two.txt", "/b/renamed.txt", overwrite=True)
    assert await tg.read_bytes("/b/renamed.txt") == b"2"
    # The overwritten destination's message (id of "one.txt") was deleted;
    # only the moved file's message remains.
    assert list(client.messages) == [2] and client.messages[2].message == "/b/renamed.txt"


async def test_move_survives_caption_edit_failure(tg, client):
    await tg.write_bytes("/a.txt", b"1")
    client.fail_edits = True
    moved = await tg.move("/a.txt", "/b.txt")
    assert moved.path == "/b.txt" and await tg.exists("/b.txt")


async def test_stale_index_reports_clearly(tg, client):
    info = await tg.write_bytes("/gone.txt", b"1")
    del client.messages[int(info.backend_id)]  # deleted from Telegram directly
    with pytest.raises(NotFoundError, match="rebuild_index"):
        await tg.read_bytes("/gone.txt")


async def test_rebuild_index_from_captions(tg, client):
    await tg.write_bytes("/docs/a.txt", b"1")
    await tg.write_bytes("/docs/b/c.txt", b"22")
    await client.send_file("e", __file__, caption="just a normal post")  # ignored
    tg._index.clear()
    assert await tg.rebuild_index() == 2
    assert sorted(i.path for i in await tg.listdir("/docs")) == ["/docs/a.txt", "/docs/b"]
    assert (await tg.stat("/docs/b/c.txt")).size == 2


async def test_use_before_connect_and_close_semantics(client):
    backend = TelegramBackend(client=client, index_path=":memory:")
    with pytest.raises(StorageError, match="not connected"):
        await backend.upload_file(__file__, "/x")
    await backend.connect()
    await backend.close()
    assert client.disconnected is False  # injected clients are not ours to close


def test_requires_credentials_without_client():
    with pytest.raises(ValueError):
        TelegramBackend(index_path=":memory:")

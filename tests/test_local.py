import pytest

from storage_interface import (
    AlreadyExistsError,
    DirectoryNotEmptyError,
    InvalidPathError,
    NotAFileError,
    NotAFolderError,
    NotFoundError,
    copy_between,
    copy_tree,
    create_backend,
)


@pytest.fixture
async def store(tmp_path):
    async with create_backend("local", root=tmp_path / "root") as backend:
        yield backend


async def test_roundtrip(store, tmp_path):
    src = tmp_path / "hello.txt"
    src.write_text("hello")
    seen = []
    info = await store.upload_file(src, "/docs/hello.txt", progress=lambda d, t: seen.append((d, t)))
    assert info.path == "/docs/hello.txt" and info.size == 5 and not info.is_dir
    assert seen[-1] == (5, 5)

    out = await store.download_file("/docs/hello.txt", tmp_path / "out" / "copy.txt")
    assert out.read_text() == "hello"
    assert await store.read_bytes("/docs/hello.txt") == b"hello"


async def test_overwrite_rules(store, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("1")
    await store.upload_file(f, "/a.txt")
    with pytest.raises(AlreadyExistsError):
        await store.upload_file(f, "/a.txt")
    f.write_text("22")
    info = await store.upload_file(f, "/a.txt", overwrite=True)
    assert info.size == 2
    with pytest.raises(AlreadyExistsError):
        await store.download_file("/a.txt", f)


async def test_listing_and_walk(store):
    await store.write_bytes("/x/one.txt", b"1")
    await store.write_bytes("/x/y/two.txt", b"22")
    names = sorted(i.name for i in await store.listdir("/x"))
    assert names == ["one.txt", "y"]
    walked = sorted([i.path async for i in store.walk("/")])
    assert walked == ["/x", "/x/one.txt", "/x/y", "/x/y/two.txt"]


async def test_errors(store):
    await store.write_bytes("/f.txt", b"1")
    await store.mkdir("/d")
    with pytest.raises(NotFoundError):
        await store.stat("/missing")
    assert not await store.exists("/missing")
    with pytest.raises(NotAFileError):
        await store.download_file("/d", "/tmp/never")
    with pytest.raises(NotAFolderError):
        await store.listdir("/f.txt")
    await store.write_bytes("/d/inner", b"1")
    with pytest.raises(DirectoryNotEmptyError):
        await store.delete("/d")
    await store.delete("/d", recursive=True)
    assert not await store.exists("/d")


async def test_cannot_escape_root(store, tmp_path):
    with pytest.raises(InvalidPathError):
        await store.stat("/../outside")
    (tmp_path / "secret.txt").write_text("s")
    (store.root / "link").symlink_to(tmp_path)
    with pytest.raises(InvalidPathError):
        await store.stat("/link/secret.txt")


async def test_move_and_copy(store):
    await store.write_bytes("/a.txt", b"data")
    moved = await store.move("/a.txt", "/sub/b.txt")
    assert moved.path == "/sub/b.txt" and not await store.exists("/a.txt")
    copied = await store.copy("/sub/b.txt", "/c.txt")
    assert copied.size == 4 and await store.exists("/sub/b.txt")
    with pytest.raises(AlreadyExistsError):
        await store.copy("/sub/b.txt", "/c.txt")


async def test_copy_between_and_tree(tmp_path):
    async with create_backend("local", root=tmp_path / "A") as a, create_backend(
        "local", root=tmp_path / "B"
    ) as b:
        await a.write_bytes("/photos/2026/x.jpg", b"xx")
        await a.write_bytes("/photos/y.jpg", b"y")
        await a.mkdir("/photos/empty")
        info = await copy_between(a, "/photos/y.jpg", b, "/inbox/y.jpg")
        assert info.size == 1
        assert await copy_tree(a, "/photos", b, "/backup") == 2
        assert await b.read_bytes("/backup/2026/x.jpg") == b"xx"
        assert await b.exists("/backup/empty")

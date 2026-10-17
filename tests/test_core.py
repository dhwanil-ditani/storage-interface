import pytest

from storage_interface import (
    InvalidPathError,
    StorageBackend,
    UnknownBackendError,
    available_backends,
    create_backend,
    get_backend_class,
    join_path,
    normalize_path,
    register_backend,
    split_path,
)
from storage_interface.backends.local import LocalBackend


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("", "/"),
        ("/", "/"),
        ("a/b", "/a/b"),
        ("//a///b/", "/a/b"),
        ("/a/./b", "/a/b"),
    ],
)
def test_normalize_path(raw, expected):
    assert normalize_path(raw) == expected


@pytest.mark.parametrize("bad", ["../x", "/a/../b", "a\x00b"])
def test_normalize_path_rejects(bad):
    with pytest.raises(InvalidPathError):
        normalize_path(bad)


def test_split_and_join():
    assert split_path("/a/b/c.txt") == ("/a/b", "c.txt")
    assert split_path("/c.txt") == ("/", "c.txt")
    assert split_path("/") == ("/", "")
    assert join_path("/", "x") == "/x"
    assert join_path("/a/", "x") == "/a/x"


def test_builtin_backends_listed():
    assert {"local", "gdrive", "telegram"} <= set(available_backends())


def test_get_backend_class_lazy_import():
    assert get_backend_class("local") is LocalBackend
    assert get_backend_class("gdrive").name == "gdrive"
    assert get_backend_class("telegram").name == "telegram"


def test_unknown_backend():
    with pytest.raises(UnknownBackendError):
        create_backend("nope")


def test_register_custom_backend(tmp_path):
    class Mine(LocalBackend):
        name = "mine"

    register_backend("mine-test", Mine, replace=True)
    assert "mine-test" in available_backends()
    backend = create_backend("mine-test", root=tmp_path)
    assert isinstance(backend, Mine) and isinstance(backend, StorageBackend)

    with pytest.raises(ValueError):
        register_backend("local", Mine)  # clashes with a built-in
    with pytest.raises(TypeError):
        register_backend("bad", object)  # type: ignore[arg-type]

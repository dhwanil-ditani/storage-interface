"""storage_interface: one async interface for many cloud storage services."""

from .base import (
    Capability,
    FileInfo,
    PathLike,
    ProgressCallback,
    StorageBackend,
    join_path,
    normalize_path,
    split_path,
)
from .errors import (
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
    UnknownBackendError,
)
from .registry import (
    available_backends,
    create_backend,
    get_backend_class,
    register_backend,
)
from .transfer import copy_between, copy_tree

__version__ = "0.1.0"

__all__ = [
    "AlreadyExistsError",
    "AuthError",
    "BackendNotInstalledError",
    "Capability",
    "DirectoryNotEmptyError",
    "FileInfo",
    "InvalidPathError",
    "NotAFileError",
    "NotAFolderError",
    "NotFoundError",
    "NotSupportedError",
    "PathLike",
    "ProgressCallback",
    "RateLimitError",
    "StorageBackend",
    "StorageError",
    "UnknownBackendError",
    "available_backends",
    "copy_between",
    "copy_tree",
    "create_backend",
    "get_backend_class",
    "join_path",
    "normalize_path",
    "register_backend",
    "split_path",
]

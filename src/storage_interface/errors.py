"""Exception hierarchy shared by every backend.

Application code should only ever need to catch these, never a
provider-specific exception (``HttpError``, ``RPCError``, ...).
"""

from __future__ import annotations


class StorageError(Exception):
    """Base class for all storage_interface errors."""


class NotFoundError(StorageError):
    """The requested file or folder does not exist."""


class AlreadyExistsError(StorageError):
    """The destination already exists and ``overwrite`` was not requested."""


class NotAFileError(StorageError):
    """A file operation was attempted on a folder."""


class NotAFolderError(StorageError):
    """A folder operation was attempted on a file."""


class DirectoryNotEmptyError(StorageError):
    """A non-empty folder was deleted without ``recursive=True``."""


class NotSupportedError(StorageError):
    """The backend does not support this operation."""


class AuthError(StorageError):
    """Credentials are missing, expired, or lack permission."""


class RateLimitError(StorageError):
    """The provider asked us to slow down."""


class InvalidPathError(StorageError, ValueError):
    """The path is malformed or escapes the backend root."""


class UnknownBackendError(StorageError, LookupError):
    """No backend is registered under the requested name."""


class BackendNotInstalledError(StorageError, ImportError):
    """An optional dependency required by the backend is missing."""

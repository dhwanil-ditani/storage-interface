"""A small SQLite path index for services that have no real folder tree.

Telegram, for example, only has messages.  The index maps virtual paths to
provider-native ids so the backend can offer ``stat`` / ``iterdir`` / ``move``
like a filesystem.  Future "flat store" backends can reuse it.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from typing import Iterable, Optional

from ..base import split_path


@dataclass
class IndexEntry:
    path: str
    is_dir: bool
    size: Optional[int] = None
    mtime: Optional[float] = None
    message_id: Optional[int] = None
    mime: Optional[str] = None

    @property
    def name(self) -> str:
        return split_path(self.path)[1]

    @property
    def parent(self) -> str:
        return split_path(self.path)[0]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    path       TEXT PRIMARY KEY,
    parent     TEXT NOT NULL,
    is_dir     INTEGER NOT NULL,
    size       INTEGER,
    mtime      REAL,
    message_id INTEGER,
    mime       TEXT
);
CREATE INDEX IF NOT EXISTS entries_parent ON entries(parent);
"""

_COLUMNS = "path, is_dir, size, mtime, message_id, mime"


def _row(row: tuple) -> IndexEntry:
    return IndexEntry(
        path=row[0],
        is_dir=bool(row[1]),
        size=row[2],
        mtime=row[3],
        message_id=row[4],
        mime=row[5],
    )


class PathIndex:
    """Thread-safe path -> entry mapping persisted in SQLite."""

    def __init__(self, db_path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- reads -------------------------------------------------------------- #
    def get(self, path: str) -> Optional[IndexEntry]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_COLUMNS} FROM entries WHERE path = ?", (path,)
            ).fetchone()
        return _row(row) if row else None

    def children(self, parent: str) -> list[IndexEntry]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_COLUMNS} FROM entries WHERE parent = ? "
                "ORDER BY is_dir DESC, path",
                (parent,),
            ).fetchall()
        return [_row(r) for r in rows]

    def descendants(self, path: str) -> list[IndexEntry]:
        """Every entry strictly below ``path``."""
        # Paths below "/a" all sort in ["/a/", "/a0") because '0' follows '/'.
        base = path.rstrip("/")
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_COLUMNS} FROM entries WHERE path >= ? AND path < ? ORDER BY path",
                (base + "/", base + "0"),
            ).fetchall()
        return [_row(r) for r in rows]

    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]

    # -- writes ------------------------------------------------------------- #
    def _insert(self, entry: IndexEntry) -> None:
        self._conn.execute(
            f"INSERT OR REPLACE INTO entries ({_COLUMNS}, parent) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                entry.path,
                int(entry.is_dir),
                entry.size,
                entry.mtime,
                entry.message_id,
                entry.mime,
                entry.parent,
            ),
        )

    def put(self, entry: IndexEntry) -> None:
        with self._lock, self._conn:
            self._insert(entry)

    def ensure_dirs(self, path: str) -> None:
        """Create ``path`` and every ancestor as folder rows (existing ones are kept)."""
        parts = [p for p in path.split("/") if p]
        current = ""
        with self._lock, self._conn:
            for part in parts:
                parent = current or "/"
                current = f"{current}/{part}"
                self._conn.execute(
                    f"INSERT OR IGNORE INTO entries ({_COLUMNS}, parent) "
                    "VALUES (?, 1, NULL, NULL, NULL, NULL, ?)",
                    (current, parent),
                )

    def delete(self, paths: Iterable[str]) -> None:
        with self._lock, self._conn:
            self._conn.executemany(
                "DELETE FROM entries WHERE path = ?", [(p,) for p in paths]
            )

    def rename_tree(self, src: str, dst: str) -> None:
        """Re-key ``src`` and everything below it to live under ``dst``."""
        moved = [e for e in [self.get(src), *self.descendants(src)] if e is not None]
        with self._lock, self._conn:
            self._conn.executemany(
                "DELETE FROM entries WHERE path = ?", [(e.path,) for e in moved]
            )
            for entry in moved:
                entry.path = dst + entry.path[len(src):]
                self._insert(entry)  # single transaction: all-or-nothing

    def clear(self) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM entries")

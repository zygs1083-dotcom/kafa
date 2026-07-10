"""Storage seam for the consistency kernel.

The canonical fact source lives behind this Store abstraction so the runtime
can swap SQLite (default, file-backed) for an in-memory test double now, and a
shared transactional store in a later release, without touching business SQL.
This module must not import harness_db or core business modules.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Callable, Iterator, Protocol

from harness_lib import ensure_parent


DB_PATH = Path(".ai-team/state/harness.db")
BeforeCommit = Callable[[sqlite3.Connection], None]


class StoreError(Exception):
    """Raised for storage-layer faults, not business-rule violations."""


class Store(Protocol):
    @property
    def root(self) -> Path: ...

    def connection(self) -> Iterator[sqlite3.Connection]:
        """Read-oriented connection context manager."""

    def backup_to(self, target: Path) -> None:
        """Write a consistent database snapshot to target."""

    def transaction(
        self,
        *,
        before_commit: BeforeCommit | None = None,
        request_id: str | None = None,
        fence: object | None = None,
    ) -> Iterator[sqlite3.Connection]:
        """Write transaction context manager."""


class SqliteStore:
    """File-backed SQLite store preserving the v1 runtime connection semantics."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def _db_file(self) -> Path:
        return self._root / DB_PATH

    def _connect(self) -> sqlite3.Connection:
        path = self._db_file()
        ensure_parent(path)
        deadline = time.monotonic() + 5.0
        last_error: sqlite3.OperationalError | None = None
        while True:
            remaining = max(0.1, deadline - time.monotonic())
            conn = sqlite3.connect(path, timeout=remaining)
            conn.row_factory = sqlite3.Row
            conn.execute(f"pragma busy_timeout = {int(remaining * 1000)}")
            try:
                conn.execute("pragma journal_mode = wal")
            except sqlite3.OperationalError as exc:
                conn.close()
                if "locked" not in str(exc).lower():
                    raise
                last_error = exc
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
                continue
            conn.execute("pragma foreign_keys = on")
            return conn
        raise last_error or sqlite3.OperationalError("database is locked")

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def backup_to(self, target: Path) -> None:
        ensure_parent(target)
        with self.connection() as source, closing(sqlite3.connect(target)) as destination:
            source.backup(destination)

    @contextmanager
    def transaction(
        self,
        *,
        before_commit: BeforeCommit | None = None,
        request_id: str | None = None,
        fence: object | None = None,
    ) -> Iterator[sqlite3.Connection]:
        # T1: request_id and fence are accepted for future store-level work.
        _ = (request_id, fence)
        conn = self._connect()
        try:
            conn.execute("begin immediate")
            yield conn
            if before_commit is not None:
                before_commit(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


class InMemoryStore:
    """Single long-lived in-memory SQLite connection for tests."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("pragma foreign_keys = on")

    @property
    def root(self) -> Path:
        return self._root

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        yield self._conn

    def backup_to(self, target: Path) -> None:
        ensure_parent(target)
        with closing(sqlite3.connect(target)) as destination:
            self._conn.backup(destination)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(
        self,
        *,
        before_commit: BeforeCommit | None = None,
        request_id: str | None = None,
        fence: object | None = None,
    ) -> Iterator[sqlite3.Connection]:
        # T1: request_id and fence are accepted for future store-level work.
        _ = (request_id, fence)
        conn = self._conn
        conn.execute("begin")
        try:
            yield conn
            if before_commit is not None:
                before_commit(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

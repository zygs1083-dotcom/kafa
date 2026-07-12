"""Storage seam for the consistency kernel.

The canonical fact source lives behind this local Store abstraction so tests
can use an in-memory SQLite double without changing business SQL.
This module must not import harness_db or core business modules.
"""

from __future__ import annotations

import errno
import json
import os
import sqlite3
import threading
import time
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Callable, Iterator, Protocol

from harness_lib import ensure_parent
from .errors import HarnessError

if os.name == "nt":  # pragma: no cover - exercised by the Windows validation job
    import msvcrt
else:  # pragma: no branch - exactly one platform backend is loaded
    import fcntl


DB_PATH = Path(".ai-team/state/harness.db")
OPERATION_LOCK_PATH = Path(".ai-team/state/harness.db.operation.lock")
MIGRATION_SENTINEL_PATH = Path(".ai-team/state/local-core-migration.lock")
DEFAULT_OPERATION_LOCK_TIMEOUT = 5.0
BeforeCommit = Callable[[sqlite3.Connection], None]


class ProjectOperationLockError(HarnessError):
    """Raised when a file-backed database operation cannot enter safely."""


_REGISTRY_GUARD = threading.Lock()
_LOCAL_LOCKS: dict[str, threading.RLock] = {}
_HELD_FDS: set[int] = set()
_THREAD_STATE = threading.local()


def _before_fork() -> None:
    _REGISTRY_GUARD.acquire()


def _after_fork_parent() -> None:
    _REGISTRY_GUARD.release()


def _after_fork_child() -> None:
    global _REGISTRY_GUARD, _LOCAL_LOCKS, _HELD_FDS, _THREAD_STATE
    for descriptor in tuple(_HELD_FDS):
        try:
            os.close(descriptor)
        except OSError:
            pass
    _REGISTRY_GUARD = threading.Lock()
    _LOCAL_LOCKS = {}
    _HELD_FDS = set()
    _THREAD_STATE = threading.local()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(
        before=_before_fork,
        after_in_parent=_after_fork_parent,
        after_in_child=_after_fork_child,
    )


def _operation_paths(root: Path) -> tuple[Path, Path, str]:
    resolved_root = Path(root).expanduser().resolve()
    lock_path = resolved_root / OPERATION_LOCK_PATH
    sentinel_path = resolved_root / MIGRATION_SENTINEL_PATH
    key = os.path.normcase(os.path.realpath(os.fspath(lock_path)))
    return lock_path, sentinel_path, key


def _thread_operations() -> dict[str, dict[str, object]]:
    held = getattr(_THREAD_STATE, "held", None)
    if held is None:
        held = {}
        _THREAD_STATE.held = held
    return held


def _local_lock(key: str) -> threading.RLock:
    with _REGISTRY_GUARD:
        lock = _LOCAL_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCAL_LOCKS[key] = lock
        return lock


def _sentinel_error(sentinel_path: Path) -> ProjectOperationLockError | None:
    try:
        with sentinel_path.open("r", encoding="utf-8") as handle:
            raw = handle.read(4096)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return ProjectOperationLockError(
            "migration-in-progress: local-core migration sentinel exists at "
            f"{sentinel_path} (metadata unreadable: {exc}); inspect the owner and remove it only "
            "after confirming no migration is active"
        )

    metadata: list[str] = []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        for field in ("pid", "created_at", "target_schema"):
            value = payload.get(field)
            if value not in (None, ""):
                metadata.append(f"{field}={value}")
    details = f" ({', '.join(metadata)})" if metadata else " (metadata invalid or incomplete)"
    return ProjectOperationLockError(
        "migration-in-progress: local-core migration sentinel exists at "
        f"{sentinel_path}{details}; inspect the owner and remove it only after confirming no migration is active"
    )


def _raise_if_migration_announced(sentinel_path: Path) -> None:
    error = _sentinel_error(sentinel_path)
    if error is not None:
        raise error


def _open_operation_lock(path: Path) -> int:
    ensure_parent(path)
    with _REGISTRY_GUARD:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.set_inheritable(descriptor, False)
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            os.chmod(path, 0o600)
            _HELD_FDS.add(descriptor)
            return descriptor
        except Exception:
            os.close(descriptor)
            raise


def _close_operation_lock(descriptor: int) -> None:
    with _REGISTRY_GUARD:
        _HELD_FDS.discard(descriptor)
        try:
            os.close(descriptor)
        except OSError:
            pass


def _try_os_lock(descriptor: int) -> None:
    if os.name == "nt":  # pragma: no cover - exercised by the Windows validation job
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_os_lock(descriptor: int) -> None:
    if os.name == "nt":  # pragma: no cover - exercised by the Windows validation job
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(descriptor, fcntl.LOCK_UN)


def _acquire_os_lock(descriptor: int, path: Path, deadline: float, timeout: float) -> None:
    while True:
        try:
            _try_os_lock(descriptor)
            return
        except OSError as exc:
            blocking_codes = {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
            if isinstance(exc, BlockingIOError) or exc.errno in blocking_codes:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProjectOperationLockError(
                        "project-db-operation-timeout: could not acquire exclusive operation lock "
                        f"{path} within {timeout:.1f} seconds"
                    ) from exc
                time.sleep(min(0.05, remaining))
                continue
            raise ProjectOperationLockError(
                f"project-db-operation-lock-error: cannot lock {path}: {exc}"
            ) from exc


@contextmanager
def project_db_operation(
    root: Path,
    *,
    purpose: str = "normal",
    timeout: float = DEFAULT_OPERATION_LOCK_TIMEOUT,
) -> Iterator[None]:
    """Serialize a complete file-backed DB operation or local-core migration."""

    if purpose not in {"normal", "migration"}:
        raise ValueError(f"unknown project DB operation purpose: {purpose!r}")
    if timeout <= 0:
        raise ValueError("project DB operation timeout must be positive")

    lock_path, sentinel_path, key = _operation_paths(root)
    held = _thread_operations()
    current = held.get(key)
    if current is not None:
        current_purpose = str(current["purpose"])
        if current_purpose == "normal" and purpose == "migration":
            raise ProjectOperationLockError(
                "project-db-operation-order-error: migration cannot start inside an active normal database operation"
            )
        current["depth"] = int(current["depth"]) + 1
        try:
            yield
        finally:
            current["depth"] = int(current["depth"]) - 1
        return

    if purpose == "normal":
        _raise_if_migration_announced(sentinel_path)

    deadline = time.monotonic() + timeout
    local_lock = _local_lock(key)
    if not local_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise ProjectOperationLockError(
            "project-db-operation-timeout: could not enter the process-local operation lock "
            f"for {lock_path} within {timeout:.1f} seconds"
        )

    descriptor: int | None = None
    os_locked = False
    try:
        descriptor = _open_operation_lock(lock_path)
        _acquire_os_lock(descriptor, lock_path, deadline, timeout)
        os_locked = True
        if purpose == "normal":
            _raise_if_migration_announced(sentinel_path)
        held[key] = {
            "pid": os.getpid(),
            "thread_id": threading.get_ident(),
            "purpose": purpose,
            "depth": 1,
            "fd": descriptor,
        }
        try:
            yield
        finally:
            held.pop(key, None)
    finally:
        if descriptor is not None:
            if os_locked:
                try:
                    _unlock_os_lock(descriptor)
                except OSError:
                    pass
            _close_operation_lock(descriptor)
        local_lock.release()


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
        with project_db_operation(self._root):
            conn = self._connect()
            try:
                yield conn
            finally:
                conn.close()

    def backup_to(self, target: Path) -> None:
        with project_db_operation(self._root):
            ensure_parent(target)
            with self.connection() as source, closing(sqlite3.connect(target)) as destination:
                source.backup(destination)

    @contextmanager
    def transaction(
        self,
        *,
        before_commit: BeforeCommit | None = None,
        request_id: str | None = None,
    ) -> Iterator[sqlite3.Connection]:
        _ = request_id
        with project_db_operation(self._root):
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
    ) -> Iterator[sqlite3.Connection]:
        _ = request_id
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

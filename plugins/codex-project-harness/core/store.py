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
from typing import Callable, Hashable, Iterator, Protocol

from harness_lib import ensure_parent
from .errors import HarnessError
from .project_fs import (
    ProjectFS,
    ProjectPathSafetyError,
    _PathSnapshot,
    pin_project_filesystem,
)

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
_LOCAL_LOCKS: dict[Hashable, threading.RLock] = {}
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


def _operation_key(project_fs: ProjectFS) -> tuple[object, ...]:
    return (*project_fs.root_identity_key, OPERATION_LOCK_PATH.as_posix())


def _thread_operations() -> dict[Hashable, dict[str, object]]:
    held = getattr(_THREAD_STATE, "held", None)
    if held is None:
        held = {}
        _THREAD_STATE.held = held
    return held


def _local_lock(key: Hashable) -> threading.RLock:
    with _REGISTRY_GUARD:
        lock = _LOCAL_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCAL_LOCKS[key] = lock
        return lock


def _sentinel_error(project_fs: ProjectFS) -> ProjectOperationLockError | None:
    snapshot = project_fs._snapshot(
        MIGRATION_SENTINEL_PATH,
        allow_missing=True,
    )
    if not snapshot.exists:
        return None
    sentinel_path = project_fs.absolute(MIGRATION_SENTINEL_PATH)
    try:
        raw = project_fs.read_bytes(
            MIGRATION_SENTINEL_PATH,
            max_bytes=4096,
        ).decode("utf-8", errors="replace")
    except ProjectPathSafetyError:
        raise
    except OSError as exc:
        return ProjectOperationLockError(
            "migration-in-progress: local-core migration sentinel exists at "
            f"{sentinel_path} (metadata unreadable: {exc}); inspect the owner and verify "
            "database/projection authority before considering removal"
        )

    metadata: list[str] = []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        for field in ("pid", "created_at", "target_schema", "status", "manifest_path"):
            value = payload.get(field)
            if value not in (None, ""):
                metadata.append(f"{field}={value}")
    details = f" ({', '.join(metadata)})" if metadata else " (metadata invalid or incomplete)"
    recovery_required = isinstance(payload, dict) and payload.get("status") in {
        "recovery-required",
        "rollback-incomplete",
    }
    guidance = (
        "recover and verify database/projection authority using the recorded manifest; "
        "do not remove the sentinel until recovery is complete"
        if recovery_required
        else (
            "inspect the owner, confirm no migration is active, and verify database/projection "
            "authority before considering sentinel removal"
        )
    )
    return ProjectOperationLockError(
        "migration-in-progress: local-core migration sentinel exists at "
        f"{sentinel_path}{details}; {guidance}"
    )


def _raise_if_migration_announced(project_fs: ProjectFS) -> None:
    error = _sentinel_error(project_fs)
    if error is not None:
        raise error


def raise_if_project_migration_announced(root: Path) -> None:
    """Fail closed on a migration sentinel without opening or creating SQLite."""

    with ProjectFS.open(root) as project_fs:
        _raise_if_migration_announced(project_fs)


def _open_operation_lock(path_or_fs: Path | ProjectFS) -> int:
    owns_fs = not isinstance(path_or_fs, ProjectFS)
    if owns_fs:
        path = Path(path_or_fs)
        try:
            root = path.parents[2]
        except IndexError as exc:
            raise ProjectOperationLockError(
                f"project-db-operation-lock-error: invalid operation lock path {path}"
            ) from exc
        project_fs = ProjectFS.open(root)
        relative = project_fs.relative_to_root(path)
    else:
        project_fs = path_or_fs
        relative = OPERATION_LOCK_PATH
    with _REGISTRY_GUARD:
        descriptor = project_fs.open_lock_fd(relative, mode=0o600)
        try:
            os.set_inheritable(descriptor, False)
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            os.fchmod(descriptor, 0o600)
            _HELD_FDS.add(descriptor)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise
        finally:
            if owns_fs:
                project_fs.close()


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
    project_fs: ProjectFS | None = None,
) -> Iterator[ProjectFS]:
    """Serialize a complete file-backed DB operation or local-core migration."""

    if purpose not in {"normal", "migration"}:
        raise ValueError(f"unknown project DB operation purpose: {purpose!r}")
    if timeout <= 0:
        raise ValueError("project DB operation timeout must be positive")

    owns_project_fs = project_fs is None
    if project_fs is None:
        project_fs = ProjectFS.open(root)
    lock_path = project_fs.absolute(OPERATION_LOCK_PATH)
    key = _operation_key(project_fs)
    held = _thread_operations()
    current = held.get(key)
    if current is not None:
        if owns_project_fs:
            project_fs.close()
        current_purpose = str(current["purpose"])
        if current_purpose == "normal" and purpose == "migration":
            raise ProjectOperationLockError(
                "project-db-operation-order-error: migration cannot start inside an active normal database operation"
            )
        current["depth"] = int(current["depth"]) + 1
        try:
            yield current["fs"]  # type: ignore[misc]
        finally:
            current["depth"] = int(current["depth"]) - 1
        return

    if purpose == "normal":
        try:
            _raise_if_migration_announced(project_fs)
        except BaseException:
            if owns_project_fs:
                project_fs.close()
            raise

    deadline = time.monotonic() + timeout
    local_lock = _local_lock(key)
    if not local_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
        if owns_project_fs:
            project_fs.close()
        raise ProjectOperationLockError(
            "project-db-operation-timeout: could not enter the process-local operation lock "
            f"for {lock_path} within {timeout:.1f} seconds"
        )

    descriptor: int | None = None
    os_locked = False
    try:
        descriptor = _open_operation_lock(project_fs)
        _acquire_os_lock(descriptor, lock_path, deadline, timeout)
        os_locked = True
        if purpose == "normal":
            _raise_if_migration_announced(project_fs)
        held[key] = {
            "pid": os.getpid(),
            "thread_id": threading.get_ident(),
            "purpose": purpose,
            "depth": 1,
            "fd": descriptor,
            "fs": project_fs,
        }
        try:
            with pin_project_filesystem(project_fs):
                yield project_fs
        finally:
            held.pop(key, None)
    finally:
        try:
            if descriptor is not None and os_locked:
                try:
                    _unlock_os_lock(descriptor)
                except OSError:
                    pass
        finally:
            try:
                if descriptor is not None:
                    _close_operation_lock(descriptor)
            finally:
                try:
                    local_lock.release()
                finally:
                    if owns_project_fs:
                        project_fs.close()


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

    @staticmethod
    def _db_family() -> tuple[Path, ...]:
        return (
            DB_PATH,
            Path(f"{DB_PATH.as_posix()}-wal"),
            Path(f"{DB_PATH.as_posix()}-shm"),
            Path(f"{DB_PATH.as_posix()}-journal"),
        )

    def _connect(
        self,
        project_fs: ProjectFS,
    ) -> tuple[sqlite3.Connection, _PathSnapshot, dict[Path, _PathSnapshot]]:
        project_fs.audit(self._db_family(), allow_missing=True)
        path = project_fs.sqlite_path(DB_PATH, access="rw", create=True)
        database_identity = project_fs._snapshot(DB_PATH, allow_missing=False)
        deadline = time.monotonic() + 5.0
        last_error: sqlite3.OperationalError | None = None
        while True:
            remaining = max(0.1, deadline - time.monotonic())
            uri = f"{path.as_uri()}?mode=rw"
            conn = sqlite3.connect(uri, uri=True, timeout=remaining)
            conn.row_factory = sqlite3.Row
            conn.execute(f"pragma busy_timeout = {int(remaining * 1000)}")
            project_fs._assert_unchanged(DB_PATH, database_identity)
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
            project_fs._assert_unchanged(DB_PATH, database_identity)
            family_identity = {
                relative: project_fs._snapshot(relative, allow_missing=True)
                for relative in self._db_family()[1:]
            }
            return conn, database_identity, family_identity
        raise last_error or sqlite3.OperationalError("database is locked")

    @staticmethod
    def _verify_connection_authority(
        project_fs: ProjectFS,
        database_identity: _PathSnapshot,
        family_identity: dict[Path, _PathSnapshot],
    ) -> None:
        project_fs._assert_unchanged(DB_PATH, database_identity)
        for relative, expected in family_identity.items():
            actual = project_fs._snapshot(relative, allow_missing=True)
            # SQLite may create or remove a WAL/SHM/journal while the same
            # pinned connection is active.  Every observed object must still
            # be safe, and an object that survives the lifecycle must retain
            # its identity.
            if expected.exists and actual.exists and actual != expected:
                raise ProjectPathSafetyError(relative, "path-identity-changed")

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        with project_db_operation(self._root) as project_fs:
            conn, database_identity, family_identity = self._connect(project_fs)
            try:
                yield conn
            finally:
                try:
                    self._verify_connection_authority(
                        project_fs,
                        database_identity,
                        family_identity,
                    )
                finally:
                    conn.close()

    def backup_to(self, target: Path) -> None:
        target = Path(target).expanduser().absolute()
        with project_db_operation(self._root) as project_fs:
            source, database_identity, family_identity = self._connect(project_fs)
            destination_fs: ProjectFS | None = None
            try:
                try:
                    relative = project_fs.relative_to_root(target)
                    active_destination_fs = project_fs
                except ProjectPathSafetyError:
                    destination_fs = ProjectFS.open(target.parent)
                    active_destination_fs = destination_fs
                    relative = Path(target.name)
                destination_snapshot = active_destination_fs._snapshot(
                    relative,
                    allow_missing=True,
                )
                if not destination_snapshot.exists:
                    active_destination_fs.create_exclusive(
                        relative,
                        b"",
                        mode=0o600,
                    )
                    destination_snapshot = active_destination_fs._snapshot(
                        relative,
                        allow_missing=False,
                    )
                destination_path = active_destination_fs.absolute(relative)
                destination_uri = f"{destination_path.as_uri()}?mode=rw"
                with closing(
                    sqlite3.connect(destination_uri, uri=True, timeout=5.0)
                ) as destination:
                    source.backup(destination)
                    destination.commit()
                    active_destination_fs._assert_unchanged(
                        relative,
                        destination_snapshot,
                    )
                self._verify_connection_authority(
                    project_fs,
                    database_identity,
                    family_identity,
                )
                active_destination_fs._assert_unchanged(
                    relative,
                    destination_snapshot,
                )
            finally:
                try:
                    source.close()
                finally:
                    if destination_fs is not None:
                        destination_fs.close()

    @contextmanager
    def transaction(
        self,
        *,
        before_commit: BeforeCommit | None = None,
        request_id: str | None = None,
    ) -> Iterator[sqlite3.Connection]:
        _ = request_id
        with project_db_operation(self._root) as project_fs:
            conn, database_identity, family_identity = self._connect(project_fs)
            try:
                conn.execute("begin immediate")
                yield conn
                if before_commit is not None:
                    before_commit(conn)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                try:
                    self._verify_connection_authority(
                        project_fs,
                        database_identity,
                        family_identity,
                    )
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

"""Storage seam for the consistency kernel.

The canonical fact source lives behind this local Store abstraction so tests
can use an in-memory SQLite double without changing business SQL.
This module must not import harness_db or core business modules.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
import unicodedata
from contextlib import closing, contextmanager
from dataclasses import dataclass
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


@dataclass(frozen=True)
class _OperationLockAuthority:
    project_fs: ProjectFS
    path_fd: int
    exclusion_fd: int
    path_snapshot: _PathSnapshot
    owns_project_fs: bool


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


def _open_operation_lock(path_or_fs: Path | ProjectFS) -> _OperationLockAuthority:
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
        path_fd = project_fs.open_lock_fd(relative, mode=0o600)
        exclusion_fd: int | None = None
        try:
            os.set_inheritable(path_fd, False)
            if os.fstat(path_fd).st_size == 0:
                os.write(path_fd, b"\0")
                os.fsync(path_fd)
            path_snapshot = project_fs._snapshot(
                relative,
                allow_missing=False,
            )
            exclusion_fd = project_fs.open_exclusion_fd()
            if exclusion_fd is None:
                exclusion_fd = path_fd
            else:
                os.set_inheritable(exclusion_fd, False)
            _HELD_FDS.add(path_fd)
            _HELD_FDS.add(exclusion_fd)
            return _OperationLockAuthority(
                project_fs=project_fs,
                path_fd=path_fd,
                exclusion_fd=exclusion_fd,
                path_snapshot=path_snapshot,
                owns_project_fs=owns_fs,
            )
        except BaseException:
            if exclusion_fd is not None and exclusion_fd != path_fd:
                os.close(exclusion_fd)
            os.close(path_fd)
            if owns_fs:
                project_fs.close()
            raise


def _close_operation_lock(authority: _OperationLockAuthority) -> None:
    with _REGISTRY_GUARD:
        descriptors = {authority.path_fd, authority.exclusion_fd}
        for descriptor in descriptors:
            _HELD_FDS.discard(descriptor)
            try:
                os.close(descriptor)
            except OSError:
                pass
        if authority.owns_project_fs:
            authority.project_fs.close()


def _verify_operation_lock_authority(
    authority: _OperationLockAuthority,
) -> None:
    authority.project_fs._assert_unchanged(
        OPERATION_LOCK_PATH,
        authority.path_snapshot,
    )


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

    authority: _OperationLockAuthority | None = None
    os_locked = False
    try:
        authority = _open_operation_lock(project_fs)
        _acquire_os_lock(authority.exclusion_fd, lock_path, deadline, timeout)
        os_locked = True
        _verify_operation_lock_authority(authority)
        if purpose == "normal":
            _raise_if_migration_announced(project_fs)
        held[key] = {
            "pid": os.getpid(),
            "thread_id": threading.get_ident(),
            "purpose": purpose,
            "depth": 1,
            "fd": authority.exclusion_fd,
            "fs": project_fs,
        }
        operation_error: BaseException | None = None
        try:
            with pin_project_filesystem(project_fs):
                yield project_fs
        except BaseException as exc:
            operation_error = exc
            raise
        finally:
            try:
                _verify_operation_lock_authority(authority)
            except BaseException as verification_error:
                if operation_error is not None:
                    operation_error.add_note(
                        f"operation lock authority verification failed: {verification_error}"
                    )
                else:
                    raise
            finally:
                held.pop(key, None)
    finally:
        try:
            if authority is not None and os_locked:
                try:
                    _unlock_os_lock(authority.exclusion_fd)
                except OSError:
                    pass
        finally:
            try:
                if authority is not None:
                    _close_operation_lock(authority)
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


def _sqlite_database_family(relative: Path) -> tuple[Path, Path, Path, Path]:
    return (
        relative,
        relative.with_name(relative.name + "-wal"),
        relative.with_name(relative.name + "-shm"),
        relative.with_name(relative.name + "-journal"),
    )


def _verify_sqlite_family_authority(
    project_fs: ProjectFS,
    database_relative: Path,
    database_identity: _PathSnapshot,
    family_identity: dict[Path, _PathSnapshot],
    *,
    allow_disappeared: bool = False,
) -> None:
    project_fs._assert_unchanged(database_relative, database_identity)
    for relative, expected in family_identity.items():
        actual = project_fs._snapshot(relative, allow_missing=True)
        if actual == expected:
            continue
        if allow_disappeared and expected.exists and not actual.exists:
            continue
        raise ProjectPathSafetyError(relative, "path-identity-changed")


def _refresh_sqlite_wal_authority(
    project_fs: ProjectFS,
    database_relative: Path,
    database_identity: _PathSnapshot,
    family_identity: dict[Path, _PathSnapshot],
) -> dict[Path, _PathSnapshot]:
    """Accept only SQLite's documented missing-to-present WAL/SHM setup seam."""

    project_fs._assert_unchanged(database_relative, database_identity)
    journal = database_relative.with_name(database_relative.name + "-journal")
    refreshed: dict[Path, _PathSnapshot] = {}
    for relative, expected in family_identity.items():
        actual = project_fs._snapshot(relative, allow_missing=True)
        if relative == journal and actual != expected:
            raise ProjectPathSafetyError(relative, "path-identity-changed")
        if expected.exists and actual.exists and actual != expected:
            raise ProjectPathSafetyError(relative, "path-identity-changed")
        refreshed[relative] = actual
    return refreshed


def _sqlite_connection_shutdown_errors(
    project_fs: ProjectFS,
    connection: sqlite3.Connection,
    database_relative: Path,
    database_identity: _PathSnapshot,
    family_identity: dict[Path, _PathSnapshot],
) -> list[tuple[str, BaseException]]:
    errors: list[tuple[str, BaseException]] = []
    try:
        _verify_sqlite_family_authority(
            project_fs,
            database_relative,
            database_identity,
            family_identity,
        )
    except BaseException as exc:
        errors.append(("connection authority pre-close verification failed", exc))
    try:
        connection.close()
    except BaseException as exc:
        errors.append(("SQLite connection close failed", exc))
    try:
        _verify_sqlite_family_authority(
            project_fs,
            database_relative,
            database_identity,
            family_identity,
            allow_disappeared=True,
        )
    except BaseException as exc:
        errors.append(("connection authority post-close verification failed", exc))
    return errors


def _apply_sqlite_teardown_errors(
    operation_error: BaseException | None,
    errors: list[tuple[str, BaseException]],
) -> None:
    if not errors:
        return
    if operation_error is not None:
        for label, error in errors:
            operation_error.add_note(f"{label}: {error}")
        return
    first_label, first_error = errors[0]
    first_error.add_note(first_label)
    for label, error in errors[1:]:
        first_error.add_note(f"{label}: {error}")
    raise first_error


@contextmanager
def _verified_sqlite_connection(
    project_fs: ProjectFS,
    relative: Path,
    *,
    access: str,
    timeout: float = 5.0,
    immutable: bool = False,
    journal_mode: str | None = None,
    setup: Callable[[sqlite3.Connection], None] | None = None,
) -> Iterator[sqlite3.Connection]:
    """Open one SQLite file while continuously pinning its complete path family."""

    family = _sqlite_database_family(relative)
    project_fs.audit(family, allow_missing=True)
    path = project_fs.sqlite_path(relative, access=access, create=False)
    database_identity = project_fs._snapshot(relative, allow_missing=False)
    family_identity = {
        member: project_fs._snapshot(member, allow_missing=True)
        for member in family[1:]
    }
    query = f"mode={access}"
    if immutable:
        query += "&immutable=1"
    uri = f"{path.as_uri()}?{query}"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=timeout)
    except BaseException as exc:
        try:
            _verify_sqlite_family_authority(
                project_fs,
                relative,
                database_identity,
                family_identity,
            )
        except BaseException as verification_error:
            verification_error.add_note(f"SQLite connect failed: {exc}")
            raise verification_error from exc
        raise

    operation_error: BaseException | None = None
    try:
        # This check is deliberately the first action after sqlite3.connect:
        # no SQL may touch an attacker-injected rollback journal or sidecar.
        _verify_sqlite_family_authority(
            project_fs,
            relative,
            database_identity,
            family_identity,
        )
        selected_mode = ""
        if journal_mode is not None:
            selected = connection.execute(
                f"pragma journal_mode = {journal_mode}"
            ).fetchone()
            if selected is None or str(selected[0]).lower() != journal_mode.lower():
                raise ProjectOperationLockError(
                    "project-db-journal-mode-error: cannot select verified "
                    f"SQLite journal mode {journal_mode}"
                )
            selected_mode = str(selected[0]).lower()
        else:
            selected = connection.execute("pragma journal_mode").fetchone()
            if selected is None:
                raise ProjectOperationLockError(
                    "project-db-journal-mode-error: cannot read verified "
                    "SQLite journal mode"
                )
            selected_mode = str(selected[0]).lower()

        # Only a confirmed WAL connection has SQLite's documented authority to
        # create missing WAL/SHM members.  Memory, DELETE, immutable, and other
        # modes keep the exact pre-connect family identity.
        if selected_mode == "wal":
            # WAL/SHM are commonly materialized only by the first schema read,
            # not by the journal-mode pragma itself.  Keep that single,
            # confirmed WAL transition inside the documented setup seam.
            connection.execute("pragma schema_version").fetchone()
            family_identity = _refresh_sqlite_wal_authority(
                project_fs,
                relative,
                database_identity,
                family_identity,
            )
        else:
            _verify_sqlite_family_authority(
                project_fs,
                relative,
                database_identity,
                family_identity,
            )
            connection.execute("pragma schema_version").fetchone()
            _verify_sqlite_family_authority(
                project_fs,
                relative,
                database_identity,
                family_identity,
            )
        if setup is not None:
            setup(connection)
        _verify_sqlite_family_authority(
            project_fs,
            relative,
            database_identity,
            family_identity,
        )
        yield connection
    except BaseException as exc:
        operation_error = exc
        raise
    finally:
        _apply_sqlite_teardown_errors(
            operation_error,
            _sqlite_connection_shutdown_errors(
                project_fs,
                connection,
                relative,
                database_identity,
                family_identity,
            ),
        )


def _temporary_sqlite_family_cleanup_errors(
    project_fs: ProjectFS,
    relative: Path,
    main_snapshot: _PathSnapshot,
    *,
    published: bool,
) -> list[tuple[str, BaseException]]:
    """Delete only a proved-owned temporary main file; retain unknown sidecars."""

    errors: list[tuple[str, BaseException]] = []
    for sidecar in _sqlite_database_family(relative)[1:]:
        try:
            snapshot = project_fs._snapshot(sidecar, allow_missing=True)
            if snapshot.exists:
                errors.append(
                    (
                        f"unverified temporary SQLite sidecar retained at {sidecar}",
                        ProjectOperationLockError(
                            "project-db-cleanup-incomplete: refusing to delete a "
                            "temporary SQLite sidecar whose creation identity cannot be proved"
                        ),
                    )
                )
        except BaseException as exc:
            cleanup_error = ProjectOperationLockError(
                "project-db-cleanup-incomplete: refusing to delete a temporary "
                "SQLite sidecar whose safety or creation identity cannot be proved"
            )
            cleanup_error.add_note(
                f"temporary SQLite sidecar inspection failed for {sidecar}: {exc}"
            )
            errors.append(
                (f"unverified temporary SQLite sidecar retained at {sidecar}", cleanup_error)
            )
    if not published:
        try:
            project_fs.unlink_regular(relative, expected=main_snapshot)
        except BaseException as exc:
            errors.append(("temporary SQLite main cleanup failed", exc))
    return errors


class SqliteStore:
    """File-backed SQLite store preserving the v1 runtime connection semantics."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    @staticmethod
    def _db_family() -> tuple[Path, ...]:
        return SqliteStore._database_family_for(DB_PATH)

    @staticmethod
    def _database_family_for(path: Path) -> tuple[Path, ...]:
        return (
            path,
            path.with_name(path.name + "-wal"),
            path.with_name(path.name + "-shm"),
            path.with_name(path.name + "-journal"),
        )

    @staticmethod
    def _physical_path_key(path: Path) -> str:
        # Backup overlap is a fail-closed authority check, not a general path
        # equality API.  Fold Unicode and case on every platform so aliases
        # that a case-insensitive volume resolves to the DB family cannot pass
        # merely because the current host's os.path.normcase is a no-op (as it
        # is on macOS).  On a case-sensitive volume this can conservatively
        # reject a case-only sibling, which is safer than publishing over a
        # source-family alias.
        absolute = os.path.normcase(os.path.abspath(os.fspath(path)))
        return unicodedata.normalize("NFC", absolute).casefold()

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
            family_before_connect = {
                relative: project_fs._snapshot(
                    relative,
                    allow_missing=True,
                )
                for relative in self._db_family()[1:]
            }
            remaining = max(0.1, deadline - time.monotonic())
            uri = f"{path.as_uri()}?mode=rw"
            conn = sqlite3.connect(uri, uri=True, timeout=remaining)
            try:
                conn.row_factory = sqlite3.Row
                project_fs._assert_unchanged(DB_PATH, database_identity)
                self._verify_connection_authority(
                    project_fs,
                    database_identity,
                    family_before_connect,
                )
                conn.execute(f"pragma busy_timeout = {int(remaining * 1000)}")
                self._verify_connection_authority(
                    project_fs,
                    database_identity,
                    family_before_connect,
                )
            except BaseException as exc:
                self._apply_teardown_errors(
                    exc,
                    self._connection_shutdown_errors(
                        project_fs,
                        conn,
                        database_identity,
                        family_before_connect,
                    ),
                )
                raise
            try:
                conn.execute("pragma journal_mode = wal")
                conn.execute("pragma schema_version").fetchone()
                family_after_journal = self._refresh_connection_family_authority(
                    project_fs,
                    database_identity,
                    family_before_connect,
                )
            except sqlite3.OperationalError as exc:
                shutdown_errors = self._connection_shutdown_errors(
                    project_fs,
                    conn,
                    database_identity,
                    family_before_connect,
                )
                if "locked" not in str(exc).lower():
                    self._apply_teardown_errors(exc, shutdown_errors)
                    raise
                if shutdown_errors:
                    self._apply_teardown_errors(exc, shutdown_errors)
                    raise
                last_error = exc
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
                continue
            except BaseException as exc:
                self._apply_teardown_errors(
                    exc,
                    self._connection_shutdown_errors(
                        project_fs,
                        conn,
                        database_identity,
                        family_before_connect,
                    ),
                )
                raise
            try:
                conn.execute("pragma foreign_keys = on")
                self._verify_connection_authority(
                    project_fs,
                    database_identity,
                    family_after_journal,
                )
                return conn, database_identity, family_after_journal
            except BaseException as exc:
                self._apply_teardown_errors(
                    exc,
                    self._connection_shutdown_errors(
                        project_fs,
                        conn,
                        database_identity,
                        family_after_journal,
                    ),
                )
                raise
        raise last_error or sqlite3.OperationalError("database is locked")

    @staticmethod
    def _verify_connection_authority(
        project_fs: ProjectFS,
        database_identity: _PathSnapshot,
        family_identity: dict[Path, _PathSnapshot],
        *,
        allow_disappeared: bool = False,
    ) -> None:
        project_fs._assert_unchanged(DB_PATH, database_identity)
        for relative, expected in family_identity.items():
            actual = project_fs._snapshot(relative, allow_missing=True)
            if actual == expected:
                continue
            if allow_disappeared and expected.exists and not actual.exists:
                continue
            raise ProjectPathSafetyError(relative, "path-identity-changed")

    @staticmethod
    def _refresh_connection_family_authority(
        project_fs: ProjectFS,
        database_identity: _PathSnapshot,
        family_identity: dict[Path, _PathSnapshot],
    ) -> dict[Path, _PathSnapshot]:
        project_fs._assert_unchanged(DB_PATH, database_identity)
        refreshed: dict[Path, _PathSnapshot] = {}
        journal_path = DB_PATH.with_name(DB_PATH.name + "-journal")
        for relative, expected in family_identity.items():
            actual = project_fs._snapshot(relative, allow_missing=True)
            if relative == journal_path and actual != expected:
                raise ProjectPathSafetyError(relative, "path-identity-changed")
            if expected.exists and actual.exists and actual != expected:
                raise ProjectPathSafetyError(relative, "path-identity-changed")
            refreshed[relative] = actual
        return refreshed

    @classmethod
    def _connection_shutdown_errors(
        cls,
        project_fs: ProjectFS,
        connection: sqlite3.Connection,
        database_identity: _PathSnapshot,
        family_identity: dict[Path, _PathSnapshot],
    ) -> list[tuple[str, BaseException]]:
        errors: list[tuple[str, BaseException]] = []
        try:
            cls._verify_connection_authority(
                project_fs,
                database_identity,
                family_identity,
            )
        except BaseException as exc:
            errors.append(("connection authority pre-close verification failed", exc))
        try:
            connection.close()
        except BaseException as exc:
            errors.append(("SQLite connection close failed", exc))
        try:
            cls._verify_connection_authority(
                project_fs,
                database_identity,
                family_identity,
                allow_disappeared=True,
            )
        except BaseException as exc:
            errors.append(("connection authority post-close verification failed", exc))
        return errors

    @staticmethod
    def _apply_teardown_errors(
        operation_error: BaseException | None,
        errors: list[tuple[str, BaseException]],
    ) -> None:
        if not errors:
            return
        if operation_error is not None:
            for label, error in errors:
                operation_error.add_note(f"{label}: {error}")
            return
        first_label, first_error = errors[0]
        first_error.add_note(first_label)
        for label, error in errors[1:]:
            first_error.add_note(f"{label}: {error}")
        raise first_error

    @staticmethod
    def _verify_sidecar_snapshots(
        project_fs: ProjectFS,
        sidecars: dict[Path, _PathSnapshot],
    ) -> None:
        for relative, expected in sidecars.items():
            project_fs._assert_unchanged(relative, expected)

    @staticmethod
    def _temporary_family_cleanup_errors(
        project_fs: ProjectFS,
        family: tuple[Path, ...],
        main_snapshot: _PathSnapshot,
        *,
        published: bool,
        cleanup_sidecars: bool = True,
    ) -> list[tuple[str, BaseException]]:
        errors: list[tuple[str, BaseException]] = []
        if cleanup_sidecars:
            for sidecar in family[1:]:
                try:
                    snapshot = project_fs._snapshot(sidecar, allow_missing=True)
                    if snapshot.exists:
                        errors.append(
                            (
                                f"unverified temporary backup sidecar retained at {sidecar}",
                                ProjectOperationLockError(
                                    "project-db-backup-cleanup-incomplete: "
                                    "refusing to delete a temporary SQLite sidecar "
                                    "whose creation identity cannot be proved"
                                ),
                            )
                        )
                except BaseException as exc:
                    errors.append(
                        (f"temporary backup sidecar cleanup failed for {sidecar}", exc)
                    )
        if not published:
            try:
                project_fs.unlink_regular(
                    family[0],
                    expected=main_snapshot,
                )
            except BaseException as exc:
                errors.append(("temporary backup main cleanup failed", exc))
        return errors

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        with project_db_operation(self._root) as project_fs:
            conn, database_identity, family_identity = self._connect(project_fs)
            operation_error: BaseException | None = None
            try:
                yield conn
            except BaseException as exc:
                operation_error = exc
                raise
            finally:
                self._apply_teardown_errors(
                    operation_error,
                    self._connection_shutdown_errors(
                        project_fs,
                        conn,
                        database_identity,
                        family_identity,
                    ),
                )

    def backup_to(self, target: Path) -> None:
        target = Path(target).expanduser().absolute()
        with project_db_operation(self._root) as project_fs:
            source, database_identity, family_identity = self._connect(project_fs)
            source_open = True
            destination_fs: ProjectFS | None = None
            active_destination_fs: ProjectFS | None = None
            temporary_relative: Path | None = None
            temporary_family: tuple[Path, ...] | None = None
            temporary_snapshot: _PathSnapshot | None = None
            temporary_sidecar_cleanup_attempted = False
            published = False
            operation_error: BaseException | None = None
            try:
                try:
                    relative = project_fs.relative_to_root(target)
                    active_destination_fs = project_fs
                except ProjectPathSafetyError:
                    destination_fs = ProjectFS.open(target.parent)
                    active_destination_fs = destination_fs
                    relative = Path(target.name)
                destination_authority = self._physical_path_key(
                    active_destination_fs.absolute(relative)
                )
                source_family_authorities = {
                    self._physical_path_key(project_fs.absolute(member))
                    for member in self._db_family()
                }
                if destination_authority in source_family_authorities:
                    raise ProjectOperationLockError(
                        "project-db-backup-destination-error: destination overlaps the source database family"
                    )
                destination_family = self._database_family_for(relative)
                destination_snapshot = active_destination_fs._snapshot(
                    relative,
                    allow_missing=True,
                )
                destination_sidecars: dict[Path, _PathSnapshot] = {}
                for sidecar in destination_family[1:]:
                    snapshot = active_destination_fs._snapshot(
                        sidecar,
                        allow_missing=True,
                    )
                    if snapshot.exists:
                        raise ProjectOperationLockError(
                            "project-db-backup-destination-busy: refusing to replace a "
                            f"database with an existing SQLite sidecar: {active_destination_fs.absolute(sidecar)}"
                        )
                    destination_sidecars[sidecar] = snapshot

                for _ in range(128):
                    candidate = relative.with_name(
                        f".{relative.name}.kafa-backup-{secrets.token_hex(12)}.tmp"
                    )
                    candidate_family = self._database_family_for(candidate)
                    active_destination_fs.audit(
                        candidate_family,
                        allow_missing=True,
                    )
                    if any(
                        active_destination_fs._snapshot(
                            member,
                            allow_missing=True,
                        ).exists
                        for member in candidate_family
                    ):
                        continue
                    try:
                        active_destination_fs.create_exclusive(
                            candidate,
                            b"",
                            mode=0o600,
                        )
                    except FileExistsError:
                        continue
                    temporary_relative = candidate
                    break
                if temporary_relative is None:
                    raise ProjectOperationLockError(
                        "project-db-backup-destination-error: cannot reserve a unique temporary database"
                    )

                temporary_family = self._database_family_for(temporary_relative)
                temporary_snapshot = active_destination_fs._snapshot(
                    temporary_relative,
                    allow_missing=False,
                )
                temporary_path = active_destination_fs.absolute(
                    temporary_relative
                )
                temporary_uri = f"{temporary_path.as_uri()}?mode=rw"
                destination = sqlite3.connect(
                    temporary_uri,
                    uri=True,
                    timeout=5.0,
                )
                destination_error: BaseException | None = None
                try:
                    active_destination_fs._assert_unchanged(
                        temporary_relative,
                        temporary_snapshot,
                    )
                    for sidecar in temporary_family[1:]:
                        if active_destination_fs._snapshot(
                            sidecar,
                            allow_missing=True,
                        ).exists:
                            raise ProjectPathSafetyError(
                                sidecar,
                                "path-identity-changed",
                            )
                    journal_mode = destination.execute(
                        "pragma journal_mode = memory"
                    ).fetchone()
                    if (
                        journal_mode is None
                        or str(journal_mode[0]).lower() != "memory"
                    ):
                        raise ProjectOperationLockError(
                            "project-db-backup-temporary-error: cannot select in-memory journaling"
                        )
                    source.backup(destination)
                    destination.commit()
                    active_destination_fs._assert_unchanged(
                        temporary_relative,
                        temporary_snapshot,
                    )
                    integrity = destination.execute(
                        "pragma integrity_check"
                    ).fetchone()
                    if integrity is None or str(integrity[0]).lower() != "ok":
                        raise ProjectOperationLockError(
                            "project-db-backup-integrity-error: temporary backup failed integrity_check"
                        )
                    foreign_keys = destination.execute(
                        "pragma foreign_key_check"
                    ).fetchall()
                    if foreign_keys:
                        raise ProjectOperationLockError(
                            "project-db-backup-integrity-error: temporary backup failed foreign_key_check"
                        )
                    destination.commit()
                    active_destination_fs._assert_unchanged(
                        temporary_relative,
                        temporary_snapshot,
                    )
                except BaseException as exc:
                    destination_error = exc
                    raise
                finally:
                    try:
                        destination.close()
                    except BaseException as close_error:
                        if destination_error is not None:
                            destination_error.add_note(
                                f"temporary SQLite connection close failed: {close_error}"
                            )
                        else:
                            raise

                temporary_sidecar_cleanup_attempted = True
                self._apply_teardown_errors(
                    None,
                    self._temporary_family_cleanup_errors(
                        active_destination_fs,
                        temporary_family,
                        temporary_snapshot,
                        published=True,
                    ),
                )
                source_shutdown_errors = self._connection_shutdown_errors(
                    project_fs,
                    source,
                    database_identity,
                    family_identity,
                )
                source_open = False
                self._apply_teardown_errors(None, source_shutdown_errors)
                active_destination_fs.sync_regular(
                    temporary_relative,
                    expected=temporary_snapshot,
                )
                temporary_digest = hashlib.sha256(
                    active_destination_fs.read_bytes(
                        temporary_relative,
                        expected=temporary_snapshot,
                    )
                ).hexdigest()
                self._verify_connection_authority(
                    project_fs,
                    database_identity,
                    family_identity,
                    allow_disappeared=True,
                )
                active_destination_fs._assert_unchanged(
                    relative,
                    destination_snapshot,
                )
                self._verify_sidecar_snapshots(
                    active_destination_fs,
                    destination_sidecars,
                )
                active_destination_fs.replace_file(
                    temporary_relative,
                    relative,
                    expected_source=temporary_snapshot,
                    expected_destination=destination_snapshot,
                )
                published = True
                final_snapshot = active_destination_fs._snapshot(
                    relative,
                    allow_missing=False,
                )
                if final_snapshot != temporary_snapshot:
                    raise ProjectPathSafetyError(
                        relative,
                        "path-identity-changed",
                    )
                final_digest = hashlib.sha256(
                    active_destination_fs.read_bytes(
                        relative,
                        expected=final_snapshot,
                    )
                ).hexdigest()
                if final_digest != temporary_digest:
                    raise ProjectPathSafetyError(
                        relative,
                        "path-identity-changed",
                    )
                self._verify_sidecar_snapshots(
                    active_destination_fs,
                    destination_sidecars,
                )
                self._verify_connection_authority(
                    project_fs,
                    database_identity,
                    family_identity,
                    allow_disappeared=True,
                )
                active_destination_fs._assert_unchanged(
                    relative,
                    final_snapshot,
                )
                if hashlib.sha256(
                    active_destination_fs.read_bytes(
                        relative,
                        expected=final_snapshot,
                    )
                ).hexdigest() != temporary_digest:
                    raise ProjectPathSafetyError(
                        relative,
                        "path-identity-changed",
                    )
                self._verify_sidecar_snapshots(
                    active_destination_fs,
                    destination_sidecars,
                )
            except BaseException as exc:
                operation_error = exc
                raise
            finally:
                teardown_errors: list[tuple[str, BaseException]] = []
                if source_open:
                    teardown_errors.extend(
                        self._connection_shutdown_errors(
                            project_fs,
                            source,
                            database_identity,
                            family_identity,
                        )
                    )
                if (
                    active_destination_fs is not None
                    and temporary_family is not None
                    and temporary_snapshot is not None
                ):
                    teardown_errors.extend(
                        self._temporary_family_cleanup_errors(
                            active_destination_fs,
                            temporary_family,
                            temporary_snapshot,
                            published=published,
                            cleanup_sidecars=(
                                not temporary_sidecar_cleanup_attempted
                            ),
                        )
                    )
                if destination_fs is not None:
                    try:
                        destination_fs.close()
                    except BaseException as exc:
                        teardown_errors.append(
                            ("backup destination filesystem close failed", exc)
                        )
                self._apply_teardown_errors(operation_error, teardown_errors)

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
            operation_error: BaseException | None = None
            try:
                conn.execute("begin immediate")
                yield conn
                if before_commit is not None:
                    before_commit(conn)
                conn.commit()
            except BaseException as exc:
                operation_error = exc
                try:
                    conn.rollback()
                except BaseException as rollback_error:
                    exc.add_note(f"SQLite rollback failed: {rollback_error}")
                raise
            finally:
                self._apply_teardown_errors(
                    operation_error,
                    self._connection_shutdown_errors(
                        project_fs,
                        conn,
                        database_identity,
                        family_identity,
                    ),
                )


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
        except BaseException as exc:
            try:
                conn.rollback()
            except BaseException as rollback_error:
                exc.add_note(f"SQLite rollback failed: {rollback_error}")
            raise

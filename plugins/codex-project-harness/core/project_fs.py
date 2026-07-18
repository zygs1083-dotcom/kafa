"""Pinned, fail-closed access to canonical project files.

The kernel owns a small set of project-relative files.  This module keeps
their lexical authority and filesystem identity separate from arbitrary user
commands, which are intentionally outside this boundary.
"""

from __future__ import annotations

import errno
import ctypes
import os
import secrets
import stat
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Callable, Iterable, Iterator

from .errors import HarnessError


MAX_AUDIT_PATHS = 256
_PINNED_PROJECT_FILESYSTEMS = threading.local()
_WINDOWS_RENAME_CAPABILITY_ERRORS = frozenset({1, 50, 87, 120, 124})
_NT_STATUS_OBJECT_NAME_COLLISION = 0xC0000035
_NT_ERROR_MR_MID_NOT_FOUND = 317
_NT_OBJ_CASE_INSENSITIVE = 0x00000040
_NT_FILE_READ_ATTRIBUTES = 0x00000080
_NT_SYNCHRONIZE = 0x00100000
_NT_FILE_CREATE = 0x00000002
_NT_FILE_CREATED = 0x00000002
_NT_FILE_DIRECTORY_FILE = 0x00000001
_NT_FILE_WRITE_THROUGH = 0x00000002
_NT_FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
_WINDOWS_REPLACE_METADATA_UNVERIFIED = (
    "Windows ReplaceFileW failure may have changed source streams or security "
    "metadata; complete source metadata rollback is not verified and requires "
    "manual review"
)
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "CLOCK$",
    *(f"COM{value}" for value in range(1, 10)),
    *(f"LPT{value}" for value in range(1, 10)),
    *(f"COM{value}" for value in "¹²³"),
    *(f"LPT{value}" for value in "¹²³"),
}
_WINDOWS_INVALID = frozenset('<>:"|?*')
_POSIX_DIR_FD_AVAILABLE = all(
    function in os.supports_dir_fd
    for function in (os.open, os.stat, os.mkdir, os.unlink, os.rmdir)
)


def _posix_flagged_rename(
    source_parent: int,
    source_name: str,
    destination_parent: int,
    destination_name: str,
    *,
    exchange: bool,
) -> None:
    if os.name == "nt":
        raise OSError(errno.ENOSYS, "POSIX rename flags are unavailable")
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        function = getattr(libc, "renameatx_np", None)
        flag = 0x00000002 if exchange else 0x00000004
    elif sys.platform.startswith("linux"):
        function = getattr(libc, "renameat2", None)
        flag = 0x00000002 if exchange else 0x00000001
    else:
        function = None
        flag = 0
    if function is None:
        raise OSError(errno.ENOSYS, "atomic rename flags are unavailable")
    function.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    function.restype = ctypes.c_int
    result = function(
        source_parent,
        os.fsencode(source_name),
        destination_parent,
        os.fsencode(destination_name),
        flag,
    )
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def _posix_rename_exchange(
    source_parent: int,
    source_name: str,
    destination_parent: int,
    destination_name: str,
) -> None:
    _posix_flagged_rename(
        source_parent,
        source_name,
        destination_parent,
        destination_name,
        exchange=True,
    )


def _posix_rename_noreplace(
    source_parent: int,
    source_name: str,
    destination_parent: int,
    destination_name: str,
) -> None:
    _posix_flagged_rename(
        source_parent,
        source_name,
        destination_parent,
        destination_name,
        exchange=False,
    )


def _before_atomic_replace(_fs: "ProjectFS", _relative: Path) -> None:
    """Deterministic test seam immediately before the final identity check."""


def _before_windows_handle_rename(
    _backend: "_WindowsBackend",
    _source: Path,
    _destination: Path,
) -> None:
    """Deterministic test seam while the exact Windows source handle is pinned."""


def _after_windows_write_chunk(
    _backend: "_WindowsBackend",
    _relative: Path,
    _written_total: int,
) -> None:
    """Deterministic test seam after a partial Windows handle write."""


def _before_windows_handle_delete(
    _backend: "_WindowsBackend",
    _relative: Path,
) -> None:
    """Deterministic test seam while the exact Windows delete handle is pinned."""


def _before_windows_directory_create(
    _backend: "_WindowsBackend",
    _relative: Path,
) -> None:
    """Deterministic test seam while Windows parent handles remain pinned."""


def _before_windows_backup_cleanup(
    _backend: "_WindowsBackend",
    _destination: Path,
    _backup: Path,
) -> None:
    """Deterministic test seam while the published Windows leaf is pinned."""


def _windows_rename_error_reason(error: OSError) -> str:
    code = getattr(error, "winerror", None)
    if code is None:
        code = error.errno
    return (
        "platform-safety-unavailable"
        if code in _WINDOWS_RENAME_CAPABILITY_ERRORS
        else "path-identity-changed"
    )


class ProjectPathSafetyError(HarnessError):
    """A canonical path cannot be used without following untrusted authority."""

    def __init__(self, relative: Path | str, reason: str) -> None:
        self.relative = Path(str(relative))
        self.reason = reason
        super().__init__(
            f"unsafe-project-path: {self.relative.as_posix()}: {reason}"
        )


def _pinned_project_filesystems() -> list["ProjectFS"]:
    values = getattr(_PINNED_PROJECT_FILESYSTEMS, "values", None)
    if values is None:
        values = []
        _PINNED_PROJECT_FILESYSTEMS.values = values
    return values


@contextmanager
def pin_project_filesystem(project_fs: "ProjectFS") -> Iterator[None]:
    """Make nested same-thread opens borrow one pinned root identity."""

    values = _pinned_project_filesystems()
    values.append(project_fs)
    try:
        yield
    finally:
        if not values or values[-1] is not project_fs:
            raise RuntimeError("project filesystem pin stack is corrupted")
        values.pop()


@dataclass(frozen=True)
class _PathIdentity:
    volume: int
    file_id: int | bytes
    kind: str
    mode_or_attributes: int
    nlink: int


@dataclass(frozen=True)
class _PathSnapshot:
    exists: bool
    identity: _PathIdentity | None = None


@dataclass
class _WritableDestinationLease:
    restore_required: bool
    working_snapshot: _PathSnapshot
    discarded: bool = False


class _MissingAncestor(Exception):
    pass


class _WindowsCapabilityError(OSError):
    pass


class _NT_UNICODE_STRING(ctypes.Structure):
    _fields_ = (
        ("length", ctypes.c_uint16),
        ("maximum_length", ctypes.c_uint16),
        ("buffer", ctypes.POINTER(ctypes.c_uint16)),
    )


class _NT_OBJECT_ATTRIBUTES(ctypes.Structure):
    _fields_ = (
        ("length", ctypes.c_uint32),
        ("root_directory", ctypes.c_void_p),
        ("object_name", ctypes.POINTER(_NT_UNICODE_STRING)),
        ("attributes", ctypes.c_uint32),
        ("security_descriptor", ctypes.c_void_p),
        ("security_quality_of_service", ctypes.c_void_p),
    )


class _NT_IO_STATUS_VALUE(ctypes.Union):
    _fields_ = (
        ("status", ctypes.c_int32),
        ("pointer", ctypes.c_void_p),
    )


class _NT_IO_STATUS_BLOCK(ctypes.Structure):
    _fields_ = (
        ("value", _NT_IO_STATUS_VALUE),
        ("information", ctypes.c_size_t),
    )


def _validate_relative(value: Path | str) -> Path:
    windows_path = isinstance(value, PureWindowsPath)
    raw = value.as_posix() if windows_path else os.fspath(value)
    if not isinstance(raw, str):
        raw = os.fsdecode(raw)
    if (
        not raw
        or raw in {".", ".."}
        or "\x00" in raw
        or (not windows_path and "\\" in raw)
    ):
        raise ProjectPathSafetyError(Path(raw or "."), "invalid-relative-path")
    lexical_parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in lexical_parts):
        raise ProjectPathSafetyError(Path(raw), "invalid-relative-path")
    windows = PureWindowsPath(raw)
    candidate = Path(raw)
    if candidate.is_absolute() or windows.is_absolute() or windows.drive:
        raise ProjectPathSafetyError(candidate, "invalid-relative-path")
    parts = candidate.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ProjectPathSafetyError(candidate, "invalid-relative-path")
    for part in parts:
        if (
            any(ord(character) < 32 for character in part)
            or any(character in _WINDOWS_INVALID for character in part)
            or part.endswith((" ", "."))
            or part.split(".", 1)[0].upper() in _WINDOWS_RESERVED
        ):
            raise ProjectPathSafetyError(candidate, "invalid-relative-path")
    normalized = Path(*parts)
    if normalized.as_posix() != candidate.as_posix():
        raise ProjectPathSafetyError(candidate, "invalid-relative-path")
    return normalized


def _kind(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "other"


def _posix_identity(metadata: os.stat_result) -> _PathIdentity:
    kind = _kind(metadata.st_mode)
    return _PathIdentity(
        volume=int(metadata.st_dev),
        file_id=int(metadata.st_ino),
        kind=kind,
        mode_or_attributes=int(metadata.st_mode),
        # Directory link counts change as children are created on several
        # filesystems and are not part of the directory object's identity.
        nlink=0 if kind == "directory" else int(metadata.st_nlink),
    )


class _PosixBackend:
    def __init__(self, root: Path) -> None:
        required = (
            hasattr(os, "O_NOFOLLOW"),
            _POSIX_DIR_FD_AVAILABLE,
        )
        if not all(required):
            raise ProjectPathSafetyError(Path("."), "platform-safety-unavailable")
        self.root = root
        descriptor = self._open_root_path()
        try:
            metadata = os.fstat(descriptor)
            identity = _posix_identity(metadata)
            if identity.kind != "directory":
                raise ProjectPathSafetyError(Path("."), "unsafe-ancestor")
            self.root_identity = identity
            self.root_descriptor = descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @property
    def identity_key(self) -> tuple[object, ...]:
        return ("posix", self.root_identity.volume, self.root_identity.file_id)

    def _open_root_path(self) -> int:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(self.root, flags)
        except OSError as exc:
            raise ProjectPathSafetyError(Path("."), "path-identity-changed") from exc
        os.set_inheritable(descriptor, False)
        return descriptor

    def _open_root(self) -> int:
        try:
            descriptor = os.dup(self.root_descriptor)
        except OSError as exc:
            raise ProjectPathSafetyError(Path("."), "path-identity-changed") from exc
        os.set_inheritable(descriptor, False)
        if _posix_identity(os.fstat(descriptor)) != self.root_identity:
            os.close(descriptor)
            raise ProjectPathSafetyError(Path("."), "path-identity-changed")
        return descriptor

    def assert_root_path(self) -> None:
        try:
            current = _posix_identity(os.stat(self.root, follow_symlinks=False))
        except OSError as exc:
            raise ProjectPathSafetyError(Path("."), "path-identity-changed") from exc
        if current != self.root_identity:
            raise ProjectPathSafetyError(Path("."), "path-identity-changed")

    def close(self) -> None:
        descriptor = getattr(self, "root_descriptor", None)
        if descriptor is None:
            return
        self.root_descriptor = None
        os.close(descriptor)

    @contextmanager
    def _parent(
        self,
        relative: Path,
        *,
        create: bool,
    ) -> Iterator[tuple[int, str, tuple[tuple[int, str, _PathIdentity], ...]]]:
        self.assert_root_path()
        descriptors: list[int] = []
        ancestry: list[tuple[int, str, _PathIdentity]] = []
        current = self._open_root()
        descriptors.append(current)
        try:
            for component in relative.parts[:-1]:
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                flags |= getattr(os, "O_CLOEXEC", 0)
                try:
                    child = os.open(component, flags, dir_fd=current)
                except FileNotFoundError:
                    if not create:
                        raise _MissingAncestor
                    try:
                        os.mkdir(component, 0o700, dir_fd=current)
                        os.fsync(current)
                    except FileExistsError:
                        pass
                    try:
                        child = os.open(component, flags, dir_fd=current)
                    except OSError as exc:
                        raise ProjectPathSafetyError(relative, "unsafe-ancestor") from exc
                except OSError as exc:
                    reason = (
                        "unsafe-ancestor"
                        if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EMLINK}
                        else "path-identity-changed"
                    )
                    raise ProjectPathSafetyError(relative, reason) from exc
                os.set_inheritable(child, False)
                identity = _posix_identity(os.fstat(child))
                if identity.kind != "directory":
                    os.close(child)
                    raise ProjectPathSafetyError(relative, "unsafe-ancestor")
                if identity.volume != self.root_identity.volume:
                    os.close(child)
                    raise ProjectPathSafetyError(relative, "cross-device-ancestor")
                ancestry.append((current, component, identity))
                descriptors.append(child)
                current = child
            yield current, relative.name, tuple(ancestry)
        finally:
            for descriptor in reversed(descriptors):
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def _check_ancestry(
        self,
        relative: Path,
        ancestry: tuple[tuple[int, str, _PathIdentity], ...],
    ) -> None:
        self.assert_root_path()
        for parent, component, expected in ancestry:
            try:
                actual = _posix_identity(
                    os.stat(component, dir_fd=parent, follow_symlinks=False)
                )
            except OSError as exc:
                raise ProjectPathSafetyError(relative, "path-identity-changed") from exc
            if actual != expected:
                raise ProjectPathSafetyError(relative, "path-identity-changed")
        self.assert_root_path()

    def _snapshot_at(
        self,
        parent: int,
        name: str,
        relative: Path,
        *,
        expect_directory: bool = False,
        allow_missing: bool,
    ) -> _PathSnapshot:
        try:
            metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            if allow_missing:
                return _PathSnapshot(False)
            raise ProjectPathSafetyError(relative, "unsafe-target")
        except OSError as exc:
            raise ProjectPathSafetyError(relative, "unsafe-target") from exc
        identity = _posix_identity(metadata)
        expected_kind = "directory" if expect_directory else "file"
        if identity.kind != expected_kind:
            raise ProjectPathSafetyError(relative, "unsafe-target")
        if identity.volume != self.root_identity.volume:
            raise ProjectPathSafetyError(relative, "cross-device-ancestor")
        if identity.kind == "file" and identity.nlink != 1:
            raise ProjectPathSafetyError(relative, "hard-linked-target")
        return _PathSnapshot(True, identity)

    def _raw_snapshot_at(
        self,
        parent: int,
        name: str,
        relative: Path,
        *,
        allow_missing: bool,
    ) -> _PathSnapshot:
        """Capture a no-follow entry identity solely for rename recovery.

        Unlike ``_snapshot_at``, this helper intentionally does not authorize
        the leaf for reads, writes, or deletion.  Recovery must be able to
        recognize and reverse an exchange even when the displaced entry is a
        symlink, a hard link, or another unsafe object raced into the final
        syscall window.
        """

        try:
            metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            if allow_missing:
                return _PathSnapshot(False)
            raise ProjectPathSafetyError(relative, "path-identity-changed")
        except OSError as exc:
            raise ProjectPathSafetyError(
                relative,
                "path-identity-changed",
            ) from exc
        return _PathSnapshot(True, _posix_identity(metadata))

    @staticmethod
    def _same_raw_object(
        actual: _PathSnapshot,
        expected: _PathSnapshot,
    ) -> bool:
        if actual.exists != expected.exists:
            return False
        if not actual.exists:
            return True
        if actual.identity is None or expected.identity is None:
            return False
        return (
            actual.identity.volume == expected.identity.volume
            and actual.identity.file_id == expected.identity.file_id
            and actual.identity.kind == expected.identity.kind
        )

    def snapshot(
        self,
        relative: Path,
        *,
        allow_missing: bool,
        expect_directory: bool = False,
    ) -> _PathSnapshot:
        try:
            with self._parent(relative, create=False) as (parent, name, ancestry):
                snapshot = self._snapshot_at(
                    parent,
                    name,
                    relative,
                    expect_directory=expect_directory,
                    allow_missing=allow_missing,
                )
                self._check_ancestry(relative, ancestry)
                return snapshot
        except _MissingAncestor:
            if allow_missing:
                self.assert_root_path()
                return _PathSnapshot(False)
            raise ProjectPathSafetyError(relative, "unsafe-target")

    def ensure_directory(self, relative: Path) -> None:
        marker = relative / "__kafa_directory_marker__"
        try:
            with self._parent(marker, create=True) as (
                parent,
                _name,
                ancestry,
            ):
                self._check_ancestry(relative, ancestry)
                metadata = os.fstat(parent)
                identity = _posix_identity(metadata)
                if identity.kind != "directory":
                    raise ProjectPathSafetyError(
                        relative,
                        "unsafe-ancestor",
                    )
                self._check_ancestry(relative, ancestry)
        except ProjectPathSafetyError as exc:
            if exc.relative == marker:
                raise ProjectPathSafetyError(relative, exc.reason) from exc
            raise

    def audit(
        self,
        relative: Path,
        *,
        allow_missing: bool,
        expect_directory: bool = False,
    ) -> None:
        self.snapshot(
            relative,
            allow_missing=allow_missing,
            expect_directory=expect_directory,
        )

    def read_bytes(
        self,
        relative: Path,
        *,
        max_bytes: int | None,
        expected: _PathSnapshot | None = None,
    ) -> bytes:
        with self._parent(relative, create=False) as (parent, name, ancestry):
            snapshot = self._snapshot_at(
                parent, name, relative, allow_missing=False
            )
            if expected is not None and snapshot != expected:
                raise ProjectPathSafetyError(relative, "path-identity-changed")
            flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
            try:
                descriptor = os.open(name, flags, dir_fd=parent)
            except OSError as exc:
                raise ProjectPathSafetyError(relative, "path-identity-changed") from exc
            try:
                os.set_inheritable(descriptor, False)
                if _posix_identity(os.fstat(descriptor)) != snapshot.identity:
                    raise ProjectPathSafetyError(relative, "path-identity-changed")
                chunks: list[bytes] = []
                remaining = max_bytes
                while remaining is None or remaining > 0:
                    size = 65536 if remaining is None else min(65536, remaining)
                    chunk = os.read(descriptor, size)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    if remaining is not None:
                        remaining -= len(chunk)
                data = b"".join(chunks)
                if _posix_identity(os.fstat(descriptor)) != snapshot.identity:
                    raise ProjectPathSafetyError(relative, "path-identity-changed")
            finally:
                os.close(descriptor)
            self._check_ancestry(relative, ancestry)
            current = self._snapshot_at(
                parent, name, relative, allow_missing=False
            )
            if current != snapshot:
                raise ProjectPathSafetyError(relative, "path-identity-changed")
            self._check_ancestry(relative, ancestry)
            return data

    def sync_regular(
        self,
        relative: Path,
        expected: _PathSnapshot | None = None,
    ) -> _PathSnapshot:
        with self._parent(relative, create=False) as (parent, name, ancestry):
            snapshot = expected or self._snapshot_at(
                parent,
                name,
                relative,
                allow_missing=False,
            )
            flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
            try:
                descriptor = os.open(name, flags, dir_fd=parent)
            except OSError as exc:
                raise ProjectPathSafetyError(
                    relative,
                    "path-identity-changed",
                ) from exc
            try:
                os.set_inheritable(descriptor, False)
                if _posix_identity(os.fstat(descriptor)) != snapshot.identity:
                    raise ProjectPathSafetyError(
                        relative,
                        "path-identity-changed",
                    )
                os.fsync(descriptor)
                if _posix_identity(os.fstat(descriptor)) != snapshot.identity:
                    raise ProjectPathSafetyError(
                        relative,
                        "path-identity-changed",
                    )
            finally:
                os.close(descriptor)
            self._check_ancestry(relative, ancestry)
            if self._snapshot_at(
                parent,
                name,
                relative,
                allow_missing=False,
            ) != snapshot:
                raise ProjectPathSafetyError(relative, "path-identity-changed")
            self._check_ancestry(relative, ancestry)
            return snapshot

    def _write_all(self, descriptor: int, data: bytes) -> None:
        view = memoryview(data)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:
                raise OSError("short project-file write")
            written += count

    @staticmethod
    def _rename_error(
        relative: Path,
        error: OSError,
    ) -> ProjectPathSafetyError:
        unsupported = {
            errno.ENOSYS,
            errno.EINVAL,
            getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
            errno.EOPNOTSUPP,
        }
        reason = (
            "platform-safety-unavailable"
            if error.errno in unsupported
            else "path-identity-changed"
        )
        return ProjectPathSafetyError(relative, reason)

    def _reconcile_exchange_interruption(
        self,
        source_parent: int,
        source_name: str,
        source: Path,
        source_snapshot: _PathSnapshot,
        destination_parent: int,
        destination_name: str,
        destination: Path,
        destination_snapshot: _PathSnapshot,
        error: BaseException,
    ) -> None:
        try:
            source_after = self._raw_snapshot_at(
                source_parent,
                source_name,
                source,
                allow_missing=True,
            )
            destination_after = self._raw_snapshot_at(
                destination_parent,
                destination_name,
                destination,
                allow_missing=True,
            )
            if (
                self._same_raw_object(source_after, source_snapshot)
                and self._same_raw_object(
                    destination_after,
                    destination_snapshot,
                )
            ):
                return
            if self._same_raw_object(source_after, source_snapshot):
                raise ProjectPathSafetyError(
                    destination,
                    "path-identity-changed",
                )
            if self._same_raw_object(
                destination_after,
                destination_snapshot,
            ):
                raise ProjectPathSafetyError(
                    destination,
                    "path-identity-changed",
                )
            if (
                not source_after.exists
                or not destination_after.exists
                or not (
                    self._same_raw_object(
                        source_after,
                        destination_snapshot,
                    )
                    or self._same_raw_object(
                        destination_after,
                        source_snapshot,
                    )
                )
            ):
                raise ProjectPathSafetyError(
                    destination,
                    "path-identity-changed",
                )
            try:
                _posix_rename_exchange(
                    source_parent,
                    source_name,
                    destination_parent,
                    destination_name,
                )
            except BaseException as rollback_error:
                if (
                    self._same_raw_object(
                        self._raw_snapshot_at(
                            source_parent,
                            source_name,
                            source,
                            allow_missing=True,
                        ),
                        source_snapshot,
                    )
                    and self._same_raw_object(
                        self._raw_snapshot_at(
                            destination_parent,
                            destination_name,
                            destination,
                            allow_missing=True,
                        ),
                        source_after,
                    )
                ):
                    return
                raise rollback_error
            if (
                not self._same_raw_object(
                    self._raw_snapshot_at(
                        source_parent,
                        source_name,
                        source,
                        allow_missing=False,
                    ),
                    source_snapshot,
                )
                or not self._same_raw_object(
                    self._raw_snapshot_at(
                        destination_parent,
                        destination_name,
                        destination,
                        allow_missing=False,
                    ),
                    source_after,
                )
            ):
                raise ProjectPathSafetyError(
                    destination,
                    "path-identity-changed",
                )
        except BaseException as rollback_error:
            error.add_note(
                f"atomic exchange interruption rollback failed: {rollback_error}"
            )

    def _reconcile_noreplace_interruption(
        self,
        source_parent: int,
        source_name: str,
        source: Path,
        source_snapshot: _PathSnapshot,
        destination_parent: int,
        destination_name: str,
        destination: Path,
        error: BaseException,
    ) -> None:
        try:
            source_after = self._snapshot_at(
                source_parent,
                source_name,
                source,
                allow_missing=True,
            )
            destination_after = self._snapshot_at(
                destination_parent,
                destination_name,
                destination,
                allow_missing=True,
            )
            if source_after == source_snapshot and not destination_after.exists:
                return
            if source_after.exists or not destination_after.exists:
                raise ProjectPathSafetyError(
                    destination,
                    "path-identity-changed",
                )
            try:
                _posix_rename_noreplace(
                    destination_parent,
                    destination_name,
                    source_parent,
                    source_name,
                )
            except BaseException as rollback_error:
                if (
                    self._snapshot_at(
                        source_parent,
                        source_name,
                        source,
                        allow_missing=True,
                    )
                    == destination_after
                    and not self._snapshot_at(
                        destination_parent,
                        destination_name,
                        destination,
                        allow_missing=True,
                    ).exists
                ):
                    return
                raise rollback_error
            if (
                self._snapshot_at(
                    source_parent,
                    source_name,
                    source,
                    allow_missing=False,
                )
                != destination_after
                or self._snapshot_at(
                    destination_parent,
                    destination_name,
                    destination,
                    allow_missing=True,
                ).exists
            ):
                raise ProjectPathSafetyError(
                    destination,
                    "path-identity-changed",
                )
        except BaseException as rollback_error:
            error.add_note(
                f"exclusive rename interruption rollback failed: {rollback_error}"
            )

    def _exchange_checked(
        self,
        source_parent: int,
        source_name: str,
        source: Path,
        source_snapshot: _PathSnapshot,
        destination_parent: int,
        destination_name: str,
        destination: Path,
        destination_snapshot: _PathSnapshot,
    ) -> None:
        try:
            source_before = self._raw_snapshot_at(
                source_parent,
                source_name,
                source,
                allow_missing=False,
            )
            destination_before = self._raw_snapshot_at(
                destination_parent,
                destination_name,
                destination,
                allow_missing=False,
            )
        except ProjectPathSafetyError as exc:
            raise ProjectPathSafetyError(
                destination,
                "path-identity-changed",
            ) from exc
        if (
            source_before != source_snapshot
            or destination_before != destination_snapshot
        ):
            raise ProjectPathSafetyError(
                destination,
                "path-identity-changed",
            )
        try:
            _posix_rename_exchange(
                source_parent,
                source_name,
                destination_parent,
                destination_name,
            )
        except BaseException as exc:
            self._reconcile_exchange_interruption(
                source_parent,
                source_name,
                source,
                source_snapshot,
                destination_parent,
                destination_name,
                destination,
                destination_snapshot,
                exc,
            )
            if isinstance(exc, OSError):
                raise self._rename_error(destination, exc) from exc
            raise
        try:
            displaced = self._raw_snapshot_at(
                source_parent,
                source_name,
                source,
                allow_missing=False,
            )
            published = self._raw_snapshot_at(
                destination_parent,
                destination_name,
                destination,
                allow_missing=False,
            )
        except BaseException as exc:
            error = ProjectPathSafetyError(
                destination,
                "path-identity-changed",
            )
            self._reconcile_exchange_interruption(
                source_parent,
                source_name,
                source,
                source_snapshot,
                destination_parent,
                destination_name,
                destination,
                destination_snapshot,
                error,
            )
            raise error from exc
        if displaced == destination_snapshot and published == source_snapshot:
            return
        error = ProjectPathSafetyError(destination, "path-identity-changed")
        self._reconcile_exchange_interruption(
            source_parent,
            source_name,
            source,
            source_snapshot,
            destination_parent,
            destination_name,
            destination,
            destination_snapshot,
            error,
        )
        raise error

    def _rename_noreplace_checked(
        self,
        source_parent: int,
        source_name: str,
        source: Path,
        source_snapshot: _PathSnapshot,
        destination_parent: int,
        destination_name: str,
        destination: Path,
    ) -> None:
        try:
            _posix_rename_noreplace(
                source_parent,
                source_name,
                destination_parent,
                destination_name,
            )
        except BaseException as exc:
            self._reconcile_noreplace_interruption(
                source_parent,
                source_name,
                source,
                source_snapshot,
                destination_parent,
                destination_name,
                destination,
                exc,
            )
            if isinstance(exc, OSError):
                raise self._rename_error(destination, exc) from exc
            raise
        try:
            published = self._snapshot_at(
                destination_parent,
                destination_name,
                destination,
                allow_missing=False,
            )
            source_after = self._snapshot_at(
                source_parent,
                source_name,
                source,
                allow_missing=True,
            )
        except BaseException as exc:
            self._reconcile_noreplace_interruption(
                source_parent,
                source_name,
                source,
                source_snapshot,
                destination_parent,
                destination_name,
                destination,
                exc,
            )
            raise
        if published == source_snapshot and not source_after.exists:
            return
        error = ProjectPathSafetyError(destination, "path-identity-changed")
        try:
            if (
                not source_after.exists
                and self._snapshot_at(
                    destination_parent,
                    destination_name,
                    destination,
                    allow_missing=False,
                )
                == published
            ):
                _posix_rename_noreplace(
                    destination_parent,
                    destination_name,
                    source_parent,
                    source_name,
                )
                if (
                    self._snapshot_at(
                        source_parent,
                        source_name,
                        source,
                        allow_missing=False,
                    )
                    != source_snapshot
                ):
                    raise ProjectPathSafetyError(
                        source,
                        "path-identity-changed",
                    )
            else:
                raise ProjectPathSafetyError(
                    destination,
                    "path-identity-changed",
                )
        except BaseException as rollback_error:
            error.add_note(f"exclusive rename rollback failed: {rollback_error}")
        raise error

    def _quarantine_entry_checked(
        self,
        parent: int,
        name: str,
        relative: Path,
        expected: _PathSnapshot,
        *,
        expect_directory: bool = False,
        consume: Callable[[str, Path], None] | None = None,
    ) -> tuple[str, Path]:
        def reconcile_interruption(
            quarantine_name: str,
            quarantine_relative: Path,
            error: BaseException,
        ) -> None:
            try:
                current = self._snapshot_at(
                    parent,
                    name,
                    relative,
                    expect_directory=expect_directory,
                    allow_missing=True,
                )
                quarantined = self._snapshot_at(
                    parent,
                    quarantine_name,
                    quarantine_relative,
                    expect_directory=expect_directory,
                    allow_missing=True,
                )
                if current == expected:
                    return
                if not current.exists and quarantined == expected:
                    _posix_rename_noreplace(
                        parent,
                        quarantine_name,
                        parent,
                        name,
                    )
                    if (
                        self._snapshot_at(
                            parent,
                            name,
                            relative,
                            expect_directory=expect_directory,
                            allow_missing=False,
                        )
                        != expected
                        or self._snapshot_at(
                            parent,
                            quarantine_name,
                            quarantine_relative,
                            expect_directory=expect_directory,
                            allow_missing=True,
                        ).exists
                    ):
                        raise ProjectPathSafetyError(
                            relative,
                            "path-identity-changed",
                        )
                    return
                raise ProjectPathSafetyError(
                    relative,
                    "path-identity-changed",
                )
            except BaseException as rollback_error:
                error.add_note(
                    f"quarantine interruption rollback failed: {rollback_error}"
                )

        for _ in range(128):
            quarantine_name = (
                f".{name}.kafa-delete-{secrets.token_hex(12)}.tmp"
            )
            quarantine_relative = relative.with_name(quarantine_name)
            try:
                _posix_rename_noreplace(
                    parent,
                    name,
                    parent,
                    quarantine_name,
                )
            except BaseException as exc:
                if isinstance(exc, OSError) and exc.errno == errno.EEXIST:
                    current = self._snapshot_at(
                        parent,
                        name,
                        relative,
                        expect_directory=expect_directory,
                        allow_missing=True,
                    )
                    if current == expected:
                        continue
                    reconcile_interruption(
                        quarantine_name,
                        quarantine_relative,
                        exc,
                    )
                    raise ProjectPathSafetyError(
                        relative,
                        "path-identity-changed",
                    ) from exc
                reconcile_interruption(
                    quarantine_name,
                    quarantine_relative,
                    exc,
                )
                if isinstance(exc, OSError):
                    raise self._rename_error(relative, exc) from exc
                raise
            try:
                quarantined = self._snapshot_at(
                    parent,
                    quarantine_name,
                    quarantine_relative,
                    expect_directory=expect_directory,
                    allow_missing=False,
                )
                current = self._snapshot_at(
                    parent,
                    name,
                    relative,
                    expect_directory=expect_directory,
                    allow_missing=True,
                )
            except BaseException as exc:
                reconcile_interruption(
                    quarantine_name,
                    quarantine_relative,
                    exc,
                )
                raise
            if quarantined == expected and not current.exists:
                if consume is not None:
                    try:
                        consume(quarantine_name, quarantine_relative)
                    except BaseException as exc:
                        reconcile_interruption(
                            quarantine_name,
                            quarantine_relative,
                            exc,
                        )
                        raise
                return quarantine_name, quarantine_relative
            error = ProjectPathSafetyError(relative, "path-identity-changed")
            try:
                if not current.exists:
                    _posix_rename_noreplace(
                        parent,
                        quarantine_name,
                        parent,
                        name,
                    )
                    if self._snapshot_at(
                        parent,
                        name,
                        relative,
                        expect_directory=expect_directory,
                        allow_missing=False,
                    ) != quarantined:
                        raise ProjectPathSafetyError(
                            relative,
                            "path-identity-changed",
                        )
                else:
                    raise ProjectPathSafetyError(
                        relative,
                        "path-identity-changed",
                    )
            except BaseException as rollback_error:
                error.add_note(
                    f"quarantine rename rollback failed: {rollback_error}"
                )
            raise error
        raise ProjectPathSafetyError(relative, "path-identity-changed")

    def _delete_quarantined_regular(
        self,
        parent: int,
        quarantine_name: str,
        quarantine_relative: Path,
        expected: _PathSnapshot,
        descriptor: int,
        restore_name: str,
        restore_relative: Path,
        report_relative: Path,
    ) -> None:
        # The caller pins the canonical object before quarantine.  The
        # exclusive rename above then moves that object to a fresh 96-bit
        # random name.  A same-user process continuously discovering and
        # replacing unpredictable quarantine names is outside the documented
        # threat model; bounded races on canonical names are closed before
        # this point.
        if _posix_identity(os.fstat(descriptor)) != expected.identity:
            raise ProjectPathSafetyError(
                report_relative,
                "path-identity-changed",
            )
        if self._snapshot_at(
            parent,
            quarantine_name,
            quarantine_relative,
            allow_missing=False,
        ) != expected:
            raise ProjectPathSafetyError(
                report_relative,
                "path-identity-changed",
            )
        try:
            os.unlink(quarantine_name, dir_fd=parent)
            deleted = _posix_identity(os.fstat(descriptor))
            assert expected.identity is not None
            if (
                deleted.volume != expected.identity.volume
                or deleted.file_id != expected.identity.file_id
                or deleted.nlink != 0
            ):
                raise ProjectPathSafetyError(
                    report_relative,
                    "path-identity-changed",
                )
        except BaseException as exc:
            rollback_note: str | None = None
            try:
                if self._snapshot_at(
                    parent,
                    restore_name,
                    restore_relative,
                    allow_missing=True,
                ).exists:
                    raise ProjectPathSafetyError(
                        restore_relative,
                        "path-identity-changed",
                    )
                if self._snapshot_at(
                    parent,
                    quarantine_name,
                    quarantine_relative,
                    allow_missing=False,
                ) != expected:
                    raise ProjectPathSafetyError(
                        report_relative,
                        "path-identity-changed",
                    )
                _posix_rename_noreplace(
                    parent,
                    quarantine_name,
                    parent,
                    restore_name,
                )
                if self._snapshot_at(
                    parent,
                    restore_name,
                    restore_relative,
                    allow_missing=False,
                ) != expected:
                    raise ProjectPathSafetyError(
                        restore_relative,
                        "path-identity-changed",
                    )
            except BaseException as rollback_error:
                rollback_note = (
                    f"quarantine delete rollback failed: {rollback_error}"
                )
            if isinstance(exc, OSError):
                error = ProjectPathSafetyError(
                    report_relative,
                    "unsafe-target",
                )
                if rollback_note is not None:
                    error.add_note(rollback_note)
                raise error from exc
            if rollback_note is not None:
                exc.add_note(rollback_note)
            raise
        if self._snapshot_at(
            parent,
            quarantine_name,
            quarantine_relative,
            allow_missing=True,
        ).exists:
            raise ProjectPathSafetyError(
                report_relative,
                "path-identity-changed",
            )

    def _delete_quarantined_directory(
        self,
        parent: int,
        quarantine_name: str,
        quarantine_relative: Path,
        expected: _PathSnapshot,
        descriptor: int,
        restore_name: str,
        restore_relative: Path,
        report_relative: Path,
    ) -> None:
        if _posix_identity(os.fstat(descriptor)) != expected.identity:
            raise ProjectPathSafetyError(
                report_relative,
                "path-identity-changed",
            )
        if self._snapshot_at(
            parent,
            quarantine_name,
            quarantine_relative,
            expect_directory=True,
            allow_missing=False,
        ) != expected:
            raise ProjectPathSafetyError(
                report_relative,
                "path-identity-changed",
            )
        try:
            os.rmdir(quarantine_name, dir_fd=parent)
        except BaseException as exc:
            rollback_note: str | None = None
            try:
                if self._snapshot_at(
                    parent,
                    restore_name,
                    restore_relative,
                    expect_directory=True,
                    allow_missing=True,
                ).exists:
                    raise ProjectPathSafetyError(
                        restore_relative,
                        "path-identity-changed",
                    )
                if self._snapshot_at(
                    parent,
                    quarantine_name,
                    quarantine_relative,
                    expect_directory=True,
                    allow_missing=False,
                ) != expected:
                    raise ProjectPathSafetyError(
                        report_relative,
                        "path-identity-changed",
                    )
                _posix_rename_noreplace(
                    parent,
                    quarantine_name,
                    parent,
                    restore_name,
                )
                if self._snapshot_at(
                    parent,
                    restore_name,
                    restore_relative,
                    expect_directory=True,
                    allow_missing=False,
                ) != expected:
                    raise ProjectPathSafetyError(
                        restore_relative,
                        "path-identity-changed",
                    )
            except BaseException as rollback_error:
                rollback_note = (
                    f"directory delete rollback failed: {rollback_error}"
                )
            if isinstance(exc, OSError):
                error = ProjectPathSafetyError(
                    report_relative,
                    "unsafe-target",
                )
                if rollback_note is not None:
                    error.add_note(rollback_note)
                raise error from exc
            if rollback_note is not None:
                exc.add_note(rollback_note)
            raise
        if self._snapshot_at(
            parent,
            quarantine_name,
            quarantine_relative,
            expect_directory=True,
            allow_missing=True,
        ).exists:
            raise ProjectPathSafetyError(
                report_relative,
                "path-identity-changed",
            )

    def _open_pinned_regular(
        self,
        parent: int,
        name: str,
        relative: Path,
        expected: _PathSnapshot,
    ) -> int:
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(name, flags, dir_fd=parent)
        except OSError as exc:
            raise ProjectPathSafetyError(relative, "unsafe-target") from exc
        try:
            os.set_inheritable(descriptor, False)
            if _posix_identity(os.fstat(descriptor)) != expected.identity:
                raise ProjectPathSafetyError(
                    relative,
                    "path-identity-changed",
                )
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def atomic_write(
        self,
        fs: "ProjectFS",
        relative: Path,
        data: bytes,
        mode: int,
        expected_destination: _PathSnapshot | None = None,
    ) -> _PathSnapshot:
        with self._parent(relative, create=True) as (parent, name, ancestry):
            expected = self._snapshot_at(
                parent, name, relative, allow_missing=True
            )
            if (
                expected_destination is not None
                and expected != expected_destination
            ):
                raise ProjectPathSafetyError(
                    relative,
                    "path-identity-changed",
                )
            temporary_name = f".{name}.kafa-{secrets.token_hex(12)}.tmp"
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            flags |= getattr(os, "O_CLOEXEC", 0)
            descriptor: int | None = None
            displaced_descriptor: int | None = None
            temporary_identity: _PathIdentity | None = None
            published = False
            operation_error: BaseException | None = None
            try:
                descriptor = os.open(
                    temporary_name,
                    flags,
                    mode,
                    dir_fd=parent,
                )
                os.set_inheritable(descriptor, False)
                self._write_all(descriptor, data)
                os.fchmod(descriptor, mode)
                os.fsync(descriptor)
                temporary_identity = _posix_identity(os.fstat(descriptor))
                if temporary_identity.kind != "file" or temporary_identity.nlink != 1:
                    raise ProjectPathSafetyError(relative, "unsafe-target")
                _before_atomic_replace(fs, relative)
                self._check_ancestry(relative, ancestry)
                try:
                    current = self._snapshot_at(
                        parent, name, relative, allow_missing=True
                    )
                except ProjectPathSafetyError as exc:
                    if exc.reason in {
                        "unsafe-target",
                        "hard-linked-target",
                        "cross-device-ancestor",
                    }:
                        raise ProjectPathSafetyError(
                            relative, "path-identity-changed"
                        ) from exc
                    raise
                if current != expected:
                    raise ProjectPathSafetyError(relative, "path-identity-changed")
                temporary_snapshot = _PathSnapshot(
                    True,
                    temporary_identity,
                )
                if expected.exists:
                    displaced_descriptor = self._open_pinned_regular(
                        parent,
                        name,
                        relative,
                        expected,
                    )
                    try:
                        self._exchange_checked(
                            parent,
                            temporary_name,
                            relative.with_name(temporary_name),
                            temporary_snapshot,
                            parent,
                            name,
                            relative,
                            expected,
                        )
                        try:
                            self._quarantine_entry_checked(
                                parent,
                                temporary_name,
                                relative.with_name(temporary_name),
                                expected,
                                consume=lambda discarded_name, discarded_relative: (
                                    self._delete_quarantined_regular(
                                        parent,
                                        discarded_name,
                                        discarded_relative,
                                        expected,
                                        displaced_descriptor,
                                        temporary_name,
                                        relative.with_name(temporary_name),
                                        relative,
                                    )
                                ),
                            )
                        except BaseException as cleanup_error:
                            try:
                                rollback_source_snapshot = self._raw_snapshot_at(
                                    parent,
                                    temporary_name,
                                    relative.with_name(temporary_name),
                                    allow_missing=False,
                                )
                                self._exchange_checked(
                                    parent,
                                    temporary_name,
                                    relative.with_name(temporary_name),
                                    rollback_source_snapshot,
                                    parent,
                                    name,
                                    relative,
                                    temporary_snapshot,
                                )
                                self._quarantine_entry_checked(
                                    parent,
                                    temporary_name,
                                    relative.with_name(temporary_name),
                                    temporary_snapshot,
                                    consume=lambda retry_name, retry_relative: (
                                        self._delete_quarantined_regular(
                                            parent,
                                            retry_name,
                                            retry_relative,
                                            temporary_snapshot,
                                            descriptor,
                                            temporary_name,
                                            relative.with_name(temporary_name),
                                            relative,
                                        )
                                    ),
                                )
                            except BaseException as rollback_error:
                                cleanup_error.add_note(
                                    f"atomic publication rollback failed: {rollback_error}"
                                )
                            raise
                    finally:
                        os.close(displaced_descriptor)
                        displaced_descriptor = None
                else:
                    self._rename_noreplace_checked(
                        parent,
                        temporary_name,
                        relative.with_name(temporary_name),
                        temporary_snapshot,
                        parent,
                        name,
                        relative,
                    )
                published = True
                final = self._snapshot_at(
                    parent, name, relative, allow_missing=False
                )
                if final.identity != temporary_identity:
                    raise ProjectPathSafetyError(relative, "path-identity-changed")
                os.fsync(parent)
                self._check_ancestry(relative, ancestry)
                if self._snapshot_at(
                    parent,
                    name,
                    relative,
                    allow_missing=False,
                ).identity != temporary_identity:
                    raise ProjectPathSafetyError(relative, "path-identity-changed")
            except BaseException as exc:
                operation_error = exc
                raise
            finally:
                if displaced_descriptor is not None:
                    os.close(displaced_descriptor)
                if not published and descriptor is not None:
                    try:
                        owned_snapshot = _PathSnapshot(
                            True,
                            _posix_identity(os.fstat(descriptor)),
                        )
                        if self._snapshot_at(
                            parent,
                            temporary_name,
                            relative.with_name(temporary_name),
                            allow_missing=True,
                        ) == owned_snapshot:
                            self._quarantine_entry_checked(
                                parent,
                                temporary_name,
                                relative.with_name(temporary_name),
                                owned_snapshot,
                                consume=lambda cleanup_name, cleanup_relative: (
                                    self._delete_quarantined_regular(
                                        parent,
                                        cleanup_name,
                                        cleanup_relative,
                                        owned_snapshot,
                                        descriptor,
                                        temporary_name,
                                        relative.with_name(temporary_name),
                                        relative,
                                    )
                                ),
                            )
                    except BaseException as cleanup_error:
                        cleanup_detail = str(cleanup_error)
                        cleanup_cause = getattr(cleanup_error, "__cause__", None)
                        if cleanup_cause is not None:
                            cleanup_detail += f" (caused by {cleanup_cause})"
                        note = (
                            "atomic temporary cleanup failed for "
                            f"{relative.with_name(temporary_name)}: {cleanup_detail}"
                        )
                        if operation_error is not None:
                            operation_error.add_note(note)
                        else:
                            raise
                if descriptor is not None:
                    os.close(descriptor)
            assert temporary_identity is not None
            return _PathSnapshot(True, temporary_identity)

    def create_exclusive(
        self,
        relative: Path,
        data: bytes,
        mode: int,
    ) -> _PathSnapshot:
        with self._parent(relative, create=True) as (parent, name, ancestry):
            existing = self._snapshot_at(
                parent, name, relative, allow_missing=True
            )
            if existing.exists:
                raise FileExistsError(os.fspath(self.root / relative))
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            flags |= getattr(os, "O_CLOEXEC", 0)
            try:
                descriptor = os.open(name, flags, mode, dir_fd=parent)
            except FileExistsError:
                current = self._snapshot_at(
                    parent, name, relative, allow_missing=True
                )
                if current.exists:
                    raise ProjectPathSafetyError(relative, "path-identity-changed")
                raise
            except OSError as exc:
                raise ProjectPathSafetyError(relative, "unsafe-target") from exc
            created_identity = _posix_identity(os.fstat(descriptor))
            try:
                os.set_inheritable(descriptor, False)
                self._write_all(descriptor, data)
                os.fchmod(descriptor, mode)
                os.fsync(descriptor)
                identity = _posix_identity(os.fstat(descriptor))
                if identity.kind != "file" or identity.nlink != 1:
                    raise ProjectPathSafetyError(relative, "unsafe-target")
            except BaseException as error:
                try:
                    created_snapshot = _PathSnapshot(
                        True,
                        created_identity,
                    )
                    if self._snapshot_at(
                        parent,
                        name,
                        relative,
                        allow_missing=False,
                    ) != created_snapshot:
                        raise ProjectPathSafetyError(
                            relative,
                            "path-identity-changed",
                        )
                    self._quarantine_entry_checked(
                        parent,
                        name,
                        relative,
                        created_snapshot,
                        consume=lambda quarantine_name, quarantine_relative: (
                            self._delete_quarantined_regular(
                                parent,
                                quarantine_name,
                                quarantine_relative,
                                created_snapshot,
                                descriptor,
                                name,
                                relative,
                                relative,
                            )
                        ),
                    )
                    os.fsync(parent)
                except BaseException as cleanup_error:
                    error.add_note(
                        f"exclusive create cleanup failed: {cleanup_error}"
                    )
                finally:
                    os.close(descriptor)
                raise
            else:
                os.close(descriptor)
            self._check_ancestry(relative, ancestry)
            if self._snapshot_at(
                parent, name, relative, allow_missing=False
            ).identity != identity:
                raise ProjectPathSafetyError(relative, "path-identity-changed")
            os.fsync(parent)
            self._check_ancestry(relative, ancestry)
            return _PathSnapshot(True, identity)

    def open_lock_fd(self, relative: Path, mode: int) -> int:
        with self._parent(relative, create=True) as (parent, name, ancestry):
            expected = self._snapshot_at(
                parent, name, relative, allow_missing=True
            )
            flags = os.O_RDWR | os.O_NOFOLLOW
            if not expected.exists:
                flags |= os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_CLOEXEC", 0)
            try:
                descriptor = os.open(name, flags, mode, dir_fd=parent)
            except FileExistsError as exc:
                raise ProjectPathSafetyError(
                    relative,
                    "path-identity-changed",
                ) from exc
            except OSError as exc:
                raise ProjectPathSafetyError(relative, "unsafe-target") from exc
            try:
                os.set_inheritable(descriptor, False)
                identity = _posix_identity(os.fstat(descriptor))
                if (
                    identity.kind != "file"
                    or identity.nlink != 1
                    or identity.volume != self.root_identity.volume
                ):
                    reason = (
                        "hard-linked-target"
                        if identity.kind == "file" and identity.nlink != 1
                        else "unsafe-target"
                    )
                    raise ProjectPathSafetyError(relative, reason)
                if expected.exists:
                    assert expected.identity is not None
                    expected_object = (
                        expected.identity.volume,
                        expected.identity.file_id,
                        expected.identity.kind,
                        expected.identity.nlink,
                    )
                    opened_object = (
                        identity.volume,
                        identity.file_id,
                        identity.kind,
                        identity.nlink,
                    )
                    if expected_object != opened_object:
                        raise ProjectPathSafetyError(
                            relative,
                            "path-identity-changed",
                        )
                os.fchmod(descriptor, mode)
                identity = _posix_identity(os.fstat(descriptor))
                self._check_ancestry(relative, ancestry)
                current = self._snapshot_at(
                    parent, name, relative, allow_missing=False
                )
                if current.identity != identity:
                    raise ProjectPathSafetyError(relative, "path-identity-changed")
                self._check_ancestry(relative, ancestry)
                return descriptor
            except BaseException:
                os.close(descriptor)
                raise

    def open_exclusion_fd(self) -> int:
        self.assert_root_path()
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(".", flags, dir_fd=self.root_descriptor)
        except OSError as exc:
            raise ProjectPathSafetyError(
                Path("."),
                "platform-safety-unavailable",
            ) from exc
        os.set_inheritable(descriptor, False)
        if _posix_identity(os.fstat(descriptor)) != self.root_identity:
            os.close(descriptor)
            raise ProjectPathSafetyError(Path("."), "path-identity-changed")
        try:
            self.assert_root_path()
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def unlink_regular(
        self,
        relative: Path,
        *,
        missing_ok: bool,
        expected: _PathSnapshot | None = None,
    ) -> None:
        try:
            with self._parent(relative, create=False) as (parent, name, ancestry):
                snapshot = self._snapshot_at(
                    parent, name, relative, allow_missing=missing_ok
                )
                if expected is not None and snapshot != expected:
                    raise ProjectPathSafetyError(relative, "path-identity-changed")
                if not snapshot.exists:
                    return
                self._check_ancestry(relative, ancestry)
                if self._snapshot_at(
                    parent, name, relative, allow_missing=False
                ) != snapshot:
                    raise ProjectPathSafetyError(relative, "path-identity-changed")
                descriptor = self._open_pinned_regular(
                    parent,
                    name,
                    relative,
                    snapshot,
                )
                try:
                    self._quarantine_entry_checked(
                        parent,
                        name,
                        relative,
                        snapshot,
                        consume=lambda quarantine_name, quarantine_relative: (
                            self._delete_quarantined_regular(
                                parent,
                                quarantine_name,
                                quarantine_relative,
                                snapshot,
                                descriptor,
                                name,
                                relative,
                                relative,
                            )
                        ),
                    )
                finally:
                    os.close(descriptor)
                os.fsync(parent)
                self._check_ancestry(relative, ancestry)
        except _MissingAncestor:
            if not missing_ok:
                raise ProjectPathSafetyError(relative, "unsafe-target")
            self.assert_root_path()

    def create_unique_directory(self, parent_relative: Path, prefix: str) -> Path:
        self.ensure_directory(parent_relative)
        marker = parent_relative / "__kafa_unique_directory__"
        with self._parent(marker, create=False) as (parent, _name, ancestry):
            for _ in range(128):
                name = f"{prefix}{secrets.token_hex(8)}"
                try:
                    os.mkdir(name, 0o700, dir_fd=parent)
                except FileExistsError:
                    continue
                self._check_ancestry(parent_relative, ancestry)
                identity = _posix_identity(
                    os.stat(name, dir_fd=parent, follow_symlinks=False)
                )
                if identity.kind != "directory" or identity.volume != self.root_identity.volume:
                    raise ProjectPathSafetyError(parent_relative / name, "unsafe-target")
                os.fsync(parent)
                self._check_ancestry(parent_relative, ancestry)
                return parent_relative / name
        raise ProjectPathSafetyError(parent_relative, "path-identity-changed")

    def create_directory_exclusive(self, relative: Path, mode: int) -> None:
        # Walking the synthetic path creates and pins relative.parent without
        # treating the requested final directory as an ancestor.
        marker = relative.parent / "__kafa_directory_parent__"
        with self._parent(marker, create=True) as (parent, _name, ancestry):
            try:
                os.mkdir(relative.name, mode, dir_fd=parent)
            except FileExistsError:
                self._snapshot_at(
                    parent,
                    relative.name,
                    relative,
                    expect_directory=True,
                    allow_missing=False,
                )
                raise FileExistsError(os.fspath(self.root / relative))
            self._check_ancestry(relative, ancestry)
            identity = _posix_identity(
                os.stat(relative.name, dir_fd=parent, follow_symlinks=False)
            )
            if identity.kind != "directory" or identity.volume != self.root_identity.volume:
                raise ProjectPathSafetyError(relative, "unsafe-target")
            os.fsync(parent)
            self._check_ancestry(relative, ancestry)

    def replace_file(
        self,
        source: Path,
        destination: Path,
        expected_source: _PathSnapshot | None = None,
        expected_destination: _PathSnapshot | None = None,
    ) -> None:
        if source == destination:
            raise ProjectPathSafetyError(destination, "invalid-relative-path")
        with self._parent(source, create=False) as (
            source_parent,
            source_name,
            source_ancestry,
        ), self._parent(destination, create=True) as (
            destination_parent,
            destination_name,
            destination_ancestry,
        ):
            source_snapshot = self._snapshot_at(
                source_parent,
                source_name,
                source,
                allow_missing=False,
            )
            destination_snapshot = self._snapshot_at(
                destination_parent,
                destination_name,
                destination,
                allow_missing=True,
            )
            if expected_source is not None and source_snapshot != expected_source:
                raise ProjectPathSafetyError(
                    source,
                    "path-identity-changed",
                )
            if (
                expected_destination is not None
                and destination_snapshot != expected_destination
            ):
                raise ProjectPathSafetyError(
                    destination,
                    "path-identity-changed",
                )
            self._check_ancestry(source, source_ancestry)
            self._check_ancestry(destination, destination_ancestry)
            if self._snapshot_at(
                source_parent,
                source_name,
                source,
                allow_missing=False,
            ) != source_snapshot:
                raise ProjectPathSafetyError(source, "path-identity-changed")
            if self._snapshot_at(
                destination_parent,
                destination_name,
                destination,
                allow_missing=True,
            ) != destination_snapshot:
                raise ProjectPathSafetyError(destination, "path-identity-changed")
            source_descriptor = self._open_pinned_regular(
                source_parent,
                source_name,
                source,
                source_snapshot,
            )
            destination_descriptor: int | None = None
            try:
                if destination_snapshot.exists:
                    destination_descriptor = self._open_pinned_regular(
                        destination_parent,
                        destination_name,
                        destination,
                        destination_snapshot,
                    )
                    self._exchange_checked(
                        source_parent,
                        source_name,
                        source,
                        source_snapshot,
                        destination_parent,
                        destination_name,
                        destination,
                        destination_snapshot,
                    )
                    try:
                        self._quarantine_entry_checked(
                            source_parent,
                            source_name,
                            source,
                            destination_snapshot,
                            consume=lambda discarded_name, discarded_relative: (
                                self._delete_quarantined_regular(
                                    source_parent,
                                    discarded_name,
                                    discarded_relative,
                                    destination_snapshot,
                                    destination_descriptor,
                                    source_name,
                                    source,
                                    source,
                                )
                            ),
                        )
                    except BaseException as cleanup_error:
                        try:
                            rollback_source_snapshot = self._raw_snapshot_at(
                                source_parent,
                                source_name,
                                source,
                                allow_missing=False,
                            )
                            self._exchange_checked(
                                source_parent,
                                source_name,
                                source,
                                rollback_source_snapshot,
                                destination_parent,
                                destination_name,
                                destination,
                                source_snapshot,
                            )
                        except BaseException as rollback_error:
                            cleanup_error.add_note(
                                f"file replacement rollback failed: {rollback_error}"
                            )
                        raise
                else:
                    self._rename_noreplace_checked(
                        source_parent,
                        source_name,
                        source,
                        source_snapshot,
                        destination_parent,
                        destination_name,
                        destination,
                    )
            finally:
                if destination_descriptor is not None:
                    os.close(destination_descriptor)
                os.close(source_descriptor)
            final = self._snapshot_at(
                destination_parent,
                destination_name,
                destination,
                allow_missing=False,
            )
            if final.identity != source_snapshot.identity:
                raise ProjectPathSafetyError(destination, "path-identity-changed")
            os.fsync(source_parent)
            if destination_parent != source_parent:
                os.fsync(destination_parent)
            self._check_ancestry(source, source_ancestry)
            self._check_ancestry(destination, destination_ancestry)
            if self._snapshot_at(
                destination_parent,
                destination_name,
                destination,
                allow_missing=False,
            ).identity != source_snapshot.identity:
                raise ProjectPathSafetyError(destination, "path-identity-changed")

    def remove_empty_directory(self, relative: Path, *, missing_ok: bool) -> None:
        marker = relative.parent / "__kafa_directory_parent__"
        try:
            with self._parent(marker, create=False) as (
                parent,
                _name,
                ancestry,
            ):
                snapshot = self._snapshot_at(
                    parent,
                    relative.name,
                    relative,
                    expect_directory=True,
                    allow_missing=missing_ok,
                )
                if not snapshot.exists:
                    return
                self._check_ancestry(relative, ancestry)
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                flags |= getattr(os, "O_CLOEXEC", 0)
                try:
                    descriptor = os.open(
                        relative.name,
                        flags,
                        dir_fd=parent,
                    )
                except OSError as exc:
                    raise ProjectPathSafetyError(
                        relative,
                        "unsafe-target",
                    ) from exc
                try:
                    if _posix_identity(os.fstat(descriptor)) != snapshot.identity:
                        raise ProjectPathSafetyError(
                            relative,
                            "path-identity-changed",
                        )
                    self._quarantine_entry_checked(
                        parent,
                        relative.name,
                        relative,
                        snapshot,
                        expect_directory=True,
                        consume=lambda quarantine_name, quarantine_relative: (
                            self._delete_quarantined_directory(
                                parent,
                                quarantine_name,
                                quarantine_relative,
                                snapshot,
                                descriptor,
                                relative.name,
                                relative,
                                relative,
                            )
                        ),
                    )
                finally:
                    os.close(descriptor)
                os.fsync(parent)
                self._check_ancestry(relative, ancestry)
        except _MissingAncestor:
            if not missing_ok:
                raise ProjectPathSafetyError(relative, "unsafe-target")
            self.assert_root_path()


if os.name == "nt":  # pragma: no cover - exercised by the Windows matrix
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _DELETE = 0x00010000
    _FILE_WRITE_ATTRIBUTES = 0x00000100
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _FILE_SHARE_DELETE = 0x00000004
    _CREATE_NEW = 1
    _OPEN_EXISTING = 3
    _OPEN_ALWAYS = 4
    _FILE_ATTRIBUTE_READONLY = 0x00000001
    _FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    _FILE_ATTRIBUTE_NORMAL = 0x00000080
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FILE_BASIC_INFO_CLASS = 0
    _FILE_DISPOSITION_INFO_CLASS = 4
    _FILE_DISPOSITION_INFO_EX_CLASS = 21
    _FILE_RENAME_INFO_EX_CLASS = 22
    _FILE_DISPOSITION_DELETE = 0x00000001
    _FILE_DISPOSITION_POSIX_SEMANTICS = 0x00000002
    _FILE_DISPOSITION_IGNORE_READONLY_ATTRIBUTE = 0x00000010
    _FILE_RENAME_REPLACE_IF_EXISTS = 0x00000001
    _FILE_RENAME_POSIX_SEMANTICS = 0x00000002
    _FILE_RENAME_IGNORE_READONLY_ATTRIBUTE = 0x00000040
    _FILE_ID_INFO_CLASS = 18
    _REPLACEFILE_FLAGS_NONE = 0

    class _FILETIME(ctypes.Structure):
        _fields_ = (("low", wintypes.DWORD), ("high", wintypes.DWORD))

    class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = (
            ("attributes", wintypes.DWORD),
            ("creation_time", _FILETIME),
            ("access_time", _FILETIME),
            ("write_time", _FILETIME),
            ("volume_serial", wintypes.DWORD),
            ("size_high", wintypes.DWORD),
            ("size_low", wintypes.DWORD),
            ("nlinks", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        )

    class _FILE_ID_128(ctypes.Structure):
        _fields_ = (("identifier", ctypes.c_ubyte * 16),)

    class _FILE_ID_INFO(ctypes.Structure):
        _fields_ = (
            ("volume_serial", ctypes.c_ulonglong),
            ("file_id", _FILE_ID_128),
        )

    class _FILE_DISPOSITION_INFO(ctypes.Structure):
        _fields_ = (("delete_file", ctypes.c_ubyte),)

    class _FILE_DISPOSITION_INFO_EX(ctypes.Structure):
        _fields_ = (("flags", wintypes.DWORD),)

    class _FILE_BASIC_INFO(ctypes.Structure):
        _fields_ = (
            ("creation_time", ctypes.c_longlong),
            ("last_access_time", ctypes.c_longlong),
            ("last_write_time", ctypes.c_longlong),
            ("change_time", ctypes.c_longlong),
            ("file_attributes", wintypes.DWORD),
        )

    class _FILE_RENAME_INFO(ctypes.Structure):
        _fields_ = (
            ("flags", wintypes.DWORD),
            ("root_directory", wintypes.HANDLE),
            ("file_name_length", wintypes.DWORD),
            ("file_name", ctypes.c_wchar * 1),
        )


class _WindowsApi:
    """Small injectable Win32 binding; unavailable calls fail closed."""

    def __init__(self) -> None:
        if os.name != "nt":
            self.available = False
            return
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            ntdll = ctypes.WinDLL("ntdll")
            self.NtCreateFile = ntdll.NtCreateFile
            self.NtCreateFile.argtypes = (
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.c_uint32,
                ctypes.POINTER(_NT_OBJECT_ATTRIBUTES),
                ctypes.POINTER(_NT_IO_STATUS_BLOCK),
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_uint32,
            )
            self.NtCreateFile.restype = ctypes.c_int32
            self.RtlNtStatusToDosError = ntdll.RtlNtStatusToDosError
            self.RtlNtStatusToDosError.argtypes = (ctypes.c_int32,)
            self.RtlNtStatusToDosError.restype = ctypes.c_uint32
            self.CreateFileW = kernel32.CreateFileW
            self.CreateFileW.argtypes = (
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.LPVOID,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.HANDLE,
            )
            self.CreateFileW.restype = wintypes.HANDLE
            self.GetFileInformationByHandle = kernel32.GetFileInformationByHandle
            self.GetFileInformationByHandle.argtypes = (
                wintypes.HANDLE,
                ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION),
            )
            self.GetFileInformationByHandle.restype = wintypes.BOOL
            self.GetFileInformationByHandleEx = kernel32.GetFileInformationByHandleEx
            self.GetFileInformationByHandleEx.argtypes = (
                wintypes.HANDLE,
                ctypes.c_int,
                wintypes.LPVOID,
                wintypes.DWORD,
            )
            self.GetFileInformationByHandleEx.restype = wintypes.BOOL
            self.SetFileInformationByHandle = (
                kernel32.SetFileInformationByHandle
            )
            self.SetFileInformationByHandle.argtypes = (
                wintypes.HANDLE,
                ctypes.c_int,
                wintypes.LPVOID,
                wintypes.DWORD,
            )
            self.SetFileInformationByHandle.restype = wintypes.BOOL
            self.FlushFileBuffers = kernel32.FlushFileBuffers
            self.FlushFileBuffers.argtypes = (wintypes.HANDLE,)
            self.FlushFileBuffers.restype = wintypes.BOOL
            self.WriteFile = kernel32.WriteFile
            self.WriteFile.argtypes = (
                wintypes.HANDLE,
                wintypes.LPCVOID,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
                wintypes.LPVOID,
            )
            self.WriteFile.restype = wintypes.BOOL
            self.ReplaceFileW = kernel32.ReplaceFileW
            self.ReplaceFileW.argtypes = (
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.LPVOID,
                wintypes.LPVOID,
            )
            self.ReplaceFileW.restype = wintypes.BOOL
            self.CloseHandle = kernel32.CloseHandle
            self.CloseHandle.argtypes = (wintypes.HANDLE,)
            self.CloseHandle.restype = wintypes.BOOL
        except (AttributeError, OSError):
            self.available = False
        else:
            self.available = True

    def error(self) -> OSError:
        code = ctypes.get_last_error()
        return OSError(code, ctypes.FormatError(code))

    def create_directory_relative(self, parent_handle: int, leaf: str) -> int:
        if (
            not leaf
            or leaf in {".", ".."}
            or "\x00" in leaf
            or "/" in leaf
            or "\\" in leaf
        ):
            raise OSError(errno.EINVAL, "invalid relative directory leaf", leaf)
        try:
            encoded = leaf.encode("utf-16-le")
        except UnicodeEncodeError as exc:
            raise OSError(errno.EINVAL, "invalid UTF-16 directory leaf", leaf) from exc
        if not encoded or len(encoded) > 0xFFFC:
            raise OSError(errno.ENAMETOOLONG, "directory leaf is too long", leaf)

        units = (ctypes.c_uint16 * (len(encoded) // 2 + 1))()
        ctypes.memmove(units, encoded, len(encoded))
        name = _NT_UNICODE_STRING(
            len(encoded),
            len(encoded) + 2,
            ctypes.cast(units, ctypes.POINTER(ctypes.c_uint16)),
        )
        attributes = _NT_OBJECT_ATTRIBUTES(
            ctypes.sizeof(_NT_OBJECT_ATTRIBUTES),
            ctypes.c_void_p(parent_handle),
            ctypes.pointer(name),
            _NT_OBJ_CASE_INSENSITIVE,
            None,
            None,
        )
        status_block = _NT_IO_STATUS_BLOCK()
        handle = ctypes.c_void_p()
        def close_output_handle() -> None:
            actual = handle.value
            if actual not in (None, _INVALID_HANDLE_VALUE):
                self.CloseHandle(actual)
                handle.value = None

        try:
            status = int(
                self.NtCreateFile(
                    ctypes.byref(handle),
                    _DELETE
                    | _NT_FILE_READ_ATTRIBUTES
                    | _NT_SYNCHRONIZE,
                    ctypes.byref(attributes),
                    ctypes.byref(status_block),
                    None,
                    _FILE_ATTRIBUTE_NORMAL,
                    _FILE_SHARE_READ | _FILE_SHARE_WRITE,
                    _NT_FILE_CREATE,
                    _NT_FILE_DIRECTORY_FILE
                    | _NT_FILE_WRITE_THROUGH
                    | _NT_FILE_SYNCHRONOUS_IO_NONALERT,
                    None,
                    0,
                )
            )
        except BaseException:
            close_output_handle()
            raise
        if status < 0:
            close_output_handle()
            if status & 0xFFFFFFFF == _NT_STATUS_OBJECT_NAME_COLLISION:
                raise FileExistsError(errno.EEXIST, "directory already exists", leaf)
            code = int(self.RtlNtStatusToDosError(status))
            if code == _NT_ERROR_MR_MID_NOT_FOUND:
                raise _WindowsCapabilityError(
                    errno.ENOSYS,
                    f"unmapped NtCreateFile status 0x{status & 0xFFFFFFFF:08x}",
                    leaf,
                )
            raise ctypes.WinError(code)

        actual_handle = handle.value
        if (
            actual_handle in (None, _INVALID_HANDLE_VALUE)
            or int(status_block.information) != _NT_FILE_CREATED
        ):
            close_output_handle()
            raise _WindowsCapabilityError(
                errno.EIO,
                "NtCreateFile did not return a newly created directory",
                leaf,
            )
        return int(actual_handle)


class _WindowsBackend:  # pragma: no cover - exercised by Windows validation
    def __init__(self, root: Path, api: _WindowsApi | None = None) -> None:
        self.root = root
        self.api = api or _WindowsApi()
        if not self.api.available:
            raise ProjectPathSafetyError(Path("."), "platform-safety-unavailable")
        handle = self._open_handle(root, directory=True)
        try:
            self.root_identity = self._identity(handle, expect_directory=True, relative=Path("."))
            self.root_handle = handle
        except BaseException:
            self._close(handle)
            raise

    @property
    def identity_key(self) -> tuple[object, ...]:
        return ("windows", self.root_identity.volume, self.root_identity.file_id)

    def _close(self, handle: int) -> None:
        if handle not in (None, _INVALID_HANDLE_VALUE):
            self.api.CloseHandle(handle)

    def assert_root_path(self) -> None:
        try:
            handle = self._open_handle(self.root, directory=True)
        except OSError as exc:
            raise ProjectPathSafetyError(Path("."), "path-identity-changed") from exc
        try:
            if (
                self._identity(
                    handle,
                    expect_directory=True,
                    relative=Path("."),
                )
                != self.root_identity
            ):
                raise ProjectPathSafetyError(Path("."), "path-identity-changed")
        finally:
            self._close(handle)

    def close(self) -> None:
        handle = getattr(self, "root_handle", _INVALID_HANDLE_VALUE)
        if handle == _INVALID_HANDLE_VALUE:
            return
        self.root_handle = _INVALID_HANDLE_VALUE
        self._close(handle)

    def _open_handle(
        self,
        path: Path,
        *,
        directory: bool,
        access: int = 0,
        disposition: int | None = None,
        share_delete: bool = False,
        exclusive_share: bool = False,
    ) -> int:
        flags = _FILE_FLAG_OPEN_REPARSE_POINT
        if directory:
            flags |= _FILE_FLAG_BACKUP_SEMANTICS
        if exclusive_share and share_delete:
            raise ValueError(
                "exclusive_share and share_delete are mutually exclusive"
            )
        share = 0
        if not exclusive_share:
            share = _FILE_SHARE_READ | _FILE_SHARE_WRITE
            if share_delete:
                share |= _FILE_SHARE_DELETE
        handle = self.api.CreateFileW(
            os.fspath(path),
            access,
            share,
            None,
            disposition or _OPEN_EXISTING,
            flags,
            None,
        )
        if handle == _INVALID_HANDLE_VALUE:
            raise self.api.error()
        return handle

    def _raw_identity(
        self,
        handle: int,
        *,
        relative: Path,
    ) -> _PathIdentity:
        basic = _BY_HANDLE_FILE_INFORMATION()
        file_id = _FILE_ID_INFO()
        if not self.api.GetFileInformationByHandle(handle, ctypes.byref(basic)):
            raise ProjectPathSafetyError(relative, "platform-safety-unavailable")
        if not self.api.GetFileInformationByHandleEx(
            handle,
            _FILE_ID_INFO_CLASS,
            ctypes.byref(file_id),
            ctypes.sizeof(file_id),
        ):
            raise ProjectPathSafetyError(relative, "platform-safety-unavailable")
        attributes = int(basic.attributes)
        is_directory = bool(attributes & _FILE_ATTRIBUTE_DIRECTORY)
        is_reparse = bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)
        identity = _PathIdentity(
            volume=int(file_id.volume_serial),
            file_id=bytes(file_id.file_id.identifier),
            kind=(
                "reparse"
                if is_reparse
                else "directory" if is_directory else "file"
            ),
            mode_or_attributes=attributes,
            nlink=0 if is_directory else int(basic.nlinks),
        )
        return identity

    def _identity(
        self,
        handle: int,
        *,
        expect_directory: bool,
        relative: Path,
    ) -> _PathIdentity:
        identity = self._raw_identity(handle, relative=relative)
        expected_kind = "directory" if expect_directory else "file"
        if identity.kind != expected_kind:
            raise ProjectPathSafetyError(relative, "unsafe-target")
        is_directory = identity.kind == "directory"
        if not is_directory and identity.nlink > 1:
            raise ProjectPathSafetyError(relative, "hard-linked-target")
        if not is_directory and identity.nlink == 0:
            raise ProjectPathSafetyError(relative, "path-identity-changed")
        return identity

    def _delete_on_close(self, handle: int, relative: Path) -> None:
        disposition = _FILE_DISPOSITION_INFO_EX(
            _FILE_DISPOSITION_DELETE
            | _FILE_DISPOSITION_POSIX_SEMANTICS
            | _FILE_DISPOSITION_IGNORE_READONLY_ATTRIBUTE
        )
        if not self.api.SetFileInformationByHandle(
            handle,
            _FILE_DISPOSITION_INFO_EX_CLASS,
            ctypes.byref(disposition),
            ctypes.sizeof(disposition),
        ):
            raise ProjectPathSafetyError(
                relative,
                "platform-safety-unavailable",
            ) from self.api.error()

    def _apply_file_mode(self, handle: int, relative: Path, mode: int) -> None:
        basic = _BY_HANDLE_FILE_INFORMATION()
        if not self.api.GetFileInformationByHandle(handle, ctypes.byref(basic)):
            raise ProjectPathSafetyError(
                relative,
                "platform-safety-unavailable",
            ) from self.api.error()
        attributes = int(basic.attributes) & ~_FILE_ATTRIBUTE_NORMAL
        if mode & 0o222:
            attributes &= ~_FILE_ATTRIBUTE_READONLY
        else:
            attributes |= _FILE_ATTRIBUTE_READONLY
        if attributes == 0:
            attributes = _FILE_ATTRIBUTE_NORMAL
        information = _FILE_BASIC_INFO(0, 0, 0, 0, attributes)
        if not self.api.SetFileInformationByHandle(
            handle,
            _FILE_BASIC_INFO_CLASS,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            raise ProjectPathSafetyError(
                relative,
                "platform-safety-unavailable",
            ) from self.api.error()

    def _cleanup_created_directory_handle(
        self,
        handle: int,
        relative: Path,
    ) -> str:
        try:
            self._delete_on_close(handle, relative)
        except BaseException as exc:
            return f"directory delete-on-close failed: {exc}"
        finally:
            self._close(handle)
        return ""

    def _create_directory_at(
        self,
        parent_handle: int,
        leaf: str,
        relative: Path,
    ) -> tuple[int, _PathIdentity]:
        try:
            handle = self.api.create_directory_relative(parent_handle, leaf)
        except FileExistsError:
            raise
        except _WindowsCapabilityError as exc:
            raise ProjectPathSafetyError(
                relative,
                "platform-safety-unavailable",
            ) from exc
        except OSError as exc:
            raise ProjectPathSafetyError(
                relative,
                _windows_rename_error_reason(exc),
            ) from exc

        try:
            identity = self._identity(
                handle,
                expect_directory=True,
                relative=relative,
            )
            if identity.volume != self.root_identity.volume:
                raise ProjectPathSafetyError(
                    relative,
                    "cross-device-ancestor",
                )
        except BaseException as exc:
            cleanup_error = self._cleanup_created_directory_handle(
                handle,
                relative,
            )
            if cleanup_error:
                exc.add_note(cleanup_error)
            raise
        return handle, identity

    def _reconcile_windows_handle_rename_interruption(
        self,
        source_handle: int,
        source_identity: _PathIdentity,
        destination: Path,
        relative: Path,
        rollback_destination: Path,
        rollback_relative: Path,
        error: BaseException,
    ) -> None:
        try:
            source_after = self.snapshot(
                rollback_relative,
                allow_missing=True,
            )
            destination_after = self.snapshot(
                relative,
                allow_missing=True,
            )
            if (
                self._same_windows_object(source_after, source_identity)
                and not destination_after.exists
            ):
                return
            if (
                source_after.exists
                or not self._same_windows_object(
                    destination_after,
                    source_identity,
                )
            ):
                raise ProjectPathSafetyError(
                    relative,
                    "path-identity-changed",
                )
            try:
                self._rename_by_handle(
                    source_handle,
                    rollback_destination,
                    rollback_relative,
                    replace_existing=False,
                )
            except BaseException as rollback_error:
                if (
                    self._same_windows_object(
                        self.snapshot(
                            rollback_relative,
                            allow_missing=False,
                        ),
                        source_identity,
                    )
                    and not self.snapshot(
                        relative,
                        allow_missing=True,
                    ).exists
                ):
                    return
                raise rollback_error
            if (
                not self._same_windows_object(
                    self.snapshot(
                        rollback_relative,
                        allow_missing=False,
                    ),
                    source_identity,
                )
                or self.snapshot(relative, allow_missing=True).exists
            ):
                raise ProjectPathSafetyError(
                    relative,
                    "path-identity-changed",
                )
        except BaseException as rollback_error:
            error.add_note(
                f"Windows handle rename interruption rollback failed: {rollback_error}"
            )

    def _rename_by_handle(
        self,
        source_handle: int,
        destination: Path,
        relative: Path,
        *,
        replace_existing: bool,
        rollback_destination: Path | None = None,
        rollback_relative: Path | None = None,
        source_identity: _PathIdentity | None = None,
    ) -> None:
        encoded_name = os.fspath(destination).encode("utf-16-le")
        file_name_offset = _FILE_RENAME_INFO.file_name.offset
        # FILE_RENAME_INFO needs the complete declared structure plus the
        # variable UTF-16 filename bytes.  The former buffer stopped at the
        # flexible-array offset, four bytes short of sizeof(FILE_RENAME_INFO)
        # on x64.  Keep an additional zeroed UTF-16 code unit defensively;
        # FileNameLength still excludes it.
        buffer_size = (
            ctypes.sizeof(_FILE_RENAME_INFO) + len(encoded_name) + 2
        )
        buffer = ctypes.create_string_buffer(
            buffer_size
        )
        rename_info = ctypes.cast(
            buffer,
            ctypes.POINTER(_FILE_RENAME_INFO),
        ).contents
        rename_info.flags = (
            _FILE_RENAME_REPLACE_IF_EXISTS
            | _FILE_RENAME_POSIX_SEMANTICS
            | _FILE_RENAME_IGNORE_READONLY_ATTRIBUTE
            if replace_existing
            else 0
        )
        rename_info.root_directory = None
        rename_info.file_name_length = len(encoded_name)
        ctypes.memmove(
            ctypes.addressof(buffer) + file_name_offset,
            encoded_name,
            len(encoded_name),
        )
        try:
            renamed = self.api.SetFileInformationByHandle(
                source_handle,
                _FILE_RENAME_INFO_EX_CLASS,
                buffer,
                len(buffer),
            )
        except BaseException as exc:
            if (
                rollback_destination is not None
                and rollback_relative is not None
                and source_identity is not None
            ):
                self._reconcile_windows_handle_rename_interruption(
                    source_handle,
                    source_identity,
                    destination,
                    relative,
                    rollback_destination,
                    rollback_relative,
                    exc,
                )
            raise
        if not renamed:
            error = self.api.error()
            failure = ProjectPathSafetyError(
                relative,
                _windows_rename_error_reason(error),
            )
            if (
                rollback_destination is not None
                and rollback_relative is not None
                and source_identity is not None
            ):
                self._reconcile_windows_handle_rename_interruption(
                    source_handle,
                    source_identity,
                    destination,
                    relative,
                    rollback_destination,
                    rollback_relative,
                    failure,
                )
            raise failure from error

    def _publish_handle_rename_checked(
        self,
        source_handle: int,
        source_path: Path,
        source: Path,
        source_identity: _PathIdentity,
        destination_path: Path,
        destination: Path,
        *,
        flush: bool,
    ) -> None:
        """Publish a missing-target rename and restore its exact pre-state on failure."""

        try:
            self._rename_by_handle(
                source_handle,
                destination_path,
                destination,
                replace_existing=False,
                rollback_destination=source_path,
                rollback_relative=source,
                source_identity=source_identity,
            )
            if self._identity(
                source_handle,
                expect_directory=False,
                relative=destination,
            ) != source_identity:
                raise ProjectPathSafetyError(
                    destination,
                    "path-identity-changed",
                )
            if flush and not self.api.FlushFileBuffers(source_handle):
                raise ProjectPathSafetyError(
                    destination,
                    "path-identity-changed",
                ) from self.api.error()
        except BaseException as exc:
            self._reconcile_windows_handle_rename_interruption(
                source_handle,
                source_identity,
                destination_path,
                destination,
                source_path,
                source,
                exc,
            )
            raise

    @staticmethod
    def _same_windows_object(
        snapshot: _PathSnapshot,
        identity: _PathIdentity,
    ) -> bool:
        if not snapshot.exists or snapshot.identity is None:
            return False
        actual = snapshot.identity
        return (
            actual.volume == identity.volume
            and actual.file_id == identity.file_id
            and actual.kind == identity.kind
            and actual.nlink == identity.nlink
        )

    @staticmethod
    def _same_windows_raw_object(
        actual: _PathSnapshot,
        expected: _PathSnapshot,
    ) -> bool:
        if actual.exists != expected.exists:
            return False
        if not actual.exists:
            return True
        if actual.identity is None or expected.identity is None:
            return False
        return (
            actual.identity.volume == expected.identity.volume
            and actual.identity.file_id == expected.identity.file_id
            and actual.identity.kind == expected.identity.kind
        )

    def _apply_file_attributes(
        self,
        handle: int,
        relative: Path,
        attributes: int,
    ) -> None:
        normalized = int(attributes) & ~_FILE_ATTRIBUTE_NORMAL
        if normalized == 0:
            normalized = _FILE_ATTRIBUTE_NORMAL
        information = _FILE_BASIC_INFO(0, 0, 0, 0, normalized)
        if not self.api.SetFileInformationByHandle(
            handle,
            _FILE_BASIC_INFO_CLASS,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            raise ProjectPathSafetyError(
                relative,
                "platform-safety-unavailable",
            ) from self.api.error()

    def _restore_known_file_attributes(
        self,
        path: Path,
        relative: Path,
        expected: _PathIdentity,
    ) -> None:
        try:
            handle = self._open_handle(
                path,
                directory=False,
                access=_FILE_WRITE_ATTRIBUTES,
            )
        except OSError as exc:
            raise ProjectPathSafetyError(
                relative,
                "path-identity-changed",
            ) from exc
        try:
            current = self._identity(
                handle,
                expect_directory=False,
                relative=relative,
            )
            if not self._same_windows_object(
                _PathSnapshot(True, current),
                expected,
            ):
                raise ProjectPathSafetyError(
                    relative,
                    "path-identity-changed",
                )
            self._apply_file_attributes(
                handle,
                relative,
                expected.mode_or_attributes,
            )
            restored = self._identity(
                handle,
                expect_directory=False,
                relative=relative,
            )
            restored_snapshot = _PathSnapshot(True, restored)
            if (
                restored != expected
                or self._recheck_snapshot(relative, restored_snapshot)
                != restored_snapshot
            ):
                raise ProjectPathSafetyError(
                    relative,
                    "path-identity-changed",
                )
        finally:
            self._close(handle)

    def _annotate_windows_replace_uncertainty(
        self,
        source_path: Path,
        source: Path,
        source_identity: _PathIdentity,
        error: BaseException,
    ) -> None:
        try:
            self._restore_known_file_attributes(
                source_path,
                source,
                source_identity,
            )
        except BaseException as source_restore_error:
            error.add_note(
                "Windows failed replacement source attribute restore failed: "
                f"{source_restore_error}"
            )
        if _WINDOWS_REPLACE_METADATA_UNVERIFIED not in getattr(
            error,
            "__notes__",
            (),
        ):
            error.add_note(_WINDOWS_REPLACE_METADATA_UNVERIFIED)

    def _reconcile_windows_replace_interruption(
        self,
        source_path: Path,
        source: Path,
        source_identity: _PathIdentity,
        destination_path: Path,
        destination: Path,
        destination_snapshot: _PathSnapshot,
        backup_path: Path,
        backup_relative: Path,
        error: BaseException,
    ) -> None:
        source_snapshot = _PathSnapshot(True, source_identity)
        try:
            final = self._raw_snapshot(destination, allow_missing=True)
            displaced = self._raw_snapshot(
                backup_relative,
                allow_missing=True,
            )
            source_after = self._raw_snapshot(source, allow_missing=True)
            if (
                self._same_windows_raw_object(final, destination_snapshot)
                and self._same_windows_raw_object(
                    source_after,
                    source_snapshot,
                )
                and not displaced.exists
            ):
                return
            if (
                final.exists
                and not source_after.exists
                and displaced.exists
            ):
                try:
                    if not self.api.ReplaceFileW(
                        os.fspath(destination_path),
                        os.fspath(backup_path),
                        os.fspath(source_path),
                        _REPLACEFILE_FLAGS_NONE,
                        None,
                        None,
                    ):
                        raise ProjectPathSafetyError(
                            destination,
                            "path-identity-changed",
                        ) from self.api.error()
                except BaseException as rollback_error:
                    if (
                        self._same_windows_raw_object(
                            self._raw_snapshot(
                                destination,
                                allow_missing=False,
                            ),
                            displaced,
                        )
                        and self._same_windows_raw_object(
                            self._raw_snapshot(
                                source,
                                allow_missing=False,
                            ),
                            final,
                        )
                        and not self._raw_snapshot(
                            backup_relative,
                            allow_missing=True,
                        ).exists
                    ):
                        return
                    raise rollback_error
                if (
                    not self._same_windows_raw_object(
                        self._raw_snapshot(
                            destination,
                            allow_missing=False,
                        ),
                        displaced,
                    )
                    or not self._same_windows_raw_object(
                        self._raw_snapshot(
                            source,
                            allow_missing=False,
                        ),
                        final,
                    )
                    or self._raw_snapshot(
                        backup_relative,
                        allow_missing=True,
                    ).exists
                ):
                    raise ProjectPathSafetyError(
                        destination,
                        "path-identity-changed",
                    )
                return
            if (
                not final.exists
                and self._same_windows_raw_object(
                    source_after,
                    source_snapshot,
                )
                and displaced.exists
            ):
                try:
                    if displaced == destination_snapshot:
                        self.replace_file(
                            backup_relative,
                            destination,
                            expected_destination=final,
                        )
                    else:
                        raw_handle = self._open_handle(
                            backup_path,
                            directory=True,
                            access=_DELETE,
                        )
                        try:
                            if not self._same_windows_raw_object(
                                _PathSnapshot(
                                    True,
                                    self._raw_identity(
                                        raw_handle,
                                        relative=backup_relative,
                                    ),
                                ),
                                displaced,
                            ):
                                raise ProjectPathSafetyError(
                                    destination,
                                    "path-identity-changed",
                                )
                            self._rename_by_handle(
                                raw_handle,
                                destination_path,
                                destination,
                                replace_existing=False,
                            )
                        finally:
                            self._close(raw_handle)
                except BaseException as rollback_error:
                    if (
                        self._same_windows_raw_object(
                            self._raw_snapshot(
                                destination,
                                allow_missing=False,
                            ),
                            displaced,
                        )
                        and self._same_windows_raw_object(
                            self._raw_snapshot(
                                source,
                                allow_missing=False,
                            ),
                            source_snapshot,
                        )
                        and not self._raw_snapshot(
                            backup_relative,
                            allow_missing=True,
                        ).exists
                    ):
                        return
                    raise rollback_error
                if (
                    not self._same_windows_raw_object(
                        self._raw_snapshot(
                            destination,
                            allow_missing=False,
                        ),
                        displaced,
                    )
                    or not self._same_windows_raw_object(
                        self._raw_snapshot(
                            source,
                            allow_missing=False,
                        ),
                        source_snapshot,
                    )
                    or self._raw_snapshot(
                        backup_relative,
                        allow_missing=True,
                    ).exists
                ):
                    raise ProjectPathSafetyError(
                        destination,
                        "path-identity-changed",
                    )
                return
            raise ProjectPathSafetyError(
                destination,
                "path-identity-changed",
            )
        except BaseException as rollback_error:
            error.add_note(
                f"Windows replacement interruption rollback failed: {rollback_error}"
            )

    def _replace_with_backup_checked(
        self,
        source_path: Path,
        source: Path,
        source_identity: _PathIdentity,
        destination_path: Path,
        destination: Path,
        destination_snapshot: _PathSnapshot,
        destination_lease: _WritableDestinationLease | None = None,
    ) -> _PathIdentity:
        if destination_lease is None:
            destination_lease = _WritableDestinationLease(
                restore_required=False,
                working_snapshot=destination_snapshot,
            )
        backup_relative: Path | None = None
        backup_path: Path | None = None
        for _ in range(128):
            candidate = destination.with_name(
                f".{destination.name}.kafa-displaced-{secrets.token_hex(12)}.tmp"
            )
            if self.snapshot(candidate, allow_missing=True).exists:
                continue
            backup_relative = candidate
            backup_path = destination_path.with_name(candidate.name)
            break
        if backup_relative is None or backup_path is None:
            raise ProjectPathSafetyError(
                destination,
                "path-identity-changed",
            )
        replace_error: OSError | None = None
        try:
            replaced = self.api.ReplaceFileW(
                os.fspath(destination_path),
                os.fspath(source_path),
                os.fspath(backup_path),
                _REPLACEFILE_FLAGS_NONE,
                None,
                None,
            )
        except BaseException as exc:
            self._reconcile_windows_replace_interruption(
                source_path,
                source,
                source_identity,
                destination_path,
                destination,
                destination_snapshot,
                backup_path,
                backup_relative,
                exc,
            )
            self._annotate_windows_replace_uncertainty(
                source_path,
                source,
                source_identity,
                exc,
            )
            raise
        if not replaced:
            # ReplaceFileW documents a partial failure (1177) where the old
            # destination has already moved to the backup name and the
            # replacement remains at its source name.  Capture GetLastError
            # before any state inspection calls overwrite it.
            try:
                replace_error = self.api.error()
            except BaseException as exc:
                self._reconcile_windows_replace_interruption(
                    source_path,
                    source,
                    source_identity,
                    destination_path,
                    destination,
                    destination_snapshot,
                    backup_path,
                    backup_relative,
                    exc,
                )
                self._annotate_windows_replace_uncertainty(
                    source_path,
                    source,
                    source_identity,
                    exc,
                )
                raise
        try:
            final = self._raw_snapshot(destination, allow_missing=True)
            displaced = self._raw_snapshot(
                backup_relative,
                allow_missing=True,
            )
            source_after = self._raw_snapshot(source, allow_missing=True)
        except BaseException as exc:
            error: BaseException = exc
            if isinstance(exc, Exception):
                error = ProjectPathSafetyError(
                    destination,
                    "path-identity-changed",
                )
            self._reconcile_windows_replace_interruption(
                source_path,
                source,
                source_identity,
                destination_path,
                destination,
                destination_snapshot,
                backup_path,
                backup_relative,
                error,
            )
            self._annotate_windows_replace_uncertainty(
                source_path,
                source,
                source_identity,
                error,
            )
            if error is exc:
                raise
            raise error from exc
        correct = (
            self._same_windows_object(final, source_identity)
            and displaced == destination_snapshot
            and not source_after.exists
        )
        original = (
            final == destination_snapshot
            and self._same_windows_object(source_after, source_identity)
            and not displaced.exists
        )
        partial_failure = (
            not final.exists
            and displaced == destination_snapshot
            and self._same_windows_object(source_after, source_identity)
        )
        if replace_error is not None and partial_failure:
            try:
                self.replace_file(
                    source,
                    destination,
                    expected_destination=final,
                )
            except BaseException as completion_error:
                error: BaseException = completion_error
                if isinstance(completion_error, Exception):
                    error = ProjectPathSafetyError(
                        destination,
                        "path-identity-changed",
                    )
                error.add_note(f"ReplaceFileW failed: {replace_error}")
                error.add_note(
                    f"Windows partial replacement completion failed: {completion_error}"
                )
                self._reconcile_windows_replace_interruption(
                    source_path,
                    source,
                    source_identity,
                    destination_path,
                    destination,
                    destination_snapshot,
                    backup_path,
                    backup_relative,
                    error,
                )
                self._annotate_windows_replace_uncertainty(
                    source_path,
                    source,
                    source_identity,
                    error,
                )
                if error is completion_error:
                    raise
                raise error from replace_error
            try:
                final = self._raw_snapshot(destination, allow_missing=True)
                displaced = self._raw_snapshot(
                    backup_relative,
                    allow_missing=True,
                )
                source_after = self._raw_snapshot(source, allow_missing=True)
            except BaseException as exc:
                error = exc
                if isinstance(exc, Exception):
                    error = ProjectPathSafetyError(
                        destination,
                        "path-identity-changed",
                    )
                self._reconcile_windows_replace_interruption(
                    source_path,
                    source,
                    source_identity,
                    destination_path,
                    destination,
                    destination_snapshot,
                    backup_path,
                    backup_relative,
                    error,
                )
                self._annotate_windows_replace_uncertainty(
                    source_path,
                    source,
                    source_identity,
                    error,
                )
                if error is exc:
                    raise
                raise error from exc
            correct = (
                self._same_windows_object(final, source_identity)
                and displaced == destination_snapshot
                and not source_after.exists
            )
        elif replace_error is not None and original:
            error = ProjectPathSafetyError(
                destination,
                _windows_rename_error_reason(replace_error),
            )
            error.add_note(f"ReplaceFileW failed: {replace_error}")
            self._annotate_windows_replace_uncertainty(
                source_path,
                source,
                source_identity,
                error,
            )
            raise error from replace_error
        if not correct:
            error = ProjectPathSafetyError(
                destination,
                "path-identity-changed",
            )
            if replace_error is not None:
                error.add_note(f"ReplaceFileW failed: {replace_error}")
            self._reconcile_windows_replace_interruption(
                source_path,
                source,
                source_identity,
                destination_path,
                destination,
                destination_snapshot,
                backup_path,
                backup_relative,
                error,
            )
            self._annotate_windows_replace_uncertainty(
                source_path,
                source,
                source_identity,
                error,
            )
            if replace_error is not None:
                raise error from replace_error
            raise error

        final_handle = _INVALID_HANDLE_VALUE
        final_identity = source_identity
        try:
            try:
                try:
                    final_handle = self._open_handle(
                        destination_path,
                        directory=False,
                        access=_GENERIC_READ | _GENERIC_WRITE,
                        exclusive_share=True,
                    )
                except OSError as exc:
                    raise ProjectPathSafetyError(
                        destination,
                        "path-identity-changed",
                    ) from exc
                final_identity = self._identity(
                    final_handle,
                    expect_directory=False,
                    relative=destination,
                )
                if not self._same_windows_object(
                    _PathSnapshot(True, final_identity),
                    source_identity,
                ):
                    raise ProjectPathSafetyError(
                        destination,
                        "path-identity-changed",
                    )
                self._apply_file_attributes(
                    final_handle,
                    destination,
                    source_identity.mode_or_attributes,
                )
                if not self.api.FlushFileBuffers(final_handle):
                    raise ProjectPathSafetyError(
                        destination,
                        "path-identity-changed",
                    ) from self.api.error()
                final_identity = self._identity(
                    final_handle,
                    expect_directory=False,
                    relative=destination,
                )
                _before_windows_backup_cleanup(
                    self,
                    destination,
                    backup_relative,
                )
                if self._identity(
                    final_handle,
                    expect_directory=False,
                    relative=destination,
                ) != final_identity:
                    raise ProjectPathSafetyError(
                        destination,
                        "path-identity-changed",
                    )
                self.unlink_regular(
                    backup_relative,
                    missing_ok=False,
                    expected=displaced,
                )
                if self._identity(
                    final_handle,
                    expect_directory=False,
                    relative=destination,
                ) != final_identity:
                    raise ProjectPathSafetyError(
                        destination,
                        "path-identity-changed",
                    )
                destination_lease.discarded = True
            finally:
                self._close(final_handle)
                final_handle = _INVALID_HANDLE_VALUE
        except BaseException as exc:
            self._reconcile_windows_replace_interruption(
                source_path,
                source,
                source_identity,
                destination_path,
                destination,
                destination_snapshot,
                backup_path,
                backup_relative,
                exc,
            )
            self._annotate_windows_replace_uncertainty(
                source_path,
                source,
                source_identity,
                exc,
            )
            raise
        return final_identity

    @contextmanager
    def _ancestors(
        self,
        relative: Path,
        *,
        create: bool,
        allow_missing: bool = False,
        with_handle: bool = False,
    ) -> Iterator[Path | tuple[Path, int]]:
        handles: list[int] = []
        current = self.root
        try:
            root_handle = self._open_handle(current, directory=True)
            handles.append(root_handle)
            if self._identity(root_handle, expect_directory=True, relative=relative) != self.root_identity:
                raise ProjectPathSafetyError(relative, "path-identity-changed")
            for component in relative.parts[:-1]:
                current = current / component
                created_identity: _PathIdentity | None = None
                try:
                    handle = self._open_handle(current, directory=True)
                except OSError as exc:
                    missing = (
                        isinstance(exc, FileNotFoundError)
                        or exc.errno in {2, 3}
                        or getattr(exc, "winerror", None) in {2, 3}
                    )
                    if not create and allow_missing and missing:
                        raise _MissingAncestor from exc
                    if not create or not missing:
                        raise ProjectPathSafetyError(relative, "unsafe-ancestor") from exc
                    try:
                        handle, created_identity = self._create_directory_at(
                            handles[-1],
                            component,
                            relative,
                        )
                    except FileExistsError:
                        try:
                            handle = self._open_handle(current, directory=True)
                        except OSError as retry_exc:
                            raise ProjectPathSafetyError(
                                relative,
                                "unsafe-ancestor",
                            ) from retry_exc
                try:
                    identity = created_identity or self._identity(
                        handle,
                        expect_directory=True,
                        relative=relative,
                    )
                except ProjectPathSafetyError as exc:
                    self._close(handle)
                    if exc.reason == "unsafe-target":
                        raise ProjectPathSafetyError(
                            relative,
                            "unsafe-ancestor",
                        ) from exc
                    raise
                if identity.volume != self.root_identity.volume:
                    self._close(handle)
                    raise ProjectPathSafetyError(relative, "cross-device-ancestor")
                handles.append(handle)
            if with_handle:
                yield current, handles[-1]
            else:
                yield current
        finally:
            for handle in reversed(handles):
                self._close(handle)

    def snapshot(
        self,
        relative: Path,
        *,
        allow_missing: bool,
        expect_directory: bool = False,
    ) -> _PathSnapshot:
        try:
            with self._ancestors(
                relative,
                create=False,
                allow_missing=allow_missing,
            ) as parent:
                path = parent / relative.name
                try:
                    handle = self._open_handle(path, directory=expect_directory)
                except OSError as exc:
                    if allow_missing and exc.errno in {2, 3}:
                        return _PathSnapshot(False)
                    raise ProjectPathSafetyError(relative, "unsafe-target") from exc
                try:
                    identity = self._identity(
                        handle,
                        expect_directory=expect_directory,
                        relative=relative,
                    )
                finally:
                    self._close(handle)
                if identity.volume != self.root_identity.volume:
                    raise ProjectPathSafetyError(relative, "cross-device-ancestor")
                return _PathSnapshot(True, identity)
        except _MissingAncestor:
            return _PathSnapshot(False)
        except ProjectPathSafetyError:
            raise
        except OSError as exc:
            if allow_missing and exc.errno in {2, 3}:
                return _PathSnapshot(False)
            raise ProjectPathSafetyError(relative, "unsafe-ancestor") from exc

    def _recheck_snapshot(
        self,
        relative: Path,
        expected: _PathSnapshot,
        *,
        expect_directory: bool = False,
    ) -> _PathSnapshot:
        try:
            return self.snapshot(
                relative,
                allow_missing=not expected.exists,
                expect_directory=expect_directory,
            )
        except ProjectPathSafetyError as exc:
            if exc.reason in {
                "unsafe-ancestor",
                "unsafe-target",
                "hard-linked-target",
                "cross-device-ancestor",
            }:
                raise ProjectPathSafetyError(
                    relative,
                    "path-identity-changed",
                ) from exc
            raise

    def _raw_snapshot(
        self,
        relative: Path,
        *,
        allow_missing: bool,
    ) -> _PathSnapshot:
        """Capture a no-follow leaf identity solely for rename recovery.

        The returned identity never authorizes content access or deletion.  It
        deliberately permits hard links and reparse points so an interrupted
        ReplaceFileW can identify and reverse the exact directory-entry move.
        """

        try:
            with self._ancestors(
                relative,
                create=False,
                allow_missing=allow_missing,
            ) as parent:
                path = parent / relative.name
                try:
                    # BACKUP_SEMANTICS permits opening a directory if an
                    # attacker raced one into the leaf, while OPEN_REPARSE_POINT
                    # in _open_handle keeps the operation no-follow.
                    handle = self._open_handle(path, directory=True)
                except OSError as exc:
                    if allow_missing and exc.errno in {2, 3}:
                        return _PathSnapshot(False)
                    raise ProjectPathSafetyError(
                        relative,
                        "path-identity-changed",
                    ) from exc
                try:
                    identity = self._raw_identity(handle, relative=relative)
                finally:
                    self._close(handle)
                return _PathSnapshot(True, identity)
        except _MissingAncestor:
            return _PathSnapshot(False)
        except ProjectPathSafetyError:
            raise
        except OSError as exc:
            if allow_missing and exc.errno in {2, 3}:
                return _PathSnapshot(False)
            raise ProjectPathSafetyError(
                relative,
                "path-identity-changed",
            ) from exc

    def ensure_directory(self, relative: Path) -> None:
        marker = relative / "__kafa_directory_marker__"
        try:
            with self._ancestors(marker, create=True):
                return
        except ProjectPathSafetyError as exc:
            if exc.relative == marker:
                raise ProjectPathSafetyError(relative, exc.reason) from exc
            raise

    def audit(
        self,
        relative: Path,
        *,
        allow_missing: bool,
        expect_directory: bool = False,
    ) -> None:
        self.snapshot(
            relative,
            allow_missing=allow_missing,
            expect_directory=expect_directory,
        )

    def read_bytes(
        self,
        relative: Path,
        *,
        max_bytes: int | None,
        expected: _PathSnapshot | None = None,
    ) -> bytes:
        with self._ancestors(relative, create=False) as parent:
            path = parent / relative.name
            try:
                handle = self._open_handle(
                    path,
                    directory=False,
                    access=_GENERIC_READ,
                )
            except OSError as exc:
                raise ProjectPathSafetyError(relative, "unsafe-target") from exc
            try:
                identity = self._identity(handle, expect_directory=False, relative=relative)
                if expected is not None and identity != expected.identity:
                    raise ProjectPathSafetyError(
                        relative,
                        "path-identity-changed",
                    )
                descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY)
                handle = _INVALID_HANDLE_VALUE
                try:
                    chunks: list[bytes] = []
                    remaining = max_bytes
                    while remaining is None or remaining > 0:
                        size = 65536 if remaining is None else min(65536, remaining)
                        chunk = os.read(descriptor, size)
                        if not chunk:
                            break
                        chunks.append(chunk)
                        if remaining is not None:
                            remaining -= len(chunk)
                    data = b"".join(chunks)
                finally:
                    os.close(descriptor)
            finally:
                self._close(handle)
            if self.snapshot(relative, allow_missing=False).identity != identity:
                raise ProjectPathSafetyError(relative, "path-identity-changed")
            return data

    def sync_regular(
        self,
        relative: Path,
        expected: _PathSnapshot | None = None,
    ) -> _PathSnapshot:
        snapshot = expected or self.snapshot(relative, allow_missing=False)
        with self._ancestors(relative, create=False) as parent:
            path = parent / relative.name
            try:
                handle = self._open_handle(
                    path,
                    directory=False,
                    access=_GENERIC_READ | _GENERIC_WRITE,
                )
            except OSError as exc:
                raise ProjectPathSafetyError(
                    relative,
                    "path-identity-changed",
                ) from exc
            try:
                if self._identity(
                    handle,
                    expect_directory=False,
                    relative=relative,
                ) != snapshot.identity:
                    raise ProjectPathSafetyError(
                        relative,
                        "path-identity-changed",
                    )
                if not self.api.FlushFileBuffers(handle):
                    raise ProjectPathSafetyError(
                        relative,
                        "path-identity-changed",
                    ) from self.api.error()
                if self._identity(
                    handle,
                    expect_directory=False,
                    relative=relative,
                ) != snapshot.identity:
                    raise ProjectPathSafetyError(
                        relative,
                        "path-identity-changed",
                    )
            finally:
                self._close(handle)
        if self.snapshot(relative, allow_missing=False) != snapshot:
            raise ProjectPathSafetyError(relative, "path-identity-changed")
        return snapshot

    def _write_handle(self, handle: int, data: bytes, relative: Path) -> None:
        view = memoryview(data)
        written_total = 0
        while written_total < len(view):
            chunk = bytes(view[written_total : written_total + 1024 * 1024])
            buffer = ctypes.create_string_buffer(chunk, len(chunk))
            written = wintypes.DWORD()
            if not self.api.WriteFile(
                handle,
                buffer,
                len(chunk),
                ctypes.byref(written),
                None,
            ):
                raise ProjectPathSafetyError(
                    relative,
                    "path-identity-changed",
                ) from self.api.error()
            if int(written.value) <= 0:
                raise ProjectPathSafetyError(
                    relative,
                    "path-identity-changed",
                )
            written_total += int(written.value)
            _after_windows_write_chunk(
                self,
                relative,
                written_total,
            )
        if not self.api.FlushFileBuffers(handle):
            raise ProjectPathSafetyError(
                relative,
                "path-identity-changed",
            ) from self.api.error()

    def _cleanup_created_handle(
        self,
        handle: int,
        path: Path,
        relative: Path,
        identity: _PathIdentity | None,
    ) -> str:
        errors: list[str] = []
        try:
            self._delete_on_close(handle, relative)
        except BaseException as exc:
            errors.append(f"delete-on-close failed: {exc}")
        finally:
            self._close(handle)

        try:
            remaining = self.snapshot(relative, allow_missing=True)
        except BaseException as exc:
            errors.append(f"cleanup verification failed: {exc}")
            return "; ".join(errors)
        if not remaining.exists:
            return "; ".join(errors)
        if identity is not None and remaining.identity != identity:
            errors.append("cleanup target identity changed")
            return "; ".join(errors)

        retry_handle = _INVALID_HANDLE_VALUE
        try:
            retry_handle = self._open_handle(
                path,
                directory=False,
                access=_DELETE,
            )
            if (
                identity is not None
                and self._identity(
                    retry_handle,
                    expect_directory=False,
                    relative=relative,
                )
                != identity
            ):
                raise ProjectPathSafetyError(
                    relative,
                    "path-identity-changed",
                )
            self._delete_on_close(retry_handle, relative)
        except BaseException as exc:
            errors.append(f"fallback cleanup failed: {exc}")
        finally:
            self._close(retry_handle)
        try:
            if self.snapshot(relative, allow_missing=True).exists:
                errors.append("partial create remains after cleanup")
        except BaseException as exc:
            errors.append(f"final cleanup verification failed: {exc}")
        return "; ".join(errors)

    def _create_file_handle(
        self,
        path: Path,
        relative: Path,
        data: bytes,
        mode: int,
    ) -> tuple[int, _PathIdentity]:
        try:
            handle = self._open_handle(
                path,
                directory=False,
                access=_GENERIC_READ | _GENERIC_WRITE | _DELETE,
                disposition=_CREATE_NEW,
            )
        except OSError:
            raise
        identity: _PathIdentity | None = None
        try:
            identity = self._identity(
                handle,
                expect_directory=False,
                relative=relative,
            )
            self._write_handle(handle, data, relative)
            self._apply_file_mode(handle, relative, mode)
            identity = self._identity(
                handle,
                expect_directory=False,
                relative=relative,
            )
        except BaseException as exc:
            cleanup_error = self._cleanup_created_handle(
                handle,
                path,
                relative,
                identity,
            )
            handle = _INVALID_HANDLE_VALUE
            if cleanup_error:
                exc.add_note(cleanup_error)
            raise
        assert identity is not None
        return handle, identity

    def _create_file(
        self,
        path: Path,
        relative: Path,
        data: bytes,
        mode: int,
    ) -> _PathIdentity:
        handle, identity = self._create_file_handle(
            path,
            relative,
            data,
            mode,
        )
        self._close(handle)
        return identity

    def _pin_destination(
        self,
        path: Path,
        relative: Path,
        expected: _PathSnapshot,
    ) -> tuple[int, _PathIdentity | None]:
        if expected.exists:
            assert expected.identity is not None
            access = (
                _FILE_WRITE_ATTRIBUTES
                if expected.identity.mode_or_attributes
                & _FILE_ATTRIBUTE_READONLY
                else 0
            )
            handle = self._open_handle(
                path,
                directory=False,
                access=access,
                share_delete=True,
            )
            try:
                identity = self._identity(
                    handle,
                    expect_directory=False,
                    relative=relative,
                )
            except BaseException:
                self._close(handle)
                raise
            if identity != expected.identity:
                self._close(handle)
                raise ProjectPathSafetyError(
                    relative,
                    "path-identity-changed",
                )
            return handle, identity
        # Do not create a placeholder for a missing destination.  A crash in
        # that window would expose an empty canonical file instead of keeping
        # the old absent state.  The parent and exact source remain pinned; a
        # concurrently created leaf is replaced as a directory entry and is
        # never followed.
        return _INVALID_HANDLE_VALUE, None

    @contextmanager
    def _temporarily_writable_destination(
        self,
        handle: int,
        relative: Path,
        expected: _PathSnapshot,
    ) -> Iterator[_WritableDestinationLease]:
        assert expected.exists and expected.identity is not None
        original = expected.identity
        needs_restore = bool(
            original.mode_or_attributes & _FILE_ATTRIBUTE_READONLY
        )
        lease = _WritableDestinationLease(
            restore_required=needs_restore,
            working_snapshot=expected,
        )
        restore_allowed = False
        primary_error: BaseException | None = None
        try:
            current = self._identity(
                handle,
                expect_directory=False,
                relative=relative,
            )
            if current != original:
                raise ProjectPathSafetyError(
                    relative,
                    "path-identity-changed",
                )
            if needs_restore:
                restore_allowed = True
                writable_attributes = (
                    original.mode_or_attributes & ~_FILE_ATTRIBUTE_READONLY
                )
                self._apply_file_attributes(
                    handle,
                    relative,
                    writable_attributes,
                )
                writable = self._identity(
                    handle,
                    expect_directory=False,
                    relative=relative,
                )
                normalized_writable = (
                    writable_attributes & ~_FILE_ATTRIBUTE_NORMAL
                ) or _FILE_ATTRIBUTE_NORMAL
                if (
                    not self._same_windows_object(
                        _PathSnapshot(True, writable),
                        original,
                    )
                    or writable.mode_or_attributes != normalized_writable
                    or self._recheck_snapshot(relative, _PathSnapshot(True, writable))
                    != _PathSnapshot(True, writable)
                ):
                    raise ProjectPathSafetyError(
                        relative,
                        "path-identity-changed",
                    )
                lease.working_snapshot = _PathSnapshot(True, writable)
            yield lease
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            if restore_allowed and not lease.discarded:
                try:
                    current_raw = self._raw_identity(handle, relative=relative)
                    if not self._same_windows_raw_object(
                        _PathSnapshot(True, current_raw),
                        expected,
                    ):
                        raise ProjectPathSafetyError(
                            relative,
                            "path-identity-changed",
                        )
                    if current_raw.nlink == 0:
                        lease.discarded = True
                    else:
                        self._apply_file_attributes(
                            handle,
                            relative,
                            original.mode_or_attributes,
                        )
                        restored = self._raw_identity(handle, relative=relative)
                        if restored != original:
                            raise ProjectPathSafetyError(
                                relative,
                                "path-identity-changed",
                            )
                except BaseException as restore_error:
                    if primary_error is None:
                        raise
                    primary_error.add_note(
                        "Windows readonly destination restore failed: "
                        f"{restore_error}"
                    )

    def atomic_write(
        self,
        fs: "ProjectFS",
        relative: Path,
        data: bytes,
        mode: int,
        expected_destination: _PathSnapshot | None = None,
    ) -> _PathSnapshot:
        with self._ancestors(relative, create=True) as parent:
            target = parent / relative.name
            expected = self.snapshot(relative, allow_missing=True)
            if (
                expected_destination is not None
                and expected != expected_destination
            ):
                raise ProjectPathSafetyError(
                    relative,
                    "path-identity-changed",
                )
            temporary_relative = relative.with_name(
                f".{relative.name}.kafa-{secrets.token_hex(12)}.tmp"
            )
            temporary = parent / temporary_relative.name
            temporary_handle, temporary_identity = self._create_file_handle(
                temporary,
                temporary_relative,
                data,
                mode,
            )
            destination_handle = _INVALID_HANDLE_VALUE
            destination_identity: _PathIdentity | None = None
            published = False
            try:
                _before_atomic_replace(fs, relative)
                if self._recheck_snapshot(relative, expected) != expected:
                    raise ProjectPathSafetyError(relative, "path-identity-changed")
                destination_handle, destination_identity = self._pin_destination(
                    target,
                    relative,
                    expected,
                )
                _before_windows_handle_rename(
                    self,
                    temporary_relative,
                    relative,
                )
                if self._identity(
                    temporary_handle,
                    expect_directory=False,
                    relative=temporary_relative,
                ) != temporary_identity:
                    raise ProjectPathSafetyError(
                        temporary_relative,
                        "path-identity-changed",
                    )
                if expected.exists:
                    assert destination_identity is not None
                    if self._identity(
                        destination_handle,
                        expect_directory=False,
                        relative=relative,
                    ) != destination_identity:
                        raise ProjectPathSafetyError(
                            relative,
                            "path-identity-changed",
                        )
                    if self._recheck_snapshot(relative, expected) != expected:
                        raise ProjectPathSafetyError(
                            relative,
                            "path-identity-changed",
                        )
                    with self._temporarily_writable_destination(
                        destination_handle,
                        relative,
                        expected,
                    ) as destination_lease:
                        self._close(temporary_handle)
                        temporary_handle = _INVALID_HANDLE_VALUE
                        self._replace_with_backup_checked(
                            temporary,
                            temporary_relative,
                            temporary_identity,
                            target,
                            relative,
                            destination_lease.working_snapshot,
                            destination_lease,
                        )
                    published = True
                else:
                    if self._recheck_snapshot(relative, expected).exists:
                        raise ProjectPathSafetyError(
                            relative,
                            "path-identity-changed",
                        )
                    self._publish_handle_rename_checked(
                        temporary_handle,
                        temporary,
                        temporary_relative,
                        temporary_identity,
                        target,
                        relative,
                        flush=True,
                    )
                    published = True
            except BaseException as exc:
                if not published:
                    if temporary_handle != _INVALID_HANDLE_VALUE:
                        cleanup_error = self._cleanup_created_handle(
                            temporary_handle,
                            temporary,
                            temporary_relative,
                            temporary_identity,
                        )
                        temporary_handle = _INVALID_HANDLE_VALUE
                    else:
                        cleanup_error = ""
                        try:
                            remaining = self.snapshot(
                                temporary_relative,
                                allow_missing=True,
                            )
                            if remaining.exists:
                                if remaining.identity != temporary_identity:
                                    raise ProjectPathSafetyError(
                                        temporary_relative,
                                        "path-identity-changed",
                                    )
                                self.unlink_regular(
                                    temporary_relative,
                                    missing_ok=False,
                                    expected=remaining,
                                )
                        except BaseException as cleanup_exc:
                            cleanup_error = str(cleanup_exc)
                    if cleanup_error:
                        exc.add_note(cleanup_error)
                raise
            finally:
                self._close(destination_handle)
                self._close(temporary_handle)
            final_expected = _PathSnapshot(True, temporary_identity)
            if self._recheck_snapshot(relative, final_expected) != final_expected:
                raise ProjectPathSafetyError(relative, "path-identity-changed")
            return _PathSnapshot(True, temporary_identity)

    def create_exclusive(
        self,
        relative: Path,
        data: bytes,
        mode: int,
    ) -> _PathSnapshot:
        with self._ancestors(relative, create=True) as parent:
            target = parent / relative.name
            if self.snapshot(relative, allow_missing=True).exists:
                raise FileExistsError(os.fspath(target))
            try:
                expected = self._create_file(
                    target,
                    relative,
                    data,
                    mode,
                )
            except FileExistsError as exc:
                raise ProjectPathSafetyError(relative, "path-identity-changed") from exc
            if self.snapshot(relative, allow_missing=False).identity != expected:
                raise ProjectPathSafetyError(relative, "path-identity-changed")
            return _PathSnapshot(True, expected)

    def open_lock_fd(self, relative: Path, mode: int) -> int:
        with self._ancestors(relative, create=True) as parent:
            target = parent / relative.name
            try:
                handle = self._open_handle(
                    target,
                    directory=False,
                    access=_GENERIC_READ | _GENERIC_WRITE,
                    disposition=_OPEN_ALWAYS,
                )
            except OSError as exc:
                raise ProjectPathSafetyError(relative, "unsafe-target") from exc
            try:
                identity = self._identity(handle, expect_directory=False, relative=relative)
                if self.snapshot(relative, allow_missing=False).identity != identity:
                    raise ProjectPathSafetyError(relative, "path-identity-changed")
                self._apply_file_mode(handle, relative, mode)
                identity = self._identity(
                    handle,
                    expect_directory=False,
                    relative=relative,
                )
                if self.snapshot(relative, allow_missing=False).identity != identity:
                    raise ProjectPathSafetyError(relative, "path-identity-changed")
                descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR)
                handle = _INVALID_HANDLE_VALUE
                os.set_inheritable(descriptor, False)
                return descriptor
            finally:
                self._close(handle)

    def open_exclusion_fd(self) -> None:
        return None

    def unlink_regular(
        self,
        relative: Path,
        *,
        missing_ok: bool,
        expected: _PathSnapshot | None = None,
    ) -> None:
        snapshot = self.snapshot(relative, allow_missing=missing_ok)
        if expected is not None and snapshot != expected:
            raise ProjectPathSafetyError(relative, "path-identity-changed")
        if not snapshot.exists:
            return
        with self._ancestors(relative, create=False) as parent:
            target = parent / relative.name
            try:
                handle = self._open_handle(
                    target,
                    directory=False,
                    access=_DELETE,
                )
            except OSError as exc:
                raise ProjectPathSafetyError(
                    relative,
                    "path-identity-changed",
                ) from exc
            try:
                identity = self._identity(
                    handle,
                    expect_directory=False,
                    relative=relative,
                )
                if identity != snapshot.identity:
                    raise ProjectPathSafetyError(
                        relative,
                        "path-identity-changed",
                    )
                _before_windows_handle_delete(self, relative)
                if self._identity(
                    handle,
                    expect_directory=False,
                    relative=relative,
                ) != identity:
                    raise ProjectPathSafetyError(
                        relative,
                        "path-identity-changed",
                    )
                self._delete_on_close(handle, relative)
            finally:
                self._close(handle)
            if self.snapshot(relative, allow_missing=True).exists:
                raise ProjectPathSafetyError(
                    relative,
                    "path-identity-changed",
                )

    def create_unique_directory(self, parent_relative: Path, prefix: str) -> Path:
        marker = parent_relative / "__kafa_unique_directory_parent__"
        with self._ancestors(
            marker,
            create=True,
            with_handle=True,
        ) as parent_receipt:
            parent, parent_handle = parent_receipt
            for _ in range(128):
                relative = parent_relative / f"{prefix}{secrets.token_hex(8)}"
                _before_windows_directory_create(self, relative)
                try:
                    handle, identity = self._create_directory_at(
                        parent_handle,
                        relative.name,
                        relative,
                    )
                except FileExistsError:
                    continue
                expected = _PathSnapshot(True, identity)
                try:
                    if self._recheck_snapshot(
                        relative,
                        expected,
                        expect_directory=True,
                    ) != expected:
                        raise ProjectPathSafetyError(
                            relative,
                            "path-identity-changed",
                        )
                except BaseException as exc:
                    cleanup_error = self._cleanup_created_directory_handle(
                        handle,
                        relative,
                    )
                    if cleanup_error:
                        exc.add_note(cleanup_error)
                    raise
                self._close(handle)
                return relative
        raise ProjectPathSafetyError(parent_relative, "path-identity-changed")

    def create_directory_exclusive(self, relative: Path, mode: int) -> None:
        _ = mode
        marker = relative.parent / "__kafa_directory_parent__"
        with self._ancestors(
            marker,
            create=True,
            with_handle=True,
        ) as parent_receipt:
            _parent, parent_handle = parent_receipt
            try:
                handle, identity = self._create_directory_at(
                    parent_handle,
                    relative.name,
                    relative,
                )
            except FileExistsError:
                self.snapshot(
                    relative,
                    allow_missing=False,
                    expect_directory=True,
                )
                raise
            expected = _PathSnapshot(True, identity)
            try:
                if self._recheck_snapshot(
                    relative,
                    expected,
                    expect_directory=True,
                ) != expected:
                    raise ProjectPathSafetyError(
                        relative,
                        "path-identity-changed",
                    )
            except BaseException as exc:
                cleanup_error = self._cleanup_created_directory_handle(
                    handle,
                    relative,
                )
                if cleanup_error:
                    exc.add_note(cleanup_error)
                raise
            self._close(handle)

    def replace_file(
        self,
        source: Path,
        destination: Path,
        expected_source: _PathSnapshot | None = None,
        expected_destination: _PathSnapshot | None = None,
    ) -> None:
        if source == destination:
            raise ProjectPathSafetyError(destination, "invalid-relative-path")
        with self._ancestors(source, create=False) as source_parent, self._ancestors(
            destination, create=True
        ) as destination_parent:
            source_snapshot = self.snapshot(source, allow_missing=False)
            destination_snapshot = self.snapshot(destination, allow_missing=True)
            if expected_source is not None and source_snapshot != expected_source:
                raise ProjectPathSafetyError(
                    source,
                    "path-identity-changed",
                )
            if (
                expected_destination is not None
                and destination_snapshot != expected_destination
            ):
                raise ProjectPathSafetyError(
                    destination,
                    "path-identity-changed",
                )
            source_path = source_parent / source.name
            destination_path = destination_parent / destination.name
            source_handle = self._open_handle(
                source_path,
                directory=False,
                access=_GENERIC_READ | _GENERIC_WRITE | _DELETE,
            )
            destination_handle = _INVALID_HANDLE_VALUE
            destination_identity: _PathIdentity | None = None
            try:
                source_identity = self._identity(
                    source_handle,
                    expect_directory=False,
                    relative=source,
                )
                if source_identity != source_snapshot.identity:
                    raise ProjectPathSafetyError(
                        source,
                        "path-identity-changed",
                    )
                if not self.api.FlushFileBuffers(source_handle):
                    raise ProjectPathSafetyError(
                        source,
                        "path-identity-changed",
                    ) from self.api.error()
                destination_handle, destination_identity = self._pin_destination(
                    destination_path,
                    destination,
                    destination_snapshot,
                )
                _before_windows_handle_rename(
                    self,
                    source,
                    destination,
                )
                if self._identity(
                    source_handle,
                    expect_directory=False,
                    relative=source,
                ) != source_identity:
                    raise ProjectPathSafetyError(
                        source,
                        "path-identity-changed",
                    )
                if destination_snapshot.exists:
                    assert destination_identity is not None
                    if self._identity(
                        destination_handle,
                        expect_directory=False,
                        relative=destination,
                    ) != destination_identity:
                        raise ProjectPathSafetyError(
                            destination,
                            "path-identity-changed",
                        )
                    if (
                        self._recheck_snapshot(
                            destination,
                            destination_snapshot,
                        )
                        != destination_snapshot
                    ):
                        raise ProjectPathSafetyError(
                            destination,
                            "path-identity-changed",
                        )
                    if self._recheck_snapshot(source, source_snapshot) != source_snapshot:
                        raise ProjectPathSafetyError(
                            source,
                            "path-identity-changed",
                        )
                    with self._temporarily_writable_destination(
                        destination_handle,
                        destination,
                        destination_snapshot,
                    ) as destination_lease:
                        self._close(source_handle)
                        source_handle = _INVALID_HANDLE_VALUE
                        self._replace_with_backup_checked(
                            source_path,
                            source,
                            source_identity,
                            destination_path,
                            destination,
                            destination_lease.working_snapshot,
                            destination_lease,
                        )
                else:
                    if self._recheck_snapshot(
                        destination,
                        destination_snapshot,
                    ).exists:
                        raise ProjectPathSafetyError(
                            destination,
                            "path-identity-changed",
                        )
                    self._publish_handle_rename_checked(
                        source_handle,
                        source_path,
                        source,
                        source_identity,
                        destination_path,
                        destination,
                        flush=False,
                    )
            finally:
                self._close(destination_handle)
                self._close(source_handle)
            if self._recheck_snapshot(
                destination,
                _PathSnapshot(True, source_snapshot.identity),
            ).identity != source_snapshot.identity:
                raise ProjectPathSafetyError(destination, "path-identity-changed")
            if self._recheck_snapshot(source, _PathSnapshot(False)).exists:
                raise ProjectPathSafetyError(source, "path-identity-changed")

    def remove_empty_directory(self, relative: Path, *, missing_ok: bool) -> None:
        snapshot = self.snapshot(
            relative,
            allow_missing=missing_ok,
            expect_directory=True,
        )
        if not snapshot.exists:
            return
        marker = relative.parent / "__kafa_directory_parent__"
        with self._ancestors(marker, create=False) as parent:
            target = parent / relative.name
            try:
                handle = self._open_handle(
                    target,
                    directory=True,
                    access=_DELETE,
                )
            except OSError as exc:
                raise ProjectPathSafetyError(
                    relative,
                    "path-identity-changed",
                ) from exc
            try:
                identity = self._identity(
                    handle,
                    expect_directory=True,
                    relative=relative,
                )
                if identity != snapshot.identity:
                    raise ProjectPathSafetyError(
                        relative,
                        "path-identity-changed",
                    )
                _before_windows_handle_delete(self, relative)
                if self._identity(
                    handle,
                    expect_directory=True,
                    relative=relative,
                ) != identity:
                    raise ProjectPathSafetyError(
                        relative,
                        "path-identity-changed",
                    )
                self._delete_on_close(handle, relative)
            finally:
                self._close(handle)
            if self.snapshot(
                relative,
                allow_missing=True,
                expect_directory=True,
            ).exists:
                raise ProjectPathSafetyError(
                    relative,
                    "path-identity-changed",
                )


class ProjectFS:
    """Operation-scoped facade for safe project-relative filesystem access."""

    def __init__(
        self,
        root: Path,
        backend: _PosixBackend | _WindowsBackend,
        *,
        root_alias: Path,
        owns_backend: bool = True,
    ) -> None:
        self._root = root
        self._root_alias = root_alias
        self._backend = backend
        self._owns_backend = owns_backend
        self._closed = False

    @classmethod
    def open(cls, root: Path) -> "ProjectFS":
        expanded = Path(root).expanduser()
        absolute_alias = Path(os.path.abspath(expanded))
        for pinned in reversed(_pinned_project_filesystems()):
            if pinned._matches_root_alias(absolute_alias):
                return pinned._borrow()
        try:
            if not expanded.exists():
                if expanded.is_symlink():
                    raise OSError("project root is a dangling link")
                expanded.mkdir(parents=True, exist_ok=True)
            resolved = expanded.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ProjectPathSafetyError(Path("."), "unsafe-ancestor") from exc
        if not resolved.is_dir():
            raise ProjectPathSafetyError(Path("."), "unsafe-ancestor")
        try:
            root_before = _posix_identity(
                os.stat(resolved, follow_symlinks=False)
            )
        except OSError as exc:
            raise ProjectPathSafetyError(Path("."), "unsafe-ancestor") from exc
        if root_before.kind != "directory":
            raise ProjectPathSafetyError(Path("."), "unsafe-ancestor")
        backend: _PosixBackend | _WindowsBackend
        try:
            if os.name == "nt":
                backend = _WindowsBackend(resolved)
            else:
                backend = _PosixBackend(resolved)
            root_after = _posix_identity(
                os.stat(resolved, follow_symlinks=False)
            )
            if root_after != root_before:
                raise ProjectPathSafetyError(
                    Path("."),
                    "path-identity-changed",
                )
            if os.name != "nt" and backend.root_identity != root_before:
                raise ProjectPathSafetyError(
                    Path("."),
                    "path-identity-changed",
                )
        except BaseException:
            if "backend" in locals():
                backend.close()
            raise
        return cls(
            resolved,
            backend,
            root_alias=Path(os.path.abspath(expanded)),
        )

    def _matches_root_alias(self, absolute: Path) -> bool:
        return (
            not self._closed
            and absolute in {self._root_alias, self._root}
        )

    def _borrow(self) -> "ProjectFS":
        if self._closed:
            raise RuntimeError("ProjectFS is closed")
        return ProjectFS(
            self._root,
            self._backend,
            root_alias=self._root_alias,
            owns_backend=False,
        )

    @property
    def root(self) -> Path:
        if self._closed:
            raise RuntimeError("ProjectFS is closed")
        self._backend.assert_root_path()
        return self._root

    @property
    def root_identity_key(self) -> tuple[object, ...]:
        return self._backend.identity_key

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_backend:
            self._backend.close()

    def __enter__(self) -> "ProjectFS":
        if self._closed:
            raise RuntimeError("ProjectFS is closed")
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def _relative(self, value: Path | str) -> Path:
        if self._closed:
            raise RuntimeError("ProjectFS is closed")
        return _validate_relative(value)

    def relative_to_root(self, value: Path) -> Path:
        """Convert an absolute or relative project path without resolving descendants."""

        path = Path(value)
        if not path.is_absolute():
            return self._relative(path)
        absolute = Path(os.path.abspath(path))
        for root in (self._root_alias, self._root):
            try:
                return self._relative(absolute.relative_to(root))
            except ValueError:
                continue
        raise ProjectPathSafetyError(path, "invalid-relative-path")

    def absolute(self, relative: Path | str) -> Path:
        normalized = self._relative(relative)
        self._backend.assert_root_path()
        return self._root / normalized

    def audit(
        self,
        paths: Iterable[Path | str],
        *,
        allow_missing: bool = True,
    ) -> None:
        values = tuple(paths)
        if len(values) > MAX_AUDIT_PATHS:
            raise ProjectPathSafetyError(Path("."), "invalid-relative-path")
        for value in values:
            relative = self._relative(value)
            self._backend.audit(relative, allow_missing=allow_missing)

    def audit_directory(
        self,
        relative: Path | str,
        *,
        allow_missing: bool = True,
    ) -> None:
        normalized = self._relative(relative)
        self._backend.audit(
            normalized,
            allow_missing=allow_missing,
            expect_directory=True,
        )

    def ensure_directory(self, relative: Path | str) -> Path:
        normalized = self._relative(relative)
        self._backend.ensure_directory(normalized)
        return self._root / normalized

    def _snapshot(
        self,
        relative: Path | str,
        *,
        allow_missing: bool,
        expect_directory: bool = False,
    ) -> _PathSnapshot:
        normalized = self._relative(relative)
        return self._backend.snapshot(
            normalized,
            allow_missing=allow_missing,
            expect_directory=expect_directory,
        )

    def _assert_unchanged(
        self,
        relative: Path | str,
        expected: _PathSnapshot,
        *,
        expect_directory: bool = False,
    ) -> None:
        normalized = self._relative(relative)
        actual = self._backend.snapshot(
            normalized,
            allow_missing=not expected.exists,
            expect_directory=expect_directory,
        )
        if actual != expected:
            raise ProjectPathSafetyError(normalized, "path-identity-changed")

    def read_bytes(
        self,
        relative: Path | str,
        *,
        max_bytes: int | None = None,
        expected: _PathSnapshot | None = None,
    ) -> bytes:
        normalized = self._relative(relative)
        if max_bytes is not None and max_bytes < 0:
            raise ValueError("max_bytes must be non-negative")
        return self._backend.read_bytes(
            normalized,
            max_bytes=max_bytes,
            expected=expected,
        )

    def sync_regular(
        self,
        relative: Path | str,
        *,
        expected: _PathSnapshot | None = None,
    ) -> _PathSnapshot:
        """Flush one exact regular-file authority without following aliases."""

        normalized = self._relative(relative)
        return self._backend.sync_regular(normalized, expected)

    def atomic_write(
        self,
        relative: Path | str,
        data: bytes,
        *,
        mode: int = 0o600,
        expected_destination: _PathSnapshot | None = None,
    ) -> _PathSnapshot:
        normalized = self._relative(relative)
        return self._backend.atomic_write(
            self,
            normalized,
            bytes(data),
            mode,
            expected_destination,
        )

    def create_exclusive(
        self,
        relative: Path | str,
        data: bytes = b"",
        *,
        mode: int = 0o600,
    ) -> _PathSnapshot:
        normalized = self._relative(relative)
        return self._backend.create_exclusive(normalized, bytes(data), mode)

    def open_lock_fd(
        self,
        relative: Path | str,
        *,
        mode: int = 0o600,
    ) -> int:
        normalized = self._relative(relative)
        return self._backend.open_lock_fd(normalized, mode)

    def open_exclusion_fd(self) -> int | None:
        if self._closed:
            raise RuntimeError("ProjectFS is closed")
        return self._backend.open_exclusion_fd()

    def unlink_regular(
        self,
        relative: Path | str,
        *,
        missing_ok: bool = False,
        expected: _PathSnapshot | None = None,
    ) -> None:
        normalized = self._relative(relative)
        self._backend.unlink_regular(
            normalized,
            missing_ok=missing_ok,
            expected=expected,
        )

    def create_unique_directory(
        self,
        parent: Path | str,
        prefix: str,
    ) -> Path:
        normalized = self._relative(parent)
        if not prefix or "/" in prefix or "\\" in prefix:
            raise ProjectPathSafetyError(normalized, "invalid-relative-path")
        # Every backend appends exactly sixteen hexadecimal characters.  Check
        # the complete candidate grammar before a parent directory is created;
        # this rejects Windows ADS/reserved-name aliases on every host.
        self._relative(normalized / f"{prefix}{'0' * 16}")
        return self._backend.create_unique_directory(normalized, prefix)

    def create_directory_exclusive(
        self,
        relative: Path | str,
        *,
        mode: int = 0o700,
    ) -> Path:
        normalized = self._relative(relative)
        self._backend.create_directory_exclusive(normalized, mode)
        return self._root / normalized

    def replace_file(
        self,
        source: Path | str,
        destination: Path | str,
        *,
        expected_source: _PathSnapshot | None = None,
        expected_destination: _PathSnapshot | None = None,
    ) -> None:
        source_relative = self._relative(source)
        destination_relative = self._relative(destination)
        self._backend.replace_file(
            source_relative,
            destination_relative,
            expected_source=expected_source,
            expected_destination=expected_destination,
        )

    def remove_empty_directory(
        self,
        relative: Path | str,
        *,
        missing_ok: bool = False,
    ) -> None:
        normalized = self._relative(relative)
        self._backend.remove_empty_directory(
            normalized,
            missing_ok=missing_ok,
        )

    def copy_file(
        self,
        source: Path | str,
        destination: Path | str,
        *,
        mode: int = 0o600,
    ) -> None:
        payload = self.read_bytes(source)
        self.atomic_write(destination, payload, mode=mode)

    def copy_from_external(
        self,
        source: Path,
        destination: Path | str,
        *,
        mode: int = 0o600,
    ) -> None:
        source_path = Path(source)
        try:
            metadata = source_path.lstat()
        except OSError as exc:
            raise ProjectPathSafetyError(Path(source_path.name), "unsafe-target") from exc
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            reason = (
                "hard-linked-target"
                if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1
                else "unsafe-target"
            )
            raise ProjectPathSafetyError(Path(source_path.name), reason)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(source_path, flags)
        except OSError as exc:
            raise ProjectPathSafetyError(Path(source_path.name), "unsafe-target") from exc
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise ProjectPathSafetyError(Path(source_path.name), "path-identity-changed")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            os.close(descriptor)
        self.atomic_write(destination, b"".join(chunks), mode=mode)

    def sqlite_path(
        self,
        relative: Path | str,
        *,
        access: str,
        create: bool = False,
    ) -> Path:
        normalized = self._relative(relative)
        if access not in {"ro", "rw"}:
            raise ValueError("SQLite access must be 'ro' or 'rw'")
        snapshot = self._backend.snapshot(
            normalized,
            allow_missing=create,
        )
        if not snapshot.exists:
            if not create:
                raise ProjectPathSafetyError(normalized, "unsafe-target")
            self.create_exclusive(normalized, b"", mode=0o600)
        return self.absolute(normalized)

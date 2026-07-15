"""Pinned, fail-closed access to canonical project files.

The kernel owns a small set of project-relative files.  This module keeps
their lexical authority and filesystem identity separate from arbitrary user
commands, which are intentionally outside this boundary.
"""

from __future__ import annotations

import errno
import os
import secrets
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Iterable, Iterator

from .errors import HarnessError


MAX_AUDIT_PATHS = 256
_PINNED_PROJECT_FILESYSTEMS = threading.local()
_WINDOWS_RENAME_CAPABILITY_ERRORS = frozenset({1, 50, 87, 120, 124})
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


class _MissingAncestor(Exception):
    pass


def _validate_relative(value: Path | str) -> Path:
    raw = os.fspath(value)
    if not isinstance(raw, str):
        raw = os.fsdecode(raw)
    if not raw or raw in {".", ".."} or "\x00" in raw or "\\" in raw:
        raise ProjectPathSafetyError(Path(raw or "."), "invalid-relative-path")
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
        descriptor = self._open_root()
        try:
            metadata = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        self.root_identity = _posix_identity(metadata)

    @property
    def identity_key(self) -> tuple[object, ...]:
        return ("posix", self.root_identity.volume, self.root_identity.file_id)

    def _open_root(self) -> int:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(self.root, flags)
        except OSError as exc:
            raise ProjectPathSafetyError(Path("."), "path-identity-changed") from exc
        os.set_inheritable(descriptor, False)
        if hasattr(self, "root_identity"):
            identity = _posix_identity(os.fstat(descriptor))
            if identity != self.root_identity:
                os.close(descriptor)
                raise ProjectPathSafetyError(Path("."), "path-identity-changed")
        return descriptor

    @contextmanager
    def _parent(
        self,
        relative: Path,
        *,
        create: bool,
    ) -> Iterator[tuple[int, str, tuple[tuple[int, str, _PathIdentity], ...]]]:
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
        for parent, component, expected in ancestry:
            try:
                actual = _posix_identity(
                    os.stat(component, dir_fd=parent, follow_symlinks=False)
                )
            except OSError as exc:
                raise ProjectPathSafetyError(relative, "path-identity-changed") from exc
            if actual != expected:
                raise ProjectPathSafetyError(relative, "path-identity-changed")

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

    def read_bytes(self, relative: Path, *, max_bytes: int | None) -> bytes:
        with self._parent(relative, create=False) as (parent, name, ancestry):
            expected = self._snapshot_at(
                parent, name, relative, allow_missing=False
            )
            flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
            try:
                descriptor = os.open(name, flags, dir_fd=parent)
            except OSError as exc:
                raise ProjectPathSafetyError(relative, "path-identity-changed") from exc
            try:
                os.set_inheritable(descriptor, False)
                if _posix_identity(os.fstat(descriptor)) != expected.identity:
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
                if _posix_identity(os.fstat(descriptor)) != expected.identity:
                    raise ProjectPathSafetyError(relative, "path-identity-changed")
            finally:
                os.close(descriptor)
            self._check_ancestry(relative, ancestry)
            current = self._snapshot_at(
                parent, name, relative, allow_missing=False
            )
            if current != expected:
                raise ProjectPathSafetyError(relative, "path-identity-changed")
            return data

    def _write_all(self, descriptor: int, data: bytes) -> None:
        view = memoryview(data)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:
                raise OSError("short project-file write")
            written += count

    def atomic_write(self, fs: "ProjectFS", relative: Path, data: bytes, mode: int) -> None:
        with self._parent(relative, create=True) as (parent, name, ancestry):
            expected = self._snapshot_at(
                parent, name, relative, allow_missing=True
            )
            temporary_name = f".{name}.kafa-{secrets.token_hex(12)}.tmp"
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            flags |= getattr(os, "O_CLOEXEC", 0)
            descriptor: int | None = None
            temporary_identity: _PathIdentity | None = None
            published = False
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
                os.replace(
                    temporary_name,
                    name,
                    src_dir_fd=parent,
                    dst_dir_fd=parent,
                )
                published = True
                final = self._snapshot_at(
                    parent, name, relative, allow_missing=False
                )
                if final.identity != temporary_identity:
                    raise ProjectPathSafetyError(relative, "path-identity-changed")
                os.fsync(parent)
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                if not published:
                    try:
                        metadata = os.stat(
                            temporary_name,
                            dir_fd=parent,
                            follow_symlinks=False,
                        )
                    except OSError:
                        pass
                    else:
                        if (
                            temporary_identity is not None
                            and _posix_identity(metadata) == temporary_identity
                        ):
                            try:
                                os.unlink(temporary_name, dir_fd=parent)
                            except OSError:
                                pass

    def create_exclusive(self, relative: Path, data: bytes, mode: int) -> None:
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
            except BaseException:
                os.close(descriptor)
                try:
                    current = _posix_identity(
                        os.stat(
                            name,
                            dir_fd=parent,
                            follow_symlinks=False,
                        )
                    )
                except OSError:
                    pass
                else:
                    if current == created_identity:
                        try:
                            os.unlink(name, dir_fd=parent)
                            os.fsync(parent)
                        except OSError:
                            pass
                raise
            else:
                os.close(descriptor)
            self._check_ancestry(relative, ancestry)
            if self._snapshot_at(
                parent, name, relative, allow_missing=False
            ).identity != identity:
                raise ProjectPathSafetyError(relative, "path-identity-changed")
            os.fsync(parent)

    def open_lock_fd(self, relative: Path, mode: int) -> int:
        with self._parent(relative, create=True) as (parent, name, ancestry):
            expected = self._snapshot_at(
                parent, name, relative, allow_missing=True
            )
            flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
            flags |= getattr(os, "O_CLOEXEC", 0)
            try:
                descriptor = os.open(name, flags, mode, dir_fd=parent)
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
                if expected.exists and expected.identity != identity:
                    raise ProjectPathSafetyError(relative, "path-identity-changed")
                os.fchmod(descriptor, mode)
                self._check_ancestry(relative, ancestry)
                current = self._snapshot_at(
                    parent, name, relative, allow_missing=False
                )
                if current.identity != identity:
                    raise ProjectPathSafetyError(relative, "path-identity-changed")
                return descriptor
            except BaseException:
                os.close(descriptor)
                raise

    def unlink_regular(self, relative: Path, *, missing_ok: bool) -> None:
        try:
            with self._parent(relative, create=False) as (parent, name, ancestry):
                expected = self._snapshot_at(
                    parent, name, relative, allow_missing=missing_ok
                )
                if not expected.exists:
                    return
                self._check_ancestry(relative, ancestry)
                if self._snapshot_at(
                    parent, name, relative, allow_missing=False
                ) != expected:
                    raise ProjectPathSafetyError(relative, "path-identity-changed")
                os.unlink(name, dir_fd=parent)
                os.fsync(parent)
        except _MissingAncestor:
            if not missing_ok:
                raise ProjectPathSafetyError(relative, "unsafe-target")

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

    def replace_file(self, source: Path, destination: Path) -> None:
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
            os.replace(
                source_name,
                destination_name,
                src_dir_fd=source_parent,
                dst_dir_fd=destination_parent,
            )
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
                os.rmdir(relative.name, dir_fd=parent)
                os.fsync(parent)
        except _MissingAncestor:
            if not missing_ok:
                raise ProjectPathSafetyError(relative, "unsafe-target")


if os.name == "nt":  # pragma: no cover - exercised by the Windows matrix
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _DELETE = 0x00010000
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _CREATE_NEW = 1
    _OPEN_EXISTING = 3
    _OPEN_ALWAYS = 4
    _FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FILE_DISPOSITION_INFO_CLASS = 4
    _FILE_RENAME_INFO_EX_CLASS = 22
    _FILE_RENAME_REPLACE_IF_EXISTS = 0x00000001
    _FILE_RENAME_POSIX_SEMANTICS = 0x00000002
    _FILE_ID_INFO_CLASS = 18

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


class _WindowsBackend:  # pragma: no cover - exercised by Windows validation
    def __init__(self, root: Path, api: _WindowsApi | None = None) -> None:
        self.root = root
        self.api = api or _WindowsApi()
        if not self.api.available:
            raise ProjectPathSafetyError(Path("."), "platform-safety-unavailable")
        handle = self._open_handle(root, directory=True)
        try:
            self.root_identity = self._identity(handle, expect_directory=True, relative=Path("."))
        finally:
            self._close(handle)

    @property
    def identity_key(self) -> tuple[object, ...]:
        return ("windows", self.root_identity.volume, self.root_identity.file_id)

    def _close(self, handle: int) -> None:
        if handle not in (None, _INVALID_HANDLE_VALUE):
            self.api.CloseHandle(handle)

    def _open_handle(
        self,
        path: Path,
        *,
        directory: bool,
        access: int = 0,
        disposition: int | None = None,
    ) -> int:
        flags = _FILE_FLAG_OPEN_REPARSE_POINT
        if directory:
            flags |= _FILE_FLAG_BACKUP_SEMANTICS
        handle = self.api.CreateFileW(
            os.fspath(path),
            access,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE,
            None,
            disposition or _OPEN_EXISTING,
            flags,
            None,
        )
        if handle == _INVALID_HANDLE_VALUE:
            raise self.api.error()
        return handle

    def _identity(
        self,
        handle: int,
        *,
        expect_directory: bool,
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
        if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise ProjectPathSafetyError(relative, "unsafe-target")
        is_directory = bool(attributes & _FILE_ATTRIBUTE_DIRECTORY)
        if is_directory != expect_directory:
            raise ProjectPathSafetyError(relative, "unsafe-target")
        identity = _PathIdentity(
            volume=int(file_id.volume_serial),
            file_id=bytes(file_id.file_id.identifier),
            kind="directory" if is_directory else "file",
            mode_or_attributes=attributes,
            nlink=0 if is_directory else int(basic.nlinks),
        )
        if not is_directory and identity.nlink != 1:
            raise ProjectPathSafetyError(relative, "hard-linked-target")
        return identity

    def _delete_on_close(self, handle: int, relative: Path) -> None:
        disposition = _FILE_DISPOSITION_INFO(1)
        if not self.api.SetFileInformationByHandle(
            handle,
            _FILE_DISPOSITION_INFO_CLASS,
            ctypes.byref(disposition),
            ctypes.sizeof(disposition),
        ):
            raise ProjectPathSafetyError(
                relative,
                "platform-safety-unavailable",
            ) from self.api.error()

    def _rename_by_handle(
        self,
        source_handle: int,
        destination: Path,
        relative: Path,
        *,
        replace_existing: bool,
    ) -> None:
        encoded_name = os.fspath(destination).encode("utf-16-le")
        file_name_offset = _FILE_RENAME_INFO.file_name.offset
        buffer = ctypes.create_string_buffer(
            file_name_offset + len(encoded_name)
        )
        rename_info = ctypes.cast(
            buffer,
            ctypes.POINTER(_FILE_RENAME_INFO),
        ).contents
        rename_info.flags = (
            _FILE_RENAME_REPLACE_IF_EXISTS
            | _FILE_RENAME_POSIX_SEMANTICS
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
        if not self.api.SetFileInformationByHandle(
            source_handle,
            _FILE_RENAME_INFO_EX_CLASS,
            buffer,
            len(buffer),
        ):
            error = self.api.error()
            raise ProjectPathSafetyError(
                relative,
                _windows_rename_error_reason(error),
            ) from error

    @contextmanager
    def _ancestors(
        self,
        relative: Path,
        *,
        create: bool,
    ) -> Iterator[Path]:
        handles: list[int] = []
        current = self.root
        try:
            root_handle = self._open_handle(current, directory=True)
            handles.append(root_handle)
            if self._identity(root_handle, expect_directory=True, relative=relative) != self.root_identity:
                raise ProjectPathSafetyError(relative, "path-identity-changed")
            for component in relative.parts[:-1]:
                current = current / component
                try:
                    handle = self._open_handle(current, directory=True)
                except OSError as exc:
                    if not create or exc.errno not in {2, 3}:
                        raise ProjectPathSafetyError(relative, "unsafe-ancestor") from exc
                    try:
                        os.mkdir(current, 0o700)
                    except FileExistsError:
                        pass
                    try:
                        handle = self._open_handle(current, directory=True)
                    except OSError as retry_exc:
                        raise ProjectPathSafetyError(relative, "unsafe-ancestor") from retry_exc
                identity = self._identity(handle, expect_directory=True, relative=relative)
                if identity.volume != self.root_identity.volume:
                    self._close(handle)
                    raise ProjectPathSafetyError(relative, "cross-device-ancestor")
                handles.append(handle)
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
            with self._ancestors(relative, create=False) as parent:
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
        except ProjectPathSafetyError:
            raise
        except OSError as exc:
            if allow_missing and exc.errno in {2, 3}:
                return _PathSnapshot(False)
            raise ProjectPathSafetyError(relative, "unsafe-ancestor") from exc

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

    def read_bytes(self, relative: Path, *, max_bytes: int | None) -> bytes:
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
        _ = mode
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
            handle = self._open_handle(path, directory=False)
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

    def atomic_write(self, fs: "ProjectFS", relative: Path, data: bytes, mode: int) -> None:
        with self._ancestors(relative, create=True) as parent:
            target = parent / relative.name
            expected = self.snapshot(relative, allow_missing=True)
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
                if self.snapshot(relative, allow_missing=True) != expected:
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
                self._rename_by_handle(
                    temporary_handle,
                    target,
                    relative,
                    replace_existing=expected.exists,
                )
                published = True
                if self._identity(
                    temporary_handle,
                    expect_directory=False,
                    relative=relative,
                ) != temporary_identity:
                    raise ProjectPathSafetyError(relative, "path-identity-changed")
                if not self.api.FlushFileBuffers(temporary_handle):
                    raise ProjectPathSafetyError(
                        relative,
                        "path-identity-changed",
                    ) from self.api.error()
            except BaseException as exc:
                if not published:
                    cleanup_error = self._cleanup_created_handle(
                        temporary_handle,
                        temporary,
                        temporary_relative,
                        temporary_identity,
                    )
                    temporary_handle = _INVALID_HANDLE_VALUE
                    if cleanup_error:
                        exc.add_note(cleanup_error)
                raise
            finally:
                self._close(destination_handle)
                self._close(temporary_handle)
            if self.snapshot(relative, allow_missing=False).identity != temporary_identity:
                raise ProjectPathSafetyError(relative, "path-identity-changed")

    def create_exclusive(self, relative: Path, data: bytes, mode: int) -> None:
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
                descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR)
                handle = _INVALID_HANDLE_VALUE
                os.set_inheritable(descriptor, False)
                _ = mode
                return descriptor
            finally:
                self._close(handle)

    def unlink_regular(self, relative: Path, *, missing_ok: bool) -> None:
        snapshot = self.snapshot(relative, allow_missing=missing_ok)
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
        with self._ancestors(marker, create=True) as parent:
            for _ in range(128):
                relative = parent_relative / f"{prefix}{secrets.token_hex(8)}"
                target = parent / relative.name
                _before_windows_directory_create(self, relative)
                try:
                    os.mkdir(target, 0o700)
                except FileExistsError:
                    continue
                self.snapshot(
                    relative,
                    allow_missing=False,
                    expect_directory=True,
                )
                return relative
        raise ProjectPathSafetyError(parent_relative, "path-identity-changed")

    def create_directory_exclusive(self, relative: Path, mode: int) -> None:
        marker = relative.parent / "__kafa_directory_parent__"
        with self._ancestors(marker, create=True) as parent:
            target = parent / relative.name
            try:
                os.mkdir(target, mode)
            except FileExistsError:
                self.snapshot(
                    relative,
                    allow_missing=False,
                    expect_directory=True,
                )
                raise
            self.snapshot(
                relative,
                allow_missing=False,
                expect_directory=True,
            )

    def replace_file(self, source: Path, destination: Path) -> None:
        if source == destination:
            raise ProjectPathSafetyError(destination, "invalid-relative-path")
        with self._ancestors(source, create=False) as source_parent, self._ancestors(
            destination, create=True
        ) as destination_parent:
            source_snapshot = self.snapshot(source, allow_missing=False)
            destination_snapshot = self.snapshot(destination, allow_missing=True)
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
                self._rename_by_handle(
                    source_handle,
                    destination_path,
                    destination,
                    replace_existing=destination_snapshot.exists,
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
            finally:
                self._close(destination_handle)
                self._close(source_handle)
            if self.snapshot(destination, allow_missing=False).identity != source_snapshot.identity:
                raise ProjectPathSafetyError(destination, "path-identity-changed")
            if self.snapshot(source, allow_missing=True).exists:
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
    ) -> None:
        self._root = root
        self._root_alias = root_alias
        self._backend = backend
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
        backend: _PosixBackend | _WindowsBackend
        if os.name == "nt":
            backend = _WindowsBackend(resolved)
        else:
            backend = _PosixBackend(resolved)
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
        )

    @property
    def root(self) -> Path:
        return self._root

    @property
    def root_identity_key(self) -> tuple[object, ...]:
        return self._backend.identity_key

    def close(self) -> None:
        self._closed = True

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
        return self._root / self._relative(relative)

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
    ) -> bytes:
        normalized = self._relative(relative)
        if max_bytes is not None and max_bytes < 0:
            raise ValueError("max_bytes must be non-negative")
        return self._backend.read_bytes(normalized, max_bytes=max_bytes)

    def atomic_write(
        self,
        relative: Path | str,
        data: bytes,
        *,
        mode: int = 0o600,
    ) -> None:
        normalized = self._relative(relative)
        self._backend.atomic_write(self, normalized, bytes(data), mode)

    def create_exclusive(
        self,
        relative: Path | str,
        data: bytes = b"",
        *,
        mode: int = 0o600,
    ) -> None:
        normalized = self._relative(relative)
        self._backend.create_exclusive(normalized, bytes(data), mode)

    def open_lock_fd(
        self,
        relative: Path | str,
        *,
        mode: int = 0o600,
    ) -> int:
        normalized = self._relative(relative)
        return self._backend.open_lock_fd(normalized, mode)

    def unlink_regular(
        self,
        relative: Path | str,
        *,
        missing_ok: bool = False,
    ) -> None:
        normalized = self._relative(relative)
        self._backend.unlink_regular(normalized, missing_ok=missing_ok)

    def create_unique_directory(
        self,
        parent: Path | str,
        prefix: str,
    ) -> Path:
        normalized = self._relative(parent)
        if not prefix or "/" in prefix or "\\" in prefix:
            raise ProjectPathSafetyError(normalized, "invalid-relative-path")
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
    ) -> None:
        source_relative = self._relative(source)
        destination_relative = self._relative(destination)
        self._backend.replace_file(source_relative, destination_relative)

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
        return self._root / normalized

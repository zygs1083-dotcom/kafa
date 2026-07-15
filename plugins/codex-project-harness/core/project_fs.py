"""Pinned, fail-closed access to canonical project files.

The kernel owns a small set of project-relative files.  This module keeps
their lexical authority and filesystem identity separate from arbitrary user
commands, which are intentionally outside this boundary.
"""

from __future__ import annotations

import errno
import os
import re
import secrets
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Iterable, Iterator

from .errors import HarnessError


MAX_AUDIT_PATHS = 256
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
    for function in (os.open, os.stat, os.mkdir, os.unlink)
)


def _before_atomic_replace(_fs: "ProjectFS", _relative: Path) -> None:
    """Deterministic test seam immediately before the final identity check."""


class ProjectPathSafetyError(HarnessError):
    """A canonical path cannot be used without following untrusted authority."""

    def __init__(self, relative: Path | str, reason: str) -> None:
        self.relative = Path(str(relative))
        self.reason = reason
        super().__init__(
            f"unsafe-project-path: {self.relative.as_posix()}: {reason}"
        )


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
        with self._parent(marker, create=True) as (parent, _name, ancestry):
            self._check_ancestry(relative, ancestry)
            metadata = os.fstat(parent)
            identity = _posix_identity(metadata)
            if identity.kind != "directory":
                raise ProjectPathSafetyError(relative, "unsafe-ancestor")

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
            try:
                os.set_inheritable(descriptor, False)
                self._write_all(descriptor, data)
                os.fchmod(descriptor, mode)
                os.fsync(descriptor)
                identity = _posix_identity(os.fstat(descriptor))
                if identity.kind != "file" or identity.nlink != 1:
                    raise ProjectPathSafetyError(relative, "unsafe-target")
            finally:
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


if os.name == "nt":  # pragma: no cover - exercised by the Windows matrix
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _CREATE_NEW = 1
    _OPEN_EXISTING = 3
    _OPEN_ALWAYS = 4
    _FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _MOVEFILE_WRITE_THROUGH = 0x00000008
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
            self.FlushFileBuffers = kernel32.FlushFileBuffers
            self.FlushFileBuffers.argtypes = (wintypes.HANDLE,)
            self.FlushFileBuffers.restype = wintypes.BOOL
            self.CloseHandle = kernel32.CloseHandle
            self.CloseHandle.argtypes = (wintypes.HANDLE,)
            self.CloseHandle.restype = wintypes.BOOL
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
            self.MoveFileExW = kernel32.MoveFileExW
            self.MoveFileExW.argtypes = (
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
                wintypes.DWORD,
            )
            self.MoveFileExW.restype = wintypes.BOOL
            self.DeleteFileW = kernel32.DeleteFileW
            self.DeleteFileW.argtypes = (wintypes.LPCWSTR,)
            self.DeleteFileW.restype = wintypes.BOOL
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
        with self._ancestors(marker, create=True):
            return

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

    def _create_file(self, path: Path, data: bytes, mode: int) -> _PathIdentity:
        try:
            handle = self._open_handle(
                path,
                directory=False,
                access=_GENERIC_READ | _GENERIC_WRITE,
                disposition=_CREATE_NEW,
            )
        except OSError:
            raise
        try:
            identity = self._identity(handle, expect_directory=False, relative=Path(path.name))
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR)
            handle = _INVALID_HANDLE_VALUE
            try:
                view = memoryview(data)
                written = 0
                while written < len(view):
                    written += os.write(descriptor, view[written:])
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            self._close(handle)
        _ = mode
        return identity

    def atomic_write(self, fs: "ProjectFS", relative: Path, data: bytes, mode: int) -> None:
        with self._ancestors(relative, create=True) as parent:
            target = parent / relative.name
            expected = self.snapshot(relative, allow_missing=True)
            temporary = parent / f".{relative.name}.kafa-{secrets.token_hex(12)}.tmp"
            temporary_identity = self._create_file(temporary, data, mode)
            published = False
            try:
                _before_atomic_replace(fs, relative)
                if self.snapshot(relative, allow_missing=True) != expected:
                    raise ProjectPathSafetyError(relative, "path-identity-changed")
                if expected.exists:
                    ok = self.api.ReplaceFileW(
                        os.fspath(target), os.fspath(temporary), None, 0, None, None
                    )
                else:
                    ok = self.api.MoveFileExW(
                        os.fspath(temporary), os.fspath(target), _MOVEFILE_WRITE_THROUGH
                    )
                if not ok:
                    raise ProjectPathSafetyError(relative, "path-identity-changed") from self.api.error()
                published = True
                if self.snapshot(relative, allow_missing=False).identity != temporary_identity:
                    raise ProjectPathSafetyError(relative, "path-identity-changed")
            finally:
                if not published and temporary.exists() and not temporary.is_symlink():
                    try:
                        temporary.unlink()
                    except OSError:
                        pass

    def create_exclusive(self, relative: Path, data: bytes, mode: int) -> None:
        with self._ancestors(relative, create=True) as parent:
            target = parent / relative.name
            if self.snapshot(relative, allow_missing=True).exists:
                raise FileExistsError(os.fspath(target))
            try:
                expected = self._create_file(target, data, mode)
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
        target = self.root / relative
        if self.snapshot(relative, allow_missing=False) != snapshot:
            raise ProjectPathSafetyError(relative, "path-identity-changed")
        if not self.api.DeleteFileW(os.fspath(target)):
            raise ProjectPathSafetyError(relative, "path-identity-changed") from self.api.error()

    def create_unique_directory(self, parent_relative: Path, prefix: str) -> Path:
        self.ensure_directory(parent_relative)
        for _ in range(128):
            relative = parent_relative / f"{prefix}{secrets.token_hex(8)}"
            target = self.root / relative
            try:
                os.mkdir(target, 0o700)
            except FileExistsError:
                continue
            self.snapshot(relative, allow_missing=False, expect_directory=True)
            return relative
        raise ProjectPathSafetyError(parent_relative, "path-identity-changed")


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

"""Canonical local artifact subject and digest adapters."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping


SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
KIND_PATTERN = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")


class ArtifactSubjectError(ValueError):
    """Raised when an artifact subject is unsafe, ambiguous, or inconsistent."""


@dataclass(frozen=True)
class RegularFileSnapshot:
    """Bytes and mode captured from one identity-bound regular-file descriptor."""

    payload: bytes
    mode: int
    _identity: tuple[int, int, int, int, int, int] = field(repr=False)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.payload).hexdigest()

    def matches_path(self, path: Path) -> bool:
        try:
            current = os.lstat(path)
        except OSError:
            return False
        return stat.S_ISREG(current.st_mode) and _file_identity(current) == self._identity


def read_regular_file(path: Path) -> RegularFileSnapshot:
    """Read one regular file without separating path validation from the read."""

    path = Path(path)
    try:
        before_path = os.lstat(path)
    except OSError as exc:
        raise ArtifactSubjectError(f"file is not a regular file: {path}") from exc
    if not stat.S_ISREG(before_path.st_mode):
        raise ArtifactSubjectError(f"file is not a regular file: {path}")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ArtifactSubjectError(f"file is not a regular file: {path}") from exc
    try:
        before_read = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before_read.st_mode)
            or _file_identity(before_read) != _file_identity(before_path)
        ):
            raise ArtifactSubjectError(f"file changed before reading: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after_read = os.fstat(descriptor)
    finally:
        os.close(descriptor)

    try:
        after_path = os.lstat(path)
    except OSError as exc:
        raise ArtifactSubjectError(f"file changed while reading: {path}") from exc
    if (
        _file_identity(after_read) != _file_identity(before_read)
        or _file_identity(after_path) != _file_identity(before_read)
    ):
        raise ArtifactSubjectError(f"file changed while reading: {path}")
    return RegularFileSnapshot(
        payload=b"".join(chunks),
        mode=before_read.st_mode,
        _identity=_file_identity(after_read),
    )


@dataclass(frozen=True)
class ArtifactSubject:
    name: str
    kind: str
    sha256: str

    def __post_init__(self) -> None:
        if (
            not self.name
            or self.name in {".", ".."}
            or "/" in self.name
            or "\\" in self.name
            or "\x00" in self.name
            or "\n" in self.name
            or "\r" in self.name
        ):
            raise ArtifactSubjectError("artifact name must be one safe basename")
        if not KIND_PATTERN.fullmatch(self.kind):
            raise ArtifactSubjectError("artifact kind is not canonical")
        if not SHA256_PATTERN.fullmatch(self.sha256) or self.sha256 == "0" * 64:
            raise ArtifactSubjectError("artifact SHA-256 is not canonical")

    @classmethod
    def from_file(cls, path: Path, *, kind: str, name: str | None = None) -> ArtifactSubject:
        path = Path(path)
        artifact_name = path.name if name is None else name
        try:
            before = os.lstat(path)
        except OSError as exc:
            raise ArtifactSubjectError(f"artifact is not a regular file: {path}") from exc
        if not stat.S_ISREG(before.st_mode):
            raise ArtifactSubjectError(f"artifact is not a regular file: {path}")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise ArtifactSubjectError(f"artifact is not a regular file: {path}") from exc
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or _file_identity(opened) != _file_identity(before):
                raise ArtifactSubjectError(f"artifact changed before hashing: {path}")
            digest = hashlib.sha256()
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            after_open = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        try:
            after_path = os.lstat(path)
        except OSError as exc:
            raise ArtifactSubjectError(f"artifact changed while hashing: {path}") from exc
        if (
            _file_identity(after_open) != _file_identity(opened)
            or _file_identity(after_path) != _file_identity(opened)
        ):
            raise ArtifactSubjectError(f"artifact changed while hashing: {path}")
        return cls(name=artifact_name, kind=kind, sha256=digest.hexdigest())

    @classmethod
    def from_manifest_record(cls, record: Mapping[str, Any]) -> ArtifactSubject:
        if set(record) != {"name", "kind", "sha256"}:
            raise ArtifactSubjectError("artifact manifest record has an invalid shape")
        if not all(isinstance(record[key], str) for key in ("name", "kind", "sha256")):
            raise ArtifactSubjectError("artifact manifest record fields must be strings")
        return cls(name=record["name"], kind=record["kind"], sha256=record["sha256"])

    def manifest_record(self) -> dict[str, str]:
        return {"name": self.name, "kind": self.kind, "sha256": self.sha256}

    def in_toto_record(self) -> dict[str, object]:
        return {"name": self.name, "digest": {"sha256": self.sha256}}

    def checksum_line(self) -> bytes:
        return f"{self.sha256}  {self.name}\n".encode("utf-8")


def parse_manifest_records(records: Iterable[Mapping[str, Any]]) -> tuple[ArtifactSubject, ...]:
    return _normalize_subjects(ArtifactSubject.from_manifest_record(record) for record in records)


def manifest_records(subjects: Iterable[ArtifactSubject]) -> list[dict[str, str]]:
    return [subject.manifest_record() for subject in _normalize_subjects(subjects)]


def sha256sum_bytes(subjects: Iterable[ArtifactSubject]) -> bytes:
    return b"".join(subject.checksum_line() for subject in _normalize_subjects(subjects))


def in_toto_subjects(subjects: Iterable[ArtifactSubject]) -> list[dict[str, object]]:
    return [subject.in_toto_record() for subject in _normalize_subjects(subjects)]


def subjects_by_kind(subjects: Iterable[ArtifactSubject]) -> dict[str, ArtifactSubject]:
    result: dict[str, ArtifactSubject] = {}
    for subject in _normalize_subjects(subjects):
        if subject.kind in result:
            raise ArtifactSubjectError(f"duplicate artifact kind: {subject.kind}")
        result[subject.kind] = subject
    return result


def assert_exact_subjects(
    expected: Iterable[ArtifactSubject],
    actual: Iterable[ArtifactSubject],
) -> None:
    expected_set = _normalize_subjects(expected)
    actual_set = _normalize_subjects(actual)
    if actual_set != expected_set:
        raise ArtifactSubjectError(
            "artifact subjects do not match: "
            f"actual={manifest_records(actual_set)} expected={manifest_records(expected_set)}"
        )


def _normalize_subjects(subjects: Iterable[ArtifactSubject]) -> tuple[ArtifactSubject, ...]:
    values = tuple(subjects)
    if not all(isinstance(subject, ArtifactSubject) for subject in values):
        raise ArtifactSubjectError("artifact subject collection contains an invalid value")
    keys = [(subject.name, subject.kind) for subject in values]
    if len(keys) != len(set(keys)) or len({subject.name for subject in values}) != len(values):
        raise ArtifactSubjectError("duplicate artifact subject")
    return tuple(sorted(values, key=lambda subject: (subject.name, subject.kind)))


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )

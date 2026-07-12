#!/usr/bin/env python3
"""Shared helpers for Codex Project Harness runtime scripts."""

from __future__ import annotations

import hashlib
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


STATE_PATH = Path(".ai-team/control/project-state.yaml")
EVENT_PATH = Path(".ai-team/runtime/events.jsonl")
HARNESS_GIT_PREFIXES = (
    ".ai-team/",
)
HARNESS_EXACT_SOURCE_PATHS = frozenset(
    {
        ".codex/agents/architect.toml",
        ".codex/agents/developer.toml",
        ".codex/agents/qa-reviewer.toml",
        "docs/harness/validation.md",
        "docs/harness/executions.md",
        "docs/harness/findings.md",
        "docs/harness/quality-gates.md",
        "docs/harness/delivery.md",
        "docs/harness/evidence.md",
    }
)
CONTENT_HASH_EXCLUDE_PREFIXES = HARNESS_GIT_PREFIXES + (
    ".git/",
    "__pycache__/",
    ".pytest_cache/",
)
SOURCE_CACHE_PARTS = frozenset(
    {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
)
SOURCE_ENVIRONMENT_ROOTS = frozenset(
    {".venv", "venv", ".tox", ".nox", "node_modules"}
)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def read_state(root: Path) -> dict[str, str]:
    path = root / STATE_PATH
    state: dict[str, str] = {}
    if not path.exists():
        return state

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if ":" not in raw_line:
            continue
        key, value = raw_line.split(":", 1)
        state[key.strip()] = value.strip() or ""
    return state


def write_state(
    root: Path,
    updates: dict[str, object],
    *,
    merge_existing: bool = True,
    include_blocked_reason: bool = True,
) -> dict[str, str]:
    normalized_updates = {key: str(value) for key, value in updates.items()}
    state = read_state(root) if merge_existing else {}
    state.update(normalized_updates)
    state.setdefault("status", "draft")
    state.setdefault("phase", "intake")
    state.setdefault("scope_status", "unconfirmed")
    state.setdefault("current_owner", "project-manager")
    if include_blocked_reason:
        state.setdefault("blocked_reason", "null")
    if "updated_at" not in normalized_updates:
        state["updated_at"] = now_iso()

    path = root / STATE_PATH
    ensure_parent(path)
    ordered_keys = [
        "status",
        "phase",
        "scope_status",
        "current_owner",
        "blocked_reason",
        "updated_at",
    ]
    lines = [f"{key}: {state[key]}" for key in ordered_keys if key in state]
    for key in sorted(k for k in state if k not in ordered_keys):
        lines.append(f"{key}: {state[key]}")
    path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    return state


def markdown_row(values: list[str]) -> str:
    safe_values = [str(value).replace("\n", " ").replace("|", "\\|") for value in values]
    return "| " + " | ".join(safe_values) + " |"


def isolated_git_environment(*, work_tree: Path | None = None) -> dict[str, str]:
    """Remove ambient overrides, replacements, and network-capable lazy reads."""

    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    environment.update(
        {
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    if work_tree is not None:
        environment["GIT_WORK_TREE"] = os.fspath(Path(work_tree).resolve())
    return environment


def framed_source_digest(entries: Iterable[tuple[bytes, bytes, str]]) -> str:
    """Hash unambiguous path/mode/fixed-content records in deterministic order."""

    digest = hashlib.sha256()
    for raw_relative, mode, content_sha256 in sorted(entries, key=lambda item: item[0]):
        digest.update(raw_relative)
        digest.update(b"\0")
        digest.update(mode)
        digest.update(b"\0")
        digest.update(content_sha256.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def git_blob_objects_available(
    root: Path,
    object_ids: Iterable[str],
    *,
    environment: dict[str, str] | None = None,
) -> bool:
    """Verify every required blob is local without permitting a promisor fetch."""

    expected = sorted(set(object_ids))
    if not expected:
        return True
    command_environment = environment or isolated_git_environment(work_tree=root)
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "cat-file",
                "--batch-check=%(objectname) %(objecttype)",
            ],
            cwd=root,
            env=command_environment,
            input="".join(f"{object_id}\n" for object_id in expected),
            text=True,
            capture_output=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    lines = result.stdout.splitlines()
    if len(lines) != len(expected):
        return False
    return all(
        line.split() == [object_id, "blob"]
        for object_id, line in zip(expected, lines, strict=True)
    )


def _source_path_excluded(
    relative: str,
    *,
    versioned_environment_roots: frozenset[str] = frozenset(),
) -> bool:
    parts = Path(relative).parts
    if any(part in SOURCE_CACHE_PARTS for part in parts):
        return True
    if (
        parts
        and parts[0] in SOURCE_ENVIRONMENT_ROOTS
        and parts[0] not in versioned_environment_roots
    ):
        return True
    if relative in HARNESS_EXACT_SOURCE_PATHS:
        return True
    return any(
        relative == prefix.rstrip("/") or relative.startswith(prefix)
        for prefix in CONTENT_HASH_EXCLUDE_PREFIXES
    )


def _git_blob_oid(content: bytes, object_format: str) -> str:
    digest = hashlib.new(object_format)
    digest.update(f"blob {len(content)}\0".encode("ascii"))
    digest.update(content)
    return digest.hexdigest()


def _invalid_git_identity(reason: str) -> RuntimeError:
    return RuntimeError(f"candidate Git source identity is invalid: {reason}")


def _parse_stage_entries(
    entries: list[bytes],
) -> tuple[dict[bytes, tuple[bytes, str]], set[bytes]]:
    tracked: dict[bytes, tuple[bytes, str]] = {}
    unmerged: set[bytes] = set()
    for raw_entry in (entry for entry in entries if entry):
        try:
            metadata, raw_relative = raw_entry.split(b"\t", 1)
            mode, object_id, stage = metadata.split(b" ", 2)
            decoded_object_id = object_id.decode("ascii")
        except (UnicodeDecodeError, ValueError) as exc:
            raise _invalid_git_identity("malformed index entry") from exc
        if stage == b"0":
            tracked[raw_relative] = (mode, decoded_object_id)
        else:
            unmerged.add(raw_relative)
    return tracked, unmerged


def _parse_tree_entries(entries: list[bytes]) -> dict[bytes, tuple[bytes, str]]:
    tree: dict[bytes, tuple[bytes, str]] = {}
    for raw_entry in (entry for entry in entries if entry):
        try:
            metadata, raw_relative = raw_entry.split(b"\t", 1)
            mode, object_type, object_id = metadata.split(b" ", 2)
            if object_type not in {b"blob", b"commit"}:
                raise ValueError("unexpected object type")
            tree[raw_relative] = (mode, object_id.decode("ascii"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise _invalid_git_identity("malformed HEAD tree entry") from exc
    return tree


def _git_source_snapshot(root: Path) -> tuple[str, bool] | None:
    root = root.resolve()
    environment = isolated_git_environment(work_tree=root)
    try:
        repository = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "rev-parse", "--is-inside-work-tree"],
            cwd=root,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if repository.returncode != 0 or repository.stdout.strip() != "true":
        return None
    try:
        head = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "rev-parse", "--verify", "HEAD"],
            cwd=root,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        ).stdout.strip()
        listed = subprocess.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "ls-files",
                "--cached",
                "--others",
                "-z",
            ],
            cwd=root,
            env=environment,
            capture_output=True,
            check=True,
            timeout=10,
        ).stdout.split(b"\0")
        visible_untracked = set(
            subprocess.run(
                [
                    "git",
                    "-c",
                    "core.fsmonitor=false",
                    "ls-files",
                    "--others",
                    "--exclude-standard",
                    "-z",
                ],
                cwd=root,
                env=environment,
                capture_output=True,
                check=True,
                timeout=10,
            ).stdout.split(b"\0")
        )
        staged_raw = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "ls-files", "--stage", "-z"],
            cwd=root,
            env=environment,
            capture_output=True,
            check=True,
            timeout=10,
        ).stdout.split(b"\0")
        object_format = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "rev-parse", "--show-object-format"],
            cwd=root,
            env=environment,
            text=True,
            capture_output=True,
            check=True,
            timeout=10,
        ).stdout.strip()
        head_raw = (
            subprocess.run(
                ["git", "-c", "core.fsmonitor=false", "ls-tree", "-r", "-z", "HEAD"],
                cwd=root,
                env=environment,
                capture_output=True,
                check=True,
                timeout=10,
            ).stdout.split(b"\0")
            if head
            else []
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise _invalid_git_identity("controlled Git inventory failed") from exc
    if object_format not in {"sha1", "sha256"}:
        raise _invalid_git_identity(f"unsupported object format {object_format!r}")

    tracked, unmerged = _parse_stage_entries(staged_raw)
    head_entries = _parse_tree_entries(head_raw)
    versioned_environment_roots = frozenset(
        parts[0]
        for raw_relative in set(tracked) | set(head_entries) | unmerged
        if (
            (parts := Path(
                raw_relative.decode("utf-8", errors="surrogateescape")
            ).parts)
            and parts[0] in SOURCE_ENVIRONMENT_ROOTS
        )
    )
    scoped_unmerged = {
        relative
        for relative in unmerged
        if not _source_path_excluded(
            relative.decode("utf-8", errors="surrogateescape"),
            versioned_environment_roots=versioned_environment_roots,
        )
    }
    if scoped_unmerged:
        raise _invalid_git_identity("unmerged source path")

    required_objects: set[str] = set()
    for raw_relative, (mode, object_id) in tracked.items():
        relative = raw_relative.decode("utf-8", errors="surrogateescape")
        if _source_path_excluded(
            relative,
            versioned_environment_roots=versioned_environment_roots,
        ):
            continue
        if mode not in {b"100644", b"100755"}:
            raise _invalid_git_identity(f"non-regular tracked source path: {relative}")
        required_objects.add(object_id)
    for raw_relative, (mode, object_id) in head_entries.items():
        relative = raw_relative.decode("utf-8", errors="surrogateescape")
        if _source_path_excluded(
            relative,
            versioned_environment_roots=versioned_environment_roots,
        ):
            continue
        if mode not in {b"100644", b"100755"}:
            raise _invalid_git_identity(f"non-regular HEAD source path: {relative}")
        required_objects.add(object_id)
    if not git_blob_objects_available(root, required_objects, environment=environment):
        raise _invalid_git_identity("required local Git object is unavailable")

    records: list[tuple[bytes, bytes, str]] = []
    actual_entries: dict[bytes, tuple[bytes, str]] = {}
    for raw_relative in sorted(relative for relative in listed if relative):
        relative = raw_relative.decode("utf-8", errors="surrogateescape")
        if _source_path_excluded(
            relative,
            versioned_environment_roots=versioned_environment_roots,
        ):
            continue
        path = root / relative
        tracked_entry = tracked.get(raw_relative)
        if path.is_symlink():
            raise _invalid_git_identity(f"symlink source path: {relative}")
        if path.exists() and not path.is_file():
            raise _invalid_git_identity(f"non-regular source path: {relative}")
        if not path.is_file():
            continue
        try:
            content = path.read_bytes()
            actual_mode = b"100755" if path.stat().st_mode & 0o111 else b"100644"
        except OSError as exc:
            raise _invalid_git_identity(f"unreadable source path: {relative}") from exc
        workspace_mode = (
            tracked_entry[0]
            if os.name == "nt" and tracked_entry is not None
            else actual_mode
        )
        content_sha256 = hashlib.sha256(content).hexdigest()
        records.append((raw_relative, workspace_mode, content_sha256))
        actual_entries[raw_relative] = (
            actual_mode,
            _git_blob_oid(content, object_format),
        )

    all_paths = set(head_entries) | set(tracked) | set(actual_entries) | scoped_unmerged
    dirty = False
    for raw_relative in all_paths:
        relative = raw_relative.decode("utf-8", errors="surrogateescape")
        if _source_path_excluded(
            relative,
            versioned_environment_roots=versioned_environment_roots,
        ):
            continue
        head_entry = head_entries.get(raw_relative)
        index_entry = tracked.get(raw_relative)
        actual_entry = actual_entries.get(raw_relative)
        if head_entry != index_entry:
            dirty = True
            break
        if index_entry is None:
            if actual_entry is not None and raw_relative in visible_untracked:
                dirty = True
                break
            continue
        if actual_entry is None:
            dirty = True
            break
        mode_changed = (
            os.name != "nt"
            and index_entry[0] in {b"100644", b"100755"}
            and actual_entry[0] != index_entry[0]
        )
        if actual_entry[1] != index_entry[1] or mode_changed:
            dirty = True
            break
    return framed_source_digest(records), dirty


def git_head_sha(root: Path) -> str | None:
    environment = isolated_git_environment(work_tree=root)
    try:
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "rev-parse", "--verify", "HEAD"],
            cwd=root,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None


def git_dirty(root: Path) -> bool | None:
    snapshot = _git_source_snapshot(root)
    return snapshot[1] if snapshot is not None else None


def git_source_tree_hash(root: Path) -> str | None:
    snapshot = _git_source_snapshot(root)
    return snapshot[0] if snapshot is not None else None


def content_source_tree_hash(root: Path) -> str:
    records: list[tuple[bytes, bytes, str]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if _source_path_excluded(relative):
            continue
        if path.is_symlink():
            raise RuntimeError(
                f"candidate content source identity is invalid: symlink source path: {relative}"
            )
        if path.is_dir():
            continue
        if not path.is_file():
            raise RuntimeError(
                "candidate content source identity is invalid: "
                f"non-regular source path: {relative}"
            )
        content = path.read_bytes()
        mode = (
            b"100755"
            if os.name != "nt" and path.stat().st_mode & 0o111
            else b"100644"
        )
        records.append(
            (
                relative.encode("utf-8", errors="surrogateescape"),
                mode,
                hashlib.sha256(content).hexdigest(),
            )
        )
    return f"content:{framed_source_digest(records)}"

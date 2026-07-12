#!/usr/bin/env python3
"""Shared helpers for Codex Project Harness runtime scripts."""

from __future__ import annotations

import hashlib
import subprocess
from datetime import datetime, timezone
from pathlib import Path


STATE_PATH = Path(".ai-team/control/project-state.yaml")
EVENT_PATH = Path(".ai-team/runtime/events.jsonl")
HARNESS_GIT_PREFIXES = (
    ".gitignore",
    ".ai-team/",
    ".codex/agents/",
    "docs/harness/",
)
CONTENT_HASH_EXCLUDE_PREFIXES = HARNESS_GIT_PREFIXES + (
    ".git/",
    "__pycache__/",
    ".pytest_cache/",
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


def write_state(root: Path, updates: dict[str, str]) -> dict[str, str]:
    state = read_state(root)
    state.update({key: str(value) for key, value in updates.items()})
    state.setdefault("status", "draft")
    state.setdefault("phase", "intake")
    state.setdefault("scope_status", "unconfirmed")
    state.setdefault("current_owner", "project-manager")
    state.setdefault("blocked_reason", "null")
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
    lines = [f"{key}: {state.get(key, '')}" for key in ordered_keys]
    for key in sorted(k for k in state if k not in ordered_keys):
        lines.append(f"{key}: {state[key]}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return state


def markdown_row(values: list[str]) -> str:
    safe_values = [str(value).replace("\n", " ").replace("|", "\\|") for value in values]
    return "| " + " | ".join(safe_values) + " |"


def git_head_sha(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip() or None


def git_dirty(root: Path) -> bool | None:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    for line in result.stdout.splitlines():
        relpath = line[3:] if len(line) > 3 else ""
        harness_path = any(relpath == prefix.rstrip("/") or relpath.startswith(prefix) for prefix in HARNESS_GIT_PREFIXES)
        if relpath and not harness_path:
            return True
    return False


def git_source_tree_hash(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    digest = hashlib.sha256()
    for relpath in sorted(path for path in result.stdout.splitlines() if path and not path.startswith(HARNESS_GIT_PREFIXES)):
        path = root / relpath
        if not path.exists() or not path.is_file():
            continue
        digest.update(relpath.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def content_source_tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        relpath = path.relative_to(root).as_posix()
        if relpath.startswith(CONTENT_HASH_EXCLUDE_PREFIXES):
            continue
        digest.update(relpath.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return f"content:{digest.hexdigest()}"

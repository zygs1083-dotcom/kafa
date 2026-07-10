#!/usr/bin/env python3
"""Shared helpers for Codex Project Harness runtime scripts."""

from __future__ import annotations

import json
import hashlib
import subprocess
import uuid
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


def append_event(root: Path, event_type: str, payload: dict[str, str]) -> None:
    path = root / EVENT_PATH
    ensure_parent(path)
    event = {
        "id": str(uuid.uuid4()),
        "schema_version": "2",
        "timestamp": now_iso(),
        "type": event_type,
        "source": "harness-runtime",
        "target": "project",
        "payload": payload,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def append_markdown(root: Path, relpath: str, content: str) -> None:
    path = root / relpath
    ensure_parent(path)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    separator = "" if existing.endswith("\n") or not existing else "\n"
    path.write_text(existing + separator + content.rstrip() + "\n", encoding="utf-8")


def markdown_row(values: list[str]) -> str:
    safe_values = [str(value).replace("\n", " ").replace("|", "\\|") for value in values]
    return "| " + " | ".join(safe_values) + " |"


def split_markdown_row(line: str) -> list[str]:
    return [cell.strip().replace("\\|", "|") for cell in line.strip().strip("|").split("|")]


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


def source_tree_hash_for_mode(root: Path, mode: str = "auto") -> str:
    if mode == "content-hash":
        return content_source_tree_hash(root)
    if mode == "git":
        return git_source_tree_hash(root) or ""
    if mode == "auto":
        return git_source_tree_hash(root) or ""
    raise ValueError(f"unknown code identity mode: {mode}")


def git_base_commit(root: Path) -> str | None:
    head = git_head_sha(root)
    if not head:
        return None
    try:
        upstream = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        result = subprocess.run(
            ["git", "merge-base", "HEAD", upstream],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() or head
    except (OSError, subprocess.CalledProcessError):
        return head


def git_tracked_diff_hash(root: Path) -> str | None:
    if not git_head_sha(root):
        return None
    try:
        result = subprocess.run(
            [
                "git",
                "diff",
                "--binary",
                "HEAD",
                "--",
                ".",
                ":(exclude).gitignore",
                ":(exclude).ai-team/**",
                ":(exclude).codex/agents/**",
                ":(exclude)docs/harness/**",
            ],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return hashlib.sha256(result.stdout.encode("utf-8")).hexdigest()


def append_table_row(root: Path, relpath: str, row: list[str], header: str) -> None:
    path = root / relpath
    ensure_parent(path)
    if not path.exists():
        path.write_text(header.rstrip() + "\n", encoding="utf-8")
    existing = path.read_text(encoding="utf-8")
    if header.splitlines()[0] not in existing:
        existing = existing.rstrip() + "\n\n" + header.rstrip() + "\n"
    path.write_text(existing.rstrip() + "\n" + markdown_row(row) + "\n", encoding="utf-8")


def replace_task_row(root: Path, task_id: str, updates: dict[str, str]) -> bool:
    path = root / ".ai-team/planning/task-board.md"
    if not path.exists():
        return False

    lines = path.read_text(encoding="utf-8").splitlines()
    changed = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = split_markdown_row(stripped)
        if len(cells) < 8 or cells[0] != task_id:
            continue
        fields = [
            "id",
            "task",
            "owner",
            "status",
            "acceptance",
            "failure_modes",
            "depends_on",
            "tool_link",
            "evidence",
        ]
        current = dict(zip(fields, cells, strict=False))
        current.update({key: value for key, value in updates.items() if value is not None})
        lines[index] = markdown_row(
            [
                current.get("id", task_id),
                current.get("task", ""),
                current.get("owner", ""),
                current.get("status", ""),
                current.get("acceptance", ""),
                current.get("failure_modes", ""),
                current.get("depends_on", ""),
                current.get("tool_link", ""),
                current.get("evidence", ""),
            ]
        )
        changed = True
        break

    if changed:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed

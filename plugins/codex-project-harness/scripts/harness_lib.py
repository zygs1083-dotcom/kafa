#!/usr/bin/env python3
"""Shared helpers for Codex Project Harness runtime scripts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


STATE_PATH = Path(".ai-team/control/project-state.yaml")
EVENT_PATH = Path(".ai-team/runtime/events.jsonl")


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
        "timestamp": now_iso(),
        "type": event_type,
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
        cells = [cell.strip().replace("\\|", "|") for cell in stripped.strip("|").split("|")]
        if len(cells) < 8 or cells[0] != task_id:
            continue
        fields = ["id", "task", "owner", "status", "acceptance", "depends_on", "tool_link", "evidence"]
        current = dict(zip(fields, cells, strict=False))
        current.update({key: value for key, value in updates.items() if value is not None})
        lines[index] = markdown_row(
            [
                current.get("id", task_id),
                current.get("task", ""),
                current.get("owner", ""),
                current.get("status", ""),
                current.get("acceptance", ""),
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

#!/usr/bin/env python3
"""Codex lifecycle hook dispatcher for Codex Project Harness.

Hooks are advisory lifecycle guardrails. They never create delivery evidence,
and strict enforcement remains in the harness runtime and delivery gates.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from typing import Any


EVENTS = {"SessionStart", "SubagentStart", "PreToolUse", "PostToolUse", "Stop"}
SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "API")
MUTATING_TOOLS = {"Bash", "apply_patch", "Edit", "Write"}
MAX_LINES = 12


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    event = argv[0] if argv else ""
    if event not in EVENTS:
        print("Codex Project Harness hook: unknown event")
        print(f"supported events: {', '.join(sorted(EVENTS))}")
        return 2

    plugin_root = Path(__file__).resolve().parents[1]
    repo_root = locate_repo_root()
    payload, payload_ok = read_payload()
    strict = os.environ.get("HARNESS_HOOK_STRICT") == "1"

    print(f"Codex Project Harness hook: {event}")
    print(f"repo: {repo_root}")

    if event == "SessionStart":
        return session_start(plugin_root, repo_root)
    if event == "SubagentStart":
        return subagent_start(payload, payload_ok)
    if event == "PreToolUse":
        return pre_tool_use(repo_root, payload, strict)
    if event == "PostToolUse":
        return post_tool_use(repo_root)
    if event == "Stop":
        return stop(plugin_root, repo_root, strict)
    return 0


def locate_repo_root() -> Path:
    env_root = os.environ.get("HARNESS_PROJECT_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip()).resolve()
    except OSError:
        pass
    return Path.cwd().resolve()


def read_payload() -> tuple[dict[str, Any], bool]:
    try:
        raw = sys.stdin.read()
    except OSError:
        return {}, False
    if not raw.strip():
        return {}, False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}, False
    return (data if isinstance(data, dict) else {}), isinstance(data, dict)


def session_start(plugin_root: Path, repo_root: Path) -> int:
    print("purpose: inject read-only project status; delivery gates remain in the harness runtime.")
    version = plugin_version(plugin_root)
    print(f"version: {version}")
    if not harness_db_exists(repo_root):
        print("harness status:")
        print("- not initialized in this project")
        print("dispatch status:")
        print("- not initialized in this project")
        return 0
    status = run_harness(plugin_root, repo_root, ["status"])
    print_block("harness status", status)
    dispatch = run_harness(plugin_root, repo_root, ["dispatch", "status"])
    print_block("dispatch status", dispatch, max_lines=6)
    return 0


def plugin_version(plugin_root: Path) -> str:
    manifest_path = plugin_root / ".codex-plugin" / "plugin.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "unknown"
    version = manifest.get("version") if isinstance(manifest, dict) else None
    return version.strip() if isinstance(version, str) and version.strip() else "unknown"


def subagent_start(payload: dict[str, Any], payload_ok: bool) -> int:
    agent_type = safe_scalar(payload.get("subagent_type") or payload.get("agent") or payload.get("type") or "unknown")
    print(f"subagent: {agent_type}")
    print(f"stdin: {'available' if payload_ok else 'unavailable'}")
    print("role boundary: stay inside the assigned role, task, acceptance criteria, file claims, and test target.")
    print("acceptance: return concrete files changed, validation run, remaining risk, and any blocker.")
    print("raw provider or worker reports are not trusted evidence until controller verification records them.")
    return 0


def pre_tool_use(repo_root: Path, payload: dict[str, Any], strict: bool) -> int:
    tool = safe_scalar(payload.get("tool_name") or payload.get("tool") or payload.get("name") or "unknown")
    warnings = []
    if tool in MUTATING_TOOLS or tool == "unknown":
        project = project_state(repo_root)
        if project and project.get("scope_status") != "confirmed":
            warnings.append(f"scope is not confirmed ({project.get('scope_status')}); freeze requirements before broad writes.")
        if project and project.get("phase") in {"intake", "baseline"}:
            warnings.append(f"project phase is {project.get('phase')}; prefer baseline/task setup before edits.")
        if active_task_count(repo_root) == 0:
            warnings.append("no active task/assignment detected; bind writes to a task, claim, and validation target.")
        if changed_files(repo_root):
            warnings.append("working tree already has changes; avoid mixing unrelated work.")

    if warnings:
        print(f"tool: {tool}")
        for warning in warnings:
            print(f"warning: {warning}")
        if strict:
            print("strict mode: blocking clear harness guardrail violation.")
            return 1
    else:
        print(f"tool: {tool}")
        print("OK: no clear harness pre-tool warning detected.")
    return 0


def post_tool_use(repo_root: Path) -> int:
    files = changed_files(repo_root)
    print("git status:")
    if files:
        for line in files[:MAX_LINES]:
            print(f"- {line}")
        if len(files) > MAX_LINES:
            print(f"- ... {len(files) - MAX_LINES} more")
    else:
        print("- clean or unavailable")
    print("next: record validation/evidence through harness commands only after controller verification or trusted test execution.")
    return 0


def stop(plugin_root: Path, repo_root: Path, strict: bool) -> int:
    if not harness_db_exists(repo_root):
        print("readiness command: skipped")
        print("readiness result:")
        print("- harness is not initialized in this project")
        return 0
    delivery = os.environ.get("HARNESS_HOOK_DELIVERY") == "1"
    args = ["validate", "--delivery"] if delivery else ["validate"]
    print(f"readiness command: harness {' '.join(args)}")
    result = run_harness(plugin_root, repo_root, args, check=False)
    print_block("readiness result", result)
    if result.returncode != 0:
        print("validation failed; hook is warn-only unless HARNESS_HOOK_STRICT=1.")
        if strict:
            print("strict mode: blocking stop on failed harness validation.")
            return result.returncode or 1
    return 0


def run_harness(plugin_root: Path, repo_root: Path, args: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
    script = plugin_root / "scripts" / "harness.py"
    return subprocess.run(
        ["python3", str(script), "--root", str(repo_root), *args],
        text=True,
        capture_output=True,
        check=check,
    )


def harness_db_exists(repo_root: Path) -> bool:
    return (repo_root / ".ai-team" / "state" / "harness.db").exists()


def print_block(title: str, result: subprocess.CompletedProcess[str], *, max_lines: int = MAX_LINES) -> None:
    print(f"{title}:")
    text = (result.stdout or result.stderr or "").strip()
    if not text:
        print("- no output")
        return
    for line in text.splitlines()[:max_lines]:
        print(sanitize_line(line))
    remaining = len(text.splitlines()) - max_lines
    if remaining > 0:
        print(f"... {remaining} more line(s)")


def project_state(repo_root: Path) -> dict[str, str]:
    db = repo_root / ".ai-team" / "state" / "harness.db"
    if not db.exists():
        return {}
    try:
        with closing(sqlite3.connect(db)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("select phase, status, scope_status from project where id = 1").fetchone()
            return dict(row) if row else {}
    except sqlite3.Error:
        return {}


def active_task_count(repo_root: Path) -> int:
    db = repo_root / ".ai-team" / "state" / "harness.db"
    if not db.exists():
        return 0
    try:
        with closing(sqlite3.connect(db)) as conn:
            task_count = conn.execute(
                "select count(*) from tasks where status in ('ready', 'claimed', 'in_progress', 'submitted', 'reviewed')"
            ).fetchone()[0]
            assignment_count = conn.execute(
                "select count(*) from dispatch_assignments where status in ('planned', 'claimed', 'reported', 'completed')"
            ).fetchone()[0]
            return int(task_count) + int(assignment_count)
    except sqlite3.Error:
        return 0


def changed_files(repo_root: Path) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "status", "--short"],
            cwd=repo_root,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        return []
    if result.returncode != 0:
        return []
    return [sanitize_line(line) for line in result.stdout.splitlines() if line.strip()]


def read_text(path: Path) -> str:
    try:
        return path.resolve().read_text(encoding="utf-8")
    except OSError:
        return ""


def safe_scalar(value: object) -> str:
    if not isinstance(value, (str, int, float, bool)):
        return "unknown"
    text = str(value)
    if any(marker in text.upper() for marker in SECRET_MARKERS):
        return "[redacted]"
    return text[:80]


def sanitize_line(line: str) -> str:
    words = []
    for word in line.split():
        upper = word.upper()
        if any(marker in upper for marker in SECRET_MARKERS):
            words.append("[redacted]")
        else:
            words.append(word)
    sanitized = " ".join(words)
    return sanitized[:240]


if __name__ == "__main__":
    raise SystemExit(main())

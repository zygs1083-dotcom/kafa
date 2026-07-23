#!/usr/bin/env python3
"""Warn-only Native Codex lifecycle hooks for the local Kafa Kernel.

Hooks are advisory and never create delivery facts or evidence. Runtime
preconditions and delivery gates remain the enforcement boundary.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "API")
MAX_LINES = 12


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    plugin_root = Path(__file__).resolve().parents[1]
    try:
        events = supported_events(plugin_root)
    except (OSError, ValueError) as exc:
        print(f"Codex Project Harness hook: invalid distribution manifest: {exc}")
        return 2
    event = argv[0] if argv else ""
    if event not in events:
        print("Codex Project Harness hook: unknown event")
        print(f"supported events: {', '.join(sorted(events))}")
        return 2

    repo_root = locate_repo_root()
    if event == "Stop":
        return stop(plugin_root, repo_root)

    print(f"Codex Project Harness hook: {event}")
    print(f"repo: {repo_root}")
    if event == "SessionStart":
        return session_start(plugin_root, repo_root)
    status = run_harness(plugin_root, repo_root, ["status"])
    if any(
        marker in status.stdout
        for marker in ("state: recovery-required", "state: error")
    ):
        print_block("harness status", status)
        return 0
    if not harness_db_exists(repo_root):
        print("skipped: harness is not initialized in this project")
        return 0

    payload, payload_ok = read_payload()
    return subagent_start(payload, payload_ok)


def supported_events(plugin_root: Path) -> frozenset[str]:
    scripts_root = plugin_root / "scripts"
    if str(scripts_root) not in sys.path:
        sys.path.insert(0, str(scripts_root))
    from harness_lib import DistributionManifestError, load_distribution_manifest

    try:
        distribution = load_distribution_manifest(plugin_root)
    except DistributionManifestError as exc:
        raise ValueError(str(exc)) from exc
    return frozenset(distribution["hooks"]["events"])


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
    except OSError:
        result = None
    if result is not None and result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip()).resolve()
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
    print("purpose: inject read-only local status; delivery gates remain in the Kafa Kernel.")
    print(f"version: {plugin_version(plugin_root)}")
    print_block("harness status", run_harness(plugin_root, repo_root, ["status"]))
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
    agent_type = safe_scalar(
        payload.get("subagent_type")
        or payload.get("agent")
        or payload.get("type")
        or "unknown"
    )
    print(f"subagent: {agent_type}")
    print(f"stdin: {'available' if payload_ok else 'unavailable'}")
    print("role boundary: stay inside the assigned task, files, acceptance, and tests.")
    print("return: changed files, commands, results, risks, and blockers to the root controller.")
    print("trust: subagents do not write Kafa facts or fabricate evidence; the root controller verifies results.")
    return 0


def stop(plugin_root: Path, repo_root: Path) -> int:
    lines = [
        "Codex Project Harness hook: Stop",
        f"repo: {repo_root}",
    ]
    if not harness_db_exists(repo_root):
        status = run_harness(plugin_root, repo_root, ["status"])
        lines.extend(
            [
                "readiness command: skipped",
                *block_lines("project state", status),
            ]
        )
        print(stop_output(lines))
        return 0

    delivery = os.environ.get("HARNESS_HOOK_DELIVERY") == "1"
    args = ["validate", "--delivery"] if delivery else ["validate"]
    lines.append(f"readiness command: harness {' '.join(args)}")
    result = run_harness(plugin_root, repo_root, args)
    lines.extend(block_lines("readiness result", result))
    if result.returncode != 0:
        lines.append("validation failed; Stop is warn-only and does not create or alter delivery facts.")
    print(stop_output(lines))
    return 0


def stop_output(lines: list[str]) -> str:
    return json.dumps(
        {
            "continue": True,
            "systemMessage": "\n".join(lines),
            "suppressOutput": False,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def run_harness(
    plugin_root: Path,
    repo_root: Path,
    args: list[str],
) -> subprocess.CompletedProcess[str]:
    script = plugin_root / "scripts" / "harness.py"
    return subprocess.run(
        [sys.executable, str(script), "--root", str(repo_root), *args],
        text=True,
        capture_output=True,
        check=False,
    )


def harness_db_exists(repo_root: Path) -> bool:
    return (repo_root / ".ai-team" / "state" / "harness.db").is_file()


def print_block(
    title: str,
    result: subprocess.CompletedProcess[str],
    *,
    max_lines: int = MAX_LINES,
) -> None:
    for line in block_lines(title, result, max_lines=max_lines):
        print(line)


def block_lines(
    title: str,
    result: subprocess.CompletedProcess[str],
    *,
    max_lines: int = MAX_LINES,
) -> list[str]:
    lines = [f"{title}:"]
    text = (result.stdout or result.stderr or "").strip()
    if not text:
        lines.append("- no output")
        return lines
    source_lines = text.splitlines()
    lines.extend(sanitize_line(line) for line in source_lines[:max_lines])
    if len(source_lines) > max_lines:
        lines.append(f"... {len(source_lines) - max_lines} more line(s)")
    return lines


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
        words.append("[redacted]" if any(marker in upper for marker in SECRET_MARKERS) else word)
    return " ".join(words)[:240]


if __name__ == "__main__":
    raise SystemExit(main())

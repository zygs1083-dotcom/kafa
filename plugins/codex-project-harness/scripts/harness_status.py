#!/usr/bin/env python3
"""Print the current Codex Project Harness state."""

from __future__ import annotations

from pathlib import Path

from harness_lib import EVENT_PATH, read_state


def count_table_rows(path: Path) -> int:
    if not path.exists():
        return 0
    rows = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        lower = stripped.lower()
        if (
            stripped.startswith("|")
            and "---" not in stripped
            and not lower.startswith("| id ")
            and not lower.startswith("| surface ")
        ):
            rows += 1
    return rows


def main() -> int:
    root = Path.cwd()
    state = read_state(root)
    print("# Harness Status")
    print(f"status: {state.get('status', 'unknown')}")
    print(f"phase: {state.get('phase', 'unknown')}")
    print(f"scope_status: {state.get('scope_status', 'unknown')}")
    print(f"current_owner: {state.get('current_owner', 'unknown')}")
    print(f"blocked_reason: {state.get('blocked_reason', 'null')}")
    print(f"tasks: {count_table_rows(root / '.ai-team/planning/task-board.md')}")
    print(f"acceptance_criteria: {count_table_rows(root / '.ai-team/requirements/acceptance.md')}")
    print(f"validation_rows: {count_table_rows(root / 'docs/harness/validation.md')}")
    event_path = root / EVENT_PATH
    if event_path.exists():
        print(f"events: {len(event_path.read_text(encoding='utf-8').splitlines())}")
    else:
        print("events: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

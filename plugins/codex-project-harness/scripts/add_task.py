#!/usr/bin/env python3
"""Append a task to the harness task board."""

from __future__ import annotations

import argparse
from pathlib import Path

from harness_lib import append_event, append_table_row


HEADER = """# Task Board

| ID | Task | Owner | Status | Acceptance | Failure Modes | Depends On | Tool Link | Evidence |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--owner", default="unassigned")
    parser.add_argument("--status", default="planned")
    parser.add_argument("--acceptance", default="")
    parser.add_argument("--failure-mode", action="append", default=[])
    parser.add_argument("--depends-on", default="")
    parser.add_argument("--tool-link", default="")
    parser.add_argument("--evidence", default="")
    args = parser.parse_args()

    root = Path.cwd()
    append_table_row(
        root,
        ".ai-team/planning/task-board.md",
        [
            args.id,
            args.task,
            args.owner,
            args.status,
            args.acceptance,
            ", ".join(args.failure_mode),
            args.depends_on,
            args.tool_link,
            args.evidence,
        ],
        HEADER,
    )
    append_event(root, "task_created", {"id": args.id, "task": args.task, "owner": args.owner})
    print(f"OK: task added {args.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

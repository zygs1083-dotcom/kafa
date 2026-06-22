#!/usr/bin/env python3
"""Update a task row in the harness task board."""

from __future__ import annotations

import argparse
from pathlib import Path

from harness_lib import append_event, replace_task_row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", required=True)
    parser.add_argument("--task")
    parser.add_argument("--owner")
    parser.add_argument("--status")
    parser.add_argument("--acceptance")
    parser.add_argument("--depends-on")
    parser.add_argument("--tool-link")
    parser.add_argument("--evidence")
    args = parser.parse_args()

    updates = {
        "task": args.task,
        "owner": args.owner,
        "status": args.status,
        "acceptance": args.acceptance,
        "depends_on": args.depends_on,
        "tool_link": args.tool_link,
        "evidence": args.evidence,
    }
    root = Path.cwd()
    if not replace_task_row(root, args.id, updates):
        print(f"ERROR: task not found: {args.id}")
        return 1
    append_event(root, "task_updated", {"id": args.id, **{k: v for k, v in updates.items() if v}})
    print(f"OK: task updated {args.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

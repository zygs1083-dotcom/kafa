#!/usr/bin/env python3
"""Append an acceptance criterion to the harness acceptance table."""

from __future__ import annotations

import argparse
from pathlib import Path

from harness_lib import append_event, append_table_row


HEADER = """# Acceptance Criteria

| ID | Criterion | Priority | Tool Link | Status |
| --- | --- | --- | --- | --- |"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", required=True)
    parser.add_argument("--criterion", required=True)
    parser.add_argument("--priority", default="must")
    parser.add_argument("--tool-link", default="")
    parser.add_argument("--status", default="planned")
    args = parser.parse_args()

    root = Path.cwd()
    append_table_row(
        root,
        ".ai-team/requirements/acceptance.md",
        [args.id, args.criterion, args.priority, args.tool_link, args.status],
        HEADER,
    )
    append_event(root, "acceptance_added", {"id": args.id, "criterion": args.criterion})
    print(f"OK: acceptance added {args.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

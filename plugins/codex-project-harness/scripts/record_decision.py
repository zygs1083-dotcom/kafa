#!/usr/bin/env python3
"""Append a decision to the harness decision log."""

from __future__ import annotations

import argparse
from pathlib import Path

from harness_lib import append_event, append_table_row, now_iso


HEADER = """# Decision Log

| Date | Decision | Reason |
| --- | --- | --- |"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decision", required=True)
    parser.add_argument("--reason", required=True)
    args = parser.parse_args()

    root = Path.cwd()
    date = now_iso()
    append_table_row(root, ".ai-team/control/decision-log.md", [date, args.decision, args.reason], HEADER)
    append_event(root, "decision_recorded", {"decision": args.decision, "reason": args.reason})
    print("OK: decision recorded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

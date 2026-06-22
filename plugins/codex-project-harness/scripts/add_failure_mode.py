#!/usr/bin/env python3
"""Append a failure mode to the harness failure-mode matrix."""

from __future__ import annotations

import argparse
from pathlib import Path

from harness_lib import append_event, append_table_row


HEADER = """# Failure Modes

| ID | Feature | Scenario | Trigger | Expected Behavior | Recovery | Data Safety | Risk | Test Mapping | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", required=True)
    parser.add_argument("--feature", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--trigger", required=True)
    parser.add_argument("--expected", required=True)
    parser.add_argument("--recovery", default="")
    parser.add_argument("--data-safety", default="")
    parser.add_argument("--risk", choices=["low", "medium", "high", "critical"], default="medium")
    parser.add_argument("--test-mapping", default="")
    parser.add_argument("--status", choices=["identified", "covered", "accepted", "exempt"], default="identified")
    args = parser.parse_args()

    root = Path.cwd()
    append_table_row(
        root,
        ".ai-team/requirements/failure-modes.md",
        [
            args.id,
            args.feature,
            args.scenario,
            args.trigger,
            args.expected,
            args.recovery,
            args.data_safety,
            args.risk,
            args.test_mapping,
            args.status,
        ],
        HEADER,
    )
    append_event(root, "failure_mode_added", {"id": args.id, "feature": args.feature, "risk": args.risk})
    print(f"OK: failure mode added {args.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

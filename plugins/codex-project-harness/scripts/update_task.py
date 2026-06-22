#!/usr/bin/env python3
"""Compatibility wrapper for `harness.py task update`."""

from __future__ import annotations

import argparse
import sys

from harness_wrapper import run_harness


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", required=True)
    parser.add_argument("--task")
    parser.add_argument("--owner")
    parser.add_argument("--status")
    parser.add_argument("--acceptance")
    parser.add_argument("--failure-modes")
    parser.add_argument("--depends-on")
    parser.add_argument("--tool-link")
    parser.add_argument("--evidence")
    args = parser.parse_args()
    unsupported = []
    for name in ["task", "owner", "acceptance", "failure_modes", "tool_link"]:
        if getattr(args, name):
            unsupported.append("--" + name.replace("_", "-"))
    if unsupported:
        print(
            "ERROR: update_task.py only updates status and dependencies; use harness.py task add/complete/block for "
            + ", ".join(unsupported),
            file=sys.stderr,
        )
        return 2
    if args.status == "accepted":
        if not args.evidence:
            print("ERROR: accepted tasks require --evidence and must use task complete", file=sys.stderr)
            return 2
        return run_harness(["task", "complete", args.id, "--evidence", args.evidence])
    if args.status == "blocked":
        return run_harness(["task", "block", args.id, "--reason", args.evidence or "blocked"])
    command = ["task", "update", args.id]
    if args.status:
        command.extend(["--status", args.status])
    if args.depends_on is not None:
        command.extend(["--depends-on", args.depends_on])
    return run_harness(command)


if __name__ == "__main__":
    raise SystemExit(main())

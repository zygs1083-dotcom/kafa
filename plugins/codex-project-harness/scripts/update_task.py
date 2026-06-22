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
            "ERROR: update_task.py only updates non-terminal status and dependencies; use harness.py task submit/review/accept/block for "
            + ", ".join(unsupported),
            file=sys.stderr,
        )
        return 2
    if args.status == "accepted":
        print("ERROR: accepted tasks must use harness.py task review followed by task accept", file=sys.stderr)
        return 2
    if args.status == "blocked":
        print("ERROR: blocked tasks must use harness.py task block with lease token and expected revision", file=sys.stderr)
        return 2
    command = ["task", "update", args.id]
    if args.status:
        command.extend(["--status", args.status])
    if args.depends_on is not None:
        command.extend(["--depends-on", args.depends_on])
    return run_harness(command)


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Compatibility wrapper for `harness.py task add`."""

from __future__ import annotations

import argparse

from harness_wrapper import run_harness


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--owner", default="unassigned")
    parser.add_argument("--status", default="ready")
    parser.add_argument("--acceptance", default="")
    parser.add_argument("--failure-mode", action="append", default=[])
    parser.add_argument("--depends-on", default="")
    parser.add_argument("--tool-link", default="")
    parser.add_argument("--evidence", default="")
    args = parser.parse_args()
    command = [
        "task",
        "add",
        "--id",
        args.id,
        "--task",
        args.task,
        "--owner",
        args.owner,
        "--status",
        args.status,
        "--acceptance",
        args.acceptance,
        "--depends-on",
        args.depends_on,
        "--tool-link",
        args.tool_link,
        "--evidence",
        args.evidence,
    ]
    for failure_mode in args.failure_mode:
        command.extend(["--failure-mode", failure_mode])
    return run_harness(command)


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Compatibility wrapper for `harness.py validation record`."""

from __future__ import annotations

import argparse

from harness_wrapper import run_harness


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--surface", required=True)
    parser.add_argument("--acceptance", default="")
    parser.add_argument("--tool-context", default="")
    parser.add_argument("--commands", default="")
    parser.add_argument("--findings", required=True)
    parser.add_argument("--result", choices=["pass", "fail", "blocked", "partial"], required=True)
    parser.add_argument("--risk", default="")
    parser.add_argument("--failure-mode", action="append", default=[])
    parser.add_argument("--test", action="append", default=[])
    parser.add_argument("--evidence", action="append", default=[])
    args = parser.parse_args()
    command = [
        "validation",
        "record",
        "--surface",
        args.surface,
        "--acceptance",
        args.acceptance,
        "--commands",
        args.commands,
        "--findings",
        args.findings,
        "--result",
        args.result,
        "--risk",
        args.risk,
    ]
    for failure_mode in args.failure_mode:
        command.extend(["--failure-mode", failure_mode])
    for test in args.test:
        command.extend(["--test", test])
    for evidence in args.evidence:
        command.extend(["--evidence", evidence])
    return run_harness(command)


if __name__ == "__main__":
    raise SystemExit(main())

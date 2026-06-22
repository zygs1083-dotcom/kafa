#!/usr/bin/env python3
"""Compatibility wrapper for `harness.py phase`."""

from __future__ import annotations

import argparse

from harness_wrapper import run_harness


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase")
    parser.add_argument("--status", default=None)
    parser.add_argument("--owner", default=None)
    parser.add_argument("--scope-status", default=None)
    parser.add_argument("--blocked-reason", default=None)
    args = parser.parse_args()
    command = ["phase", args.phase]
    if args.status is not None:
        command.extend(["--status", args.status])
    if args.owner is not None:
        command.extend(["--owner", args.owner])
    return run_harness(command)


if __name__ == "__main__":
    raise SystemExit(main())

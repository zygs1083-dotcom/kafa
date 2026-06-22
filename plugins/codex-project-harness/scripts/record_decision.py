#!/usr/bin/env python3
"""Compatibility wrapper for `harness.py decision record`."""

from __future__ import annotations

import argparse

from harness_wrapper import run_harness


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decision", required=True)
    parser.add_argument("--reason", required=True)
    args = parser.parse_args()
    return run_harness(["decision", "record", "--decision", args.decision, "--reason", args.reason])


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Compatibility wrapper for `harness.py validate --delivery`."""

from __future__ import annotations

from harness_wrapper import run_harness


def main() -> int:
    return run_harness(["validate", "--delivery"])


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Compatibility wrapper for `harness.py status`."""

from __future__ import annotations

from harness_wrapper import run_harness


def main() -> int:
    return run_harness(["status"])


if __name__ == "__main__":
    raise SystemExit(main())

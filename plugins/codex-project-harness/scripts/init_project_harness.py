#!/usr/bin/env python3
"""Compatibility wrapper for `harness.py init`."""

from __future__ import annotations

from harness_wrapper import run_harness


def main() -> int:
    return run_harness(["init"])


if __name__ == "__main__":
    raise SystemExit(main())

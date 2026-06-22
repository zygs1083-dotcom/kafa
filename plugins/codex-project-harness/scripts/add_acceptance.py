#!/usr/bin/env python3
"""Compatibility wrapper for `harness.py acceptance add`."""

from __future__ import annotations

import argparse

from harness_wrapper import run_harness


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", required=True)
    parser.add_argument("--criterion", required=True)
    parser.add_argument("--priority", default="must")
    parser.add_argument("--tool-link", default="")
    parser.add_argument("--status", default="planned")
    args = parser.parse_args()
    return run_harness(
        [
            "acceptance",
            "add",
            "--id",
            args.id,
            "--criterion",
            args.criterion,
            "--priority",
            args.priority,
            "--tool-link",
            args.tool_link,
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())

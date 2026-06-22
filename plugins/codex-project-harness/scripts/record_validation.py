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
    args = parser.parse_args()
    return run_harness(
        [
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
    )


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Compatibility wrapper for `harness.py failure-mode add`."""

from __future__ import annotations

import argparse

from harness_wrapper import run_harness


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", required=True)
    parser.add_argument("--feature", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--trigger", required=True)
    parser.add_argument("--expected", required=True)
    parser.add_argument("--recovery", default="")
    parser.add_argument("--data-safety", default="")
    parser.add_argument("--risk", choices=["low", "medium", "high", "critical"], default="medium")
    parser.add_argument("--test-mapping", default="")
    parser.add_argument("--status", choices=["identified", "accepted", "exempt"], default="identified")
    parser.add_argument("--accepted-by", default="")
    parser.add_argument("--acceptance-reason", default="")
    parser.add_argument("--acceptance-scope", default="")
    parser.add_argument("--expires-at", default="")
    args = parser.parse_args()
    command = [
        "failure-mode",
        "add",
        "--id",
        args.id,
        "--feature",
        args.feature,
        "--scenario",
        args.scenario,
        "--trigger",
        args.trigger,
        "--expected",
        args.expected,
        "--risk",
        args.risk,
        "--status",
        args.status,
        "--recovery",
        args.recovery,
        "--data-safety",
        args.data_safety,
    ]
    for flag, value in [
        ("--accepted-by", args.accepted_by),
        ("--acceptance-reason", args.acceptance_reason),
        ("--acceptance-scope", args.acceptance_scope),
        ("--expires-at", args.expires_at),
    ]:
        if value:
            command.extend([flag, value])
    if args.test_mapping:
        command.extend(["--acceptance", args.test_mapping])
    return run_harness(command)


if __name__ == "__main__":
    raise SystemExit(main())

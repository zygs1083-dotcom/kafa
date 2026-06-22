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
    parser.add_argument("--command", default="")
    parser.add_argument("--exit-code", type=int)
    parser.add_argument("--stdout-sha256", default="")
    parser.add_argument("--artifact-path", default="")
    parser.add_argument("--target", default="")
    parser.add_argument("--executed-count", type=int)
    parser.add_argument("--allow-unlisted", action="store_true")
    parser.add_argument("--no-network", action="store_true")
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
    if args.command:
        command.extend(["--command", args.command])
    if args.exit_code is not None:
        command.extend(["--exit-code", str(args.exit_code)])
    if args.stdout_sha256:
        command.extend(["--stdout-sha256", args.stdout_sha256])
    if args.artifact_path:
        command.extend(["--artifact-path", args.artifact_path])
    if args.target:
        command.extend(["--target", args.target])
    if args.executed_count is not None:
        command.extend(["--executed-count", str(args.executed_count)])
    if args.allow_unlisted:
        command.append("--allow-unlisted")
    if args.no_network:
        command.append("--no-network")
    for failure_mode in args.failure_mode:
        command.extend(["--failure-mode", failure_mode])
    for test in args.test:
        command.extend(["--test", test])
    for evidence in args.evidence:
        command.extend(["--evidence", evidence])
    return run_harness(command)


if __name__ == "__main__":
    raise SystemExit(main())

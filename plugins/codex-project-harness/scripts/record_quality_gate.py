#!/usr/bin/env python3
"""Compatibility wrapper for `harness.py gate record`."""

from __future__ import annotations

import argparse

from harness_wrapper import run_harness


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate", default="independent_qa")
    parser.add_argument("--commit", default="auto")
    parser.add_argument("--reviewer-context", choices=["fresh", "same-context-degraded", "external"], required=True)
    parser.add_argument("--result", choices=["pass", "fail", "conditional", "blocked"], required=True)
    parser.add_argument("--blocking-findings", default="")
    parser.add_argument("--commands", default="")
    parser.add_argument("--evidence", default="")
    parser.add_argument("--residual-risk", default="")
    parser.add_argument("--reviewer-session-id", default="")
    parser.add_argument("--reviewer-attestation-id", default="")
    args = parser.parse_args()
    command = [
        "gate",
        "record",
        "--gate",
        args.gate,
        "--reviewer-context",
        args.reviewer_context,
        "--result",
        args.result,
        "--blocking-findings",
        args.blocking_findings,
        "--commands",
        args.commands,
        "--evidence",
        args.evidence,
        "--residual-risk",
        args.residual_risk,
    ]
    if args.reviewer_session_id:
        command.extend(["--reviewer-session-id", args.reviewer_session_id])
    if args.reviewer_attestation_id:
        command.extend(["--reviewer-attestation-id", args.reviewer_attestation_id])
    return run_harness(command)


if __name__ == "__main__":
    raise SystemExit(main())

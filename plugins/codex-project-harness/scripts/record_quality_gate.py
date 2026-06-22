#!/usr/bin/env python3
"""Append an independent QA gate decision."""

from __future__ import annotations

import argparse
from pathlib import Path

from harness_lib import append_event, append_table_row, git_dirty, git_head_sha


HEADER = """# Quality Gates

| Gate | Commit | Reviewer Context | Result | Blocking Findings | Commands | Evidence | Residual Risk |
| --- | --- | --- | --- | --- | --- | --- | --- |"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate", default="independent_qa")
    parser.add_argument("--commit", default="auto")
    parser.add_argument(
        "--reviewer-context",
        choices=["fresh", "same-context-degraded", "external"],
        required=True,
    )
    parser.add_argument("--result", choices=["pass", "fail", "conditional", "blocked"], required=True)
    parser.add_argument("--blocking-findings", default="")
    parser.add_argument("--commands", default="")
    parser.add_argument("--evidence", default="")
    parser.add_argument("--residual-risk", default="")
    args = parser.parse_args()

    root = Path.cwd()
    commit = args.commit
    if commit in {"auto", "HEAD"}:
        commit = git_head_sha(root) or "no-git"
    dirty = git_dirty(root)
    if args.result == "pass" and dirty:
        print("ERROR: cannot record a passing quality gate with a dirty git worktree")
        return 1

    append_table_row(
        root,
        "docs/harness/quality-gates.md",
        [
            args.gate,
            commit,
            args.reviewer_context,
            args.result,
            args.blocking_findings,
            args.commands,
            args.evidence,
            args.residual_risk,
        ],
        HEADER,
    )
    append_event(
        root,
        "quality_gate_recorded",
        {"gate": args.gate, "commit": commit, "result": args.result},
    )
    print(f"OK: quality gate recorded {args.gate}={args.result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

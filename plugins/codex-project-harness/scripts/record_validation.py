#!/usr/bin/env python3
"""Append QA or validation evidence to docs/harness/validation.md."""

from __future__ import annotations

import argparse
from pathlib import Path

from harness_lib import append_event, append_table_row


HEADER = """# Validation

| Surface | Acceptance | Tool Context | Commands | Findings | Pass/Fail | Residual Risk |
| --- | --- | --- | --- | --- | --- | --- |"""


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

    root = Path.cwd()
    append_table_row(
        root,
        "docs/harness/validation.md",
        [
            args.surface,
            args.acceptance,
            args.tool_context,
            args.commands,
            args.findings,
            args.result,
            args.risk,
        ],
        HEADER,
    )
    append_event(root, "validation_recorded", {"surface": args.surface, "result": args.result})
    print("OK: validation recorded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

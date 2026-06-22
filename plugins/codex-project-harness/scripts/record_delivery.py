#!/usr/bin/env python3
"""Compatibility wrapper for `harness.py delivery record`."""

from __future__ import annotations

import argparse

from harness_wrapper import run_harness


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--acceptance", default="")
    parser.add_argument("--changed-files", default="")
    parser.add_argument("--validation", default="")
    parser.add_argument("--qa", default="")
    parser.add_argument("--collaboration-links", default="")
    parser.add_argument("--failure-mode-coverage", default="")
    parser.add_argument("--quality-gate", default="")
    parser.add_argument("--data-config-notes", default="")
    parser.add_argument("--known-gaps", default="")
    parser.add_argument("--handoff", default="")
    args = parser.parse_args()
    return run_harness(
        [
            "delivery",
            "record",
            "--scope",
            args.scope,
            "--acceptance",
            args.acceptance,
            "--changed-files",
            args.changed_files,
            "--validation",
            args.validation,
            "--qa",
            args.qa,
            "--collaboration-links",
            args.collaboration_links,
            "--failure-mode-coverage",
            args.failure_mode_coverage,
            "--quality-gate",
            args.quality_gate,
            "--data-config-notes",
            args.data_config_notes,
            "--known-gaps",
            args.known_gaps,
            "--handoff",
            args.handoff,
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())

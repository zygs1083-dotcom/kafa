#!/usr/bin/env python3
"""Validate fresh-session skill flow evidence without requiring a Codex service."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "docs" / "runtime" / "skill-eval-transcript-fixture.txt"

REQUIRED_MARKERS = [
    "harness.py --root . init",
    "harness.py --root . phase project_bootstrap",
    "harness.py --root . phase requirement_baseline",
    "harness.py --root . scope confirm",
    "harness.py --root . baseline freeze",
    "harness.py --root . requirement link",
    "harness.py --root . task add",
    "harness.py --root . test-target add",
    "--stdout-sha256",
    "--artifact-path",
    "--target",
    "--executed-count",
    "harness.py --root . validation record",
    "harness.py --root . gate record",
    "harness.py --root . phase delivery_readiness",
    "harness.py --root . delivery record",
]


def transcript_text() -> str:
    command = os.environ.get("CODEX_EVAL_CMD", "").strip()
    if not command:
        return FIXTURE.read_text(encoding="utf-8")

    with tempfile.TemporaryDirectory() as temp:
        result = subprocess.run(
            command,
            cwd=temp,
            text=True,
            shell=True,
            capture_output=True,
            check=False,
            timeout=120,
        )
    return result.stdout + "\n" + result.stderr


def main() -> int:
    text = transcript_text()
    missing = [marker for marker in REQUIRED_MARKERS if marker not in text]
    if missing:
        for marker in missing:
            print(f"ERROR: missing skill eval marker: {marker}")
        return 1
    print(f"OK: skill eval transcript passed ({len(REQUIRED_MARKERS)} markers)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

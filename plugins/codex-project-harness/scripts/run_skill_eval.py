#!/usr/bin/env python3
"""Validate fresh-session skill flow evidence without requiring a Codex service."""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "docs" / "runtime" / "skill-eval-transcript-fixture.txt"

REQUIRED_MARKERS = [
    "harness.py --root . init",
    "harness.py --root . requirement link",
    "harness.py --root . baseline confirm",
    "harness.py --root . task add",
    "harness.py --root . task start",
    "harness.py --root . task submit",
    "--context-id producer-context",
    "harness.py --root . test-target add",
    "--result-format pytest-json",
    "--result-path .ai-team/results/unit.json",
    "harness.py --root . test-target link",
    "harness.py --root . test-target qualify",
    "harness.py --root . verify run",
    "harness.py --root . task accept",
    "harness.py --root . gate record",
    "--reviewer-context-id reviewer-context",
    "--qualification Q1",
    "harness.py --root . delivery ready",
    "harness.py --root . delivery record",
    "harness.py --root . validate --delivery",
    "Native Codex/ChatGPT owns",
    "human-review-required",
]

ORDERED_MARKERS = [
    "Native Codex/ChatGPT owns",
    "harness.py --root . init",
    "harness.py --root . requirement link",
    "harness.py --root . baseline confirm",
    "harness.py --root . task add",
    "harness.py --root . task start",
    "harness.py --root . task submit",
    "--context-id producer-context",
    "harness.py --root . test-target add",
    "--result-format pytest-json",
    "--result-path .ai-team/results/unit.json",
    "harness.py --root . test-target link",
    "harness.py --root . test-target qualify",
    "harness.py --root . verify run",
    "harness.py --root . task accept",
    "harness.py --root . gate record",
    "--reviewer-context-id reviewer-context",
    "--qualification Q1",
    "harness.py --root . delivery ready",
    "harness.py --root . delivery record",
    "harness.py --root . validate --delivery",
    "human-review-required",
]

FORBIDDEN_MARKERS = [
    "harness.py --root . phase ",
    "harness.py --root . scope ",
    "harness.py --root . session ",
    "harness.py --root . dispatch ",
    "harness.py --root . evidence ",
    "harness.py --root . test record",
    "--reviewer-session-id",
    "--reviewer-attestation-id",
]


def transcript_evidence() -> tuple[str, int, str]:
    command = os.environ.get("CODEX_EVAL_CMD", "").strip()
    if not command:
        return FIXTURE.read_text(encoding="utf-8"), 0, "fixture"

    try:
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
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return stdout + "\n" + stderr, 124, "host-command"
    return result.stdout + "\n" + result.stderr, result.returncode, "host-command"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate local-only fresh-session Skill transcript markers"
    )
    parser.parse_args(argv)

    text, command_returncode, source = transcript_evidence()
    missing = [marker for marker in REQUIRED_MARKERS if marker not in text]
    retired = [marker for marker in FORBIDDEN_MARKERS if marker in text]
    positions = [(marker, text.find(marker)) for marker in ORDERED_MARKERS]
    ordered_positions = [(marker, position) for marker, position in positions if position >= 0]
    out_of_order = [
        marker
        for index, (marker, position) in enumerate(ordered_positions)
        if index and position < ordered_positions[index - 1][1]
    ]
    if command_returncode != 0:
        print(
            "ERROR: host skill eval command failed "
            f"(source={source}, returncode={command_returncode})"
        )
    if missing:
        for marker in missing:
            print(f"ERROR: missing skill eval marker: {marker}")
    if retired:
        for marker in retired:
            print(f"ERROR: retired skill eval marker present: {marker}")
    if out_of_order:
        for marker in out_of_order:
            print(f"ERROR: out-of-order skill eval marker: {marker}")
    if command_returncode != 0 or missing or retired or out_of_order:
        return 1
    print(
        "OK: local-only skill eval transcript passed "
        f"({len(REQUIRED_MARKERS)} required markers)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

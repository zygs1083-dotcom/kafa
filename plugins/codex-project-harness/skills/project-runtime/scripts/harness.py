#!/usr/bin/env python3
"""Run Codex Project Harness scripts from an installed project-runtime skill."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


COMMANDS = {
    "init": "init_project_harness.py",
    "status": "harness_status.py",
    "phase": "update_phase.py",
    "acceptance-add": "add_acceptance.py",
    "failure-mode-add": "add_failure_mode.py",
    "task-add": "add_task.py",
    "task-update": "update_task.py",
    "decision-record": "record_decision.py",
    "validation-record": "record_validation.py",
    "gate-record": "record_quality_gate.py",
    "delivery-record": "record_delivery.py",
    "validate": "validate_harness_state.py",
}


def plugin_root() -> Path:
    return Path(__file__).resolve().parents[3]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="Target project root. Defaults to the current directory.")
    parser.add_argument("command", choices=sorted(COMMANDS))
    args, passthrough = parser.parse_known_args()

    script = plugin_root() / "scripts" / COMMANDS[args.command]
    if not script.exists():
        print(f"ERROR: harness script not found: {script}")
        return 1

    completed = subprocess.run(
        [sys.executable, str(script), *passthrough],
        cwd=Path(args.root).resolve(),
        text=True,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())

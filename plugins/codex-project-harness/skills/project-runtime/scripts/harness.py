#!/usr/bin/env python3
"""Run the installed Codex Project Harness CLI from the project-runtime skill."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def plugin_root() -> Path:
    return Path(__file__).resolve().parents[3]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="Target project root. Defaults to the current directory.")
    args, passthrough = parser.parse_known_args()

    cli = plugin_root() / "scripts" / "harness.py"
    if not cli.exists():
        print(f"ERROR: harness CLI not found: {cli}")
        return 1

    completed = subprocess.run(
        [sys.executable, str(cli), "--root", str(Path(args.root).resolve()), *passthrough],
        text=True,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())

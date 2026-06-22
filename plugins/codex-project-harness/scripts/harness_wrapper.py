#!/usr/bin/env python3
"""Compatibility helpers for legacy harness scripts."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def run_harness(args: list[str]) -> int:
    cli = Path(__file__).resolve().parent / "harness.py"
    completed = subprocess.run([sys.executable, str(cli), "--root", str(Path.cwd()), *args], text=True)
    return completed.returncode

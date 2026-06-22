#!/usr/bin/env python3
"""Compatibility wrapper for the runtime smoke scenarios.

Forward evaluation now names its executable CLI checks honestly as runtime
smoke tests. Keep this wrapper so existing CI and user commands keep working.
"""

from __future__ import annotations

from run_runtime_smoke import main


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Validate the Codex Project Harness plugin structure."""

from __future__ import annotations

import json
import sys
from pathlib import Path


REQUIRED_SKILLS = [
    "project-harness",
    "requirement-baseline",
    "team-architecture",
    "minimal-safe-change",
    "test-first-delivery",
    "bug-fix-loop",
    "independent-quality-gate",
    "release-readiness",
    "harness-audit",
    "project-retrospective",
]


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    manifest = root / ".codex-plugin" / "plugin.json"
    if not manifest.exists():
        print(f"ERROR: missing {manifest}")
        return 1

    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"ERROR: invalid plugin.json: {exc}")
        return 1

    errors: list[str] = []
    if data.get("name") != "codex-project-harness":
        errors.append("plugin name must be codex-project-harness")

    for skill in REQUIRED_SKILLS:
        skill_md = root / "skills" / skill / "SKILL.md"
        if not skill_md.exists():
            errors.append(f"missing skill file: {skill_md}")
            continue
        text = skill_md.read_text(encoding="utf-8")
        if not text.startswith("---"):
            errors.append(f"missing front matter: {skill_md}")
        if f'name: "{skill}"' not in text and f"name: {skill}" not in text:
            errors.append(f"skill name mismatch: {skill_md}")

    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1

    print("OK: plugin structure is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

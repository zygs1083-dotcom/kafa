#!/usr/bin/env python3
"""Validate the Codex Project Harness plugin structure."""

from __future__ import annotations

import json
import sys
from pathlib import Path


REQUIRED_SKILLS = [
    "project-harness",
    "project-bootstrap",
    "project-runtime",
    "requirement-baseline",
    "team-architecture",
    "minimal-safe-change",
    "test-first-delivery",
    "bug-fix-loop",
    "independent-quality-gate",
    "delivery-readiness",
    "harness-audit",
    "project-retrospective",
]

REQUIRED_REFERENCES = [
    "collaboration-tools.md",
    "tool-adapters.md",
]

REQUIRED_SCRIPTS = [
    "init_project_harness.py",
    "validate_structure.py",
    "harness_lib.py",
    "harness_status.py",
    "update_phase.py",
    "add_acceptance.py",
    "add_task.py",
    "update_task.py",
    "record_decision.py",
    "record_validation.py",
    "record_delivery.py",
    "validate_harness_state.py",
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

    manifest_skills = data.get("skills", [])
    for skill in REQUIRED_SKILLS:
        expected_path = f"skills/{skill}"
        if expected_path not in manifest_skills:
            errors.append(f"manifest missing skill path: {expected_path}")

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

    for ref in REQUIRED_REFERENCES:
        ref_path = root / "references" / ref
        if not ref_path.exists():
            errors.append(f"missing reference file: {ref_path}")

    for script in REQUIRED_SCRIPTS:
        script_path = root / "scripts" / script
        if not script_path.exists():
            errors.append(f"missing runtime script: {script_path}")

    stale_paths = [
        root / "skills" / "release-readiness" / "SKILL.md",
        root / "templates" / "agents" / "release-engineer.toml",
    ]
    for stale in stale_paths:
        if stale.exists():
            errors.append(f"stale delivery-only replacement still exists: {stale}")

    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1

    print("OK: plugin structure is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

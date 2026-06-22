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

REQUIRED_CORE = [
    "__init__.py",
    "api.py",
    "scheduler.py",
    "gate_engine.py",
    "lock_manager.py",
    "schema_guard.py",
    "event_bus.py",
    "executor.py",
    "invariant_checker.py",
    "projections.py",
]

REQUIRED_SCRIPTS = [
    "init_project_harness.py",
    "validate_structure.py",
    "harness_lib.py",
    "harness_wrapper.py",
    "harness_status.py",
    "update_phase.py",
    "add_acceptance.py",
    "add_failure_mode.py",
    "add_task.py",
    "update_task.py",
    "record_decision.py",
    "record_validation.py",
    "record_quality_gate.py",
    "record_delivery.py",
    "validate_harness_state.py",
    "harness_db.py",
    "harness.py",
    "run_runtime_smoke.py",
    "run_forward_eval.py",
    "run_skill_eval.py",
]

REQUIRED_SCHEMAS = [
    "project-state.schema.json",
    "requirement.schema.json",
    "acceptance.schema.json",
    "task.schema.json",
    "event.schema.json",
    "quality-gate.schema.json",
    "failure-mode.schema.json",
    "validation.schema.json",
    "evidence.schema.json",
    "test.schema.json",
    "finding.schema.json",
    "invalidation.schema.json",
    "delivery.schema.json",
    "adapter.schema.json",
    "adapter-action.schema.json",
    "agent.schema.json",
    "baseline.schema.json",
    "dispatch-run.schema.json",
    "dispatch-assignment.schema.json",
    "runtime-snapshot.schema.json",
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
    if "schema_version" in data:
        errors.append("plugin.json must not use legacy schema_version")
    if "display_name" in data:
        errors.append("plugin.json must not use legacy display_name")
    if not isinstance(data.get("author"), dict):
        errors.append("plugin author must be an object")
    if data.get("skills") != "./skills/":
        errors.append('plugin skills must be the relative string "./skills/"')

    interface = data.get("interface")
    if not isinstance(interface, dict):
        errors.append("plugin interface is required")
    else:
        for key in [
            "displayName",
            "shortDescription",
            "longDescription",
            "developerName",
            "category",
            "capabilities",
            "defaultPrompt",
        ]:
            if key not in interface:
                errors.append(f"plugin interface missing field: {key}")
        if "capabilities" in interface and not isinstance(interface["capabilities"], list):
            errors.append("plugin interface.capabilities must be a list")
        if "defaultPrompt" in interface and not isinstance(interface["defaultPrompt"], list):
            errors.append("plugin interface.defaultPrompt must be a list")

    for skill in REQUIRED_SKILLS:
        skill_dir = root / "skills" / skill
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            errors.append(f"missing skill file: {skill_md}")
            continue
        text = skill_md.read_text(encoding="utf-8")
        if not text.startswith("---"):
            errors.append(f"missing front matter: {skill_md}")
        if f'name: "{skill}"' not in text and f"name: {skill}" not in text:
            errors.append(f"skill name mismatch: {skill_md}")
        if "description:" not in text.split("---", 2)[1]:
            errors.append(f"missing description in front matter: {skill_md}")
        openai_yaml = skill_dir / "agents" / "openai.yaml"
        if not openai_yaml.exists():
            errors.append(f"missing skill UI metadata: {openai_yaml}")
        else:
            yaml_text = openai_yaml.read_text(encoding="utf-8")
            for required in ["interface:", "display_name:", "short_description:", "default_prompt:"]:
                if required not in yaml_text:
                    errors.append(f"openai.yaml missing {required}: {openai_yaml}")

    skill_dirs = {path.name for path in (root / "skills").iterdir() if path.is_dir()}
    extra_skills = sorted(skill_dirs - set(REQUIRED_SKILLS))
    for skill in extra_skills:
        errors.append(f"unexpected skill directory: {root / 'skills' / skill}")

    for ref in REQUIRED_REFERENCES:
        ref_path = root / "references" / ref
        if not ref_path.exists():
            errors.append(f"missing reference file: {ref_path}")

    for core_file in REQUIRED_CORE:
        core_path = root / "core" / core_file
        if not core_path.exists():
            errors.append(f"missing kernel core file: {core_path}")

    for script in REQUIRED_SCRIPTS:
        script_path = root / "scripts" / script
        if not script_path.exists():
            errors.append(f"missing runtime script: {script_path}")

    runtime_cli = root / "skills" / "project-runtime" / "scripts" / "harness.py"
    if not runtime_cli.exists():
        errors.append(f"missing project-runtime self-contained CLI: {runtime_cli}")

    for schema in REQUIRED_SCHEMAS:
        schema_path = root / "schemas" / schema
        if not schema_path.exists():
            errors.append(f"missing schema file: {schema_path}")
            continue
        try:
            json.loads(schema_path.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(f"invalid schema json {schema_path}: {exc}")

    install_md = root.parent.parent / "INSTALL.md"
    if install_md.exists() and "Copy every folder under" in install_md.read_text(encoding="utf-8"):
        errors.append("INSTALL.md still documents broken copy-skills installation mode")

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

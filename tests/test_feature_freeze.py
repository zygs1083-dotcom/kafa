from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "codex-project-harness"
SCRIPTS = PLUGIN_ROOT / "scripts"
RELEASE = json.loads((REPO_ROOT / "release.json").read_text(encoding="utf-8"))

for path in [PLUGIN_ROOT, SCRIPTS]:
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import harness  # noqa: E402
import harness_db  # noqa: E402
from core import KERNEL_VERSION, RUNTIME_VERSION, SCHEMA_VERSION  # noqa: E402
from validate_structure import (  # noqa: E402
    REQUIRED_CORE,
    REQUIRED_HOOKS,
    REQUIRED_SCHEMAS,
    REQUIRED_SCRIPTS,
    REQUIRED_SKILLS,
)


EXPECTED_TABLES = {
    "project",
    "delivery_cycles",
    "requirements",
    "acceptance",
    "requirement_acceptance",
    "failure_modes",
    "failure_mode_acceptance",
    "baselines",
    "tasks",
    "task_acceptance",
    "task_failure_modes",
    "task_dependencies",
    "test_targets",
    "acceptance_target_qualifications",
    "task_test_targets",
    "executions",
    "validations",
    "validation_executions",
    "validation_failure_modes",
    "findings",
    "quality_gates",
    "quality_gate_findings",
    "quality_gate_qualifications",
    "deliveries",
    "delivery_acceptance",
    "decisions",
    "invalidations",
    "migrations",
    "events",
    "outcome_observations",
}

EXPECTED_CLI_SURFACE = {
    "acceptance",
    "acceptance.add",
    "baseline",
    "baseline.diff",
    "baseline.freeze",
    "baseline.confirm",
    "baseline.validate",
    "cycle",
    "cycle.audit",
    "cycle.close",
    "cycle.outcome-record",
    "cycle.outcome-report",
    "cycle.start",
    "cycle.status",
    "decision",
    "decision.record",
    "delivery",
    "delivery.record",
    "delivery.ready",
    "doctor",
    "failure-mode",
    "failure-mode.add",
    "finding",
    "finding.record",
    "gate",
    "gate.record",
    "init",
    "migrate",
    "projection",
    "projection.rebuild",
    "quickstart",
    "quickstart.minimal",
    "quickstart.status",
    "repair",
    "requirement",
    "requirement.add",
    "requirement.link",
    "status",
    "task",
    "task.accept",
    "task.add",
    "task.block",
    "task.cancel",
    "task.list",
    "task.start",
    "task.submit",
    "test-target",
    "test-target.add",
    "test-target.link",
    "test-target.qualify",
    "test-target.list",
    "trace",
    "trace.show",
    "trace.validate",
    "validate",
    "validation",
    "validation.record",
    "verify",
    "verify.run",
}


def copy_source_layout(target: Path) -> Path:
    plugin_copy = target / "plugins" / "codex-project-harness"
    plugin_copy.parent.mkdir(parents=True)
    shutil.copytree(PLUGIN_ROOT, plugin_copy, ignore=shutil.ignore_patterns("__pycache__"))
    for name in ["VERSION", "release.json", "pyproject.toml"]:
        shutil.copyfile(REPO_ROOT / name, target / name)
    return plugin_copy


class FeatureFreezeTest(unittest.TestCase):
    def test_release_manifest_is_the_single_expected_version_source(self) -> None:
        plugin = json.loads(
            (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )

        self.assertEqual((REPO_ROOT / "VERSION").read_text().strip(), RELEASE["version"])
        self.assertEqual(plugin["version"], RELEASE["version"])
        self.assertEqual(RUNTIME_VERSION, RELEASE["runtime_version"])
        self.assertEqual(KERNEL_VERSION, RELEASE["kernel_version"])
        self.assertEqual(SCHEMA_VERSION, RELEASE["schema_version_runtime"])
        self.assertEqual(harness_db.RUNTIME_VERSION, RUNTIME_VERSION)
        self.assertEqual(harness_db.SCHEMA_VERSION, SCHEMA_VERSION)

    def test_schema_version_and_repair_message_use_the_canonical_value(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repair_plan = harness_db.repair(Path(temp), dry_run=True)
        self.assertIn(f"repair action: migrate schema to {SCHEMA_VERSION}", repair_plan)

    def test_feature_freeze_rejects_schema_growth(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "select name from sqlite_master "
                        "where type='table' and name not like 'sqlite_%'"
                    )
                }

        self.assertEqual(len(EXPECTED_TABLES), 30)
        self.assertEqual(tables, EXPECTED_TABLES)

    def test_feature_freeze_rejects_cli_surface_growth(self) -> None:
        self.assertEqual(len(EXPECTED_CLI_SURFACE), 59)
        self.assertEqual(_cli_surface(harness.build_parser()), EXPECTED_CLI_SURFACE)

    def test_feature_freeze_protects_public_files(self) -> None:
        skill_dirs = {path.name for path in (PLUGIN_ROOT / "skills").iterdir() if path.is_dir()}
        schema_files = {path.name for path in (PLUGIN_ROOT / "schemas").glob("*.json")}
        core_files = {path.name for path in (PLUGIN_ROOT / "core").glob("*.py")}
        script_files = {path.name for path in (PLUGIN_ROOT / "scripts").glob("*.py")}
        hook_files = {path.name for path in (PLUGIN_ROOT / "hooks").iterdir() if path.is_file()}

        self.assertEqual(skill_dirs, set(REQUIRED_SKILLS))
        self.assertEqual(schema_files, set(REQUIRED_SCHEMAS))
        self.assertEqual(set(REQUIRED_CORE), {"__init__.py", "api.py"})
        self.assertTrue(set(REQUIRED_CORE).issubset(core_files))
        self.assertEqual(script_files, set(REQUIRED_SCRIPTS))
        self.assertEqual(hook_files, set(REQUIRED_HOOKS))

    def test_validate_structure_rejects_extra_schema_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            plugin_copy = copy_source_layout(Path(temp))
            (plugin_copy / "schemas" / "unexpected.schema.json").write_text("{}")
            result = self.run_structure(plugin_copy)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unexpected schema file", result.stdout)

    def test_validate_structure_rejects_extra_hook_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            plugin_copy = copy_source_layout(Path(temp))
            (plugin_copy / "hooks" / "unexpected.py").write_text("print('unexpected')\n")
            result = self.run_structure(plugin_copy)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unexpected hook file", result.stdout)

    def test_internal_core_modules_are_not_frozen_by_filename(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            plugin_copy = copy_source_layout(Path(temp))
            (plugin_copy / "core" / "internal_delivery_module.py").write_text(
                '\"\"\"Private Kernel implementation module.\"\"\"\n'
            )
            result = self.run_structure(plugin_copy)
            from kafa.cli import static_plugin_structure

            structure_ok, structure_details = static_plugin_structure(plugin_copy)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(structure_ok, structure_details)

    def test_validate_structure_rejects_a_missing_imported_core_module(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            plugin_copy = copy_source_layout(Path(temp))
            (plugin_copy / "core" / "store.py").unlink()
            result = self.run_structure(plugin_copy)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing local Python import: core.store", result.stdout)

    @staticmethod
    def run_structure(plugin_copy: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(plugin_copy / "scripts" / "validate_structure.py"), str(plugin_copy)],
            text=True,
            capture_output=True,
            check=False,
        )


def _cli_surface(parser: argparse.ArgumentParser) -> set[str]:
    surface: set[str] = set()

    def walk(current: argparse.ArgumentParser, prefix: tuple[str, ...] = ()) -> None:
        for action in current._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, subparser in action.choices.items():
                    path = prefix + (name,)
                    surface.add(".".join(path))
                    walk(subparser, path)

    walk(parser)
    return surface


if __name__ == "__main__":
    unittest.main()

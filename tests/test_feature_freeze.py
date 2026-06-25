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

for path in [PLUGIN_ROOT, SCRIPTS]:
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import harness  # noqa: E402
import harness_db  # noqa: E402
from core import KERNEL_VERSION  # noqa: E402
from validate_structure import REQUIRED_CORE, REQUIRED_HOOKS, REQUIRED_SCHEMAS, REQUIRED_SCRIPTS, REQUIRED_SKILLS  # noqa: E402


EXPECTED_PLUGIN_VERSION = "1.12.0-beta.1"
EXPECTED_RUNTIME_VERSION = "4.5.0"
EXPECTED_SCHEMA_VERSION = 22

EXPECTED_TABLES = {
    "acceptance",
    "adapter_actions",
    "adapters",
    "agent_capabilities",
    "agent_provider_events",
    "agent_provider_sessions",
    "agent_reports",
    "agent_sessions",
    "agents",
    "baselines",
    "ci_verifications",
    "codex_fanout_exports",
    "command_log",
    "decisions",
    "deliveries",
    "delivery_acceptance",
    "dispatch_assignments",
    "dispatch_runs",
    "dispatch_worktrees",
    "events",
    "evidence",
    "executor_allowlist",
    "external_session_verifications",
    "failure_mode_acceptance",
    "failure_modes",
    "findings",
    "integration_attempts",
    "invalidations",
    "migrations",
    "project",
    "quality_gate_findings",
    "quality_gates",
    "requirement_acceptance",
    "requirements",
    "runtime_snapshots",
    "sandbox_executions",
    "session_attestations",
    "task_acceptance",
    "task_attempts",
    "task_dependencies",
    "task_failure_modes",
    "task_file_claims",
    "task_test_targets",
    "tasks",
    "test_targets",
    "tests",
    "validation_evidence",
    "validation_failure_modes",
    "validation_tests",
    "validations",
}

EXPECTED_CLI_SURFACE = {
    "acceptance",
    "acceptance.add",
    "adapter",
    "adapter.ci-verify",
    "adapter.complete",
    "adapter.confirm",
    "adapter.draft",
    "adapter.external-session-verify",
    "adapter.plan",
    "adapter.reconcile",
    "adapter.record",
    "agent",
    "agent.capability",
    "agent.capability.add",
    "agents",
    "agents.install",
    "baseline",
    "baseline.diff",
    "baseline.freeze",
    "baseline.validate",
    "checkpoint",
    "checkpoint.create",
    "checkpoint.export",
    "checkpoint.import",
    "checkpoint.list",
    "decision",
    "decision.record",
    "delivery",
    "delivery.record",
    "dispatch",
    "dispatch.claim-next",
    "dispatch.export-csv",
    "dispatch.file-claim",
    "dispatch.file-claim.add",
    "dispatch.file-claim.list",
    "dispatch.file-claim.release",
    "dispatch.import-csv",
    "dispatch.integrate",
    "dispatch.plan",
    "dispatch.provider",
    "dispatch.provider.cancel",
    "dispatch.provider.collect",
    "dispatch.provider.reconcile",
    "dispatch.provider.start",
    "dispatch.provider.status",
    "dispatch.recover-stale",
    "dispatch.run",
    "dispatch.status",
    "dispatch.verify-attempt",
    "doctor",
    "event",
    "event.export",
    "event.validate",
    "evidence",
    "evidence.record",
    "executor",
    "executor.allow-prefix",
    "executor.allow-prefix.add",
    "executor.allow-prefix.list",
    "failure-mode",
    "failure-mode.add",
    "finding",
    "finding.record",
    "gate",
    "gate.record",
    "init",
    "invariant",
    "invariant.validate",
    "kernel",
    "kernel.doctor",
    "migrate",
    "phase",
    "projection",
    "projection.rebuild",
    "repair",
    "requirement",
    "requirement.add",
    "requirement.link",
    "risk",
    "risk.sweep-expired",
    "scope",
    "scope.confirm",
    "session",
    "session.attest",
    "session.close",
    "session.status",
    "status",
    "task",
    "task.accept",
    "task.add",
    "task.block",
    "task.claim",
    "task.complete",
    "task.heartbeat",
    "task.next",
    "task.recover-stale",
    "task.release",
    "task.review",
    "task.start",
    "task.submit",
    "task.update",
    "test",
    "test.record",
    "test-target",
    "test-target.add",
    "test-target.link",
    "test-target.list",
    "trace",
    "trace.show",
    "trace.validate",
    "validate",
    "validation",
    "validation.record",
}


class FeatureFreezeTest(unittest.TestCase):
    def test_versions_are_consistent_for_v1120(self) -> None:
        plugin = json.loads((PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))

        self.assertEqual((REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip(), EXPECTED_PLUGIN_VERSION)
        self.assertEqual(plugin["version"], EXPECTED_PLUGIN_VERSION)
        self.assertEqual(harness_db.RUNTIME_VERSION, EXPECTED_RUNTIME_VERSION)
        self.assertEqual(KERNEL_VERSION, EXPECTED_RUNTIME_VERSION)

    def test_schema_version_remains_22(self) -> None:
        self.assertEqual(harness_db.SCHEMA_VERSION, EXPECTED_SCHEMA_VERSION)
        with tempfile.TemporaryDirectory() as temp:
            repair_plan = harness_db.repair(Path(temp), dry_run=True)
        self.assertIn("repair action: migrate schema to 22", repair_plan)

    def test_feature_freeze_rejects_schema_growth(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "select name from sqlite_master where type = 'table' and name not like 'sqlite_%'"
                    )
                }

        self.assertEqual(tables, EXPECTED_TABLES)

    def test_feature_freeze_rejects_cli_surface_growth(self) -> None:
        self.assertEqual(_cli_surface(harness.build_parser()), EXPECTED_CLI_SURFACE)

    def test_feature_freeze_rejects_extra_skill_schema_core_script_hook_files(self) -> None:
        skill_dirs = {path.name for path in (PLUGIN_ROOT / "skills").iterdir() if path.is_dir()}
        schema_files = {path.name for path in (PLUGIN_ROOT / "schemas").iterdir() if path.is_file() and path.suffix == ".json"}
        core_files = {path.name for path in (PLUGIN_ROOT / "core").iterdir() if path.is_file() and path.suffix == ".py"}
        script_files = {path.name for path in (PLUGIN_ROOT / "scripts").iterdir() if path.is_file() and path.suffix == ".py"}
        hook_files = {path.name for path in (PLUGIN_ROOT / "hooks").iterdir() if path.is_file()}

        self.assertEqual(skill_dirs, set(REQUIRED_SKILLS))
        self.assertEqual(schema_files, set(REQUIRED_SCHEMAS))
        self.assertEqual(core_files, set(REQUIRED_CORE))
        self.assertEqual(script_files, set(REQUIRED_SCRIPTS))
        self.assertEqual(hook_files, set(REQUIRED_HOOKS))

    def test_validate_structure_rejects_extra_schema_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            plugin_copy = temp_root / "plugins" / "codex-project-harness"
            plugin_copy.parent.mkdir(parents=True)
            shutil.copytree(PLUGIN_ROOT, plugin_copy, ignore=shutil.ignore_patterns("__pycache__"))
            (temp_root / "VERSION").write_text(EXPECTED_PLUGIN_VERSION + "\n", encoding="utf-8")
            (plugin_copy / "schemas" / "unexpected.schema.json").write_text("{}", encoding="utf-8")

            result = subprocess.run(
                [sys.executable, str(plugin_copy / "scripts" / "validate_structure.py"), str(plugin_copy)],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unexpected schema file", result.stdout)

    def test_validate_structure_rejects_extra_hook_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            plugin_copy = temp_root / "plugins" / "codex-project-harness"
            plugin_copy.parent.mkdir(parents=True)
            shutil.copytree(PLUGIN_ROOT, plugin_copy, ignore=shutil.ignore_patterns("__pycache__"))
            (temp_root / "VERSION").write_text(EXPECTED_PLUGIN_VERSION + "\n", encoding="utf-8")
            (plugin_copy / "hooks" / "unexpected.py").write_text("print('unexpected')\n", encoding="utf-8")

            result = subprocess.run(
                [sys.executable, str(plugin_copy / "scripts" / "validate_structure.py"), str(plugin_copy)],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unexpected hook file", result.stdout)


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

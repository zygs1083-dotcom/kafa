from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "codex-project-harness"


class ControlPlaneArchitectureTest(unittest.TestCase):
    def read(self, path: Path) -> str:
        return path.read_text(encoding="utf-8")

    def test_project_runtime_skill_is_natural_language_entry_not_fact_source(self) -> None:
        text = self.read(PLUGIN_ROOT / "skills" / "project-runtime" / "SKILL.md")

        self.assertIn("natural-language Skill Entry", text)
        self.assertIn("Prefer the self-contained CLI", text)
        self.assertIn("SQLite-backed harness runtime", text)
        self.assertIn("Markdown files are generated views, not the primary fact source", text)

    def test_hooks_are_advisory_and_do_not_write_evidence(self) -> None:
        hook_json = json.loads(self.read(PLUGIN_ROOT / "hooks" / "hooks.json"))
        hook_dispatcher = self.read(PLUGIN_ROOT / "hooks" / "harness_hook.py")

        self.assertEqual(
            set(hook_json["hooks"]),
            {"SessionStart", "SubagentStart", "PreToolUse", "PostToolUse", "Stop"},
        )
        self.assertIn("Hooks are advisory lifecycle guardrails", hook_dispatcher)
        self.assertIn("never create delivery evidence", hook_dispatcher)
        self.assertNotIn("insert into evidence", hook_dispatcher.lower())
        self.assertNotIn("validation record", hook_dispatcher.lower())

    def test_host_codex_provider_reports_are_raw_until_verify_attempt(self) -> None:
        provider = self.read(PLUGIN_ROOT / "core" / "agent_provider.py")
        runtime = self.read(PLUGIN_ROOT / "scripts" / "harness_db.py")
        skill = self.read(PLUGIN_ROOT / "skills" / "project-runtime" / "SKILL.md")

        self.assertIn("class HostCodexProvider", provider)
        self.assertIn("delivery evidence", provider)
        self.assertIn("insert into agent_reports", runtime)
        self.assertIn("insert into task_attempts", runtime)
        self.assertIn("status = 'verified'", runtime)
        self.assertIn("def dispatch_verify_attempt", runtime)
        self.assertIn("still raw reports until controller verification", skill)

    def test_connector_adapters_are_workflow_sync_not_delivery_evidence(self) -> None:
        runtime = self.read(PLUGIN_ROOT / "scripts" / "harness_db.py")
        docs = self.read(REPO_ROOT / "docs" / "runtime" / "OS_RUNTIME.md")

        for operation in [
            "github.issue.create",
            "linear.issue.create",
            "notion.page.create",
            "figma.comment.create",
            "slack.message.post",
        ]:
            self.assertIn(operation, runtime)
        self.assertIn("def execute_connector_action", runtime)
        self.assertIn("Connector results are workflow synchronization records, not trusted delivery evidence", docs)

    def test_stability_eval_covers_control_plane_boundaries(self) -> None:
        eval_runner = self.read(PLUGIN_ROOT / "scripts" / "run_agent_e2e_eval.py")

        for scenario in [
            "scenario_host_codex_fake_sdk_e2e",
            "scenario_connector_mock_server_e2e",
            "scenario_crash_retry_recovery",
            "scenario_sqlite_contention_stress",
        ]:
            self.assertIn(scenario, eval_runner)
        self.assertIn("\"stability\": run_stability", eval_runner)
        self.assertIn("false_pass_count", eval_runner)
        self.assertIn("forged_evidence_block_count", eval_runner)

    def test_kafa_doctor_reports_control_plane_contract(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "kafa.cli", "doctor", "--repo", str(REPO_ROOT), "--json"],
            text=True,
            capture_output=True,
            check=True,
        )
        report = json.loads(result.stdout)
        checks = {check["name"]: check for check in report["checks"]}

        self.assertTrue(report["ok"], report)
        self.assertIn("control plane contract", checks)
        self.assertTrue(checks["control plane contract"]["ok"], checks["control plane contract"])
        self.assertIn("Skill Entry", checks["control plane contract"]["details"])
        self.assertIn("Kernel Trust Layer", checks["control plane contract"]["details"])
        self.assertIn("connector namespace boundary", checks)
        self.assertTrue(checks["connector namespace boundary"]["ok"], checks["connector namespace boundary"])
        self.assertIn("does not create external workspaces", checks["connector namespace boundary"]["details"])


if __name__ == "__main__":
    unittest.main()

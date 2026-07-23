from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "codex-project-harness"
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from harness_lib import load_distribution_manifest  # noqa: E402

DISTRIBUTION = load_distribution_manifest(PLUGIN_ROOT)


class ControlPlaneArchitectureTest(unittest.TestCase):
    def read(self, path: Path) -> str:
        return path.read_text(encoding="utf-8")

    def test_project_harness_is_local_delivery_entry_not_fact_source(self) -> None:
        text = self.read(PLUGIN_ROOT / "skills" / "project-harness" / "SKILL.md")
        contract = json.loads(
            self.read(PLUGIN_ROOT / "references" / "workflow-contract.json")
        )

        self.assertEqual(contract["contract_version"], 1)
        self.assertEqual(
            {item["id"] for item in contract["authorities"]},
            {
                "openspec",
                "sqlite",
                "delivery-evaluator",
                "workflow-contract",
                "native-host",
                "root-controller",
            },
        )
        for item in contract["safeguards"]:
            self.assertIn(item["id"], text)
            self.assertIn(item["rule"], text)
        for item in contract["routes"]:
            self.assertIn(item["when"], text)
            self.assertIn(item["obligation"], text)
        self.assertIn("Generated Markdown is a human-readable projection, not a fact source", text)
        self.assertIn("Only the root controller writes Kafa delivery facts", text)
        self.assertIn("BEGIN GENERATED: workflow-contract:entry-workflow", text)

    def test_hooks_are_advisory_and_do_not_write_evidence(self) -> None:
        hook_json = json.loads(self.read(PLUGIN_ROOT / "hooks" / "hooks.json"))
        hook_dispatcher = self.read(PLUGIN_ROOT / "hooks" / "harness_hook.py")

        self.assertEqual(
            set(hook_json["hooks"]),
            set(DISTRIBUTION["hooks"]["events"]),
        )
        self.assertIn("Hooks are advisory", hook_dispatcher)
        self.assertIn("never create delivery facts or evidence", hook_dispatcher)
        self.assertIn("Stop is warn-only", hook_dispatcher)
        self.assertNotIn("insert into evidence", hook_dispatcher.lower())
        self.assertNotIn("validation record", hook_dispatcher.lower())

    def test_stability_eval_covers_control_plane_boundaries(self) -> None:
        eval_runner = self.read(PLUGIN_ROOT / "scripts" / "run_agent_e2e_eval.py")

        self.assertIn("scenario_sqlite_contention_stress", eval_runner)
        for retired in ["scenario_host_codex_fake_sdk_e2e", "HostCodexProvider", "openai_codex"]:
            self.assertNotIn(retired, eval_runner)
        self.assertIn("\"stability\": run_stability", eval_runner)
        self.assertIn("false_pass_count", eval_runner)
        self.assertIn("forged_evidence_block_count", eval_runner)

    def test_kafa_doctor_reports_control_plane_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shutil.copytree(PLUGIN_ROOT, root / "plugins" / "codex-project-harness")
            shutil.copyfile(REPO_ROOT / "VERSION", root / "VERSION")
            shutil.copyfile(REPO_ROOT / "pyproject.toml", root / "pyproject.toml")
            env = os.environ.copy()
            env["PYTHONPATH"] = str(REPO_ROOT)
            subprocess.run(
                [sys.executable, "-m", "kafa.cli", "plugin", "install", "--repo", str(root)],
                text=True,
                capture_output=True,
                check=True,
                env=env,
            )
            result = subprocess.run(
                [sys.executable, "-m", "kafa.cli", "doctor", "--repo", str(root), "--json"],
                text=True,
                capture_output=True,
                check=True,
                env=env,
            )
            report = json.loads(result.stdout)
        checks = {check["name"]: check for check in report["checks"]}

        self.assertTrue(report["ok"], report)
        self.assertIn("control plane contract", checks)
        self.assertTrue(checks["control plane contract"]["ok"], checks["control plane contract"])
        self.assertIn("Skill Entry", checks["control plane contract"]["details"])
        self.assertIn("Local Runtime Boundary", checks["control plane contract"]["details"])
        self.assertIn("Kernel Trust Layer", checks["control plane contract"]["details"])


if __name__ == "__main__":
    unittest.main()

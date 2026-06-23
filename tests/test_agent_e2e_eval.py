from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
EVAL = REPO_ROOT / "plugins/codex-project-harness/scripts/run_agent_e2e_eval.py"


def run_eval(*args: str, env: dict[str, str] | None = None) -> dict[str, object]:
    command_env = os.environ.copy()
    if env is not None:
        command_env.update(env)
    result = subprocess.run(["python3", str(EVAL), *args], text=True, capture_output=True, check=False, env=command_env)
    if result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return json.loads(result.stdout)


class AgentE2EEvalTest(unittest.TestCase):
    def test_fixture_eval_runs_all_required_scenarios(self) -> None:
        report = run_eval("--mode", "fixture")

        summary = report["summary"]
        scenarios = {scenario["name"]: scenario for scenario in report["scenarios"]}
        self.assertEqual(report["mode"], "fixture")
        self.assertEqual(summary["scenario_count"], 5)
        self.assertEqual(summary["failed_count"], 0)
        self.assertEqual(summary["false_pass_count"], 0)
        self.assertEqual(summary["forged_evidence_block_count"], 1)
        self.assertEqual(summary["human_intervention_count"], 0)
        self.assertEqual(set(scenarios), {"parallel_success", "dependency_blocked", "same_file_conflict", "forged_evidence_blocked", "integration_regression_blocked"})
        self.assertTrue(all(scenario["pass"] for scenario in scenarios.values()))

    def test_fixture_eval_blocks_forged_evidence_and_integration_regression(self) -> None:
        report = run_eval("--mode", "fixture")
        scenarios = {scenario["name"]: scenario for scenario in report["scenarios"]}

        forged = scenarios["forged_evidence_blocked"]["details"]
        regression = scenarios["integration_regression_blocked"]["details"]
        dependency = scenarios["dependency_blocked"]["details"]
        self.assertNotEqual(forged["delivery_returncode"], 0)
        self.assertEqual(forged["controller_evidence_count"], 0)
        self.assertEqual(dependency["planned"], ["T1"])
        self.assertEqual(dependency["exported"], ["T1"])
        self.assertNotEqual(dependency["claim_returncode"], 0)
        self.assertNotEqual(regression["integrate_returncode"], 0)
        self.assertEqual(regression["status"], "verification_failed")
        self.assertTrue(regression["finding_recorded"])

    def test_live_command_without_command_is_skipped(self) -> None:
        report = run_eval("--mode", "live-command", env={"CODEX_AGENT_EVAL_CMD": ""})

        self.assertEqual(report["mode"], "live-command")
        self.assertTrue(report["live_skipped"])
        self.assertEqual(report["summary"]["scenario_count"], 0)
        self.assertEqual(report["summary"]["failed_count"], 0)


if __name__ == "__main__":
    unittest.main()

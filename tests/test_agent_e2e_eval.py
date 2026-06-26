from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
EVAL = REPO_ROOT / "plugins/codex-project-harness/scripts/run_agent_e2e_eval.py"
sys.path.insert(0, str(EVAL.parent))
import run_agent_e2e_eval  # noqa: E402


def run_eval(*args: str, env: dict[str, str] | None = None) -> dict[str, object]:
    command_env = os.environ.copy()
    if env is not None:
        command_env.update(env)
    result = subprocess.run([sys.executable, str(EVAL), *args], text=True, capture_output=True, check=False, env=command_env)
    if result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return json.loads(result.stdout)


class AgentE2EEvalTest(unittest.TestCase):
    def test_fixture_eval_runs_all_required_scenarios(self) -> None:
        report = run_eval("--mode", "fixture")

        summary = report["summary"]
        scenarios = {scenario["name"]: scenario for scenario in report["scenarios"]}
        self.assertEqual(report["mode"], "fixture")
        self.assertIn("matrix", report)
        self.assertEqual(report["matrix"]["profile"], "fixture")
        self.assertEqual(summary["scenario_count"], 5)
        self.assertEqual(summary["failed_count"], 0)
        self.assertEqual(summary["false_pass_count"], 0)
        self.assertEqual(summary["forged_evidence_block_count"], 1)
        self.assertEqual(summary["human_intervention_count"], 0)
        self.assertEqual(summary["sqlite_lock_error_count"], 0)
        self.assertEqual(set(scenarios), {"parallel_success", "dependency_blocked", "same_file_conflict", "forged_evidence_blocked", "integration_regression_blocked"})
        self.assertTrue(all(scenario["pass"] for scenario in scenarios.values()))
        self.assertTrue(all("category" in scenario and "mode" in scenario and "skip_reason" in scenario for scenario in scenarios.values()))

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
        self.assertIn("CODEX_AGENT_EVAL_CMD", report["matrix"]["live_skipped_reasons"][0])
        self.assertEqual(report["summary"]["scenario_count"], 0)
        self.assertEqual(report["summary"]["failed_count"], 0)

    def test_stability_eval_runs_matrix_scenarios(self) -> None:
        report = run_eval("--mode", "stability")

        summary = report["summary"]
        scenarios = {scenario["name"]: scenario for scenario in report["scenarios"]}
        self.assertEqual(report["mode"], "stability")
        self.assertEqual(report["matrix"]["profile"], "stability")
        self.assertTrue(report["matrix"]["connector_mock"])
        self.assertTrue(report["matrix"]["sqlite_stress"])
        self.assertEqual(summary["failed_count"], 0)
        self.assertEqual(summary["false_pass_count"], 0)
        self.assertGreaterEqual(summary["forged_evidence_block_count"], 1)
        self.assertEqual(summary["sqlite_lock_error_count"], 0)
        self.assertEqual(summary["human_intervention_count"], 0)
        for name in {
            "host_codex_fake_sdk_e2e",
            "multi_role_thread_lifecycle",
            "connector_mock_server_e2e",
            "connector_exactly_once_recovery",
            "crash_retry_recovery",
            "sqlite_contention_stress",
        }:
            self.assertIn(name, scenarios)
            self.assertTrue(scenarios[name]["pass"], scenarios[name]["details"])
        self.assertEqual(scenarios["connector_mock_server_e2e"]["details"]["evidence_count"], 0)
        self.assertEqual(scenarios["connector_exactly_once_recovery"]["details"]["writes"], 0)
        self.assertEqual(scenarios["connector_exactly_once_recovery"]["details"]["remote_recovery_count"], 1)
        self.assertEqual(scenarios["crash_retry_recovery"]["details"]["reports"], 1)
        self.assertEqual(scenarios["sqlite_contention_stress"]["details"]["sqlite_lock_error_count"], 0)

    def test_live_codex_without_enable_is_skipped(self) -> None:
        report = run_eval("--mode", "live-codex", env={"HARNESS_E2E_ENABLE_LIVE_CODEX": ""})

        self.assertEqual(report["mode"], "live-codex")
        self.assertTrue(report["live_skipped"])
        self.assertEqual(report["summary"]["failed_count"], 0)
        self.assertEqual(report["summary"]["skipped_count"], 2)
        self.assertIn("HARNESS_E2E_ENABLE_LIVE_CODEX", "; ".join(report["matrix"]["live_skipped_reasons"]))

    def test_should_fail_thresholds(self) -> None:
        base = {
            "mode": "stability",
            "live_skipped": False,
            "summary": {
                "scenario_count": 10,
                "failed_count": 0,
                "false_pass_count": 0,
                "forged_evidence_block_count": 1,
                "human_intervention_count": 0,
                "sqlite_lock_error_count": 0,
            },
        }
        self.assertFalse(run_agent_e2e_eval.should_fail(base))
        locked = json.loads(json.dumps(base))
        locked["summary"]["sqlite_lock_error_count"] = 1
        self.assertTrue(run_agent_e2e_eval.should_fail(locked))
        false_pass = json.loads(json.dumps(base))
        false_pass["summary"]["false_pass_count"] = 1
        self.assertTrue(run_agent_e2e_eval.should_fail(false_pass))
        live_skipped = {"mode": "live-codex", "live_skipped": True, "summary": {"failed_count": 0}}
        self.assertFalse(run_agent_e2e_eval.should_fail(live_skipped))

    def test_out_matches_stdout_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "report.json"
            report = run_eval("--mode", "live-codex", "--out", str(out), env={"HARNESS_E2E_ENABLE_LIVE_CODEX": ""})
            from_file = json.loads(out.read_text(encoding="utf-8"))

        self.assertEqual(from_file, report)
        self.assertIn("matrix", from_file)
        self.assertIn("summary", from_file)
        self.assertIn("scenarios", from_file)


if __name__ == "__main__":
    unittest.main()

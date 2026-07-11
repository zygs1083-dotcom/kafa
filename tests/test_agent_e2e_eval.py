from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
EVAL = REPO_ROOT / "plugins/codex-project-harness/scripts/run_agent_e2e_eval.py"
sys.path.insert(0, str(EVAL.parent))
import run_agent_e2e_eval  # noqa: E402


def run_eval(*args: str, env: dict[str, str] | None = None) -> dict[str, object]:
    result = run_eval_process(*args, env=env)
    if result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return json.loads(result.stdout)


def run_eval_process(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    command_env = os.environ.copy()
    if env is not None:
        command_env.update(env)
    return subprocess.run([sys.executable, str(EVAL), *args], text=True, capture_output=True, check=False, env=command_env)


class AgentE2EEvalTest(unittest.TestCase):
    def test_git_porcelain_paths_preserve_leading_status_columns(self) -> None:
        paths = run_agent_e2e_eval.git_porcelain_paths(" M app.py\n?? generated.txt\n")

        self.assertEqual(paths, {"app.py", "generated.txt"})

    def test_live_receipt_is_written_under_ignored_runtime_state(self) -> None:
        root = Path("/tmp/business")

        receipt = run_agent_e2e_eval.live_receipt_path(root, "RUN-1")

        self.assertEqual(receipt.relative_to(root).as_posix(), ".ai-team/runtime/live-codex/RUN-1/native-receipt.json")

    def test_live_file_claim_uses_native_effective_agent_identity(self) -> None:
        assignment = {"agent_id": "", "capability": "developer", "owner": "agent-t1"}

        self.assertEqual(run_agent_e2e_eval.effective_assignment_agent(assignment), "developer")

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
        self.assertTrue(scenarios["parallel_success"]["details"]["integrate_via_public_cli"])
        self.assertEqual(scenarios["parallel_success"]["details"]["delivery_validation_issues"], [])

    def test_success_scenarios_do_not_replace_delivery_validator(self) -> None:
        tree = ast.parse(EVAL.read_text(encoding="utf-8"))
        assignments = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr == "validate_runtime":
                    assignments.append(node.lineno)

        self.assertEqual(assignments, [], f"release-critical validate_runtime replaced at lines {assignments}")

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
        self.assertNotEqual(regression["test_exit_code"], 0)
        self.assertGreater(regression["executed_count"], 0)
        self.assertEqual(regression["executed_count_source"], "parsed")
        self.assertIn("test_no_integration_regression", regression["test_output_tail"])
        self.assertNotIn("delivery requires validation evidence", regression["stdout_tail"])
        self.assertNotIn("requires a quality gate record", regression["stdout_tail"])

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
            "host_codex_spark_policy_fake_sdk_e2e",
            "multi_role_thread_lifecycle",
            "connector_mock_server_e2e",
            "connector_exactly_once_recovery",
            "crash_retry_recovery",
            "sqlite_contention_stress",
        }:
            self.assertIn(name, scenarios)
            self.assertTrue(scenarios[name]["pass"], scenarios[name]["details"])
        self.assertEqual(scenarios["connector_mock_server_e2e"]["details"]["evidence_count"], 0)
        self.assertEqual(scenarios["host_codex_spark_policy_fake_sdk_e2e"]["details"]["thread_run_model"], "gpt-5.3-codex-spark")
        self.assertEqual(scenarios["host_codex_spark_policy_fake_sdk_e2e"]["details"]["evidence_count"], 0)
        self.assertEqual(scenarios["connector_exactly_once_recovery"]["details"]["writes"], 0)
        self.assertEqual(scenarios["connector_exactly_once_recovery"]["details"]["remote_recovery_count"], 1)
        self.assertEqual(scenarios["crash_retry_recovery"]["details"]["reports"], 1)
        self.assertEqual(scenarios["sqlite_contention_stress"]["details"]["sqlite_lock_error_count"], 0)

    def test_live_codex_without_enable_is_not_run_and_fails_explicit_profile(self) -> None:
        result = run_eval_process("--mode", "live-codex", env={"HARNESS_E2E_ENABLE_LIVE_CODEX": ""})
        report = json.loads(result.stdout)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(report["mode"], "live-codex")
        self.assertTrue(report["live_skipped"])
        self.assertEqual(report["live_status"], "not-run")
        self.assertEqual(report["summary"]["passed_count"], 0)
        self.assertEqual(report["summary"]["failed_count"], 0)
        self.assertEqual(report["summary"]["skipped_count"], 2)
        self.assertTrue(all(not scenario["pass"] for scenario in report["scenarios"]))
        self.assertIn("HARNESS_E2E_ENABLE_LIVE_CODEX", "; ".join(report["matrix"]["live_skipped_reasons"]))

    def test_live_codex_enabled_without_authenticated_codex_is_blocked(self) -> None:
        result = run_eval_process(
            "--mode",
            "live-codex",
            env={
                "HARNESS_E2E_ENABLE_LIVE_CODEX": "1",
                "HARNESS_E2E_CODEX_BIN": sys.executable,
            },
        )
        report = json.loads(result.stdout)

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(report["live_skipped"])
        self.assertEqual(report["live_status"], "blocked")
        self.assertEqual(report["summary"]["passed_count"], 0)
        self.assertGreaterEqual(report["summary"]["failed_count"], 1)
        self.assertTrue(all(not scenario["skip_reason"] for scenario in report["scenarios"]))
        self.assertTrue(all(scenario["details"]["capability_status"] == "blocked" for scenario in report["scenarios"]))

    def test_live_codex_has_no_permanent_repository_profile_skip(self) -> None:
        source = EVAL.read_text(encoding="utf-8")

        self.assertNotIn("no repository-local live profile is configured", source)

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
        self.assertTrue(run_agent_e2e_eval.should_fail(live_skipped))

    def test_out_matches_stdout_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "report.json"
            result = run_eval_process("--mode", "live-codex", "--out", str(out), env={"HARNESS_E2E_ENABLE_LIVE_CODEX": ""})
            report = json.loads(result.stdout)
            from_file = json.loads(out.read_text(encoding="utf-8"))

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(from_file, report)
        self.assertIn("matrix", from_file)
        self.assertIn("summary", from_file)
        self.assertIn("scenarios", from_file)


if __name__ == "__main__":
    unittest.main()

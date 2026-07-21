from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
BENCHMARKS = REPO_ROOT / "benchmarks"
SCRIPT = BENCHMARKS / "run_delivery_integrity_outcome_benchmark.py"
if str(BENCHMARKS) not in sys.path:
    sys.path.insert(0, str(BENCHMARKS))

import run_delivery_integrity_outcome_benchmark as outcome_benchmark  # noqa: E402


FIXED_IDENTITY = {
    "generated_at": "2026-07-21T10:00:00Z",
    "git_head": "a" * 40,
    "git_dirty": True,
    "workspace_sha256": "b" * 64,
    "status_sha256": "c" * 64,
    "status_entry_count": 1,
    "source_scope": ["tests/"],
}


def passing_runner(scenario: dict[str, str]) -> dict[str, object]:
    return {
        "status": "passed",
        "result": scenario["after_result"],
        "command": ["deterministic-test", scenario["test_id"]],
        "returncode": 0,
        "duration_seconds": 0.0,
        "stdout_sha256": "d" * 64,
        "stderr_sha256": "e" * 64,
        "stdout_tail": "",
        "stderr_tail": "",
        "not_run_reason": "",
    }


class OutcomeBenchmarkContractTests(unittest.TestCase):
    def test_not_run_report_never_fabricates_zero_or_field_improvement(self) -> None:
        report = outcome_benchmark.build_report(
            run_after=False,
            generated_at="2026-07-21T10:00:00Z",
            source_identity=FIXED_IDENTITY,
        )

        self.assertEqual(report["benchmark_status"], "not-run")
        self.assertEqual(report["evidence_mode"], "regression-benchmark")
        self.assertEqual(report["summary"]["passed_count"], 0)
        self.assertEqual(report["summary"]["not_run_count"], 4)
        self.assertIsNone(report["summary"]["regression_closure_rate"])
        self.assertFalse(report["field_improvement_claimed"])
        self.assertEqual(report["field_evidence"]["status"], "not-run")
        for metric in report["field_evidence"]["metrics"].values():
            self.assertEqual(metric["status"], "not-run")
            self.assertIsNone(metric["value"])
        self.assertEqual(outcome_benchmark.validate_report(report), [])

    def test_passing_report_uses_identical_inventory_and_regression_semantics(self) -> None:
        report = outcome_benchmark.build_report(
            scenario_runner=passing_runner,
            generated_at="2026-07-21T10:00:00Z",
            source_identity=FIXED_IDENTITY,
        )

        self.assertEqual(report["benchmark_status"], "passed")
        self.assertEqual(report["summary"]["scenario_count"], 4)
        self.assertEqual(report["summary"]["passed_count"], 4)
        self.assertEqual(report["summary"]["failed_count"], 0)
        self.assertEqual(report["summary"]["not_run_count"], 0)
        self.assertEqual(report["summary"]["regression_closure_rate"], 1.0)
        self.assertEqual(report["numerator"]["before_false_delivery_count"], 4)
        self.assertEqual(report["numerator"]["after_fail_closed_count"], 4)
        self.assertEqual(report["denominator"]["value"], 4)
        self.assertTrue(report["inventory"]["before_after_inventory_matches"])
        self.assertTrue(report["window"]["complete"])
        self.assertEqual(
            [scenario["id"] for scenario in report["scenarios"]],
            list(report["inventory"]["scenario_ids"]),
        )
        for scenario in report["scenarios"]:
            self.assertEqual(scenario["before"]["result"], "false-delivery")
            self.assertEqual(scenario["after"]["result"], "fail-closed")
        self.assertEqual(outcome_benchmark.validate_report(report), [])

    def test_validator_rejects_inventory_or_field_evidence_relabelling(self) -> None:
        report = outcome_benchmark.build_report(
            scenario_runner=passing_runner,
            generated_at="2026-07-21T10:00:00Z",
            source_identity=FIXED_IDENTITY,
        )
        report["inventory"]["scenario_ids"] = ["different"]
        report["field_evidence"]["status"] = "passed"
        report["field_improvement_claimed"] = True

        issues = outcome_benchmark.validate_report(report)

        self.assertTrue(any("inventory drift" in issue for issue in issues), issues)
        self.assertTrue(any("field evidence" in issue for issue in issues), issues)
        self.assertTrue(any("field improvement" in issue for issue in issues), issues)

    def test_generated_at_is_captured_after_the_after_window_runs(self) -> None:
        with mock.patch.object(
            outcome_benchmark,
            "_utc_now",
            side_effect=["2026-07-21T09:00:00Z", "2026-07-21T10:00:00Z"],
        ):
            report = outcome_benchmark.build_report(
                scenario_runner=passing_runner,
                source_identity=FIXED_IDENTITY,
            )

        self.assertEqual(report["started_at"], "2026-07-21T09:00:00Z")
        self.assertEqual(report["generated_at"], "2026-07-21T10:00:00Z")
        self.assertEqual(report["window"]["after"], "2026-07-21T10:00:00Z")

    def test_real_fixed_scenarios_generate_a_valid_passing_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "outcome-benchmark.json"
            result = subprocess.run(
                [sys.executable, "-B", str(SCRIPT), "--out", str(output)],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["benchmark_status"], "passed")
            self.assertEqual(report["summary"]["passed_count"], 4)
            self.assertEqual(report["summary"]["failed_count"], 0)
            self.assertEqual(report["summary"]["not_run_count"], 0)
            self.assertEqual(outcome_benchmark.validate_report(report), [])


if __name__ == "__main__":
    unittest.main()

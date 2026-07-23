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
        self.assertEqual(report["field_metrics_status"], "not-observed")
        self.assertNotIn("field_evidence", report)
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

    def test_lwl_p2_f6_validator_rejects_forged_zero_aggregates(self) -> None:
        report = outcome_benchmark.build_report(
            scenario_runner=passing_runner,
            generated_at="2026-07-21T10:00:00Z",
            source_identity=FIXED_IDENTITY,
        )
        report["numerator"]["before_false_delivery_count"] = 0
        report["numerator"]["after_fail_closed_count"] = 0
        report["denominator"]["value"] = 0
        report["summary"]["scenario_count"] = 0
        report["summary"]["passed_count"] = 0
        report["summary"]["failed_count"] = 0
        report["summary"]["not_run_count"] = 0
        report["summary"]["regression_closure_rate"] = 0
        report["window"]["complete"] = False

        issues = outcome_benchmark.validate_report(report)

        self.assertTrue(
            issues,
            "validator accepted forged zero aggregates for four passed scenarios",
        )

    def test_validator_rejects_unbound_source_and_extra_claims(self) -> None:
        report = outcome_benchmark.build_report(
            scenario_runner=passing_runner,
            generated_at="2026-07-21T10:00:00Z",
            source_identity=FIXED_IDENTITY,
        )
        report["evaluation_source"] = {}
        report["unvalidated_claim"] = {"field_improved": True}

        issues = outcome_benchmark.validate_report(report)

        self.assertTrue(any("keys mismatch" in issue for issue in issues), issues)
        self.assertTrue(any("evaluation_source shape" in issue for issue in issues), issues)

    def test_validator_closes_evaluation_source_semantics(self) -> None:
        mutations = {
            "zero workspace digest": {"workspace_sha256": "0" * 64},
            "boolean status count": {"status_entry_count": True},
            "false clean state": {
                "git_dirty": False,
                "status_entry_count": 1,
            },
            "unsafe source scope": {"source_scope": ["../outside"]},
            "non-string source scope": {"source_scope": [{"path": "kafa/"}]},
        }
        for label, mutation in mutations.items():
            with self.subTest(label=label):
                report = outcome_benchmark.build_report(
                    scenario_runner=passing_runner,
                    generated_at="2026-07-21T10:00:00Z",
                    source_identity=FIXED_IDENTITY,
                )
                report["evaluation_source"].update(mutation)
                self.assertTrue(outcome_benchmark.validate_report(report))

    def test_validator_rejects_inventory_or_absent_field_sentinel_relabelling(self) -> None:
        report = outcome_benchmark.build_report(
            scenario_runner=passing_runner,
            generated_at="2026-07-21T10:00:00Z",
            source_identity=FIXED_IDENTITY,
        )
        report["inventory"]["scenario_ids"] = ["different"]
        report["field_metrics_status"] = "observed"
        report["field_evidence"] = {"status": "passed", "metrics": {}}
        report["field_improvement_claimed"] = True

        issues = outcome_benchmark.validate_report(report)

        self.assertTrue(any("inventory drift" in issue for issue in issues), issues)
        self.assertTrue(any("field metrics" in issue for issue in issues), issues)
        self.assertTrue(any("field improvement" in issue for issue in issues), issues)

    def test_historical_v1_artifact_remains_structurally_valid(self) -> None:
        historical = json.loads(
            (REPO_ROOT / "docs/runtime/delivery-integrity-outcome-benchmark.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertEqual(historical["report_version"], "kafa-outcome-benchmark-v1")
        self.assertEqual(outcome_benchmark.validate_report(historical), [])

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

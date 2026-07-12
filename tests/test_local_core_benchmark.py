from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE = json.loads((REPO_ROOT / "release.json").read_text(encoding="utf-8"))
BENCHMARK_PATH = REPO_ROOT / "benchmarks/run_local_core_benchmark.py"
SPEC = importlib.util.spec_from_file_location("run_local_core_benchmark", BENCHMARK_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load benchmark module: {BENCHMARK_PATH}")
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


class LocalCoreBenchmarkTests(unittest.TestCase):
    def test_small_report_covers_all_metrics_without_timing_gate(self) -> None:
        report = benchmark.build_report(fact_count=25, repetitions=2)

        self.assertEqual(report["benchmark_kind"], "comparative-report-only")
        self.assertIs(report["timing_assertions"], False)
        self.assertEqual(report["baseline"]["fact_count"], 5_000)
        self.assertEqual(report["baseline"]["targeted_projection_status"], "not-recorded")
        self.assertEqual(
            report["schema30"]["schema_version"], RELEASE["schema_version_runtime"]
        )
        self.assertEqual(report["schema30"]["runtime_version"], RELEASE["runtime_version"])
        self.assertEqual(
            report["schema30"]["single_mutation_after_local_facts"]["fact_count"],
            25,
        )
        self.assertEqual(
            report["schema30"]["targeted_projection"]["projections"],
            ["project-state", "requirements", "traceability"],
        )
        self.assertEqual(
            report["schema30"]["full_projection"]["projection_count"],
            13,
        )
        self.assertEqual(report["schema30"]["full_test"]["status"], "not-run")
        self.assertIsNone(report["schema30"]["full_test"]["seconds"])
        self.assertEqual(
            report["comparison"]["targeted_projection_seconds"]["status"],
            "not-comparable",
        )
        self.assertNotIn("pass", report)

    def test_cli_writes_report_and_accepts_measured_test_duration(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "benchmark.json"
            exit_code = benchmark.main(
                [
                    "--facts",
                    "10",
                    "--samples",
                    "1",
                    "--test-duration-seconds",
                    "12.5",
                    "--test-count",
                    "7",
                    "--test-status",
                    "passed",
                    "--out",
                    str(out),
                ]
            )
            payload = __import__("json").loads(out.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["schema30"]["full_test"]["status"], "passed")
        self.assertEqual(payload["schema30"]["full_test"]["seconds"], 12.5)
        self.assertEqual(payload["schema30"]["full_test"]["test_count"], 7)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "plugins/codex-project-harness/scripts/run_runtime_smoke.py"
SPEC = importlib.util.spec_from_file_location("run_runtime_smoke", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
run_runtime_smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(run_runtime_smoke)


class RuntimeSmokeContractTest(unittest.TestCase):
    def test_benchmark_ratio_preserves_raw_decision_value_and_threshold(self) -> None:
        below = run_runtime_smoke.directed_invariant_benchmark_result(
            initialized_returncode=0,
            full_issue_count=0,
            directed_issue_count=0,
            full_seconds=9.999,
            directed_seconds=1.0,
        )
        boundary = run_runtime_smoke.directed_invariant_benchmark_result(
            initialized_returncode=0,
            full_issue_count=0,
            directed_issue_count=0,
            full_seconds=10.0,
            directed_seconds=1.0,
        )

        self.assertFalse(below["pass"])
        self.assertEqual(below["full_seconds"], 9.999)
        self.assertEqual(below["directed_seconds"], 1.0)
        self.assertEqual(below["ratio"], 9.999)
        self.assertEqual(below["minimum_ratio"], 10.0)
        self.assertTrue(boundary["pass"])
        self.assertEqual(boundary["ratio"], boundary["minimum_ratio"])

    def test_help_exits_without_running_or_writing_results(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result_path = Path(temp) / "must-not-exist.json"
            stdout = io.StringIO()
            with (
                mock.patch.object(run_runtime_smoke, "RESULT_PATH", result_path),
                mock.patch.object(
                    run_runtime_smoke,
                    "scenario_local_delivery",
                    side_effect=AssertionError("--help executed smoke"),
                ),
                mock.patch.object(
                    run_runtime_smoke,
                    "scenario_directed_invariant_benchmark",
                    side_effect=AssertionError("--help executed benchmark"),
                ),
                contextlib.redirect_stdout(stdout),
                self.assertRaises(SystemExit) as raised,
            ):
                run_runtime_smoke.main(["--help"])

            self.assertEqual(raised.exception.code, 0)
            self.assertIn("usage:", stdout.getvalue().lower())
            self.assertFalse(result_path.exists())

    def test_explicit_out_writes_only_the_requested_report(self) -> None:
        smoke = {"name": "local_delivery_runtime", "pass": True}
        benchmark = {"name": "directed_invariant_benchmark", "pass": True}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            default_path = root / "default.json"
            requested_path = root / "requested.json"
            with (
                mock.patch.object(run_runtime_smoke, "RESULT_PATH", default_path),
                mock.patch.object(run_runtime_smoke, "scenario_local_delivery", return_value=smoke),
                mock.patch.object(
                    run_runtime_smoke,
                    "scenario_directed_invariant_benchmark",
                    return_value=benchmark,
                ),
            ):
                result = run_runtime_smoke.main(["--out", str(requested_path)])

            self.assertEqual(result, 0)
            self.assertFalse(default_path.exists())
            self.assertEqual(json.loads(requested_path.read_text(encoding="utf-8")), [smoke, benchmark])


if __name__ == "__main__":
    unittest.main()

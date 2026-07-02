from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "plugins/codex-project-harness/scripts/harness.py"


def run_harness(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["python3", str(HARNESS), "--root", str(root), *args], text=True, capture_output=True, check=False)
    if check and result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return result


def bootstrap(root: Path) -> None:
    run_harness(root, "init")
    run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")


class DispatchRouteAdviceTest(unittest.TestCase):
    def test_route_advice_marks_low_risk_developer_task_as_spark_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bootstrap(root)
            run_harness(root, "test-target", "add", "--id", "UNIT", "--kind", "unit", "--command-template", "python3 -m unittest")
            run_harness(root, "task", "add", "--id", "T1", "--task", "Small deterministic patch", "--owner", "developer", "--acceptance", "AC1")
            run_harness(root, "test-target", "link", "--task", "T1", "--target", "UNIT")

            report = json.loads(run_harness(root, "dispatch", "route-advice", "--json").stdout)
            task = report["tasks"][0]

        self.assertEqual(task["recommendation"], "host-codex-spark")
        self.assertTrue(task["spark_eligible"])
        self.assertEqual(report["summary"]["spark_eligible_count"], 1)
        self.assertIn("dispatch plan", report["next_commands"][0])

    def test_route_advice_with_run_id_emits_spark_policy_start_hint(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bootstrap(root)
            run_harness(root, "test-target", "add", "--id", "UNIT", "--kind", "unit", "--command-template", "python3 -m unittest")
            run_harness(root, "task", "add", "--id", "T1", "--task", "Small deterministic patch", "--owner", "developer", "--acceptance", "AC1")
            run_harness(root, "test-target", "link", "--task", "T1", "--target", "UNIT")
            run_id = run_harness(root, "dispatch", "plan", "--scope", "Spark candidates").stdout.strip().split()[-1]

            report = json.loads(run_harness(root, "dispatch", "route-advice", "--run-id", run_id, "--json").stdout)

        self.assertEqual(report["run_id"], run_id)
        self.assertEqual(report["summary"]["spark_eligible_count"], 1)
        self.assertIn("HARNESS_CODEX_MODEL_POLICY=spark-deterministic", report["next_commands"][0])
        self.assertIn("--provider host-codex", report["next_commands"][0])

    def test_route_advice_keeps_non_developer_high_risk_and_sandbox_tasks_off_spark(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bootstrap(root)
            run_harness(root, "test-target", "add", "--id", "UNIT", "--kind", "unit", "--command-template", "python3 -m unittest")
            run_harness(root, "test-target", "add", "--id", "SANDBOX", "--kind", "unit", "--command-template", "python3 -m unittest", "--requires-sandbox")
            run_harness(
                root,
                "failure-mode",
                "add",
                "--id",
                "FM1",
                "--feature",
                "payments",
                "--scenario",
                "wrong charge",
                "--trigger",
                "bad code",
                "--expected",
                "blocked",
                "--risk",
                "critical",
            )
            run_harness(root, "task", "add", "--id", "ARCH", "--task", "Architecture decision", "--owner", "architect", "--acceptance", "AC1")
            run_harness(root, "task", "add", "--id", "RISK", "--task", "Critical behavior change", "--owner", "developer", "--acceptance", "AC1", "--failure-mode", "FM1")
            run_harness(root, "task", "add", "--id", "SBOX", "--task", "Sandboxed change", "--owner", "developer", "--acceptance", "AC1")
            run_harness(root, "test-target", "link", "--task", "ARCH", "--target", "UNIT")
            run_harness(root, "test-target", "link", "--task", "RISK", "--target", "UNIT")
            run_harness(root, "test-target", "link", "--task", "SBOX", "--target", "SANDBOX")

            report = json.loads(run_harness(root, "dispatch", "route-advice", "--json").stdout)
            tasks = {task["task_id"]: task for task in report["tasks"]}

        self.assertFalse(tasks["ARCH"]["spark_eligible"])
        self.assertEqual(tasks["ARCH"]["recommendation"], "main-model-or-manual")
        self.assertIn("not developer", tasks["ARCH"]["reason"])
        self.assertFalse(tasks["RISK"]["spark_eligible"])
        self.assertEqual(tasks["RISK"]["recommendation"], "host-codex-default")
        self.assertIn("critical", tasks["RISK"]["reason"])
        self.assertFalse(tasks["SBOX"]["spark_eligible"])
        self.assertEqual(tasks["SBOX"]["recommendation"], "host-codex-default")
        self.assertIn("requires sandbox", tasks["SBOX"]["reason"])
        self.assertEqual(report["summary"]["spark_eligible_count"], 0)


if __name__ == "__main__":
    unittest.main()

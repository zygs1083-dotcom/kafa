from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "plugins" / "codex-project-harness" / "scripts" / "harness.py"


def run_harness(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, str(HARNESS), "--root", str(root), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return result


def git_init(root: Path) -> None:
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=root, check=True)


def write_tiny_python_project(root: Path) -> None:
    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (root / "test_calc.py").write_text(
        "import unittest\n\n"
        "from calc import add\n\n"
        "class CalcTest(unittest.TestCase):\n"
        "    def test_add(self):\n"
        "        self.assertEqual(add(2, 3), 5)\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "calc.py", "test_calc.py"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "initial tiny project"], cwd=root, check=True, capture_output=True, text=True)


class ColdStartGuidedLoopTest(unittest.TestCase):
    def test_uninitialized_commands_are_friendly(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            for args in [("status",), ("doctor",), ("validate",), ("cycle", "status")]:
                result = run_harness(root, *args, check=False)
                text = result.stdout + result.stderr
                self.assertNotIn("Traceback", text)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("not initialized", text)
                self.assertIn("harness.py --root", text)
                self.assertIn("init", text)

    def test_init_updates_gitignore_without_overwriting_user_rules(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".gitignore").write_text("custom.log\n", encoding="utf-8")

            run_harness(root, "init")

            text = (root / ".gitignore").read_text(encoding="utf-8")
            self.assertIn("custom.log", text)
            self.assertIn(".ai-team/state/", text)
            self.assertIn(".ai-team/runtime/", text)
            self.assertIn("__pycache__/", text)

    def test_quickstart_status_reports_checklist(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")

            report = json.loads(run_harness(root, "quickstart", "status", "--json").stdout)

            self.assertFalse(report["ready_for_delivery"])
            self.assertIn("requirement", report["missing"])
            self.assertIn("acceptance", report["missing"])
            self.assertIn("next_commands", report)

    def test_quickstart_minimal_execute_reaches_delivered_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            git_init(root)
            write_tiny_python_project(root)

            result = run_harness(
                root,
                "quickstart",
                "minimal",
                "--id",
                "SMOKE",
                "--goal",
                "Keep add working",
                "--acceptance",
                "add(2, 3) returns 5",
                "--task",
                "Verify calculator add",
                "--test-command",
                "python3 -B -m unittest discover -s . -p 'test_*.py'",
                "--execute",
            )
            cycle = json.loads(run_harness(root, "cycle", "status", "--json").stdout)
            status = run_harness(root, "status").stdout

            self.assertIn("OK: quickstart minimal delivered SMOKE", result.stdout)
            self.assertEqual(cycle["status"], "delivered")
            self.assertIn("phase: delivery_readiness", status)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                self.assertEqual(conn.execute("select count(*) from evidence").fetchone()[0], 1)
                self.assertEqual(conn.execute("select count(*) from deliveries").fetchone()[0], 1)
                self.assertEqual(conn.execute("select status from tasks where id = 'SMOKE-T1'").fetchone()[0], "accepted")

    def test_task_accept_ready_hides_review_lease_mechanics(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
            run_harness(root, "task", "add", "--id", "T1", "--task", "Example", "--acceptance", "AC1")
            claim = run_harness(root, "task", "claim", "T1", "--agent", "developer", "--expected-revision", "1").stdout
            token = claim.split("token=", 1)[1].split()[0]
            run_harness(root, "task", "start", "T1", "--agent", "developer", "--lease-token", token, "--expected-revision", "2")
            run_harness(root, "task", "submit", "T1", "--agent", "developer", "--lease-token", token, "--expected-revision", "3", "--evidence", "done")

            result = run_harness(root, "task", "accept-ready", "--id", "T1", "--agent", "qa-reviewer", "--evidence", "reviewed")

            self.assertIn("OK: task accepted T1", result.stdout)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                self.assertEqual(conn.execute("select status from tasks where id = 'T1'").fetchone()[0], "accepted")

    def test_validation_without_evidence_warns_audit_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")

            result = run_harness(
                root,
                "validation",
                "record",
                "--surface",
                "unit",
                "--acceptance",
                "AC1",
                "--commands",
                "echo ok",
                "--findings",
                "manual ok",
                "--result",
                "pass",
            )

            self.assertIn("audit-only", result.stdout)
            self.assertIn("will not satisfy delivery gate", result.stdout)


if __name__ == "__main__":
    unittest.main()

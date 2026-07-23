from __future__ import annotations

import json
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


def run_guided_command(root: Path, command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=root, shell=True, text=True, capture_output=True, check=False)


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
                self.assertIn("harness.py", text)
                self.assertIn("--root", text)
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

            envelope = json.loads(run_harness(root, "quickstart", "status", "--json").stdout)
            report = envelope["details"]

            self.assertFalse(report["ready_for_delivery"])
            self.assertIn("requirement", report["missing"])
            self.assertIn("acceptance", report["missing"])
            self.assertIn("next_commands", report)

    def test_quickstart_minimal_execute_stops_before_independent_review(self) -> None:
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
            status = run_harness(root, "status", "--verbose").stdout

            self.assertIn("OK: quickstart minimal verified setup SMOKE", result.stdout)
            self.assertIn("OK: verify run execution=", result.stdout)
            self.assertIn("OK: task submitted SMOKE-T1", result.stdout)
            self.assertIn("NEXT: stop for independent review of SMOKE-T1", result.stdout)
            self.assertNotIn("task accept", result.stdout)
            self.assertNotIn("gate record", result.stdout)
            self.assertNotIn("delivery record", result.stdout)
            self.assertEqual(cycle["status"], "active")
            self.assertIn("schema_version: 31", status)
            self.assertIn("tasks: 1", status)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                self.assertEqual(conn.execute("select count(*) from executions").fetchone()[0], 1)
                self.assertEqual(conn.execute("select count(*) from validations").fetchone()[0], 1)
                self.assertEqual(conn.execute("select count(*) from validation_executions").fetchone()[0], 1)
                self.assertEqual(
                    conn.execute(
                        "select count(*) from events where event_type = 'verification_recorded'"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    conn.execute(
                        """
                        select e.target_id, e.exit_code, e.executed_count, e.semantic_status,
                               v.result, v.validation_status
                        from executions e
                        join validation_executions ve on ve.execution_id = e.id
                        join validations v on v.id = ve.validation_id
                        """
                    ).fetchone(),
                    ("SMOKE-UNIT", 0, 1, "pass", "pass", "active"),
                )
                self.assertEqual(conn.execute("select count(*) from quality_gates").fetchone()[0], 0)
                self.assertEqual(conn.execute("select count(*) from deliveries").fetchone()[0], 0)
                self.assertEqual(conn.execute("select status from tasks where id = 'SMOKE-T1'").fetchone()[0], "submitted")

    def test_quickstart_status_stops_at_independent_review_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            git_init(root)
            write_tiny_python_project(root)
            run_harness(
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

            report = json.loads(
                run_harness(root, "quickstart", "status", "--json").stdout
            )["details"]

            self.assertFalse(report["ready_for_delivery"])
            self.assertEqual(report["cycle_status"], "active")
            self.assertIn("accepted_task", report["missing"])
            self.assertIn("quality_gate", report["missing"])
            self.assertIn("delivery", report["missing"])
            self.assertNotIn("controller_execution", report["missing"])
            self.assertFalse(any("task accept" in command for command in report["next_commands"]))
            self.assertFalse(any("gate record" in command for command in report["next_commands"]))
            self.assertFalse(any(" delivery record" in command for command in report["next_commands"]))
            self.assertFalse(any(" phase " in command for command in report["next_commands"]))
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                self.assertEqual(conn.execute("select count(*) from quality_gates").fetchone()[0], 0)
                self.assertEqual(conn.execute("select count(*) from deliveries").fetchone()[0], 0)

    def test_candidate_change_requires_new_verify_run_and_reuses_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            git_init(root)
            write_tiny_python_project(root)
            run_harness(
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

            (root / "calc.py").write_text(
                "def add(a, b):\n    return a + b\n\n# candidate revision\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "calc.py"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "candidate revision"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )

            report = json.loads(
                run_harness(root, "quickstart", "status", "--json").stdout
            )["details"]
            verify_command = next(
                command for command in report["next_commands"] if "verify run" in command
            )

            self.assertIn("controller_execution", report["missing"])
            self.assertIn(str(HARNESS.resolve()), verify_command)
            self.assertIn("verify run --target SMOKE-UNIT --acceptance SMOKE-AC1", verify_command)
            self.assertFalse(any("test-target add" in command for command in report["next_commands"]))
            self.assertFalse(any("test-target link" in command for command in report["next_commands"]))
            verified = run_guided_command(root, verify_command)
            self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)

            followup = json.loads(
                run_harness(root, "quickstart", "status", "--json").stdout
            )["details"]
            self.assertNotIn("controller_execution", followup["missing"])
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                self.assertEqual(conn.execute("select count(*) from executions").fetchone()[0], 2)
                self.assertEqual(conn.execute("select count(*) from validation_executions").fetchone()[0], 2)

    def test_task_lifecycle_needs_no_review_lease_mechanics(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
            run_harness(root, "task", "add", "--id", "T1", "--task", "Example", "--acceptance", "AC1")
            run_harness(root, "task", "start", "T1")
            run_harness(root, "task", "submit", "T1", "--context-id", "producer-context", "--evidence", "done")

            result = run_harness(root, "task", "accept", "T1", "--evidence", "reviewed")

            self.assertIn("OK: task accepted T1", result.stdout)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                self.assertEqual(conn.execute("select status from tasks where id = 'T1'").fetchone()[0], "accepted")

    def test_manual_validation_judgment_without_execution_is_audit_only(self) -> None:
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
                "--findings",
                "manual ok",
                "--result",
                "pass",
            )

            self.assertIn("audit-only", result.stdout)
            self.assertIn("use verify run", result.stdout)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                self.assertEqual(conn.execute("select count(*) from validations").fetchone()[0], 1)
                self.assertEqual(conn.execute("select count(*) from executions").fetchone()[0], 0)
                self.assertEqual(conn.execute("select count(*) from validation_executions").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()

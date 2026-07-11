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
            status = run_harness(root, "status").stdout

            self.assertIn("OK: quickstart minimal verified setup SMOKE", result.stdout)
            self.assertIn("NEXT: reviewer must review", result.stdout)
            self.assertIn("fresh requires an attested independent session", result.stdout)
            self.assertEqual(cycle["status"], "active")
            self.assertIn("phase: implementation", status)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                self.assertEqual(conn.execute("select count(*) from evidence").fetchone()[0], 1)
                self.assertEqual(conn.execute("select count(*) from deliveries").fetchone()[0], 0)
                self.assertEqual(conn.execute("select status from tasks where id = 'SMOKE-T1'").fetchone()[0], "submitted")

    def test_quickstart_guidance_uses_executable_commands_real_ids_and_legal_phases(self) -> None:
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

            report = json.loads(run_harness(root, "quickstart", "status", "--json").stdout)
            accept_command = next(command for command in report["next_commands"] if "task accept-ready" in command)
            self.assertIn(str(HARNESS.resolve()), accept_command)
            self.assertIn("--id SMOKE-T1", accept_command)
            self.assertNotIn("--id T1 ", accept_command)
            self.assertEqual(run_guided_command(root, accept_command).returncode, 0)

            report = json.loads(run_harness(root, "quickstart", "status", "--json").stdout)
            gate_command = next(command for command in report["next_commands"] if "gate record" in command)
            self.assertIn("--reviewer-context same-context-degraded", gate_command)
            self.assertEqual(run_guided_command(root, gate_command).returncode, 0)

            report = json.loads(run_harness(root, "quickstart", "status", "--json").stdout)
            completion_commands = [
                command
                for command in report["next_commands"]
                if " phase " in command or " delivery record " in command
            ]
            self.assertEqual(len(completion_commands), 3)
            self.assertIn("phase qa", completion_commands[0])
            self.assertIn("phase delivery_readiness", completion_commands[1])
            self.assertIn("delivery record", completion_commands[2])
            for command in completion_commands:
                result = run_guided_command(root, command)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            cycle = json.loads(run_harness(root, "cycle", "status", "--json").stdout)

        self.assertEqual(cycle["status"], "delivered")

    def test_new_cycle_guidance_requires_current_cycle_evidence_and_reuses_target(self) -> None:
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
            run_harness(root, "task", "accept-ready", "--id", "SMOKE-T1", "--agent", "qa-reviewer", "--evidence", "reviewed")
            run_harness(root, "gate", "record", "--reviewer-context", "same-context-degraded", "--result", "pass")
            run_harness(root, "phase", "qa")
            run_harness(root, "phase", "delivery_readiness")
            run_harness(root, "delivery", "record", "--scope", "first cycle")
            run_harness(root, "cycle", "start", "--id", "CYCLE-2", "--name", "Second cycle", "--goal", "Continue")
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                started_at = conn.execute("select started_at from delivery_cycles where id = 'CYCLE-2'").fetchone()[0]
                conn.execute("update evidence set created_at = ?", (started_at,))
                conn.commit()

            report = json.loads(run_harness(root, "quickstart", "status", "--json").stdout)

            for command in report["next_commands"]:
                result = run_guided_command(root, command)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            followup = json.loads(run_harness(root, "quickstart", "status", "--json").stdout)
            for command in followup["next_commands"]:
                result = run_guided_command(root, command)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            cycle = json.loads(run_harness(root, "cycle", "status", "--json").stdout)

        self.assertIn("controller_evidence", report["missing"])
        target_commands = [command for command in report["next_commands"] if "test-target" in command]
        self.assertTrue(any("test-target link --task T1 --target SMOKE-UNIT" in command for command in target_commands))
        self.assertFalse(any("test-target add" in command for command in target_commands))
        self.assertEqual(cycle["status"], "delivered")

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

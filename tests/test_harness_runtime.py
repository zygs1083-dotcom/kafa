from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "plugins" / "codex-project-harness" / "scripts"
HARNESS = SCRIPTS / "harness.py"
RUNTIME_CLI = REPO_ROOT / "plugins" / "codex-project-harness" / "skills" / "project-harness" / "scripts" / "harness.py"
DEFAULT_TEST_COMMAND = "python3 -B -m unittest test_harness_dummy.py"


def run_harness(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(HARNESS), *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=check,
    )


def ensure_dummy_unittest(root: Path) -> None:
    (root / "test_harness_dummy.py").write_text(
        "import unittest\n\n"
        "class HarnessDummyTest(unittest.TestCase):\n"
        "    def test_dummy(self):\n"
        "        self.assertTrue(True)\n",
        encoding="utf-8",
    )


class HarnessRuntimeValidationTest(unittest.TestCase):
    def make_project(self) -> tempfile.TemporaryDirectory[str]:
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        run_harness(root, "init")
        run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example acceptance")
        run_harness(
            root,
            "task",
            "add",
            "--id",
            "T1",
            "--task",
            "Example task",
            "--owner",
            "developer",
            "--acceptance",
            "AC1",
        )
        run_harness(root, "task", "start", "T1")
        run_harness(
            root,
            "task",
            "submit",
            "T1",
            "--context-id",
            "producer-context",
            "--evidence",
            "example evidence",
        )
        run_harness(root, "task", "accept", "T1", "--evidence", "reviewed")
        return temp

    def add_pass_validation(self, root: Path, *, failure_mode: bool = True) -> None:
        ensure_dummy_unittest(root)
        run_harness(root, "test-target", "add", "--id", "TARGET1", "--kind", "unit", "--command-template", DEFAULT_TEST_COMMAND)
        command = [
            "verify",
            "run",
            "--target",
            "TARGET1",
            "--acceptance",
            "AC1",
        ]
        if failure_mode:
            command.extend(["--failure-mode", "FM1"])
        result = run_harness(root, *command)
        self.assertIn("OK: verification recorded execution=", result.stdout)
        self.assertIn(" validation=", result.stdout)

    def add_failure_mode(self, root: Path, status: str = "identified", risk: str = "low") -> None:
        run_harness(
            root,
            "failure-mode",
            "add",
            "--id",
            "FM1",
            "--feature",
            "Example",
            "--scenario",
            "Critical path",
            "--trigger",
            "bad input",
            "--expected",
            "safe failure",
            "--risk",
            risk,
            "--acceptance",
            "AC1",
            "--status",
            status,
        )

    def add_quality_gate(self, root: Path, result: str = "pass", reviewer_context: str = "same-context-degraded") -> None:
        run_harness(
            root,
            "gate",
            "record",
            "--reviewer-context",
            reviewer_context,
            "--result",
            result,
        )

    def validate(self, root: Path) -> subprocess.CompletedProcess[str]:
        return run_harness(root, "validate", "--delivery", check=False)

    def test_delivery_passes_with_closed_failure_mode_and_passing_gate(self) -> None:
        with self.make_project() as temp:
            root = Path(temp)
            self.add_failure_mode(root)
            self.add_pass_validation(root)
            self.add_quality_gate(root, result="pass")

            result = self.validate(root)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OK: harness state is valid", result.stdout)

    def test_empty_quality_gate_table_is_not_a_gate(self) -> None:
        with self.make_project() as temp:
            root = Path(temp)
            self.add_failure_mode(root)
            self.add_pass_validation(root)

            result = self.validate(root)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("delivery requires a quality gate record", result.stdout)

    def test_failed_quality_gate_blocks_delivery(self) -> None:
        with self.make_project() as temp:
            root = Path(temp)
            self.add_failure_mode(root)
            self.add_pass_validation(root)
            self.add_quality_gate(root, result="fail")

            result = self.validate(root)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("latest quality gate is not pass", result.stdout)

    def test_audit_only_failed_validation_blocks_delivery(self) -> None:
        with self.make_project() as temp:
            root = Path(temp)
            self.add_failure_mode(root)
            recorded = run_harness(
                root,
                "validation",
                "record",
                "--surface",
                "Example behavior",
                "--acceptance",
                "AC1",
                "--findings",
                "failed",
                "--result",
                "fail",
                "--failure-mode",
                "FM1",
            )
            self.assertIn("audit-only", recorded.stdout)
            self.add_quality_gate(root, result="pass")

            result = self.validate(root)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("validation is not pass", result.stdout)

    def test_open_critical_failure_mode_blocks_delivery(self) -> None:
        with self.make_project() as temp:
            root = Path(temp)
            self.add_failure_mode(root, status="identified", risk="critical")
            self.add_pass_validation(root, failure_mode=False)
            self.add_quality_gate(root, result="pass")

            result = self.validate(root)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("critical failure mode is not covered", result.stdout)

    def test_project_harness_cli_locates_plugin_scripts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            init = subprocess.run(
                ["python3", str(RUNTIME_CLI), "--root", str(root), "init"],
                text=True,
                capture_output=True,
                check=False,
            )
            status = subprocess.run(
                ["python3", str(RUNTIME_CLI), "--root", str(root), "status"],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(init.returncode, 0, init.stdout + init.stderr)
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        self.assertIn("OK: project harness initialized", init.stdout)

    def test_decision_cli_records_sqlite_projection_and_compact_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            result = run_harness(
                root,
                "decision",
                "record",
                "--decision",
                'Use SQLite "runtime"',
                "--reason",
                "Avoid split-brain markdown writes",
            )
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                decision = conn.execute("select decision, reason from decisions").fetchone()
                event_json = conn.execute(
                    "select after_json from events where event_type = 'decision_recorded'"
                ).fetchone()[0]
            rendered = (root / ".ai-team/control/decision-log.md").read_text(encoding="utf-8")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            decision,
            ('Use SQLite "runtime"', "Avoid split-brain markdown writes"),
        )
        self.assertEqual(json.loads(event_json)["decision"], 'Use SQLite "runtime"')
        self.assertIn('Use SQLite "runtime"', rendered)


if __name__ == "__main__":
    unittest.main()

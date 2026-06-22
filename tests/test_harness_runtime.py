from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "plugins" / "codex-project-harness" / "scripts"
RUNTIME_CLI = REPO_ROOT / "plugins" / "codex-project-harness" / "skills" / "project-runtime" / "scripts" / "harness.py"


def run_script(root: Path, script: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(SCRIPTS / script), *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=check,
    )


class HarnessRuntimeValidationTest(unittest.TestCase):
    def make_project(self) -> tempfile.TemporaryDirectory[str]:
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        run_script(root, "init_project_harness.py")
        run_script(root, "add_acceptance.py", "--id", "AC1", "--criterion", "Example acceptance")
        run_script(
            root,
            "add_task.py",
            "--id",
            "T1",
            "--task",
            "Example task",
            "--owner",
            "developer",
            "--acceptance",
            "AC1",
        )
        run_script(root, "harness.py", "task", "start", "T1", "--agent", "developer")
        run_script(root, "harness.py", "task", "complete", "T1", "--evidence", "example evidence")
        return temp

    def add_pass_validation(self, root: Path) -> None:
        run_script(
            root,
            "record_validation.py",
            "--surface",
            "Example behavior",
            "--acceptance",
            "AC1",
            "--commands",
            "example test",
            "--findings",
            "passed",
            "--result",
            "pass",
        )

    def add_failure_mode(self, root: Path, status: str = "covered") -> None:
        run_script(
            root,
            "add_failure_mode.py",
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
            "critical",
            "--test-mapping",
            "AC1",
            "--status",
            status,
        )

    def add_quality_gate(self, root: Path, result: str = "pass", reviewer_context: str = "fresh") -> None:
        run_script(
            root,
            "record_quality_gate.py",
            "--reviewer-context",
            reviewer_context,
            "--result",
            result,
            "--commands",
            "example test",
            "--evidence",
            "reviewed",
        )

    def validate(self, root: Path) -> subprocess.CompletedProcess[str]:
        return run_script(root, "validate_harness_state.py", check=False)

    def test_delivery_passes_with_closed_failure_mode_and_passing_gate(self) -> None:
        with self.make_project() as temp:
            root = Path(temp)
            self.add_pass_validation(root)
            self.add_failure_mode(root, status="covered")
            self.add_quality_gate(root, result="pass")

            result = self.validate(root)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OK: harness state is valid", result.stdout)

    def test_empty_quality_gate_table_is_not_a_gate(self) -> None:
        with self.make_project() as temp:
            root = Path(temp)
            self.add_pass_validation(root)
            self.add_failure_mode(root, status="covered")

            result = self.validate(root)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("delivery requires a quality gate record", result.stdout)

    def test_failed_quality_gate_blocks_delivery(self) -> None:
        with self.make_project() as temp:
            root = Path(temp)
            self.add_pass_validation(root)
            self.add_failure_mode(root, status="covered")
            self.add_quality_gate(root, result="fail")

            result = self.validate(root)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("latest quality gate is not pass", result.stdout)

    def test_failed_validation_blocks_delivery(self) -> None:
        with self.make_project() as temp:
            root = Path(temp)
            run_script(
                root,
                "record_validation.py",
                "--surface",
                "Example behavior",
                "--acceptance",
                "AC1",
                "--commands",
                "example test",
                "--findings",
                "failed",
                "--result",
                "fail",
            )
            self.add_failure_mode(root, status="covered")
            self.add_quality_gate(root, result="pass")

            result = self.validate(root)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("validation is not pass", result.stdout)

    def test_open_critical_failure_mode_blocks_delivery(self) -> None:
        with self.make_project() as temp:
            root = Path(temp)
            self.add_pass_validation(root)
            self.add_failure_mode(root, status="identified")
            self.add_quality_gate(root, result="pass")

            result = self.validate(root)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("critical failure mode is not closed", result.stdout)

    def test_project_runtime_cli_locates_plugin_scripts(self) -> None:
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


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import hashlib
import subprocess
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "plugins" / "codex-project-harness" / "scripts"
RUNTIME_CLI = REPO_ROOT / "plugins" / "codex-project-harness" / "skills" / "project-runtime" / "scripts" / "harness.py"
DEFAULT_TEST_COMMAND = "python3 -B -m unittest test_harness_dummy.py"


def run_script(root: Path, script: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(SCRIPTS / script), *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=check,
    )


def task_revision(root: Path, task_id: str) -> int:
    with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
        return int(conn.execute("select revision from tasks where id = ?", (task_id,)).fetchone()[0])


def token_from_stdout(stdout: str) -> str:
    return stdout.split("token=", 1)[1].split(None, 1)[0].strip()


def trusted_artifact(root: Path, suffix: str = "1", *, content: str = "ok\n") -> tuple[str, str]:
    artifact = root / ".ai-team" / "runtime" / "test-artifacts" / f"stdout-{suffix}.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(content, encoding="utf-8")
    return artifact.relative_to(root).as_posix(), hashlib.sha256(content.encode("utf-8")).hexdigest()


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
        claim = run_script(root, "harness.py", "task", "claim", "T1", "--agent", "developer", "--expected-revision", str(task_revision(root, "T1")))
        producer_token = token_from_stdout(claim.stdout)
        run_script(root, "harness.py", "task", "start", "T1", "--agent", "developer", "--lease-token", producer_token, "--expected-revision", str(task_revision(root, "T1")))
        run_script(root, "harness.py", "task", "submit", "T1", "--agent", "developer", "--lease-token", producer_token, "--expected-revision", str(task_revision(root, "T1")), "--evidence", "example evidence")
        review = run_script(root, "harness.py", "task", "review", "T1", "--agent", "qa-reviewer", "--expected-revision", str(task_revision(root, "T1")))
        reviewer_token = token_from_stdout(review.stdout)
        run_script(root, "harness.py", "task", "accept", "T1", "--agent", "qa-reviewer", "--lease-token", reviewer_token, "--expected-revision", str(task_revision(root, "T1")), "--evidence", "reviewed")
        return temp

    def add_pass_validation(self, root: Path, *, failure_mode: bool = True) -> None:
        ensure_dummy_unittest(root)
        run_script(root, "harness.py", "test-target", "add", "--id", "TARGET1", "--kind", "unit", "--command-template", DEFAULT_TEST_COMMAND)
        evidence_result = run_script(
            root,
            "harness.py",
            "dispatch",
            "run",
            "--agent",
            "developer",
            "--target",
            "TARGET1",
            "--command",
            DEFAULT_TEST_COMMAND,
            "--code-identity",
            "content-hash",
        )
        evidence_id = evidence_result.stdout.strip().rsplit(" ", 1)[-1]
        run_script(root, "harness.py", "test", "record", "--id", "TEST1", "--surface", "Example behavior", "--command", DEFAULT_TEST_COMMAND, "--result", "pass", "--evidence", evidence_id)
        command = [
            "record_validation.py",
            "--surface",
            "Example behavior",
            "--acceptance",
            "AC1",
            "--commands",
            DEFAULT_TEST_COMMAND,
            "--findings",
            "passed",
            "--result",
            "pass",
            "--test",
            "TEST1",
            "--evidence",
            evidence_id,
            "--target",
            "TARGET1",
            "--code-identity",
            "content-hash",
        ]
        if failure_mode:
            command.extend(["--failure-mode", "FM1"])
        run_script(
            root,
            *command,
        )

    def add_failure_mode(self, root: Path, status: str = "identified", risk: str = "low") -> None:
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
            risk,
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

    def test_failed_validation_blocks_delivery(self) -> None:
        with self.make_project() as temp:
            root = Path(temp)
            self.add_failure_mode(root)
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
                "--failure-mode",
                "FM1",
            )
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

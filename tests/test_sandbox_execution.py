from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "plugins/codex-project-harness/scripts/harness.py"
PLUGIN_ROOT = REPO_ROOT / "plugins/codex-project-harness"
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import harness_db  # noqa: E402


def run_harness(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["python3", str(HARNESS), "--root", str(root), *args], text=True, capture_output=True, check=False)
    if check and result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return result


def git_repo(root: Path) -> None:
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
    (root / "test_sample.py").write_text(
        "import unittest\n\nclass Sample(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "test_sample.py"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)


def make_reported_attempt(root: Path) -> tuple[str, str]:
    run_harness(root, "init")
    run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
    run_harness(root, "test-target", "add", "--id", "UNIT", "--kind", "unit", "--command-template", "python3 -m unittest test_sample.py")
    run_harness(root, "task", "add", "--id", "T1", "--task", "Example", "--acceptance", "AC1")
    run_harness(root, "test-target", "link", "--task", "T1", "--target", "UNIT")
    run_id = run_harness(root, "dispatch", "plan", "--scope", "Sandbox").stdout.strip().split()[-1]
    branch = f"agent/{run_id}/T1/developer"
    subprocess.run(["git", "branch", branch, "HEAD"], cwd=root, check=True, capture_output=True)
    head = subprocess.run(["git", "rev-parse", branch], cwd=root, text=True, capture_output=True, check=True).stdout.strip()
    tree = subprocess.run(["git", "rev-parse", f"{branch}^{{tree}}"], cwd=root, text=True, capture_output=True, check=True).stdout.strip()
    with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
        conn.execute("update dispatch_assignments set agent_id = 'developer', status = 'reported' where run_id = ? and task_id = 'T1'", (run_id,))
        conn.execute(
            """
            insert into task_attempts
            (id, run_id, task_id, agent_id, fence, base_commit_sha, head_commit_sha, tree_sha,
             branch_name, target_id, status, provider_session_id, agent_session_id, report_id, evidence_id, started_at, finished_at)
            values ('ATTEMPT1', ?, 'T1', 'developer', 0, ?, ?, ?, ?, 'UNIT', 'reported', '', '', '', '', 'now', '')
            """,
            (run_id, head, head, tree, branch),
        )
        conn.execute(
            """
            insert into dispatch_worktrees
            (id, run_id, task_id, agent_id, branch_name, worktree_path, status, created_at, cleaned_at)
            values ('WT1', ?, 'T1', 'developer', ?, '', 'active', 'now', '')
            """,
            (run_id, branch),
        )
        conn.commit()
    return run_id, branch


class SandboxExecutionTest(unittest.TestCase):
    def test_container_verify_attempt_fails_closed_when_engine_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            git_repo(root)
            run_id, _branch = make_reported_attempt(root)

            with mock.patch("core.agent_runner.shutil.which", return_value=None):
                with self.assertRaises(harness_db.HarnessError) as ctx:
                    harness_db.dispatch_verify_attempt(root, run_id, "T1", runner="container")

            self.assertIn("sandbox-unavailable", str(ctx.exception))
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                attempt = conn.execute("select status, evidence_id from task_attempts where id = 'ATTEMPT1'").fetchone()
                evidence_count = conn.execute("select count(*) from evidence").fetchone()[0]
                sandbox_count = conn.execute("select count(*) from sandbox_executions").fetchone()[0]
            self.assertEqual(attempt, ("reported", ""))
            self.assertEqual(evidence_count, 0)
            self.assertEqual(sandbox_count, 0)

    def test_container_image_precedence_cli_then_control_then_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            control = root / ".ai-team/control/container-image.txt"
            control.write_text("python:3.11-slim\n", encoding="utf-8")

            self.assertEqual(harness_db.resolve_container_image(root, "python:3.10-slim"), "python:3.10-slim")
            self.assertEqual(harness_db.resolve_container_image(root, ""), "python:3.11-slim")
            control.write_text("\n", encoding="utf-8")
            self.assertEqual(harness_db.resolve_container_image(root, ""), "python:3.12-slim")

    def test_schema_21_migration_adds_sandbox_and_integration_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute("drop table sandbox_executions")
                conn.execute("drop table integration_attempts")
                for table in ("evidence", "validations"):
                    conn.execute(f"alter table {table} drop column sandbox_execution_id")
                    conn.execute(f"alter table {table} drop column sandbox_engine")
                    conn.execute(f"alter table {table} drop column container_image")
                conn.execute("update project set schema_version = 21, runtime_version = '3.9.0'")
                conn.commit()

            run_harness(root, "migrate", "--from-version", "21", "--to-version", "26")

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                tables = {row[0] for row in conn.execute("select name from sqlite_master where type='table'")}
                evidence_columns = {row[1] for row in conn.execute("pragma table_info(evidence)")}
                validation_columns = {row[1] for row in conn.execute("pragma table_info(validations)")}
            doctor = run_harness(root, "doctor")
            self.assertIn("sandbox_executions", tables)
            self.assertIn("integration_attempts", tables)
            self.assertIn("sandbox_execution_id", evidence_columns)
            self.assertIn("container_image", validation_columns)
            self.assertIn("OK: harness doctor passed", doctor.stdout)


if __name__ == "__main__":
    unittest.main()

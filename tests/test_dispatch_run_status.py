from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


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


def prepare_two_reported_attempts(root: Path) -> str:
    run_harness(root, "init")
    for acceptance_id, task_id in (("AC1", "T1"), ("AC2", "T2")):
        run_harness(root, "acceptance", "add", "--id", acceptance_id, "--criterion", f"Criterion {acceptance_id}")
        run_harness(root, "task", "add", "--id", task_id, "--task", f"Task {task_id}", "--acceptance", acceptance_id)
    run_harness(root, "test-target", "add", "--id", "UNIT", "--kind", "unit", "--command-template", "python3 -m unittest test_sample.py")
    for task_id in ("T1", "T2"):
        run_harness(root, "test-target", "link", "--task", task_id, "--target", "UNIT")
    run_id = run_harness(root, "dispatch", "plan", "--scope", "P1 aggregate").stdout.strip().split()[-1]
    with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
        for task_id in ("T1", "T2"):
            branch = f"agent/{run_id}/{task_id}/developer"
            subprocess.run(["git", "branch", branch, "HEAD"], cwd=root, check=True, capture_output=True)
            head = subprocess.run(["git", "rev-parse", branch], cwd=root, text=True, capture_output=True, check=True).stdout.strip()
            tree = subprocess.run(["git", "rev-parse", f"{branch}^{{tree}}"], cwd=root, text=True, capture_output=True, check=True).stdout.strip()
            attempt_id = f"ATTEMPT-{task_id}"
            conn.execute(
                "update dispatch_assignments set agent_id = 'developer', status = 'reported' where run_id = ? and task_id = ?",
                (run_id, task_id),
            )
            conn.execute(
                """
                insert into task_attempts
                (id, run_id, task_id, agent_id, fence, base_commit_sha, head_commit_sha, tree_sha,
                 branch_name, target_id, status, provider_session_id, agent_session_id, report_id, evidence_id, started_at, finished_at)
                values (?, ?, ?, 'developer', 0, ?, ?, ?, ?, 'UNIT', 'reported', '', '', '', '', 'now', '')
                """,
                (attempt_id, run_id, task_id, head, head, tree, branch),
            )
            conn.execute(
                """
                insert into dispatch_worktrees
                (id, run_id, task_id, agent_id, branch_name, worktree_path, status, created_at, cleaned_at)
                values (?, ?, ?, 'developer', ?, '', 'active', 'now', '')
                """,
                (f"WT-{task_id}", run_id, task_id, branch),
            )
        conn.execute("update dispatch_runs set status = 'reported' where id = ?", (run_id,))
        conn.commit()
    return run_id


def run_status(root: Path, run_id: str) -> str:
    with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
        return conn.execute("select status from dispatch_runs where id = ?", (run_id,)).fetchone()[0]


class DispatchRunStatusAggregationTest(unittest.TestCase):
    def test_run_is_not_completed_until_all_assignments_are_completed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            git_repo(root)
            run_id = prepare_two_reported_attempts(root)

            run_harness(root, "dispatch", "verify-attempt", "--run-id", run_id, "--task", "T1")

            self.assertEqual(run_status(root, run_id), "reported")

            run_harness(root, "dispatch", "verify-attempt", "--run-id", run_id, "--task", "T2")

            self.assertEqual(run_status(root, run_id), "completed")

    def test_failure_status_takes_priority_over_completed_assignments(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            git_repo(root)
            run_id = prepare_two_reported_attempts(root)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute("update dispatch_assignments set status = 'completed' where run_id = ? and task_id = 'T1'", (run_id,))
                conn.execute("update dispatch_assignments set status = 'verification_failed' where run_id = ? and task_id = 'T2'", (run_id,))
                conn.commit()

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.row_factory = sqlite3.Row
                harness_db.refresh_dispatch_run_status(conn, run_id)
                conn.commit()
            self.assertEqual(run_status(root, run_id), "verification_failed")


if __name__ == "__main__":
    unittest.main()

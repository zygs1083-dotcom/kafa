from __future__ import annotations

import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "plugins" / "codex-project-harness" / "scripts" / "harness.py"


def run_harness(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(HARNESS), "--root", str(root), *args],
        text=True,
        capture_output=True,
        check=check,
    )


def stdout_field(stdout: str, name: str) -> str:
    marker = f"{name}="
    tail = stdout.split(marker, 1)[1]
    return tail.split(None, 1)[0].strip()


def task_revision(root: Path, task_id: str) -> int:
    with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
        return int(conn.execute("select revision from tasks where id = ?", (task_id,)).fetchone()[0])


def task_fence(root: Path, task_id: str) -> int:
    with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
        return int(conn.execute("select fence from tasks where id = ?", (task_id,)).fetchone()[0])


def init_task(root: Path, task_id: str = "T1") -> None:
    run_harness(root, "init")
    run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
    run_harness(root, "task", "add", "--id", task_id, "--task", "Fence flow", "--acceptance", "AC1")


def claim(root: Path, task_id: str, agent: str) -> tuple[str, int]:
    result = run_harness(root, "task", "claim", task_id, "--agent", agent, "--expected-revision", str(task_revision(root, task_id)))
    return stdout_field(result.stdout, "token"), int(stdout_field(result.stdout, "fence"))


def review(root: Path, task_id: str, agent: str) -> tuple[str, int]:
    result = run_harness(root, "task", "review", task_id, "--agent", agent, "--expected-revision", str(task_revision(root, task_id)))
    return stdout_field(result.stdout, "token"), int(stdout_field(result.stdout, "fence"))


class TaskFencingTest(unittest.TestCase):
    def test_stale_holder_cannot_write_after_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            init_task(root)
            token_a, fence_a = claim(root, "T1", "developer")
            run_harness(root, "task", "start", "T1", "--agent", "developer", "--lease-token", token_a, "--expected-revision", str(task_revision(root, "T1")), "--fence", str(fence_a))

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute("update tasks set lease_expires_at = '2000-01-01T00:00:00+00:00' where id = 'T1'")
                conn.commit()
            run_harness(root, "task", "recover-stale")
            recovered_fence = task_fence(root, "T1")
            self.assertEqual(recovered_fence, fence_a + 1)

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute(
                    """
                    update tasks set status = 'in_progress', lease_agent = 'developer',
                      lease_token = ?, lease_expires_at = '2099-01-01T00:00:00+00:00'
                    where id = 'T1'
                    """,
                    (token_a,),
                )
                conn.execute("update agents set lease_task_id = 'T1', status = 'leased' where id = 'developer'")
                conn.commit()
                revision = conn.execute("select revision from tasks where id = 'T1'").fetchone()[0]

            stale = run_harness(
                root,
                "task",
                "submit",
                "T1",
                "--agent",
                "developer",
                "--lease-token",
                token_a,
                "--expected-revision",
                str(revision),
                "--fence",
                str(fence_a),
                "--evidence",
                "stale overwrite",
                check=False,
            )

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                task = conn.execute("select status, evidence, fence from tasks where id = 'T1'").fetchone()
                conn.execute(
                    """
                    update tasks set status = 'ready', lease_agent = null, lease_token = null,
                      lease_heartbeat_at = null, lease_expires_at = null
                    where id = 'T1'
                    """
                )
                conn.execute("update agents set lease_task_id = '', status = 'available' where id = 'developer'")
                conn.commit()

            self.assertNotEqual(stale.returncode, 0)
            self.assertIn("fence-stale", stale.stdout)
            self.assertEqual(task, ("in_progress", "", recovered_fence))

            token_b, fence_b = claim(root, "T1", "qa-reviewer")
            self.assertEqual(fence_b, recovered_fence)
            run_harness(root, "task", "start", "T1", "--agent", "qa-reviewer", "--lease-token", token_b, "--expected-revision", str(task_revision(root, "T1")), "--fence", str(fence_b))
            run_harness(root, "task", "submit", "T1", "--agent", "qa-reviewer", "--lease-token", token_b, "--expected-revision", str(task_revision(root, "T1")), "--fence", str(fence_b), "--evidence", "fresh work")
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                self.assertEqual(conn.execute("select status, evidence from tasks where id = 'T1'").fetchone(), ("submitted", "fresh work"))

    def test_stale_reviewer_cannot_accept_after_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            init_task(root)
            producer_token, producer_fence = claim(root, "T1", "developer")
            run_harness(root, "task", "start", "T1", "--agent", "developer", "--lease-token", producer_token, "--expected-revision", str(task_revision(root, "T1")), "--fence", str(producer_fence))
            run_harness(root, "task", "submit", "T1", "--agent", "developer", "--lease-token", producer_token, "--expected-revision", str(task_revision(root, "T1")), "--fence", str(producer_fence), "--evidence", "ready for review")
            reviewer_token, reviewer_fence = review(root, "T1", "qa-reviewer")

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute("update tasks set lease_expires_at = '2000-01-01T00:00:00+00:00' where id = 'T1'")
                conn.commit()
            run_harness(root, "task", "recover-stale")
            recovered_fence = task_fence(root, "T1")
            self.assertEqual(recovered_fence, reviewer_fence + 1)

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute(
                    """
                    update tasks set status = 'review', lease_agent = 'qa-reviewer',
                      lease_token = ?, lease_expires_at = '2099-01-01T00:00:00+00:00'
                    where id = 'T1'
                    """,
                    (reviewer_token,),
                )
                conn.execute("update agents set lease_task_id = 'T1', status = 'leased' where id = 'qa-reviewer'")
                conn.commit()
                revision = conn.execute("select revision from tasks where id = 'T1'").fetchone()[0]

            stale = run_harness(
                root,
                "task",
                "accept",
                "T1",
                "--agent",
                "qa-reviewer",
                "--lease-token",
                reviewer_token,
                "--expected-revision",
                str(revision),
                "--fence",
                str(reviewer_fence),
                "--evidence",
                "stale accept",
                check=False,
            )
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                task = conn.execute("select status, accepted_by, evidence, fence from tasks where id = 'T1'").fetchone()
                conn.execute(
                    """
                    update tasks set status = 'submitted', lease_agent = null, lease_token = null,
                      lease_heartbeat_at = null, lease_expires_at = null
                    where id = 'T1'
                    """
                )
                conn.execute("update agents set lease_task_id = '', status = 'available' where id = 'qa-reviewer'")
                conn.commit()

            self.assertNotEqual(stale.returncode, 0)
            self.assertIn("fence-stale", stale.stdout)
            self.assertEqual(task, ("review", "", "ready for review", recovered_fence))

            fresh_token, fresh_fence = review(root, "T1", "qa-reviewer")
            self.assertEqual(fresh_fence, recovered_fence + 1)
            run_harness(root, "task", "accept", "T1", "--agent", "qa-reviewer", "--lease-token", fresh_token, "--expected-revision", str(task_revision(root, "T1")), "--fence", str(fresh_fence), "--evidence", "fresh accept")
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                self.assertEqual(conn.execute("select status, accepted_by, evidence from tasks where id = 'T1'").fetchone(), ("accepted", "qa-reviewer", "fresh accept"))

    def test_release_bumps_fence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            init_task(root)
            token, fence = claim(root, "T1", "developer")

            run_harness(root, "task", "release", "T1", "--agent", "developer", "--lease-token", token, "--expected-revision", str(task_revision(root, "T1")), "--fence", str(fence))

            self.assertEqual(task_fence(root, "T1"), fence + 1)

    def test_fence_none_is_backward_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            init_task(root)
            producer_token, _ = claim(root, "T1", "developer")
            run_harness(root, "task", "start", "T1", "--agent", "developer", "--lease-token", producer_token, "--expected-revision", str(task_revision(root, "T1")))
            run_harness(root, "task", "submit", "T1", "--agent", "developer", "--lease-token", producer_token, "--expected-revision", str(task_revision(root, "T1")), "--evidence", "legacy submit")
            reviewer_token, _ = review(root, "T1", "qa-reviewer")
            run_harness(root, "task", "accept", "T1", "--agent", "qa-reviewer", "--lease-token", reviewer_token, "--expected-revision", str(task_revision(root, "T1")), "--evidence", "legacy accept")

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                self.assertEqual(conn.execute("select status from tasks where id = 'T1'").fetchone()[0], "accepted")

    def test_schema_14_migration_adds_fence_default_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            init_task(root)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute("update project set schema_version = 14, runtime_version = '3.3.2'")
                conn.execute("alter table tasks drop column fence")
                conn.commit()

            run_harness(root, "migrate", "--from-version", "14", "--to-version", "28")

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                columns = {row[1] for row in conn.execute("pragma table_info(tasks)")}
                fence = conn.execute("select fence from tasks where id = 'T1'").fetchone()[0]
            doctor = run_harness(root, "doctor")
            self.assertIn("fence", columns)
            self.assertEqual(fence, 0)
            self.assertIn("OK: harness doctor passed", doctor.stdout)


if __name__ == "__main__":
    unittest.main()

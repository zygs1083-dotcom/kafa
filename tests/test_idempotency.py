from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "plugins" / "codex-project-harness" / "scripts" / "harness.py"


def run_harness(root: Path, *args: str, check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    command_env = None if env is None else {**os.environ, **env}
    return subprocess.run(
        ["python3", str(HARNESS), "--root", str(root), *args],
        text=True,
        capture_output=True,
        check=check,
        env=command_env,
    )


def stdout_field(stdout: str, name: str) -> str:
    marker = f"{name}="
    return stdout.split(marker, 1)[1].split(None, 1)[0].strip()


def task_revision(root: Path, task_id: str) -> int:
    with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
        return int(conn.execute("select revision from tasks where id = ?", (task_id,)).fetchone()[0])


def init_task(root: Path) -> None:
    run_harness(root, "init")
    run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
    run_harness(root, "task", "add", "--id", "T1", "--task", "Idempotent task", "--acceptance", "AC1")


def claim(root: Path, task_id: str, agent: str) -> tuple[str, int]:
    result = run_harness(root, "task", "claim", task_id, "--agent", agent, "--expected-revision", str(task_revision(root, task_id)))
    return stdout_field(result.stdout, "token"), int(stdout_field(result.stdout, "fence"))


def review(root: Path, task_id: str, agent: str) -> tuple[str, int]:
    result = run_harness(root, "task", "review", task_id, "--agent", agent, "--expected-revision", str(task_revision(root, task_id)))
    return stdout_field(result.stdout, "token"), int(stdout_field(result.stdout, "fence"))


def submit_for_review(root: Path) -> None:
    token, fence = claim(root, "T1", "developer")
    run_harness(root, "task", "start", "T1", "--agent", "developer", "--lease-token", token, "--expected-revision", str(task_revision(root, "T1")), "--fence", str(fence))
    run_harness(root, "task", "submit", "T1", "--agent", "developer", "--lease-token", token, "--expected-revision", str(task_revision(root, "T1")), "--fence", str(fence), "--evidence", "ready")


class CommandIdempotencyTest(unittest.TestCase):
    def test_duplicate_request_applies_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            init_task(root)
            submit_for_review(root)
            review_token, review_fence = review(root, "T1", "qa-reviewer")
            revision_before = task_revision(root, "T1")

            first = run_harness(
                root,
                "task",
                "accept",
                "T1",
                "--agent",
                "qa-reviewer",
                "--lease-token",
                review_token,
                "--expected-revision",
                str(revision_before),
                "--fence",
                str(review_fence),
                "--evidence",
                "accepted once",
                "--request-id",
                "REQ-accept-1",
            )
            second = run_harness(
                root,
                "task",
                "accept",
                "T1",
                "--agent",
                "qa-reviewer",
                "--lease-token",
                review_token,
                "--expected-revision",
                str(revision_before),
                "--fence",
                str(review_fence),
                "--evidence",
                "accepted once",
                "--request-id",
                "REQ-accept-1",
            )

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                task = conn.execute("select status, evidence, revision from tasks where id = 'T1'").fetchone()
                event_count = conn.execute("select count(*) from events where type = 'task_accepted'").fetchone()[0]
                log_count = conn.execute("select count(*) from command_log where request_id = 'REQ-accept-1'").fetchone()[0]

            self.assertEqual(first.stdout, second.stdout)
            self.assertEqual(task, ("accepted", "accepted once", revision_before + 1))
            self.assertEqual(event_count, 1)
            self.assertEqual(log_count, 1)

    def test_same_request_id_different_args_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "decision", "record", "--decision", "A", "--reason", "same", "--request-id", "REQ-conflict")

            conflict = run_harness(root, "decision", "record", "--decision", "B", "--reason", "same", "--request-id", "REQ-conflict", check=False)

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                decisions = conn.execute("select decision from decisions order by created_at").fetchall()
            self.assertNotEqual(conflict.returncode, 0)
            self.assertIn("idempotency-conflict", conflict.stdout)
            self.assertEqual([row[0] for row in decisions], ["A"])

    def test_no_request_id_is_backward_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            init_task(root)
            submit_for_review(root)
            review_token, review_fence = review(root, "T1", "qa-reviewer")

            result = run_harness(root, "task", "accept", "T1", "--agent", "qa-reviewer", "--lease-token", review_token, "--expected-revision", str(task_revision(root, "T1")), "--fence", str(review_fence), "--evidence", "legacy accept")

            self.assertIn("OK: task accepted T1", result.stdout)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                self.assertEqual(conn.execute("select count(*) from command_log").fetchone()[0], 0)

    def test_command_log_row_in_same_transaction_as_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")

            failed = run_harness(
                root,
                "decision",
                "record",
                "--decision",
                "rollback",
                "--reason",
                "before commit",
                "--request-id",
                "REQ-rollback",
                check=False,
                env={"HARNESS_TEST_FAIL_AFTER_COMMAND_LOG": "REQ-rollback"},
            )

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                decisions = conn.execute("select count(*) from decisions where decision = 'rollback'").fetchone()[0]
                logs = conn.execute("select count(*) from command_log where request_id = 'REQ-rollback'").fetchone()[0]
            self.assertNotEqual(failed.returncode, 0)
            self.assertEqual(decisions, 0)
            self.assertEqual(logs, 0)

    def test_empty_cached_result_does_not_rerun_business(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "decision", "record", "--decision", "cached", "--reason", "first", "--request-id", "REQ-empty")
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute("update command_log set result_json = '' where request_id = 'REQ-empty'")
                conn.commit()

            replay = run_harness(root, "decision", "record", "--decision", "cached", "--reason", "first", "--request-id", "REQ-empty")

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                decisions = conn.execute("select count(*) from decisions where decision = 'cached'").fetchone()[0]
            self.assertIn("already-applied: REQ-empty", replay.stdout)
            self.assertEqual(decisions, 1)

    def test_schema_15_migration_adds_command_log(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute("drop table command_log")
                conn.execute("update project set schema_version = 15, runtime_version = '3.4.0'")
                conn.commit()

            run_harness(root, "migrate", "--from-version", "15", "--to-version", "17")

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                exists = conn.execute("select 1 from sqlite_master where type = 'table' and name = 'command_log'").fetchone()
            doctor = run_harness(root, "doctor")
            self.assertIsNotNone(exists)
            self.assertIn("OK: harness doctor passed", doctor.stdout)


if __name__ == "__main__":
    unittest.main()

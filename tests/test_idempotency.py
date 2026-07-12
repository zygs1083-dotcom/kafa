from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "plugins/codex-project-harness/scripts/harness.py"


def run_harness(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HARNESS), "--root", str(root), *args],
        text=True,
        capture_output=True,
        check=False,
    )


class LocalMutationIdempotencyTest(unittest.TestCase):
    def test_natural_key_upsert_does_not_duplicate_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(run_harness(root, "init").returncode, 0)
            first = run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "first")
            retry = run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "updated")
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                rows = conn.execute("select criterion from acceptance where id='AC1'").fetchall()

        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(retry.returncode, 0, retry.stdout + retry.stderr)
        self.assertEqual(rows, [("updated",)])

    def test_retried_state_transition_fails_without_duplicate_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(run_harness(root, "init").returncode, 0)
            self.assertEqual(
                run_harness(root, "task", "add", "--id", "T1", "--task", "implement").returncode,
                0,
            )
            first = run_harness(root, "task", "start", "T1")
            retry = run_harness(root, "task", "start", "T1")
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                row = conn.execute("select status, revision from tasks where id='T1'").fetchone()
                event_count = conn.execute(
                    "select count(*) from events where event_type='task_started' and entity_id='T1'"
                ).fetchone()[0]

        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertNotEqual(retry.returncode, 0)
        self.assertEqual(row, ("active", 2))
        self.assertEqual(event_count, 1)

    def test_request_id_and_command_log_are_absent_in_schema30(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(run_harness(root, "init").returncode, 0)
            rejected = run_harness(
                root,
                "decision",
                "record",
                "--decision",
                "retired",
                "--reason",
                "duplicate lifecycle",
                "--request-id",
                "REQ-retired",
            )
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                command_log = conn.execute(
                    "select 1 from sqlite_master where type='table' and name='command_log'"
                ).fetchone()
                decision_count = conn.execute("select count(*) from decisions").fetchone()[0]

        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("unrecognized arguments", (rejected.stdout + rejected.stderr).lower())
        self.assertIsNone(command_log)
        self.assertEqual(decision_count, 0)

    def test_concurrent_natural_key_upserts_leave_one_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(run_harness(root, "init").returncode, 0)

            def add(body: str) -> subprocess.CompletedProcess[str]:
                return run_harness(
                    root,
                    "requirement",
                    "add",
                    "--id",
                    "R1",
                    "--kind",
                    "functional",
                    "--body",
                    body,
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(add, ["first", "second"]))
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                rows = conn.execute("select body from requirements where id='R1'").fetchall()

        for result in results:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(rows), 1)
        self.assertIn(rows[0][0], {"first", "second"})


if __name__ == "__main__":
    unittest.main()

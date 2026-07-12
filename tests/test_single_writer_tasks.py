from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "codex-project-harness"
SCRIPTS = PLUGIN_ROOT / "scripts"
HARNESS = SCRIPTS / "harness.py"

for path in (PLUGIN_ROOT, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import harness  # noqa: E402


APPROVED_TASK_COMMANDS = {"add", "list", "start", "submit", "accept", "block", "cancel"}
RETIRED_TASK_COLUMNS = {
    "lease_agent",
    "lease_token",
    "lease_heartbeat_at",
    "lease_expires_at",
    "retry_count",
    "retry_budget",
    "fence",
    "reviewer_lease_token",
}


def run_harness(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HARNESS), "--root", str(root), *args],
        text=True,
        capture_output=True,
        check=False,
    )


def task_subcommands() -> set[str]:
    parser = harness.build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            task_parser = action.choices["task"]
            for task_action in task_parser._actions:
                if isinstance(task_action, argparse._SubParsersAction):
                    return set(task_action.choices)
    raise AssertionError("task parser is missing")


def task_row(root: Path, task_id: str) -> sqlite3.Row:
    with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("select * from tasks where id = ?", (task_id,)).fetchone()
        if row is None:
            raise AssertionError(f"missing task {task_id}")
        return row


class SingleWriterTaskTests(unittest.TestCase):
    def test_public_task_surface_is_the_single_writer_lifecycle(self) -> None:
        self.assertEqual(task_subcommands(), APPROVED_TASK_COMMANDS)

    def test_planned_active_submitted_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            commands = [
                ("init",),
                ("acceptance", "add", "--id", "AC1", "--criterion", "task completes"),
                ("task", "add", "--id", "T1", "--task", "implement", "--acceptance", "AC1"),
                ("task", "start", "T1"),
                ("task", "submit", "T1", "--context-id", "producer-context", "--evidence", "implemented"),
                ("task", "accept", "T1", "--evidence", "reviewed"),
            ]
            for command in commands:
                result = run_harness(root, *command)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            row = task_row(root, "T1")

        self.assertEqual(row["status"], "accepted")
        self.assertEqual(row["submitted_context_id"], "producer-context")
        self.assertEqual(int(row["revision"]), 4)

    def test_block_and_cancel_follow_the_locked_state_graph(self) -> None:
        cases = [
            ("cancel-planned", [], ("task", "cancel", "T1", "--reason", "not needed"), "cancelled"),
            ("cancel-active", [("task", "start", "T1")], ("task", "cancel", "T1"), "cancelled"),
            (
                "cancel-submitted",
                [("task", "start", "T1"), ("task", "submit", "T1", "--evidence", "done")],
                ("task", "cancel", "T1"),
                "cancelled",
            ),
            ("block-active", [("task", "start", "T1")], ("task", "block", "T1", "--reason", "blocked"), "blocked"),
            (
                "block-submitted",
                [("task", "start", "T1"), ("task", "submit", "T1", "--evidence", "done")],
                ("task", "block", "T1", "--reason", "review blocked"),
                "blocked",
            ),
        ]
        for name, setup, transition, expected in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                self.assertEqual(run_harness(root, "init").returncode, 0)
                self.assertEqual(run_harness(root, "task", "add", "--id", "T1", "--task", "implement").returncode, 0)
                for command in setup:
                    result = run_harness(root, *command)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                result = run_harness(root, *transition)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(task_row(root, "T1")["status"], expected)

    def test_retried_transition_fails_precondition_without_duplicate_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(run_harness(root, "init").returncode, 0)
            self.assertEqual(run_harness(root, "task", "add", "--id", "T1", "--task", "implement").returncode, 0)
            first = run_harness(root, "task", "start", "T1")
            retry = run_harness(root, "task", "start", "T1")
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                revision = int(conn.execute("select revision from tasks where id='T1'").fetchone()[0])
                event_count = int(conn.execute("select count(*) from events where event_type='task_started'").fetchone()[0])

        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertNotEqual(retry.returncode, 0)
        self.assertIn("expected planned", (retry.stdout + retry.stderr).lower())
        self.assertEqual((revision, event_count), (2, 1))

    def test_same_producer_and_reviewer_context_is_rejected_without_gate_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(run_harness(root, "init").returncode, 0)
            self.assertEqual(run_harness(root, "task", "add", "--id", "T1", "--task", "implement").returncode, 0)
            self.assertEqual(run_harness(root, "task", "start", "T1").returncode, 0)
            self.assertEqual(
                run_harness(root, "task", "submit", "T1", "--context-id", "ctx-producer", "--evidence", "done").returncode,
                0,
            )
            rejected = run_harness(
                root,
                "gate",
                "record",
                "--reviewer-context",
                "fresh",
                "--reviewer-context-id",
                "ctx-producer",
                "--result",
                "fail",
            )
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                gate_count = int(conn.execute("select count(*) from quality_gates").fetchone()[0])

        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("matches producer context", (rejected.stdout + rejected.stderr).lower())
        self.assertEqual(gate_count, 0)

    def test_accept_rejects_planned_and_active_without_mutation(self) -> None:
        for started in (False, True):
            with self.subTest(started=started), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                self.assertEqual(run_harness(root, "init").returncode, 0)
                self.assertEqual(run_harness(root, "task", "add", "--id", "T1", "--task", "implement").returncode, 0)
                if started:
                    start = run_harness(root, "task", "start", "T1")
                    self.assertEqual(start.returncode, 0, start.stdout + start.stderr)
                before = dict(task_row(root, "T1"))
                rejected = run_harness(root, "task", "accept", "T1", "--evidence", "premature")
                after = dict(task_row(root, "T1"))

            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("cannot transition", (rejected.stdout + rejected.stderr).lower())
            self.assertEqual(after, before)

    def test_task_schema_has_no_distributed_writer_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(run_harness(root, "init").returncode, 0)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                columns = {row[1] for row in conn.execute("pragma table_info(tasks)")}

        self.assertEqual(columns & RETIRED_TASK_COLUMNS, set())

    def test_schema_guard_rejects_empty_task_id_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(run_harness(root, "init").returncode, 0)
            invalid = run_harness(root, "task", "add", "--id", "", "--task", "Bad")
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                count = int(conn.execute("select count(*) from tasks").fetchone()[0])

        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn("task id is required", invalid.stdout + invalid.stderr)
        self.assertEqual(count, 0)

    def test_direct_sql_cannot_write_a_retired_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(run_harness(root, "init").returncode, 0)
            self.assertEqual(run_harness(root, "task", "add", "--id", "T1", "--task", "implement").returncode, 0)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute("update tasks set status='review' where id='T1'")
            status = task_row(root, "T1")["status"]

        self.assertEqual(status, "planned")

    def test_admin_read_succeeds_during_controller_write_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(run_harness(root, "init").returncode, 0)
            self.assertEqual(run_harness(root, "task", "add", "--id", "T1", "--task", "implement").returncode, 0)
            db = root / ".ai-team/state/harness.db"
            with closing(sqlite3.connect(db, timeout=2)) as writer:
                writer.execute("pragma journal_mode=wal")
                writer.execute("begin immediate")
                writer.execute("update tasks set task=task where id='T1'")
                with closing(sqlite3.connect(db, timeout=2)) as reader:
                    status = reader.execute("select status from tasks where id='T1'").fetchone()[0]
                writer.rollback()

        self.assertEqual(status, "planned")

    def test_subagent_skill_returns_results_to_root_controller(self) -> None:
        skill = (PLUGIN_ROOT / "skills/project-harness/SKILL.md").read_text(encoding="utf-8").lower()
        self.assertIn("only the root controller writes kafa delivery facts", skill)
        self.assertNotIn("task claim", skill)
        self.assertNotIn("task heartbeat", skill)
        self.assertNotIn("lease-token", skill)


if __name__ == "__main__":
    unittest.main()

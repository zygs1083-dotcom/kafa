from __future__ import annotations

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

import harness_db  # noqa: E402
from core import schema_lifecycle  # noqa: E402
from core.schema_lifecycle import (  # noqa: E402
    SCHEMA30_TABLES,
    SCHEMA30_VERSION,
    SchemaLifecycleError,
    create_schema30,
)


def run_harness(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HARNESS), "--root", str(root), *args],
        text=True,
        capture_output=True,
        check=False,
    )


def table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "select name from sqlite_master "
            "where type='table' and name not like 'sqlite_%'"
        )
    }


class SchemaLifecycleTest(unittest.TestCase):
    def test_file_fsync_helpers_use_update_capable_descriptors(self) -> None:
        class RecordingPath:
            def __init__(self, target: Path, modes: list[str]) -> None:
                self.target = target
                self.modes = modes

            def open(self, mode: str):
                self.modes.append(mode)
                return self.target.open(mode)

        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "durable-file"
            target.write_bytes(b"durable")
            modes: list[str] = []
            recorded = RecordingPath(target, modes)

            schema_lifecycle._fsync_file(recorded)  # type: ignore[arg-type]

        self.assertEqual(modes, ["rb+"])

    def test_create_schema30_rolls_back_with_the_caller_transaction(self) -> None:
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.execute("begin immediate")
            create_schema30(conn)
            self.assertEqual(table_names(conn), set(SCHEMA30_TABLES))
            conn.rollback()
            self.assertEqual(table_names(conn), set())

    def test_create_schema30_requires_an_explicit_transaction(self) -> None:
        with closing(sqlite3.connect(":memory:")) as conn:
            with self.assertRaisesRegex(SchemaLifecycleError, "active transaction"):
                create_schema30(conn)

    def test_create_schema30_rejects_nonempty_database_without_mutation(self) -> None:
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.execute("create table caller_fact (id integer primary key)")
            conn.execute("begin immediate")
            with self.assertRaisesRegex(SchemaLifecycleError, "requires an empty"):
                create_schema30(conn)
            conn.rollback()
            self.assertEqual(table_names(conn), {"caller_fact"})

    def test_current_schema_migration_is_an_idempotent_noop(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(run_harness(root, "init").returncode, 0)
            result = run_harness(
                root,
                "migrate",
                "--from-version",
                str(SCHEMA30_VERSION),
                "--to-version",
                str(SCHEMA30_VERSION),
            )
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                version = int(conn.execute("select schema_version from project").fetchone()[0])

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(version, SCHEMA30_VERSION)

    def test_current_schema_dry_run_is_non_mutating(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(run_harness(root, "init").returncode, 0)
            db_path = root / ".ai-team/state/harness.db"
            before = db_path.read_bytes()
            result = run_harness(
                root,
                "migrate",
                "--from-version",
                str(SCHEMA30_VERSION),
                "--to-version",
                str(SCHEMA30_VERSION),
                "--dry-run",
            )
            after = db_path.read_bytes()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("DRY-RUN", result.stdout)
        self.assertEqual(before, after)

    def test_migration_rejects_wrong_source_downgrade_and_unknown_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(run_harness(root, "init").returncode, 0)
            wrong_source = run_harness(
                root, "migrate", "--from-version", "29", "--to-version", "30"
            )
            downgrade = run_harness(
                root, "migrate", "--from-version", "30", "--to-version", "29"
            )
            unknown = run_harness(
                root, "migrate", "--from-version", "30", "--to-version", "31"
            )

        self.assertNotEqual(wrong_source.returncode, 0)
        self.assertIn("migration source mismatch", wrong_source.stdout + wrong_source.stderr)
        self.assertNotEqual(downgrade.returncode, 0)
        self.assertIn("downgrade is not supported", downgrade.stdout + downgrade.stderr)
        self.assertNotEqual(unknown.returncode, 0)
        self.assertIn("unregistered migration target", unknown.stdout + unknown.stderr)

    def test_current_schema_noop_rejects_incomplete_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(run_harness(root, "init").returncode, 0)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute("pragma foreign_keys=off")
                conn.execute("drop table decisions")
                conn.commit()
            result = run_harness(
                root,
                "migrate",
                "--from-version",
                str(SCHEMA30_VERSION),
                "--to-version",
                str(SCHEMA30_VERSION),
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("current schema is incomplete", result.stdout + result.stderr)

    def test_retired_checkpoint_and_event_replay_commands_fail_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(run_harness(root, "init").returncode, 0)
            db_path = root / ".ai-team/state/harness.db"
            before = db_path.read_bytes()
            checkpoint = run_harness(root, "checkpoint", "create")
            event = run_harness(root, "event", "export")
            after = db_path.read_bytes()

        self.assertNotEqual(checkpoint.returncode, 0)
        self.assertNotEqual(event.returncode, 0)
        self.assertEqual(before, after)

    def test_schema30_version_matches_runtime_constant(self) -> None:
        self.assertEqual(SCHEMA30_VERSION, harness_db.SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()

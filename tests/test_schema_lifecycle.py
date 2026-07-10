from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "codex-project-harness"
SCRIPTS_ROOT = PLUGIN_ROOT / "scripts"
HARNESS = SCRIPTS_ROOT / "harness.py"
for path in [PLUGIN_ROOT, SCRIPTS_ROOT]:
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import harness_db
from core.event_bus import rebuild_state_from_events
from core.store import InMemoryStore, SqliteStore


def run_harness(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HARNESS), "--root", str(root), *args],
        text=True,
        capture_output=True,
        check=False,
    )


def project_schema_version(root: Path) -> int:
    with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
        return int(conn.execute("select schema_version from project where id = 1").fetchone()[0])


def migration_count(root: Path) -> int:
    with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
        return int(conn.execute("select count(*) from migrations").fetchone()[0])


class SchemaLifecycleTest(unittest.TestCase):
    def test_schema_script_rejects_connection_without_transaction(self) -> None:
        with closing(sqlite3.connect(":memory:")) as conn:
            with self.assertRaisesRegex(harness_db.HarnessError, "requires an active transaction"):
                harness_db.create_schema(conn)

    def test_db_001_create_schema_rolls_back_with_caller_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = SqliteStore(root)

            with self.assertRaisesRegex(RuntimeError, "injected migration failure"):
                with store.transaction() as conn:
                    conn.execute("create table caller_fact (id integer primary key, value text not null)")
                    conn.execute("insert into caller_fact (value) values ('must rollback')")
                    harness_db.create_schema(conn)
                    raise RuntimeError("injected migration failure")

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "select name from sqlite_master where type = 'table' and name in ('caller_fact', 'project')"
                    )
                }

        self.assertEqual(
            tables,
            set(),
            "DB-001: create_schema committed caller work before the surrounding transaction failed",
        )

    def test_db_001_in_memory_schema_rollback_matches_file_store(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = InMemoryStore(Path(temp))
            self.addCleanup(store.close)

            with self.assertRaisesRegex(RuntimeError, "injected migration failure"):
                with store.transaction() as conn:
                    conn.execute("create table caller_fact (id integer primary key, value text not null)")
                    harness_db.create_schema(conn)
                    raise RuntimeError("injected migration failure")

            with store.connection() as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "select name from sqlite_master where type = 'table' and name in ('caller_fact', 'project')"
                    )
                }

        self.assertEqual(tables, set())

    def test_db_002_migrate_rejects_mismatched_actual_from_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(run_harness(root, "init").returncode, 0)

            result = run_harness(root, "migrate", "--from-version", "6", "--to-version", "28")
            version = project_schema_version(root)
            migrations = migration_count(root)

        self.assertNotEqual(
            result.returncode,
            0,
            "DB-002: migrate trusted caller-authored from-version instead of the database version",
        )
        self.assertEqual(version, 28)
        self.assertEqual(migrations, 0)

    def test_db_002_migrate_rejects_unknown_target_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(run_harness(root, "init").returncode, 0)

            result = run_harness(root, "migrate", "--from-version", "28", "--to-version", "999")
            version = project_schema_version(root)

        self.assertNotEqual(result.returncode, 0, "DB-002: unknown migration target 999 was accepted")
        self.assertEqual(version, 28)

    def test_db_002_migrate_rejects_downgrade(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(run_harness(root, "init").returncode, 0)

            result = run_harness(root, "migrate", "--from-version", "28", "--to-version", "27")
            version = project_schema_version(root)

        self.assertNotEqual(result.returncode, 0, "DB-002: schema downgrade was accepted")
        self.assertEqual(version, 28)

    def test_db_002_dry_run_validates_migration_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(run_harness(root, "init").returncode, 0)

            result = run_harness(
                root,
                "migrate",
                "--from-version",
                "28",
                "--to-version",
                "999",
                "--dry-run",
            )
            version = project_schema_version(root)

        self.assertNotEqual(result.returncode, 0, "DB-002: dry-run reported an unknown migration path as valid")
        self.assertEqual(version, 28)

    def test_db_002_markdown_import_rejects_non_current_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            result = run_harness(
                root,
                "migrate",
                "--from-version",
                "markdown-v1",
                "--to-version",
                "13",
                "--dry-run",
            )

        self.assertNotEqual(
            result.returncode,
            0,
            "DB-002: markdown importer accepted a target version different from the schema it creates",
        )

    def test_current_schema_migration_is_a_noop(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(run_harness(root, "init").returncode, 0)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                before = conn.execute("select schema_version, revision from project where id = 1").fetchone()

            result = run_harness(root, "migrate", "--from-version", "28", "--to-version", "28")

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                after = conn.execute("select schema_version, revision from project where id = 1").fetchone()
                migrations = conn.execute("select count(*) from migrations").fetchone()[0]

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(after, before)
        self.assertEqual(migrations, 0)

    def test_migration_failure_restores_version_rows_and_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            harness_db.add_requirement(root, "R1", "functional", "Preserve me")
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute("update project set schema_version = 24, runtime_version = '4.11.0'")
                conn.commit()

            with patch.object(
                harness_db,
                "require_full_invariants",
                side_effect=harness_db.HarnessError("injected migration invariant failure"),
            ):
                with self.assertRaisesRegex(harness_db.HarnessError, "injected migration invariant failure"):
                    harness_db.migrate(root, "24", 28)

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                version = conn.execute("select schema_version from project where id = 1").fetchone()[0]
                requirement = conn.execute("select body from requirements where id = 'R1'").fetchone()[0]
                migrations = conn.execute("select count(*) from migrations").fetchone()[0]

        self.assertEqual(version, 24)
        self.assertEqual(requirement, "Preserve me")
        self.assertEqual(migrations, 0)

    def test_concurrent_migration_loser_cannot_restore_over_winner(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute("update project set schema_version = 24, runtime_version = '4.11.0'")
                conn.commit()

            barrier = threading.Barrier(2)
            original_backup = harness_db.backup_runtime

            def synchronized_backup(project_root: Path, reason: str) -> Path:
                backup = original_backup(project_root, reason)
                barrier.wait(timeout=5)
                return backup

            def migrate_once() -> str:
                try:
                    harness_db.migrate(root, "24", 28)
                except harness_db.HarnessError as exc:
                    return str(exc)
                return "ok"

            with patch.object(harness_db, "backup_runtime", side_effect=synchronized_backup):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = list(executor.map(lambda _: migrate_once(), range(2)))

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                version = conn.execute("select schema_version from project where id = 1").fetchone()[0]
                migrations = conn.execute("select count(*) from migrations").fetchone()[0]

        self.assertEqual(results.count("ok"), 1, results)
        self.assertTrue(any("migration source changed concurrently" in result for result in results), results)
        self.assertEqual(version, 28)
        self.assertEqual(migrations, 1)

    def test_checkpoint_round_trip_preserves_foreign_key_relationships(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            imported = Path(temp) / "imported"
            package = Path(temp) / "checkpoint.json"
            harness_db.init_runtime(root)
            harness_db.add_acceptance(root, "AC1", "Example")
            harness_db.add_task(root, "T1", "Example", acceptance="AC1")
            harness_db.add_test_target(root, "UNIT", "unit", "true", "Example target")
            harness_db.link_task_test_target(root, "T1", "UNIT")
            harness_db.create_checkpoint(root, "relations")
            harness_db.export_checkpoint(root, package)
            harness_db.init_runtime(imported)

            issues = harness_db.import_checkpoint(imported, package, apply=True)

            with closing(sqlite3.connect(imported / ".ai-team/state/harness.db")) as conn:
                link = conn.execute(
                    "select task_id, target_id from task_test_targets where task_id = 'T1' and target_id = 'UNIT'"
                ).fetchone()
                foreign_key_errors = conn.execute("pragma foreign_key_check").fetchall()

        self.assertEqual(issues, [])
        self.assertEqual(link, ("T1", "UNIT"))
        self.assertEqual(foreign_key_errors, [])

    def test_event_replay_failure_removes_partial_output_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            out = root / "failed-replay.db"
            harness_db.init_runtime(root)
            harness_db.create_checkpoint(root, "start")
            with harness_db.connection(root) as conn:
                sequence = int(conn.execute("select max(sequence) from events").fetchone()[0])
            original_restore = harness_db.restore_snapshot

            def fail_after_restore(conn: sqlite3.Connection, snapshot: dict[str, object]) -> None:
                original_restore(conn, snapshot)
                raise RuntimeError("injected replay failure")

            with patch.object(harness_db, "restore_snapshot", side_effect=fail_after_restore):
                with self.assertRaisesRegex(RuntimeError, "injected replay failure"):
                    rebuild_state_from_events(root, sequence, out)
            self.assertFalse(out.exists(), "event replay left a partially committed output database")

    def test_markdown_migration_concurrency_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            requirements = root / ".ai-team/requirements"
            requirements.mkdir(parents=True)
            (requirements / "acceptance.md").write_text(
                "# Acceptance\n\n"
                "| ID | Criterion | Priority | Tool Link | Status |\n"
                "| --- | --- | --- | --- | --- |\n"
                "| AC1 | Imported once | must | | active |\n",
                encoding="utf-8",
            )
            barrier = threading.Barrier(2)
            original_backup = harness_db.backup_runtime

            def synchronized_backup(project_root: Path, reason: str) -> Path:
                backup = original_backup(project_root, reason)
                barrier.wait(timeout=5)
                return backup

            def migrate_once() -> str:
                try:
                    harness_db.migrate(root, "markdown-v1", 28)
                except Exception as exc:
                    return f"{type(exc).__name__}: {exc}"
                return "ok"

            with patch.object(harness_db, "backup_runtime", side_effect=synchronized_backup):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = list(executor.map(lambda _: migrate_once(), range(2)))

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                acceptance_count = conn.execute("select count(*) from acceptance where id = 'AC1'").fetchone()[0]
                migration_count = conn.execute(
                    "select count(*) from migrations where from_version = 1 and to_version = 28"
                ).fetchone()[0]

        self.assertEqual(results, ["ok", "ok"])
        self.assertEqual(acceptance_count, 1)
        self.assertEqual(migration_count, 1)

    def test_current_schema_noop_rejects_incomplete_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute("drop table command_log")
                conn.commit()

            result = run_harness(root, "migrate", "--from-version", "28", "--to-version", "28")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("current schema is incomplete", result.stdout + result.stderr)

    def test_projection_failure_after_commit_has_idempotent_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute("update project set schema_version = 24, runtime_version = '4.11.0'")
                conn.commit()

            with patch.object(harness_db, "render_all", side_effect=OSError("projection unavailable")):
                with self.assertRaisesRegex(harness_db.HarnessError, "migration committed but projection rebuild failed"):
                    harness_db.migrate(root, "24", 28)

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                committed = conn.execute("select schema_version from project where id = 1").fetchone()[0]
                migrations = conn.execute("select count(*) from migrations where from_version = 24 and to_version = 28").fetchone()[0]

            with patch.object(harness_db, "render_all", wraps=harness_db.render_all) as render:
                harness_db.migrate(root, "28", 28)

        self.assertEqual(committed, 28)
        self.assertEqual(migrations, 1)
        render.assert_called_once_with(root)

    def test_markdown_projection_failure_has_idempotent_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            requirements = root / ".ai-team/requirements"
            requirements.mkdir(parents=True)
            (requirements / "acceptance.md").write_text(
                "# Acceptance\n\n"
                "| ID | Criterion | Priority | Tool Link | Status |\n"
                "| --- | --- | --- | --- | --- |\n"
                "| AC1 | Imported once | must | | active |\n",
                encoding="utf-8",
            )

            with patch.object(harness_db, "render_all", side_effect=OSError("projection unavailable")):
                with self.assertRaisesRegex(harness_db.HarnessError, "markdown migration committed but projection rebuild failed"):
                    harness_db.migrate(root, "markdown-v1", 28)

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                committed = conn.execute(
                    "select count(*) from migrations where from_version = 1 and to_version = 28"
                ).fetchone()[0]
                acceptance_count = conn.execute("select count(*) from acceptance where id = 'AC1'").fetchone()[0]

            with patch.object(harness_db, "render_all", wraps=harness_db.render_all) as render:
                harness_db.migrate(root, "markdown-v1", 28)

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                migration_count = conn.execute(
                    "select count(*) from migrations where from_version = 1 and to_version = 28"
                ).fetchone()[0]

        self.assertEqual(committed, 1)
        self.assertEqual(acceptance_count, 1)
        self.assertEqual(migration_count, 1)
        render.assert_called_once_with(root)


if __name__ == "__main__":
    unittest.main()

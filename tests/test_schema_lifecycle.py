from __future__ import annotations

import json
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

            result = run_harness(root, "migrate", "--from-version", "6", "--to-version", "29")
            version = project_schema_version(root)
            migrations = migration_count(root)

        self.assertNotEqual(
            result.returncode,
            0,
            "DB-002: migrate trusted caller-authored from-version instead of the database version",
        )
        self.assertEqual(version, 29)
        self.assertEqual(migrations, 0)

    def test_db_002_migrate_rejects_unknown_target_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(run_harness(root, "init").returncode, 0)

            result = run_harness(root, "migrate", "--from-version", "29", "--to-version", "999")
            version = project_schema_version(root)

        self.assertNotEqual(result.returncode, 0, "DB-002: unknown migration target 999 was accepted")
        self.assertEqual(version, 29)

    def test_db_002_migrate_rejects_downgrade(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(run_harness(root, "init").returncode, 0)

            result = run_harness(root, "migrate", "--from-version", "29", "--to-version", "28")
            version = project_schema_version(root)

        self.assertNotEqual(result.returncode, 0, "DB-002: schema downgrade was accepted")
        self.assertEqual(version, 29)

    def test_db_002_dry_run_validates_migration_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(run_harness(root, "init").returncode, 0)

            result = run_harness(
                root,
                "migrate",
                "--from-version",
                "29",
                "--to-version",
                "999",
                "--dry-run",
            )
            version = project_schema_version(root)

        self.assertNotEqual(result.returncode, 0, "DB-002: dry-run reported an unknown migration path as valid")
        self.assertEqual(version, 29)

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

            result = run_harness(root, "migrate", "--from-version", "29", "--to-version", "29")

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
                    harness_db.migrate(root, "24", 29)

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
                    harness_db.migrate(root, "24", 29)
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
        self.assertEqual(version, 29)
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

    def test_schema28_checkpoint_is_rejected_before_restore(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source"
            target = Path(temp) / "target"
            package = Path(temp) / "schema28-checkpoint.json"
            harness_db.init_runtime(source)
            harness_db.add_requirement(source, "R1", "functional", "Legacy package")
            harness_db.create_checkpoint(source, "legacy-package")
            harness_db.export_checkpoint(source, package)
            data = json.loads(package.read_text(encoding="utf-8"))
            data["schema_version"] = 28
            package.write_text(json.dumps(data), encoding="utf-8")
            harness_db.init_runtime(target)

            issues = harness_db.import_checkpoint(target, package, apply=True)

            with harness_db.connection(target) as conn:
                count = conn.execute("select count(*) from requirements").fetchone()[0]

        self.assertEqual(issues, ["schema version differs: package=28 runtime=29"])
        self.assertEqual(count, 0)

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

    def test_event_replay_preserves_same_local_id_in_multiple_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            out = root / "replayed.db"
            harness_db.init_runtime(root)
            harness_db.add_requirement(root, "R1", "functional", "First cycle")
            harness_db.create_checkpoint(root, "before-next-cycle")
            harness_db.cycle_close(root, "archived")
            harness_db.cycle_start(root, "CYCLE-next", "Next", "Iterate")
            harness_db.add_requirement(root, "R1", "functional", "Second cycle")
            harness_db.add_acceptance(root, "AC1", "Second acceptance")
            harness_db.link_requirement_acceptance(root, "R1", "AC1")
            harness_db.add_task(root, "T1", "Second task", acceptance="AC1")
            with harness_db.connection(root) as conn:
                sequence = int(conn.execute("select max(sequence) from events").fetchone()[0])
                live_project = conn.execute(
                    "select current_cycle_id, phase from project where id = 1"
                ).fetchone()
                live_cycles = conn.execute(
                    "select id, status, phase from delivery_cycles order by id"
                ).fetchall()
                live_relations = {
                    table: conn.execute(f"select * from {table} order by 1, 2").fetchall()
                    for table in ["requirement_acceptance", "task_acceptance"]
                }

            rebuild_state_from_events(root, sequence, out)

            with closing(sqlite3.connect(out)) as conn:
                rows = conn.execute(
                    "select cycle_id, id, body from requirements where id = 'R1' order by cycle_id"
                ).fetchall()
                replay_project = conn.execute(
                    "select current_cycle_id, phase from project where id = 1"
                ).fetchone()
                replay_cycles = conn.execute(
                    "select id, status, phase from delivery_cycles order by id"
                ).fetchall()
                replay_relations = {
                    table: conn.execute(f"select * from {table} order by 1, 2").fetchall()
                    for table in ["requirement_acceptance", "task_acceptance"]
                }

        self.assertEqual(
            rows,
            [("CYCLE-current", "R1", "First cycle"), ("CYCLE-next", "R1", "Second cycle")],
        )
        self.assertEqual(replay_project, tuple(live_project))
        self.assertEqual(replay_cycles, [tuple(row) for row in live_cycles])
        self.assertEqual(replay_relations, {table: [tuple(row) for row in values] for table, values in live_relations.items()})

    def test_event_replay_preserves_quality_gate_supersession(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            out = root / "gate-replayed.db"
            harness_db.init_runtime(root)
            harness_db.create_checkpoint(root, "before-gates")
            harness_db.record_gate(root, "fresh", "pass")
            harness_db.record_gate(root, "fresh", "fail")
            with harness_db.connection(root) as conn:
                sequence = int(conn.execute("select max(sequence) from events").fetchone()[0])
                live = conn.execute(
                    "select id, sequence, result, gate_status, superseded_by from quality_gates order by sequence"
                ).fetchall()

            rebuild_state_from_events(root, sequence, out)

            with closing(sqlite3.connect(out)) as conn:
                replayed = conn.execute(
                    "select id, sequence, result, gate_status, superseded_by from quality_gates order by sequence"
                ).fetchall()

        self.assertEqual(replayed, [tuple(row) for row in live])

    def test_schema29_event_without_mutation_journal_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            out = root / "invalid-event-replay.db"
            harness_db.init_runtime(root)
            harness_db.create_checkpoint(root, "before-invalid-event")
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute(
                    """
                    insert into events
                    (id, schema_version, type, source, target, correlation_id, causation_id, idempotency_key, payload_json, created_at)
                    values ('missing-journal', 29, 'task_changed', 'test', 'task:T1', '', '', '', '{}', '2026-07-10T00:00:00Z')
                    """
                )
                sequence = int(conn.execute("select max(sequence) from events").fetchone()[0])
                conn.commit()

            issues = harness_db.validate_events(root)
            with self.assertRaisesRegex(ValueError, "missing canonical_mutations"):
                rebuild_state_from_events(root, sequence, out)

        self.assertTrue(any("missing canonical_mutations" in issue for issue in issues), issues)
        self.assertFalse(out.exists())

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
                    harness_db.migrate(root, "markdown-v1", 29)
                except Exception as exc:
                    return f"{type(exc).__name__}: {exc}"
                return "ok"

            with patch.object(harness_db, "backup_runtime", side_effect=synchronized_backup):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = list(executor.map(lambda _: migrate_once(), range(2)))

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                acceptance_count = conn.execute("select count(*) from acceptance where id = 'AC1'").fetchone()[0]
                migration_count = conn.execute(
                    "select count(*) from migrations where from_version = 1 and to_version = 29"
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

            result = run_harness(root, "migrate", "--from-version", "29", "--to-version", "29")

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
                    harness_db.migrate(root, "24", 29)

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                committed = conn.execute("select schema_version from project where id = 1").fetchone()[0]
                migrations = conn.execute("select count(*) from migrations where from_version = 24 and to_version = 29").fetchone()[0]

            with patch.object(harness_db, "render_all", wraps=harness_db.render_all) as render:
                harness_db.migrate(root, "29", 29)

        self.assertEqual(committed, 29)
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
                    harness_db.migrate(root, "markdown-v1", 29)

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                committed = conn.execute(
                    "select count(*) from migrations where from_version = 1 and to_version = 29"
                ).fetchone()[0]
                acceptance_count = conn.execute("select count(*) from acceptance where id = 'AC1'").fetchone()[0]

            with patch.object(harness_db, "render_all", wraps=harness_db.render_all) as render:
                harness_db.migrate(root, "markdown-v1", 29)

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                migration_count = conn.execute(
                    "select count(*) from migrations where from_version = 1 and to_version = 29"
                ).fetchone()[0]

        self.assertEqual(committed, 1)
        self.assertEqual(acceptance_count, 1)
        self.assertEqual(migration_count, 1)
        render.assert_called_once_with(root)


if __name__ == "__main__":
    unittest.main()

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
SCRIPTS_ROOT = PLUGIN_ROOT / "scripts"
HARNESS = SCRIPTS_ROOT / "harness.py"
for path in [PLUGIN_ROOT, SCRIPTS_ROOT]:
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import harness_db
from core.store import SqliteStore


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


if __name__ == "__main__":
    unittest.main()

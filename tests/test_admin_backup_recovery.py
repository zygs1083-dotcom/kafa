from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "codex-project-harness"
SCRIPTS = PLUGIN_ROOT / "scripts"

for path in (PLUGIN_ROOT, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import harness_db  # noqa: E402
from core.schema_lifecycle import SchemaLifecycleError, backup_sqlite_database  # noqa: E402


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class AdminBackupRecoveryTests(unittest.TestCase):
    def test_schema30_admin_backup_is_consistent_digested_and_non_mutating(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            harness_db.add_requirement(root, "R1", "functional", "preserve me")
            active = root / ".ai-team/state/harness.db"
            active_digest_before = sha256(active)
            with closing(sqlite3.connect(active)) as conn:
                event_count_before = int(conn.execute("select count(*) from events").fetchone()[0])

            manifest = backup_sqlite_database(
                root,
                expected_source_version=30,
                created_at="2026-07-11T03:04:05Z",
            )

            backup = Path(manifest.backup_path)
            payload = json.loads(Path(manifest.manifest_path).read_text(encoding="utf-8"))
            with closing(sqlite3.connect(backup)) as conn:
                requirement = conn.execute("select body from requirements where id='R1'").fetchone()[0]
                integrity = conn.execute("pragma integrity_check").fetchone()[0]
                foreign_keys = conn.execute("pragma foreign_key_check").fetchall()
            with closing(sqlite3.connect(active)) as conn:
                event_count_after = int(conn.execute("select count(*) from events").fetchone()[0])

            self.assertEqual(manifest.source_version, 30)
            self.assertEqual(manifest.sha256, sha256(backup))
            self.assertEqual(payload["sha256"], manifest.sha256)
            self.assertGreaterEqual(payload["row_counts"]["requirements"], 1)
            self.assertEqual(payload["source_integrity_check"], ["ok"])
            self.assertEqual(payload["backup_integrity_check"], ["ok"])
            self.assertEqual(requirement, "preserve me")
            self.assertEqual(integrity, "ok")
            self.assertEqual(foreign_keys, [])
            self.assertEqual(active_digest_before, sha256(active))
            self.assertEqual(event_count_after, event_count_before)

    def test_repair_creates_verified_sqlite_backup_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            harness_db.add_requirement(root, "R1", "functional", "repair sentinel")

            self.assertEqual(harness_db.repair(root), [])

            manifests = sorted(
                (root / ".ai-team/backups").glob(
                    "schema-30-before-local-core-*/backup-manifest.json"
                )
            )
            self.assertEqual(len(manifests), 1)
            payload = json.loads(manifests[0].read_text(encoding="utf-8"))
            backup = Path(payload["backup_path"])
            with closing(sqlite3.connect(backup)) as conn:
                sentinel = conn.execute("select body from requirements where id='R1'").fetchone()[0]
                integrity = conn.execute("pragma integrity_check").fetchone()[0]

            self.assertEqual(payload["source_version"], 30)
            self.assertEqual(payload["sha256"], sha256(backup))
            self.assertEqual(payload["backup_integrity_check"], ["ok"])
            self.assertEqual(sentinel, "repair sentinel")
            self.assertEqual(integrity, "ok")

    def test_admin_backup_rejects_foreign_key_corruption_without_partial_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            active = root / ".ai-team/state/harness.db"
            with closing(sqlite3.connect(active)) as conn:
                conn.execute("pragma foreign_keys = off")
                conn.execute(
                    "insert into task_acceptance (cycle_id, task_id, acceptance_id) "
                    "values ('CYCLE-current', 'missing-task', 'missing-acceptance')"
                )
                conn.commit()

            with self.assertRaisesRegex(SchemaLifecycleError, r"foreign_key_issues=[1-9]"):
                backup_sqlite_database(
                    root,
                    expected_source_version=30,
                    created_at="2026-07-11T03:04:06Z",
                )

            backups = (
                list((root / ".ai-team/backups").glob("**/*"))
                if (root / ".ai-team/backups").exists()
                else []
            )

            self.assertEqual(backups, [])


if __name__ == "__main__":
    unittest.main()

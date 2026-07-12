from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "codex-project-harness"
SCRIPTS = PLUGIN_ROOT / "scripts"

for path in (PLUGIN_ROOT, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import harness_db  # noqa: E402
from core.event_bus import SUMMARY_STRING_LIMIT, compact_summary, validate_audit_events  # noqa: E402


class LocalTransactionCostTests(unittest.TestCase):
    def test_requirement_mutation_does_not_enumerate_unrelated_tables(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)

            def reject_table_scan(*args, **kwargs):
                raise AssertionError(f"normal mutation enumerated runtime table: {args!r} {kwargs!r}")

            if hasattr(harness_db, "table_rows"):
                with mock.patch.object(harness_db, "table_rows", side_effect=reject_table_scan):
                    harness_db.add_requirement(root, "R1", "functional", "local mutation")
            else:
                harness_db.add_requirement(root, "R1", "functional", "local mutation")

    def test_mutation_appends_compact_non_replay_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            harness_db.add_requirement(root, "R1", "functional", "local mutation")
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.row_factory = sqlite3.Row
                tables = {row[0] for row in conn.execute("select name from sqlite_master where type='table'")}
                event = conn.execute("select * from events order by sequence desc limit 1").fetchone()

        self.assertNotIn("runtime_snapshots", tables)
        self.assertIsNotNone(event)
        before = json.loads(event["before_json"])
        after = json.loads(event["after_json"])
        self.assertNotIn("canonical_mutations", before)
        self.assertNotIn("canonical_mutations", after)
        self.assertTrue(event["entity_type"])
        self.assertTrue(event["entity_id"])
        self.assertTrue(event["actor"])
        self.assertTrue(event["command"])
        self.assertTrue(event["correlation_id"])
        self.assertLess(len(json.dumps(before, sort_keys=True)) + len(json.dumps(after, sort_keys=True)), 4096)

    def test_audit_events_are_append_only_and_summaries_are_bounded(self) -> None:
        summary = compact_summary(
            {
                "id": "R1",
                "reason": "x" * (SUMMARY_STRING_LIMIT + 100),
                "connector_token": "must-not-enter-audit-summary",
            }
        )
        self.assertEqual(len(summary["reason"]), SUMMARY_STRING_LIMIT)
        self.assertNotIn("connector_token", summary)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            harness_db.add_requirement(root, "R1", "functional", "append-only audit")
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.row_factory = sqlite3.Row
                self.assertEqual(validate_audit_events(conn), [])
                sequence = conn.execute("select max(sequence) from events").fetchone()[0]
                with self.assertRaisesRegex(sqlite3.IntegrityError, "events are append-only"):
                    conn.execute("update events set actor='forged' where sequence=?", (sequence,))
                with self.assertRaisesRegex(sqlite3.IntegrityError, "events are append-only"):
                    conn.execute("delete from events where sequence=?", (sequence,))

    def test_requirement_mutation_rebuilds_only_locked_projection_set(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            unrelated = {
                root / "docs/harness/delivery.md": "damaged delivery sentinel\n",
                root / "docs/harness/findings.md": "damaged findings sentinel\n",
            }
            for path, content in unrelated.items():
                path.write_text(content, encoding="utf-8")

            with mock.patch.object(
                harness_db,
                "render_affected",
                wraps=harness_db.render_affected,
            ) as targeted, mock.patch.object(
                harness_db,
                "render_all",
                side_effect=AssertionError("normal requirement mutation used full rebuild"),
            ):
                harness_db.add_requirement(root, "R1", "functional", "targeted views")

            targeted.assert_called_once_with(
                root,
                "project-state",
                "requirements",
                "traceability",
            )
            requirement_view = (
                root / ".ai-team/requirements/requirements.md"
            ).read_text(encoding="utf-8")
            traceability_view = (
                root / ".ai-team/requirements/traceability.md"
            ).read_text(encoding="utf-8")
            for path, content in unrelated.items():
                self.assertEqual(path.read_text(encoding="utf-8"), content)

        self.assertIn("targeted views", requirement_view)
        self.assertIn("R1", traceability_view)


if __name__ == "__main__":
    unittest.main()

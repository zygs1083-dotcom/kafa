from __future__ import annotations

import ast
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "codex-project-harness"
SCRIPTS_ROOT = PLUGIN_ROOT / "scripts"
for path in [PLUGIN_ROOT, SCRIPTS_ROOT]:
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import harness_db
from core.store import InMemoryStore, SqliteStore


def task_revision(root: Path, task_id: str) -> int:
    with harness_db.connection(root) as conn:
        return int(conn.execute("select revision from tasks where id = ?", (task_id,)).fetchone()[0])


class StoreSeamTest(unittest.TestCase):
    def use_in_memory_store(self, root: Path) -> InMemoryStore:
        store = InMemoryStore(root)
        harness_db.set_store_factory(lambda _: store)
        self.addCleanup(harness_db.set_store_factory, SqliteStore)
        return store

    def test_in_memory_store_runs_task_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.use_in_memory_store(root)

            harness_db.init_runtime(root)
            harness_db.add_acceptance(root, "AC1", "Example acceptance")
            harness_db.add_task(root, "T1", "Example task", owner="developer", acceptance="AC1")
            producer_token = harness_db.claim_task(root, "T1", "developer", task_revision(root, "T1"))
            harness_db.start_task(root, "T1", "developer", lease_token=producer_token, expected_revision=task_revision(root, "T1"))
            harness_db.submit_task(root, "T1", "implemented", agent="developer", lease_token=producer_token, expected_revision=task_revision(root, "T1"))
            reviewer_token = harness_db.review_task(root, "T1", "qa-reviewer", task_revision(root, "T1"))
            harness_db.accept_task(root, "T1", "accepted", agent="qa-reviewer", lease_token=reviewer_token, expected_revision=task_revision(root, "T1"))

            with harness_db.connection(root) as conn:
                status = conn.execute("select status from tasks where id = 'T1'").fetchone()[0]

            self.assertEqual(status, "accepted")

    def test_store_transaction_before_commit_rolls_back_invariant_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.use_in_memory_store(root)
            harness_db.init_runtime(root)
            harness_db.add_acceptance(root, "AC1", "Example acceptance")
            harness_db.add_task(root, "T1", "Example task", owner="developer", acceptance="AC1")

            with self.assertRaises(harness_db.HarnessError):
                with harness_db.transaction(root, touched=[("task", "T1")]) as conn:
                    conn.execute(
                        """
                        update tasks
                        set status = 'accepted', evidence = 'manual', accepted_by = 'developer'
                        where id = 'T1'
                        """
                    )

            with harness_db.connection(root) as conn:
                status, accepted_by = conn.execute("select status, accepted_by from tasks where id = 'T1'").fetchone()

            self.assertEqual((status, accepted_by), ("ready", ""))

    def test_store_seam_static_boundaries(self) -> None:
        store_text = (PLUGIN_ROOT / "core/store.py").read_text(encoding="utf-8")
        harness_db_text = (SCRIPTS_ROOT / "harness_db.py").read_text(encoding="utf-8")
        imported_modules = {
            alias.name
            for node in ast.walk(ast.parse(store_text))
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported_modules.update(
            node.module or ""
            for node in ast.walk(ast.parse(store_text))
            if isinstance(node, ast.ImportFrom)
        )

        self.assertNotIn("harness_db", imported_modules)
        for forbidden in ["core.api", "core.gate_engine", "core.invariant_checker", "core.projections"]:
            self.assertNotIn(forbidden, imported_modules)
        self.assertNotIn("sqlite3.connect(", harness_db_text)

    def test_store_api_is_reexported_through_core_api(self) -> None:
        from core import api

        self.assertIs(api.DB_PATH, harness_db.DB_PATH)
        self.assertIs(api.set_store_factory, harness_db.set_store_factory)
        self.assertIs(api.get_store, harness_db.get_store)
        self.assertTrue(callable(api.connection))
        self.assertTrue(callable(api.transaction))


if __name__ == "__main__":
    unittest.main()

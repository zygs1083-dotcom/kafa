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


class StoreSeamTest(unittest.TestCase):
    def use_in_memory_store(self, root: Path) -> InMemoryStore:
        store = InMemoryStore(root)
        harness_db.set_store_factory(lambda _: store)
        self.addCleanup(harness_db.set_store_factory, SqliteStore)
        self.addCleanup(store.close)
        return store

    def test_in_memory_store_runs_task_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.use_in_memory_store(root)

            harness_db.init_runtime(root)
            harness_db.add_acceptance(root, "AC1", "Example acceptance")
            harness_db.add_task(root, "T1", "Example task", owner="developer", acceptance="AC1")
            harness_db.start_task(root, "T1")
            harness_db.submit_task(root, "T1", "implemented", context_id="producer-context")
            harness_db.accept_task(root, "T1", "accepted")

            with harness_db.connection(root) as conn:
                status = conn.execute("select status from tasks where id = 'T1'").fetchone()[0]

            self.assertEqual(status, "accepted")
            self.assertFalse(
                (root / ".ai-team/state/harness.db.operation.lock").exists()
            )

    def test_store_transaction_before_commit_rolls_back_missing_evidence(self) -> None:
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
                        update tasks set status = 'accepted', accepted_by = 'root-controller'
                        where id = 'T1'
                        """
                    )

            with harness_db.connection(root) as conn:
                status, evidence, accepted_by = conn.execute(
                    "select status, evidence, accepted_by from tasks where id = 'T1'"
                ).fetchone()

            self.assertEqual((status, evidence, accepted_by), ("planned", "", ""))

    def test_in_memory_transaction_rolls_back_base_exception(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = self.use_in_memory_store(Path(temp))
            with store.connection() as conn:
                conn.execute("create table sample (value text not null)")
                conn.commit()

            with self.assertRaisesRegex(KeyboardInterrupt, "injected-cancel"):
                with store.transaction() as conn:
                    conn.execute("insert into sample (value) values ('cancelled')")
                    raise KeyboardInterrupt("injected-cancel")

            with store.connection() as conn:
                self.assertFalse(conn.in_transaction)
                self.assertEqual(
                    conn.execute("select count(*) from sample").fetchone()[0],
                    0,
                )

            with store.transaction() as conn:
                conn.execute("insert into sample (value) values ('recovered')")
            with store.connection() as conn:
                self.assertEqual(
                    conn.execute("select count(*) from sample").fetchone()[0],
                    1,
                )

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

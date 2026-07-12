from __future__ import annotations

import ast
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "codex-project-harness"
CORE_ROOT = PLUGIN_ROOT / "core"
HARNESS_DB = PLUGIN_ROOT / "scripts" / "harness_db.py"
HARNESS_CLI = PLUGIN_ROOT / "scripts" / "harness.py"


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    modules.update(
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    )
    return modules


class KernelModuleArchitectureTest(unittest.TestCase):
    def test_runtime_api_is_an_explicit_contract(self) -> None:
        api_text = (CORE_ROOT / "api.py").read_text(encoding="utf-8")
        api_tree = ast.parse(api_text)
        cli_tree = ast.parse(HARNESS_CLI.read_text(encoding="utf-8"))
        cli_names = {
            alias.name
            for node in ast.walk(cli_tree)
            if isinstance(node, ast.ImportFrom) and node.module == "core.api"
            for alias in node.names
        }
        api_names = {
            alias.asname or alias.name
            for node in ast.walk(api_tree)
            if isinstance(node, ast.ImportFrom) and node.module == "harness_db"
            for alias in node.names
        }
        api_names.update(
            node.name
            for node in api_tree.body
            if isinstance(node, (ast.FunctionDef, ast.ClassDef))
        )

        self.assertNotIn("for _name in dir(_db)", api_text)
        self.assertNotIn("def __getattr__", api_text)
        self.assertIn("__all__", api_text)
        self.assertTrue(cli_names.issubset(api_names), sorted(cli_names - api_names))

    def test_schema30_delivery_is_the_only_delivery_decision_module(self) -> None:
        delivery = CORE_ROOT / "delivery.py"

        self.assertTrue(delivery.is_file())
        self.assertFalse((CORE_ROOT / "gate_engine.py").exists())
        self.assertNotIn("harness_db", imported_modules(delivery))

    def test_schema_lifecycle_owns_database_ddl(self) -> None:
        lifecycle = CORE_ROOT / "schema_lifecycle.py"
        self.assertTrue(lifecycle.is_file())
        lifecycle_text = lifecycle.read_text(encoding="utf-8")
        harness_db_text = HARNESS_DB.read_text(encoding="utf-8")

        self.assertIn("def create_schema", lifecycle_text)
        self.assertIn("create table if not exists project", lifecycle_text)
        self.assertNotIn("create table if not exists project", harness_db_text)
        self.assertIn("from core.schema_lifecycle import", harness_db_text)

    def test_cycle_ledger_owns_cycle_scoped_read_models(self) -> None:
        ledger = CORE_ROOT / "cycle_ledger.py"
        self.assertTrue(ledger.is_file())
        ledger_text = ledger.read_text(encoding="utf-8")
        harness_db_text = HARNESS_DB.read_text(encoding="utf-8")

        self.assertNotIn("harness_db", imported_modules(ledger))
        for function in [
            "current_cycle_row",
            "baseline_issues",
            "traceability_issues",
        ]:
            self.assertIn(f"def {function}", ledger_text)
            self.assertNotIn(f"def {function}", harness_db_text)
        self.assertIn("from core.cycle_ledger import", harness_db_text)

    def test_harness_db_is_reduced_by_a_real_module_extraction(self) -> None:
        line_count = len(HARNESS_DB.read_text(encoding="utf-8").splitlines())

        self.assertLess(line_count, 9000)


if __name__ == "__main__":
    unittest.main()

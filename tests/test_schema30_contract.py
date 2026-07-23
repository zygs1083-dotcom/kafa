from __future__ import annotations

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
from harness_lib import load_distribution_manifest  # noqa: E402
from core.schema_lifecycle import (  # noqa: E402
    ACTIVE_JSON_SCHEMAS,
    ACTIVE_SCHEMA_VERSION,
    ACTIVE_TABLES,
    SCHEMA30_JSON_SCHEMAS,
    SCHEMA30_TABLES,
    SCHEMA30_VERSION,
    create_active_schema,
    create_schema30,
)


APPROVED_SCHEMA30_TABLES = {
    "project",
    "delivery_cycles",
    "requirements",
    "acceptance",
    "requirement_acceptance",
    "failure_modes",
    "failure_mode_acceptance",
    "baselines",
    "tasks",
    "task_acceptance",
    "task_failure_modes",
    "task_dependencies",
    "test_targets",
    "task_test_targets",
    "executions",
    "validations",
    "validation_executions",
    "validation_failure_modes",
    "findings",
    "quality_gates",
    "quality_gate_findings",
    "deliveries",
    "delivery_acceptance",
    "decisions",
    "invalidations",
    "migrations",
    "events",
}

APPROVED_SCHEMA30_JSON_SCHEMAS = {
    "acceptance.schema.json",
    "baseline.schema.json",
    "delivery-cycle.schema.json",
    "delivery.schema.json",
    "event.schema.json",
    "execution.schema.json",
    "failure-mode.schema.json",
    "finding.schema.json",
    "invalidation.schema.json",
    "project-state.schema.json",
    "quality-gate.schema.json",
    "requirement.schema.json",
    "task-test-target.schema.json",
    "task.schema.json",
    "test-target.schema.json",
    "validation.schema.json",
}

EXECUTION_PROPERTIES = {
    "id",
    "cycle_id",
    "candidate_sha",
    "target_id",
    "command",
    "exit_code",
    "stdout_sha256",
    "artifact_path",
    "executed_count",
    "result_format",
    "semantic_status",
    "runner",
    "sandbox_status",
    "no_network",
    "policy_status",
    "created_at",
}

REQUIRED_EXECUTION_PROPERTIES = EXECUTION_PROPERTIES - {"target_id"}

ACTIVE_EXECUTION_PROPERTIES = EXECUTION_PROPERTIES | {
    "target_definition_sha256",
    "platform",
    "runtime_executable",
    "runtime_version",
    "runtime_executable_sha256",
    "policy_version",
    "container_engine",
    "container_engine_version",
    "container_engine_endpoint",
    "container_image_requested",
    "container_image_digest",
    "provenance_status",
}

APPROVED_ACTIVE_TABLES = APPROVED_SCHEMA30_TABLES | {
    "acceptance_target_qualifications",
    "quality_gate_qualifications",
    "outcome_observations",
}

DISTRIBUTION_SCHEMAS = set(load_distribution_manifest(PLUGIN_ROOT)["schemas"])

RETIRED_TABLES = {
    "adapters",
    "adapter_actions",
    "advisory_fallbacks",
    "agent_capabilities",
    "agent_provider_events",
    "agent_provider_sessions",
    "agent_reports",
    "agent_sessions",
    "agents",
    "ci_verifications",
    "codex_fanout_exports",
    "command_log",
    "connector_budgets",
    "connector_profiles",
    "dispatch_assignments",
    "dispatch_runs",
    "dispatch_worktrees",
    "evidence",
    "executor_allowlist",
    "external_session_verifications",
    "integration_attempts",
    "runtime_snapshots",
    "sandbox_executions",
    "session_attestations",
    "task_attempts",
    "task_file_claims",
    "tests",
    "validation_evidence",
    "validation_tests",
}


def runtime_tables(root: Path) -> set[str]:
    with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
        return {
            row[0]
            for row in conn.execute(
                "select name from sqlite_master where type='table' and name not like 'sqlite_%'"
            )
        }


class Schema30ContractTests(unittest.TestCase):
    def test_runtime_enums_have_one_schema_guard_source(self) -> None:
        from core import invariant_checker, schema_guard

        self.assertIs(harness_db.TASK_STATUSES, schema_guard.TASK_STATUSES)
        self.assertIs(invariant_checker.TASK_STATUSES, schema_guard.TASK_STATUSES)
        self.assertIs(
            harness_db.FAILURE_MODE_STATUSES,
            schema_guard.FAILURE_MODE_STATUSES,
        )
        self.assertIs(
            invariant_checker.FAILURE_MODE_STATUSES,
            schema_guard.FAILURE_MODE_STATUSES,
        )

    def test_schema30_inventory_and_execution_ddl_remain_locked(self) -> None:
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.execute("begin immediate")
            create_schema30(conn)
            conn.commit()
            execution_columns = conn.execute("pragma table_info(executions)").fetchall()
        ddl_properties = {row[1] for row in execution_columns}
        ddl_required = {row[1] for row in execution_columns if row[3] or row[5]}

        self.assertEqual(len(APPROVED_SCHEMA30_JSON_SCHEMAS), 16)
        self.assertEqual(SCHEMA30_JSON_SCHEMAS, APPROVED_SCHEMA30_JSON_SCHEMAS)
        self.assertEqual(ddl_properties, EXECUTION_PROPERTIES)
        self.assertEqual(ddl_required, REQUIRED_EXECUTION_PROPERTIES)

    def test_active_json_schema_inventory_and_execution_contract_are_locked(self) -> None:
        execution_schema = json.loads(
            (PLUGIN_ROOT / "schemas" / "execution.schema.json").read_text(encoding="utf-8")
        )
        quality_gate_schema = json.loads(
            (PLUGIN_ROOT / "schemas" / "quality-gate.schema.json").read_text(
                encoding="utf-8"
            )
        )
        delivery_schema = json.loads(
            (PLUGIN_ROOT / "schemas" / "delivery.schema.json").read_text(
                encoding="utf-8"
            )
        )
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.execute("begin immediate")
            create_active_schema(conn)
            conn.commit()
            execution_columns = conn.execute("pragma table_info(executions)").fetchall()
        ddl_properties = {row[1] for row in execution_columns}
        ddl_required = {row[1] for row in execution_columns if row[3] or row[5]}

        self.assertEqual(len(DISTRIBUTION_SCHEMAS), 18)
        self.assertEqual(ACTIVE_JSON_SCHEMAS, DISTRIBUTION_SCHEMAS)
        self.assertEqual(ddl_properties, ACTIVE_EXECUTION_PROPERTIES)
        self.assertEqual(ddl_required, ACTIVE_EXECUTION_PROPERTIES - {"target_id"})
        self.assertEqual(set(execution_schema["properties"]), ACTIVE_EXECUTION_PROPERTIES)
        self.assertEqual(
            set(execution_schema["required"]),
            ACTIVE_EXECUTION_PROPERTIES - {"target_id"},
        )
        self.assertIs(execution_schema["readOnly"], True)
        self.assertIs(execution_schema["additionalProperties"], False)
        self.assertEqual(
            quality_gate_schema["properties"]["review_status"]["enum"],
            ["reviewed-local", "same-context-degraded"],
        )
        self.assertEqual(
            delivery_schema["properties"]["decision_status"]["enum"],
            [
                "delivered",
                "accepted-risk",
                "same-context-degraded",
                "historical-migrated",
            ],
        )

    def test_staging_factory_creates_only_the_locked_schema30_inventory(self) -> None:
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.execute("pragma foreign_keys = on")
            conn.execute("begin immediate")
            create_schema30(conn)
            conn.commit()
            tables = {
                row[0]
                for row in conn.execute(
                    "select name from sqlite_master where type='table' and name not like 'sqlite_%'"
                )
            }
            foreign_key_issues = conn.execute("pragma foreign_key_check").fetchall()

        self.assertEqual(SCHEMA30_VERSION, 30)
        self.assertEqual(SCHEMA30_TABLES, APPROVED_SCHEMA30_TABLES)
        self.assertEqual(tables, APPROVED_SCHEMA30_TABLES)
        self.assertEqual(foreign_key_issues, [])

    def test_greenfield_has_exact_approved_active_tables(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            tables = runtime_tables(root)

        self.assertEqual(harness_db.SCHEMA_VERSION, ACTIVE_SCHEMA_VERSION)
        self.assertEqual(len(APPROVED_SCHEMA30_TABLES), 27)
        self.assertEqual(len(APPROVED_ACTIVE_TABLES), 30)
        self.assertEqual(ACTIVE_TABLES, APPROVED_ACTIVE_TABLES)
        self.assertEqual(tables, APPROVED_ACTIVE_TABLES)

    def test_unknown_or_retired_table_fails_structure_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            tables = runtime_tables(root)

        self.assertFalse(tables & RETIRED_TABLES, sorted(tables & RETIRED_TABLES))
        self.assertEqual(tables - APPROVED_ACTIVE_TABLES, set())

    def test_identified_failure_mode_uses_non_null_empty_acceptance_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            harness_db.add_acceptance(root, "AC1", "safe failure", "must")
            harness_db.add_failure_mode(
                root,
                "FM1",
                "delivery",
                "failure",
                "trigger",
                "fail closed",
                risk="high",
                acceptance="AC1",
            )
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                row = conn.execute(
                    "select accepted_by, acceptance_reason, acceptance_scope, expires_at "
                    "from failure_modes where id='FM1'"
                ).fetchone()

        self.assertEqual(row, ("", "", "", ""))


if __name__ == "__main__":
    unittest.main()

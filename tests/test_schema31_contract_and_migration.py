from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "codex-project-harness"
SCRIPTS = PLUGIN_ROOT / "scripts"
HARNESS = SCRIPTS / "harness.py"
for path in (PLUGIN_ROOT, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import harness_db  # noqa: E402
from core import delivery as delivery_policy  # noqa: E402
from core import local_core_migration, schema_lifecycle  # noqa: E402
from core.cycle_ledger import current_candidate_sha  # noqa: E402
from core.local_core_migration import (  # noqa: E402
    InjectedLocalCoreMigrationFailure,
    LocalCoreMigrationError,
)
from core.projections import PROJECTION_ROLLBACK_PATHS, render_failure_modes  # noqa: E402
from core.store import ProjectOperationLockError, SqliteStore  # noqa: E402
from tests.legacy_schema_fixtures import create_schema28_fixture  # noqa: E402
from tests.test_schema30_migration import init_schema29_fixture  # noqa: E402
from run_agent_e2e_eval import _create_schema27_fixture  # noqa: E402


APPROVED_SCHEMA31_TABLES = frozenset(
    {
        *schema_lifecycle.SCHEMA30_TABLES,
        "acceptance_target_qualifications",
        "quality_gate_qualifications",
        "outcome_observations",
    }
)
RETIRED_TABLES = frozenset(
    {
        "adapters",
        "adapter_actions",
        "agent_sessions",
        "ci_verifications",
        "connector_budgets",
        "connector_profiles",
        "dispatch_assignments",
        "dispatch_runs",
        "external_session_verifications",
        "integration_attempts",
        "session_attestations",
        "task_attempts",
    }
)


def run_harness(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HARNESS), "--root", str(root), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def require_callable(test: unittest.TestCase, module: object, name: str):
    value = getattr(module, name, None)
    test.assertTrue(
        callable(value),
        f"EXPECTED_CONTRACT_RED missing API: {module.__name__}.{name}",
    )
    return value


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def table_names(conn: sqlite3.Connection, *, include_internal: bool = False) -> set[str]:
    condition = "" if include_internal else " and name not like 'sqlite_%'"
    return {
        str(row[0])
        for row in conn.execute(
            "select name from sqlite_master where type='table'" + condition
        )
    }


def create_schema30_source(
    root: Path,
    *,
    requirement_status: str = "active",
    acceptance_status: str = "active",
    failure_mode_status: str = "active",
) -> Path:
    db = root / ".ai-team/state/harness.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    artifact = root / ".ai-team/runtime/legacy-execution.out"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"legacy execution passed\n")
    candidate = "a" * 64
    with closing(sqlite3.connect(db)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("pragma foreign_keys = on")
        conn.execute("begin immediate")
        schema_lifecycle.create_schema30(conn)
        conn.execute(
            """
            insert into project
            (id, project_id, schema_version, runtime_version, phase, current_cycle_id,
             status, scope_status, current_owner, revision, updated_at)
            values (1, 'schema30-source', 30, '5.0.0', 'intake', 'CYCLE-current',
                    'active', 'draft', 'controller', 1, '2026-07-20T00:00:00Z')
            """
        )
        conn.execute(
            """
            insert into delivery_cycles
            (id, name, goal, status, phase, base_ref, candidate_sha, started_at,
             closed_at, created_at, updated_at)
            values ('CYCLE-current', 'Schema 30 source', 'preserve all local facts',
                    'active', 'intake', '', ?, '2026-07-20T00:00:00Z', '',
                    '2026-07-20T00:00:00Z', '2026-07-20T00:00:00Z')
            """,
            (candidate,),
        )
        conn.execute(
            """
            insert into requirements
            (id, cycle_id, kind, body, priority, status, revision, updated_at)
            values ('REQ1', 'CYCLE-current', 'functional', 'preserve requirement',
                    'must', ?, 1, '2026-07-20T00:00:00Z')
            """,
            (requirement_status,),
        )
        conn.execute(
            """
            insert into acceptance
            (id, cycle_id, criterion, priority, status, revision)
            values ('AC1', 'CYCLE-current', 'preserve acceptance', 'must', ?, 1)
            """,
            (acceptance_status,),
        )
        conn.execute(
            "insert into requirement_acceptance values "
            "('CYCLE-current', 'REQ1', 'AC1')"
        )
        conn.execute(
            """
            insert into failure_modes
            (id, cycle_id, feature, scenario, trigger, expected_behavior, recovery,
             data_safety, risk, status, accepted_by, acceptance_reason,
             acceptance_scope, accepted_revision, expires_at, revision)
            values ('FM1', 'CYCLE-current', 'migration', 'copy fails', 'migration',
                    'rollback', 'restore backup', 'no loss', 'low', ?, '', '', '',
                    null, '', 1)
            """,
            (failure_mode_status,),
        )
        conn.execute(
            "insert into failure_mode_acceptance values "
            "('CYCLE-current', 'FM1', 'AC1')"
        )
        conn.execute(
            """
            insert into tasks
            (id, cycle_id, task, owner, status, evidence, submitted_context_id,
             accepted_by, revision, updated_at)
            values ('T1', 'CYCLE-current', 'preserve task', 'developer', 'accepted',
                    'reviewed', 'producer-context', 'root-controller', 4,
                    '2026-07-20T00:00:00Z')
            """
        )
        conn.execute(
            "insert into task_acceptance values ('CYCLE-current', 'T1', 'AC1')"
        )
        conn.execute(
            """
            insert into test_targets
            (id, kind, command_template, description, gateable, gate_block_reason,
             stack_profile, container_image, requires_sandbox, requires_no_network,
             result_format, result_path, created_at, updated_at)
            values ('UNIT', 'unit', 'python3 -B -m unittest test_legacy.py',
                    'legacy target', 1, '', 'python', '', 0, 0, 'regex', '',
                    '2026-07-20T00:00:00Z', '2026-07-20T00:00:00Z')
            """
        )
        conn.execute(
            "insert into task_test_targets values ('CYCLE-current', 'T1', 'UNIT')"
        )
        conn.execute(
            """
            insert into executions
            (id, cycle_id, candidate_sha, target_id, command, exit_code,
             stdout_sha256, artifact_path, executed_count, result_format,
             semantic_status, runner, sandbox_status, no_network, policy_status,
             created_at)
            values ('EX1', 'CYCLE-current', ?, 'UNIT',
                    'python3 -B -m unittest test_legacy.py', 0, ?,
                    '.ai-team/runtime/legacy-execution.out', 1, 'regex', 'pass',
                    'local', 'available', 0, 'allowed', '2026-07-20T00:00:00Z')
            """,
            (candidate, sha256_file(artifact)),
        )
        conn.execute(
            """
            insert into validations
            (id, cycle_id, candidate_sha, acceptance_id, surface, result,
             validation_status, superseded_by, findings, residual_risk, created_at)
            values ('VAL1', 'CYCLE-current', ?, 'AC1', 'unit', 'pass', 'active',
                    null, '', '', '2026-07-20T00:00:00Z')
            """,
            (candidate,),
        )
        conn.execute(
            "insert into validation_executions values "
            "('VAL1', 'EX1', 'CYCLE-current', ?)",
            (candidate,),
        )
        conn.execute(
            "insert into validation_failure_modes values "
            "('VAL1', 'CYCLE-current', 'FM1')"
        )
        conn.execute(
            """
            insert into quality_gates
            (id, sequence, cycle_id, candidate_sha, gate_status, superseded_by,
             gate, producer_context_id, reviewer_context_id, review_status, result,
             blocking_findings, residual_risk, reviewed_revision, created_at)
            values ('G1', 1, 'CYCLE-current', ?, 'active', null, 'independent_qa',
                    'producer-context', 'reviewer-context', 'reviewed-local', 'pass',
                    '', '', 1, '2026-07-20T00:00:00Z')
            """,
            (candidate,),
        )
        conn.execute(
            """
            insert into decisions
            (id, cycle_id, candidate_sha, decision, reason, created_at)
            values ('D1', 'CYCLE-current', ?, 'preserve', 'migration fixture',
                    '2026-07-20T00:00:00Z')
            """,
            (candidate,),
        )
        conn.commit()
    (root / ".gitignore").write_text(
        "\n".join(harness_db.RUNTIME_GITIGNORE_PATTERNS) + "\n",
        encoding="utf-8",
    )
    return db


def write_projection_bundle(root: Path) -> dict[str, tuple[bytes, int]]:
    snapshot: dict[str, tuple[bytes, int]] = {}
    for index, relative in enumerate(PROJECTION_ROLLBACK_PATHS):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = f"schema30:{index}:{relative.as_posix()}\n".encode("utf-8")
        path.write_bytes(payload)
        path.chmod(0o640 if index % 2 == 0 else 0o444)
        snapshot[relative.as_posix()] = (payload, path.stat().st_mode & 0o7777)
    return snapshot


def assert_projection_bundle(
    test: unittest.TestCase,
    root: Path,
    expected: dict[str, tuple[bytes, int]],
) -> None:
    for relative, (payload, mode) in expected.items():
        path = root / relative
        test.assertEqual(path.read_bytes(), payload, relative)
        test.assertEqual(path.stat().st_mode & 0o7777, mode, relative)


def latest_manifest(root: Path) -> tuple[Path, dict[str, object]]:
    manifests = sorted((root / ".ai-team/backups").glob("**/migration-manifest.json"))
    if not manifests:
        raise AssertionError("migration manifest is missing")
    path = manifests[-1]
    return path, json.loads(path.read_text(encoding="utf-8"))


def active_projection_validator(root: Path):
    def validate(_active_path: Path) -> None:
        harness_db.render_all(root)
        issues = harness_db.doctor(root, require_project_files=False)
        if issues:
            raise LocalCoreMigrationError(
                "schema31 active validation failed: " + "; ".join(issues)
            )

    return validate


def hard_exit_after_schema31_activation(root_value: str) -> None:
    migrate = getattr(local_core_migration, "migrate_project_to_active_schema", None)
    if not callable(migrate):
        os._exit(91)

    def terminate(_active_path: Path) -> None:
        os._exit(17)

    migrate(Path(root_value), active_validator=terminate)


class Schema31CatalogRedTests(unittest.TestCase):
    def test_schema31_factory_has_exact_30_product_tables_and_declared_internal_table(self) -> None:
        create_schema31 = require_callable(
            self, schema_lifecycle, "create_schema31"
        )
        self.assertEqual(getattr(schema_lifecycle, "SCHEMA31_VERSION", None), 31)
        self.assertEqual(
            getattr(schema_lifecycle, "SCHEMA31_TABLES", None),
            APPROVED_SCHEMA31_TABLES,
        )
        self.assertEqual(len(APPROVED_SCHEMA31_TABLES), 30)
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.execute("pragma foreign_keys = on")
            conn.execute("begin immediate")
            create_schema31(conn)
            conn.commit()
            product = table_names(conn)
            catalog = table_names(conn, include_internal=True)
            fk_issues = conn.execute("pragma foreign_key_check").fetchall()

        self.assertEqual(product, set(APPROVED_SCHEMA31_TABLES))
        self.assertEqual(catalog - product, {"sqlite_sequence"})
        self.assertEqual(fk_issues, [])

    def test_schema31_closes_states_and_keeps_qualification_and_execution_immutable(self) -> None:
        create_schema31 = require_callable(
            self, schema_lifecycle, "create_schema31"
        )
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.execute("pragma foreign_keys = on")
            conn.execute("begin immediate")
            create_schema31(conn)
            conn.execute(
                "insert into delivery_cycles values "
                "('CYCLE-current','Current','test','active','intake','',?, 'now','', 'now','now')",
                ("a" * 64,),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "insert into requirements "
                    "(id,cycle_id,kind,body,status,updated_at) values "
                    "('BAD-R','CYCLE-current','functional','bad','nonsense','now')"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "insert into acceptance "
                    "(id,cycle_id,criterion,status) values "
                    "('BAD-A','CYCLE-current','bad','nonsense')"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "insert into failure_modes "
                    "(id,cycle_id,feature,scenario,trigger,expected_behavior,risk,status) "
                    "values ('BAD-F','CYCLE-current','x','x','x','x','low','nonsense')"
                )
            conn.execute(
                "insert into acceptance (id,cycle_id,criterion,status) values "
                "('AC1','CYCLE-current','qualified behavior','active')"
            )
            conn.execute(
                "insert into test_targets "
                "(id,kind,command_template,description,gateable,stack_profile,"
                "requires_sandbox,requires_no_network,result_format,result_path,"
                "created_at,updated_at) values "
                "('UNIT','unit','python3 -B -m unittest','test',1,'python',0,0,"
                "'regex','','now','now')"
            )
            conn.execute(
                "insert into acceptance_target_qualifications "
                "(id,cycle_id,acceptance_id,acceptance_revision,target_id,"
                "target_definition_sha256,rationale,qualified_by,created_at) values "
                "('Q1','CYCLE-current','AC1',1,'UNIT',?,'explicit mapping','root','now')",
                ("b" * 64,),
            )
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute(
                    "update acceptance_target_qualifications set rationale='changed' where id='Q1'"
                )
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("delete from acceptance_target_qualifications where id='Q1'")
            conn.execute(
                """
                insert into executions
                (id, cycle_id, candidate_sha, target_id, target_definition_sha256,
                 command, exit_code, stdout_sha256, artifact_path, executed_count,
                 result_format, semantic_status, runner, sandbox_status, no_network,
                 policy_status, platform, runtime_executable, runtime_version,
                 runtime_executable_sha256, policy_version, container_engine,
                 container_engine_version, container_engine_endpoint,
                 container_image_requested,
                 container_image_digest, provenance_status, created_at)
                values ('EX1','CYCLE-current',?,'UNIT',?,'python3 -B -m unittest',0,?,
                        '.ai-team/runtime/out',1,'regex','pass','local','available',0,
                        'allowed','test-platform','/usr/bin/python3','3.11',?,'schema31-v2','','','',
                        '','','complete','now')
                """,
                ("a" * 64, "b" * 64, "c" * 64, "d" * 64),
            )
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("update executions set platform='changed' where id='EX1'")
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("delete from executions where id='EX1'")
            conn.rollback()


class Schema31MigrationRedTests(unittest.TestCase):
    def test_legacy_decision_write_uses_source_event_schema_and_migrates(
        self,
    ) -> None:
        stage = require_callable(
            self,
            local_core_migration,
            "stage_supported_schema_to_active",
        )
        for source_version, fixture in (
            (27, _create_schema27_fixture),
            (29, init_schema29_fixture),
            (30, create_schema30_source),
        ):
            with self.subTest(source=source_version), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                fixture(root)
                source = root / ".ai-team/state/harness.db"
                with closing(sqlite3.connect(source)) as conn:
                    before_decisions = int(
                        conn.execute("select count(*) from decisions").fetchone()[0]
                    )
                    before_events = int(
                        conn.execute("select count(*) from events").fetchone()[0]
                    )

                harness_db.record_decision(
                    root,
                    f"legacy decision {source_version}",
                    "committed before the migration operation lock",
                )

                with closing(sqlite3.connect(source)) as conn:
                    decision = conn.execute(
                        "select id from decisions where decision = ?",
                        (f"legacy decision {source_version}",),
                    ).fetchone()
                    event_columns = {
                        str(row[1]) for row in conn.execute("pragma table_info(events)")
                    }
                    event_type_column = (
                        "event_type" if "event_type" in event_columns else "type"
                    )
                    event = conn.execute(
                        f"select schema_version, {event_type_column} from events "
                        "order by sequence desc limit 1"
                    ).fetchone()
                    self.assertEqual(
                        int(conn.execute("select count(*) from decisions").fetchone()[0]),
                        before_decisions + 1,
                    )
                    self.assertEqual(
                        int(conn.execute("select count(*) from events").fetchone()[0]),
                        before_events + 1,
                    )
                self.assertIsNotNone(decision)
                self.assertEqual(event, (source_version, "decision_recorded"))

                staging = root / ".ai-team/backups/test/harness.schema31.new.db"
                stage(source, staging, project_root=root)
                with closing(sqlite3.connect(staging)) as conn:
                    migrated_decision = conn.execute(
                        "select decision, reason from decisions where id = ?",
                        (decision[0],),
                    ).fetchone()
                    migrated_event = conn.execute(
                        "select event_type from events "
                        "where event_type = 'decision_recorded' "
                        "order by sequence desc limit 1"
                    ).fetchone()
                self.assertEqual(
                    migrated_decision,
                    (
                        f"legacy decision {source_version}",
                        "committed before the migration operation lock",
                    ),
                )
                self.assertEqual(migrated_event, ("decision_recorded",))

    def test_schema30_to_31_dry_run_has_no_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = create_schema30_source(root)
            projections = write_projection_bundle(root)
            before = db.read_bytes()

            result = run_harness(
                root,
                "migrate",
                "--from-version",
                "30",
                "--to-version",
                "31",
                "--dry-run",
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("DRY-RUN", result.stdout)
            self.assertEqual(db.read_bytes(), before)
            assert_projection_bundle(self, root, projections)
            self.assertFalse((root / ".ai-team/backups").exists())
            self.assertFalse(
                (root / ".ai-team/state/local-core-migration.lock").exists()
            )

    def test_schema30_to_31_dry_run_rejects_the_same_invalid_states_as_real_migration(
        self,
    ) -> None:
        cases = (
            ({"requirement_status": "nonsense"}, "invalid requirement status"),
            ({"acceptance_status": "nonsense"}, "invalid acceptance status"),
            ({"failure_mode_status": "nonsense"}, "invalid failure-mode status"),
        )
        for source_kwargs, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                db = create_schema30_source(root, **source_kwargs)
                projections = write_projection_bundle(root)
                before = db.read_bytes()

                result = run_harness(
                    root,
                    "migrate",
                    "--from-version",
                    "30",
                    "--to-version",
                    "31",
                    "--dry-run",
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stdout + result.stderr)
                self.assertEqual(db.read_bytes(), before)
                assert_projection_bundle(self, root, projections)
                self.assertFalse((root / ".ai-team/backups").exists())
                self.assertFalse(
                    (root / ".ai-team/state/local-core-migration.lock").exists()
                )

    def test_schema30_to_31_real_migration_preserves_facts_without_inventing_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = create_schema30_source(root)

            result = run_harness(
                root, "migrate", "--from-version", "30", "--to-version", "31"
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            with closing(sqlite3.connect(db)) as conn:
                version = conn.execute(
                    "select schema_version from project where id=1"
                ).fetchone()[0]
                facts = (
                    conn.execute("select body from requirements where id='REQ1'").fetchone()[0],
                    conn.execute("select criterion from acceptance where id='AC1'").fetchone()[0],
                    conn.execute("select status from tasks where id='T1'").fetchone()[0],
                    conn.execute("select decision from decisions where id='D1'").fetchone()[0],
                )
                invented = (
                    conn.execute(
                        "select count(*) from acceptance_target_qualifications"
                    ).fetchone()[0],
                    conn.execute(
                        "select count(*) from quality_gate_qualifications"
                    ).fetchone()[0],
                    conn.execute("select count(*) from outcome_observations").fetchone()[0],
                )
                execution = conn.execute(
                    "select provenance_status, target_definition_sha256, platform, "
                    "runtime_executable_sha256 from executions where id='EX1'"
                ).fetchone()
                qualification_id = conn.execute(
                    "select qualification_id from validations where id='VAL1'"
                ).fetchone()[0]
                tables = table_names(conn)
                fk_issues = conn.execute("pragma foreign_key_check").fetchall()
            manifest_path, manifest = latest_manifest(root)
            backup_path = Path(str(manifest["backup"]["backup_path"]))
            with closing(sqlite3.connect(backup_path)) as backup_conn:
                backup_version = backup_conn.execute(
                    "select schema_version from project where id=1"
                ).fetchone()[0]

            self.assertEqual(version, 31)
            self.assertEqual(
                facts,
                (
                    "preserve requirement",
                    "preserve acceptance",
                    "accepted",
                    "preserve",
                ),
            )
            self.assertEqual(invented, (0, 0, 0))
            self.assertEqual(execution, ("legacy-incomplete", "", "", ""))
            self.assertIsNone(qualification_id)
            self.assertEqual(tables, set(APPROVED_SCHEMA31_TABLES))
            self.assertFalse(tables & RETIRED_TABLES)
            self.assertEqual(fk_issues, [])
            self.assertEqual(backup_version, 30)
            self.assertEqual(manifest["source_version"], 30)
            self.assertEqual(manifest["target_version"], 31)
            self.assertTrue(manifest_path.is_file())
            with closing(sqlite3.connect(db)) as conn:
                conn.row_factory = sqlite3.Row
                legacy_execution = conn.execute(
                    "select * from executions where id='EX1'"
                ).fetchone()
                legacy_issues = delivery_policy.execution_issues(
                    conn,
                    root,
                    legacy_execution,
                    current_candidate_sha(root),
                )
            self.assertIn("legacy-incomplete", " ".join(legacy_issues))

    def test_schema30_copy_migrates_then_completes_public_delivery_journey_in_isolated_home(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as temp,
            tempfile.TemporaryDirectory() as isolated_home,
            patch.dict(os.environ, {"HOME": isolated_home}),
        ):
            root = Path(temp)
            create_schema30_source(root)
            (root / "test_current.py").write_text(
                "import unittest\n\n"
                "class CurrentCandidateTest(unittest.TestCase):\n"
                "    def test_current_candidate(self):\n"
                "        self.assertTrue(True)\n",
                encoding="utf-8",
            )

            commands = (
                ("migrate", "--from-version", "30", "--to-version", "31"),
                (
                    "test-target",
                    "add",
                    "--id",
                    "CURRENT",
                    "--kind",
                    "unit",
                    "--command-template",
                    "python3 -B -m unittest test_current.py",
                ),
                ("test-target", "link", "--task", "T1", "--target", "CURRENT"),
                (
                    "test-target",
                    "qualify",
                    "--id",
                    "CURRENT-Q1",
                    "--target",
                    "CURRENT",
                    "--acceptance",
                    "AC1",
                    "--rationale",
                    "current unit target directly verifies migrated AC1",
                    "--by",
                    "migration-controller",
                ),
                (
                    "baseline",
                    "confirm",
                    "--id",
                    "MIGRATED-B1",
                    "--summary",
                    "migrated schema31 delivery graph",
                    "--by",
                    "migration-controller",
                ),
                (
                    "verify",
                    "run",
                    "--target",
                    "CURRENT",
                    "--acceptance",
                    "AC1",
                    "--failure-mode",
                    "FM1",
                ),
                (
                    "gate",
                    "record",
                    "--reviewer-context",
                    "fresh",
                    "--reviewer-context-id",
                    "migration-reviewer",
                    "--result",
                    "pass",
                    "--qualification",
                    "CURRENT-Q1",
                ),
                ("delivery", "ready"),
                ("delivery", "record", "--scope", "migrated schema31 graph"),
            )
            results = [run_harness(root, *args) for args in commands]

            self.assertEqual(
                [result.returncode for result in results],
                [0] * len(commands),
                "\n".join(result.stdout + result.stderr for result in results),
            )
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                version, phase = conn.execute(
                    "select schema_version, phase from project where id=1"
                ).fetchone()
                cycle_status, closed_at = conn.execute(
                    "select status, closed_at from delivery_cycles "
                    "where id='CYCLE-current'"
                ).fetchone()
                facts = tuple(
                    conn.execute(f"select count(*) from {table}").fetchone()[0]
                    for table in (
                        "acceptance_target_qualifications",
                        "quality_gate_qualifications",
                        "deliveries",
                    )
                )
            self.assertEqual((version, phase), (31, "delivery_readiness"))
            self.assertEqual(cycle_status, "delivered")
            self.assertTrue(closed_at)
            self.assertEqual(facts, (1, 1, 1))

    def test_supported_schema_27_28_29_stage_directly_to_schema31(self) -> None:
        stage = require_callable(
            self, local_core_migration, "stage_supported_schema_to_active"
        )
        for source_version, fixture in (
            (27, _create_schema27_fixture),
            (28, create_schema28_fixture),
            (29, init_schema29_fixture),
        ):
            with self.subTest(source=source_version), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                fixture(root)
                source = root / ".ai-team/state/harness.db"
                before = source.read_bytes()
                staging = root / ".ai-team/backups/test/harness.schema31.new.db"

                report = stage(source, staging, project_root=root)

                self.assertEqual(source.read_bytes(), before)
                self.assertEqual(report.source_version, source_version)
                self.assertEqual(report.target_version, 31)
                with closing(sqlite3.connect(staging)) as conn:
                    version = conn.execute(
                        "select schema_version from project where id=1"
                    ).fetchone()[0]
                    tables = table_names(conn)
                    invented = (
                        conn.execute(
                            "select count(*) from acceptance_target_qualifications"
                        ).fetchone()[0],
                        conn.execute(
                            "select count(*) from quality_gate_qualifications"
                        ).fetchone()[0],
                        conn.execute(
                            "select count(*) from outcome_observations"
                        ).fetchone()[0],
                    )
                    provenance = {
                        str(row[0])
                        for row in conn.execute(
                            "select distinct provenance_status from executions"
                        )
                    }
                self.assertEqual(version, 31)
                self.assertEqual(tables, set(APPROVED_SCHEMA31_TABLES))
                self.assertFalse(tables & RETIRED_TABLES)
                self.assertEqual(invented, (0, 0, 0))
                self.assertLessEqual(provenance, {"legacy-incomplete"})

    def test_supported_legacy_generations_reject_every_invalid_entity_state(
        self,
    ) -> None:
        stage = require_callable(
            self,
            local_core_migration,
            "stage_supported_schema_to_active",
        )
        fixtures = (
            (27, _create_schema27_fixture),
            (28, create_schema28_fixture),
            (29, init_schema29_fixture),
        )
        for source_version, fixture in fixtures:
            for table, label in (
                ("requirements", "requirement"),
                ("acceptance", "acceptance"),
                ("failure_modes", "failure-mode"),
            ):
                with (
                    self.subTest(source=source_version, entity=label),
                    tempfile.TemporaryDirectory() as temp,
                ):
                    root = Path(temp)
                    fixture(root)
                    source = root / ".ai-team/state/harness.db"
                    with closing(sqlite3.connect(source)) as conn:
                        conn.execute("pragma ignore_check_constraints=on")
                        existing = conn.execute(
                            f"select id from {table} order by rowid limit 1"
                        ).fetchone()
                        if existing is not None:
                            conn.execute(
                                f"update {table} set status='nonsense' where id=?",
                                (existing[0],),
                            )
                        elif table == "requirements":
                            conn.execute(
                                "insert into requirements "
                                "(id, cycle_id, kind, body, status, updated_at) "
                                "values ('BAD-R', 'CYCLE-current', 'functional', "
                                "'invalid state', 'nonsense', 'now')"
                            )
                        elif table == "acceptance":
                            conn.execute(
                                "insert into acceptance "
                                "(id, cycle_id, criterion, status) "
                                "values ('BAD-A', 'CYCLE-current', "
                                "'invalid state', 'nonsense')"
                            )
                        else:
                            conn.execute(
                                "insert into failure_modes "
                                "(id, cycle_id, feature, scenario, trigger, "
                                "expected_behavior, risk, status) values "
                                "('BAD-F', 'CYCLE-current', 'migration', "
                                "'invalid state', 'migration', 'reject', "
                                "'low', 'nonsense')"
                            )
                        conn.commit()
                    before = source.read_bytes()
                    staging = (
                        root / ".ai-team/backups/test/harness.schema31.new.db"
                    )

                    with self.assertRaisesRegex(
                        Exception,
                        rf"invalid {label} status",
                    ):
                        stage(source, staging, project_root=root)

                    self.assertEqual(source.read_bytes(), before)
                    self.assertFalse(staging.exists())

    def test_unknown_legacy_states_fail_preflight_without_active_mutation(self) -> None:
        cases = (
            ("requirement", {"requirement_status": "nonsense"}),
            ("acceptance", {"acceptance_status": "nonsense"}),
            ("failure-mode", {"failure_mode_status": "nonsense"}),
        )
        for label, kwargs in cases:
            with self.subTest(entity=label), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                db = create_schema30_source(root, **kwargs)
                before = db.read_bytes()

                result = run_harness(
                    root, "migrate", "--from-version", "30", "--to-version", "31"
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    f"invalid {label} status",
                    result.stdout + result.stderr,
                )
                self.assertEqual(db.read_bytes(), before)
                self.assertFalse(
                    (root / ".ai-team/state/local-core-migration.lock").exists()
                )

    def test_schema30_state_preflight_does_not_trim_casefold_or_default_values(self) -> None:
        preflight = require_callable(
            self,
            local_core_migration,
            "preflight_schema30_to_active",
        )
        cases = tuple(
            (label, field, value)
            for label, field in (
                ("requirement", "requirement_status"),
                ("acceptance", "acceptance_status"),
                ("failure-mode", "failure_mode_status"),
            )
            for value in ("", " active ", "Active")
        )
        for label, field, value in cases:
            with (
                self.subTest(entity=label, value=value),
                tempfile.TemporaryDirectory() as temp,
            ):
                root = Path(temp)
                db = create_schema30_source(root, **{field: value})
                before = db.read_bytes()

                with self.assertRaisesRegex(
                    LocalCoreMigrationError,
                    rf"invalid {label} status",
                ):
                    preflight(db)

                self.assertEqual(db.read_bytes(), before)
                self.assertFalse((root / ".ai-team/backups").exists())

    def test_schema30_allowed_states_are_preserved_without_silent_coercion(self) -> None:
        stage = require_callable(
            self,
            local_core_migration,
            "stage_supported_schema_to_active",
        )
        cases = (
            ("active", "active", "identified"),
            ("cancelled", "cancelled", "accepted"),
            ("active", "active", "exempt"),
        )
        for requirement, acceptance, failure_mode in cases:
            with (
                self.subTest(
                    requirement=requirement,
                    acceptance=acceptance,
                    failure_mode=failure_mode,
                ),
                tempfile.TemporaryDirectory() as temp,
            ):
                root = Path(temp)
                source = create_schema30_source(
                    root,
                    requirement_status=requirement,
                    acceptance_status=acceptance,
                    failure_mode_status=failure_mode,
                )
                with closing(sqlite3.connect(source)) as conn:
                    conn.execute(
                        "update requirements set revision=5 where id='REQ1'"
                    )
                    conn.execute(
                        "update acceptance set revision=6 where id='AC1'"
                    )
                    conn.execute(
                        """
                        update failure_modes
                        set revision=7, accepted_by=?, acceptance_reason=?,
                            acceptance_scope=?, accepted_revision=?, expires_at=?
                        where id='FM1'
                        """,
                        (
                            "risk-owner" if failure_mode in {"accepted", "exempt"} else "",
                            "preserve exact metadata" if failure_mode in {"accepted", "exempt"} else "",
                            "legacy candidate" if failure_mode in {"accepted", "exempt"} else "",
                            1 if failure_mode in {"accepted", "exempt"} else None,
                            "2099-01-01T00:00:00Z" if failure_mode in {"accepted", "exempt"} else "",
                        ),
                    )
                    conn.commit()
                staging = root / ".ai-team/backups/test/harness.schema31.new.db"

                report = stage(source, staging, project_root=root)

                with closing(sqlite3.connect(staging)) as conn:
                    actual = (
                        conn.execute(
                            "select status, revision from requirements where id='REQ1'"
                        ).fetchone(),
                        conn.execute(
                            "select status, revision from acceptance where id='AC1'"
                        ).fetchone(),
                        conn.execute(
                            "select status, revision, accepted_by, acceptance_reason, "
                            "acceptance_scope, accepted_revision, expires_at "
                            "from failure_modes where id='FM1'"
                        ).fetchone(),
                    )
                accepted_metadata = (
                    "risk-owner",
                    "preserve exact metadata",
                    "legacy candidate",
                    1,
                    "2099-01-01T00:00:00Z",
                ) if failure_mode in {"accepted", "exempt"} else ("", "", "", None, "")
                self.assertEqual(
                    actual,
                    (
                        (requirement, 5),
                        (acceptance, 6),
                        (failure_mode, 7, *accepted_metadata),
                    ),
                )
                self.assertEqual(report.normalized_failure_mode_count, 0)

    def test_schema30_normalization_counts_match_dry_run_and_real_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = create_schema30_source(root, failure_mode_status="active")
            with closing(sqlite3.connect(db)) as conn:
                rows = (
                    ("FM2", "active", "", "", "", None, ""),
                    ("FM3", "active", "", "", "", None, ""),
                    ("FM4", "identified", "", "", "", None, ""),
                    (
                        "FM5",
                        "accepted",
                        "risk-owner",
                        "accepted legacy risk",
                        "legacy candidate",
                        1,
                        "2099-01-01T00:00:00Z",
                    ),
                    (
                        "FM6",
                        "exempt",
                        "risk-owner",
                        "exempt legacy risk",
                        "legacy candidate",
                        1,
                        "2099-01-01T00:00:00Z",
                    ),
                )
                for (
                    fm_id,
                    status,
                    accepted_by,
                    reason,
                    scope,
                    accepted_revision,
                    expires_at,
                ) in rows:
                    conn.execute(
                        """
                        insert into failure_modes
                        (id, cycle_id, feature, scenario, trigger,
                         expected_behavior, risk, status, accepted_by,
                         acceptance_reason, acceptance_scope,
                         accepted_revision, expires_at, revision)
                        values (?, 'CYCLE-current', 'migration', 'count',
                                'migration', 'preserve exact count', 'low', ?,
                                ?, ?, ?, ?, ?, 1)
                        """,
                        (
                            fm_id,
                            status,
                            accepted_by,
                            reason,
                            scope,
                            accepted_revision,
                            expires_at,
                        ),
                    )
                conn.commit()

            dry_run = run_harness(
                root,
                "migrate",
                "--from-version",
                "30",
                "--to-version",
                "31",
                "--dry-run",
            )
            self.assertEqual(dry_run.returncode, 0, dry_run.stdout + dry_run.stderr)
            self.assertIn("normalized_failure_modes: 3", dry_run.stdout)

            real = run_harness(
                root,
                "migrate",
                "--from-version",
                "30",
                "--to-version",
                "31",
            )
            self.assertEqual(real.returncode, 0, real.stdout + real.stderr)
            with closing(sqlite3.connect(db)) as conn:
                states = dict(
                    conn.execute(
                        "select id, status from failure_modes order by id"
                    )
                )
            _, manifest = latest_manifest(root)
            self.assertEqual(
                states,
                {
                    "FM1": "identified",
                    "FM2": "identified",
                    "FM3": "identified",
                    "FM4": "identified",
                    "FM5": "accepted",
                    "FM6": "exempt",
                },
            )
            self.assertEqual(
                manifest["staging"]["normalized_failure_mode_count"],
                3,
            )

    def test_schema30_failure_mode_projection_does_not_require_candidate_identity(
        self,
    ) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink primitive unavailable")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            create_schema30_source(root, failure_mode_status="active")
            source = root / "business-source.txt"
            source.write_text("business input\n", encoding="utf-8")
            (root / "business-link.txt").symlink_to(source.name)

            render_failure_modes(root)

            projection = (
                root / ".ai-team/requirements/failure-modes.md"
            ).read_text(encoding="utf-8")
            self.assertIn("| FM1 ", projection)
            self.assertIn("| active | covered |", projection)

    def test_schema30_active_failure_mode_normalizes_only_to_identified(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = create_schema30_source(root, failure_mode_status="active")

            result = run_harness(
                root, "migrate", "--from-version", "30", "--to-version", "31"
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            with closing(sqlite3.connect(db)) as conn:
                row = conn.execute(
                    "select status, revision from failure_modes where id='FM1'"
                ).fetchone()
            _, manifest = latest_manifest(root)
            self.assertEqual(row, ("identified", 1))
            self.assertEqual(
                manifest["staging"]["normalized_failure_mode_count"],
                1,
            )


class Schema31RollbackRedTests(unittest.TestCase):
    def migration_api(self):
        return require_callable(
            self, local_core_migration, "migrate_project_to_active_schema"
        )

    def test_pre_activation_failure_preserves_exact_schema30_authority(self) -> None:
        migrate = self.migration_api()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = create_schema30_source(root)
            projections = write_projection_bundle(root)
            before = db.read_bytes()

            with self.assertRaises(InjectedLocalCoreMigrationFailure):
                migrate(
                    root,
                    fail_at="before_atomic_replace",
                    active_validator=active_projection_validator(root),
                )

            self.assertEqual(db.read_bytes(), before)
            assert_projection_bundle(self, root, projections)
            _, manifest = latest_manifest(root)
            self.assertEqual(manifest["status"], "failed-before-activation")
            self.assertFalse(
                (root / ".ai-team/state/local-core-migration.lock").exists()
            )

    def test_every_injected_boundary_preserves_or_restores_exact_authority(self) -> None:
        migrate = self.migration_api()
        for failure_point in sorted(
            local_core_migration.MIGRATION_FAILURE_POINTS
        ):
            with self.subTest(failure_point=failure_point), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                db = create_schema30_source(root)
                projections = write_projection_bundle(root)
                before = db.read_bytes()

                with self.assertRaises(InjectedLocalCoreMigrationFailure):
                    migrate(
                        root,
                        fail_at=failure_point,
                        active_validator=active_projection_validator(root),
                    )

                self.assertEqual(db.read_bytes(), before)
                assert_projection_bundle(self, root, projections)
                _, manifest = latest_manifest(root)
                self.assertEqual(
                    manifest["status"],
                    "rolled-back"
                    if failure_point == "after_atomic_replace"
                    else "failed-before-activation",
                )
                self.assertFalse(
                    (root / ".ai-team/state/local-core-migration.lock").exists()
                )

    def test_pre_and_post_activation_cancellation_restore_exact_authority(self) -> None:
        migrate = self.migration_api()
        for phase in ("staging", "active"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                db = create_schema30_source(root)
                projections = write_projection_bundle(root)
                before = db.read_bytes()

                def cancel(_path: Path) -> None:
                    raise KeyboardInterrupt(f"forced {phase} cancellation")

                with self.assertRaisesRegex(
                    KeyboardInterrupt,
                    f"forced {phase} cancellation",
                ):
                    migrate(
                        root,
                        staging_validator=cancel if phase == "staging" else None,
                        active_validator=(
                            cancel
                            if phase == "active"
                            else active_projection_validator(root)
                        ),
                    )

                self.assertEqual(db.read_bytes(), before)
                assert_projection_bundle(self, root, projections)
                _, manifest = latest_manifest(root)
                self.assertEqual(
                    manifest["status"],
                    "failed-before-activation"
                    if phase == "staging"
                    else "rolled-back",
                )
                self.assertFalse(
                    (root / ".ai-team/state/local-core-migration.lock").exists()
                )

    def test_post_activation_doctor_failure_restores_database_and_projections(self) -> None:
        migrate = self.migration_api()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = create_schema30_source(root)
            projections = write_projection_bundle(root)
            before_digest = sha256_file(db)

            def fail_doctor(_active_path: Path) -> None:
                raise RuntimeError("forced schema31 doctor failure")

            with self.assertRaisesRegex(RuntimeError, "forced schema31 doctor failure"):
                migrate(root, active_validator=fail_doctor)

            self.assertEqual(sha256_file(db), before_digest)
            assert_projection_bundle(self, root, projections)
            _, manifest = latest_manifest(root)
            self.assertEqual(manifest["status"], "rolled-back")
            self.assertEqual(manifest["database_restore_status"], "restored")
            self.assertEqual(manifest["projection_restore_status"], "restored")

    def test_partial_projection_publication_failure_restores_exact_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = create_schema30_source(root)
            projections = write_projection_bundle(root)
            before_digest = sha256_file(db)
            original_render = harness_db.render_all

            def fail_live_render(render_root: Path) -> None:
                if render_root.resolve() != root.resolve():
                    original_render(render_root)
                    return
                target = root / PROJECTION_ROLLBACK_PATHS[0]
                target.write_bytes(b"partial schema31 publication\n")
                raise harness_db.HarnessError("partial schema31 projection failure")

            with patch.object(harness_db, "render_all", side_effect=fail_live_render):
                with self.assertRaisesRegex(
                    harness_db.HarnessError,
                    "partial schema31 projection failure",
                ):
                    harness_db.migrate(root, "30", 31)

            self.assertEqual(sha256_file(db), before_digest)
            assert_projection_bundle(self, root, projections)
            _, manifest = latest_manifest(root)
            self.assertEqual(manifest["status"], "rolled-back")
            self.assertEqual(manifest["projection_restore_status"], "restored")

    def test_projection_verification_failure_rolls_back(self) -> None:
        migrate = self.migration_api()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = create_schema30_source(root)
            projections = write_projection_bundle(root)
            before_digest = sha256_file(db)

            def publish_corrupt(_active_path: Path) -> None:
                harness_db.render_all(root)
                (root / PROJECTION_ROLLBACK_PATHS[0]).write_bytes(
                    b"corrupt after publication\n"
                )

            with self.assertRaisesRegex(
                LocalCoreMigrationError,
                "projection content verification",
            ):
                migrate(root, active_validator=publish_corrupt)

            self.assertEqual(sha256_file(db), before_digest)
            assert_projection_bundle(self, root, projections)

    def test_database_restore_failure_is_rollback_incomplete_and_fail_closed(self) -> None:
        migrate = self.migration_api()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            create_schema30_source(root)
            write_projection_bundle(root)
            with patch.object(
                local_core_migration,
                "_restore_verified_backup",
                side_effect=SystemExit("forced database restore failure"),
            ):
                with self.assertRaisesRegex(
                    LocalCoreMigrationError,
                    "database rollback failed",
                ):
                    migrate(
                        root,
                        fail_at="after_atomic_replace",
                        active_validator=active_projection_validator(root),
                    )

            _, manifest = latest_manifest(root)
            sentinel = root / ".ai-team/state/local-core-migration.lock"
            self.assertEqual(manifest["status"], "rollback-incomplete")
            self.assertEqual(manifest["database_restore_status"], "failed")
            self.assertIn(
                "forced database restore failure",
                str(manifest["database_restore_error"]),
            )
            self.assertTrue(sentinel.is_file())
            with self.assertRaises(ProjectOperationLockError):
                with SqliteStore(root).connection():
                    self.fail("store opened during incomplete database rollback")

    def test_projection_restore_failure_never_reports_complete_rollback(self) -> None:
        migrate = self.migration_api()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = create_schema30_source(root)
            write_projection_bundle(root)
            with patch.object(
                local_core_migration,
                "_restore_projection_backup",
                side_effect=LocalCoreMigrationError("forced projection restore failure"),
            ):
                with self.assertRaisesRegex(
                    Exception,
                    "forced projection restore failure",
                ):
                    migrate(
                        root,
                        fail_at="after_atomic_replace",
                        active_validator=active_projection_validator(root),
                    )

            _, manifest = latest_manifest(root)
            with closing(sqlite3.connect(db)) as conn:
                version = conn.execute(
                    "select schema_version from project where id=1"
                ).fetchone()[0]
            self.assertEqual(version, 30)
            self.assertEqual(manifest["status"], "rollback-incomplete")
            self.assertEqual(manifest["database_restore_status"], "restored")
            self.assertEqual(manifest["projection_restore_status"], "failed")
            self.assertTrue(
                (root / ".ai-team/state/local-core-migration.lock").is_file()
            )

    def test_hard_exit_after_activation_leaves_recovery_required_sentinel(self) -> None:
        self.migration_api()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = create_schema30_source(root)
            projections = write_projection_bundle(root)
            context = multiprocessing.get_context("spawn")
            process = context.Process(
                target=hard_exit_after_schema31_activation,
                args=(str(root),),
            )
            process.start()
            process.join(30)
            if process.is_alive():
                process.terminate()
                process.join(5)
                self.fail("hard-exit schema31 migration child did not terminate")

            sentinel = root / ".ai-team/state/local-core-migration.lock"
            payload = json.loads(sentinel.read_text(encoding="utf-8"))
            with closing(sqlite3.connect(db)) as conn:
                active_version = conn.execute(
                    "select schema_version from project where id=1"
                ).fetchone()[0]

            self.assertEqual(process.exitcode, 17)
            self.assertEqual(active_version, 31)
            assert_projection_bundle(self, root, projections)
            self.assertEqual(payload["status"], "recovery-required")
            self.assertTrue(Path(str(payload["manifest_path"])).is_file())
            with self.assertRaisesRegex(
                ProjectOperationLockError,
                "recovery-required.*do not remove",
            ):
                with SqliteStore(root).connection():
                    self.fail("store opened a split-authority hard-exit migration")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "codex-project-harness"
SCRIPTS = PLUGIN_ROOT / "scripts"

for path in (PLUGIN_ROOT, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import harness_db  # noqa: E402
from core import local_core_migration  # noqa: E402
from core.event_bus import validate_audit_events  # noqa: E402
from core.schema_lifecycle import SchemaLifecycleError, backup_sqlite_database  # noqa: E402
from core.store import ProjectOperationLockError, SqliteStore  # noqa: E402
from core.local_core_migration import (  # noqa: E402
    InjectedLocalCoreMigrationFailure,
    LocalCoreMigrationError,
    migrate_project_to_schema30 as _core_migrate_project_to_schema30,
    stage_schema29_to_schema30,
    stage_supported_schema_to_schema30,
)
from core.projections import (  # noqa: E402
    PROJECTION_PATHS,
    PROJECTION_ROLLBACK_PATHS,
    render_executions,
)
from run_agent_e2e_eval import _create_schema27_fixture  # noqa: E402
from tests.legacy_schema_fixtures import create_schema28_fixture  # noqa: E402


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def init_schema29_fixture(root: Path) -> None:
    db = root / ".ai-team/state/harness.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("pragma foreign_keys = on")
        conn.execute("begin immediate")
        harness_db.create_schema29(conn)
        conn.execute(
            """
            insert into project
            (id, project_id, schema_version, runtime_version, phase, current_cycle_id,
             status, scope_status, current_owner, revision, updated_at)
            values (1, 'schema29-fixture', 29, '4.18.0', 'intake', 'CYCLE-current',
                    'draft', 'unconfirmed', 'project-manager', 1, 'now')
            """
        )
        harness_db.ensure_delivery_cycles(conn)
        conn.commit()


def _hard_exit_active_validator(_active_path: Path) -> None:
    os._exit(17)


def _active_projection_validator(root: Path):
    def validate(_active_path: Path) -> None:
        harness_db.render_all(root)
        issues = harness_db.doctor(root, require_project_files=False)
        if issues:
            raise LocalCoreMigrationError(
                "test projection activation validation failed: " + "; ".join(issues)
            )

    return validate


def migrate_project_to_schema30(root: Path, **kwargs):
    kwargs.setdefault("active_validator", _active_projection_validator(root))
    return _core_migrate_project_to_schema30(root, **kwargs)


def _run_hard_exit_after_schema30_activation(root_value: str) -> None:
    migrate_project_to_schema30(
        Path(root_value),
        active_validator=_hard_exit_active_validator,
    )


def add_retired_schema29_fixture_surface(conn: sqlite3.Connection) -> None:
    """Recreate only the retired columns/tables needed to test v1 -> v2 filtering."""
    conn.execute("alter table requirements add column tool_link text not null default ''")
    conn.execute("alter table acceptance add column tool_link text not null default ''")
    conn.execute("drop table tasks")
    conn.execute(
        """
        create table tasks (
            uid text primary key default (lower(hex(randomblob(16)))),
            id text not null,
            cycle_id text not null default '',
            task text not null,
            owner text not null,
            status text not null,
            evidence text not null default '',
            tool_link text not null default '',
            submitted_by text not null default '',
            submitted_session_id text not null default '',
            accepted_by text not null default '',
            accepted_session_id text not null default '',
            lease_agent text,
            lease_token text,
            lease_heartbeat_at text,
            lease_expires_at text,
            retry_count integer not null default 0,
            retry_budget integer not null default 2,
            fence integer not null default 0,
            revision integer not null default 1,
            updated_at text not null,
            unique(cycle_id, id)
        )
        """
    )
    conn.execute("alter table evidence add column trust_anchor text not null default ''")
    conn.execute("alter table evidence add column trust_anchor_id text not null default ''")
    conn.execute(
        """
        create table connector_profiles (
            id text primary key,
            tool text not null,
            project_key text not null,
            status text not null,
            scope_json text not null,
            created_at text not null,
            updated_at text not null
        )
        """
    )


class Schema30BackupTests(unittest.TestCase):
    def test_exception_text_preserves_manual_review_notes(self) -> None:
        error = LocalCoreMigrationError("unsafe-project-path: target.txt")
        error.add_note("complete metadata rollback requires manual review")

        self.assertEqual(
            local_core_migration._exception_text(error),
            "unsafe-project-path: target.txt\n"
            "NOTE: complete metadata rollback requires manual review",
        )

    def test_backup_records_safe_recovery_metadata_and_preserves_complete_database(self) -> None:
        secret = "connector-token-must-not-enter-manifest"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            init_schema29_fixture(root)
            source = root / ".ai-team/state/harness.db"
            with closing(sqlite3.connect(source)) as conn:
                conn.execute(
                    "insert into decisions (id, decision, reason, created_at) values ('D-secret', 'private', ?, 'now')",
                    (secret,),
                )
                conn.commit()
            markdown_before = {
                path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
                for path in root.rglob("*.md")
            }

            manifest = backup_sqlite_database(
                root,
                expected_source_version=29,
                created_at="2026-07-11T01:02:03Z",
            )
            backup = Path(manifest.backup_path)
            manifest_path = Path(manifest.manifest_path)
            payload_text = manifest_path.read_text(encoding="utf-8")
            payload = json.loads(payload_text)
            backup_digest = sha256(backup)
            backup_files = {path.name for path in backup.parent.iterdir()}
            markdown_after = {
                path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
                for path in root.rglob("*.md")
            }
            with closing(sqlite3.connect(backup)) as conn:
                restored_reason = conn.execute("select reason from decisions where id='D-secret'").fetchone()[0]
                backup_version = int(conn.execute("select schema_version from project where id=1").fetchone()[0])

        self.assertEqual(manifest.source_version, 29)
        self.assertEqual(manifest.target_version, 30)
        self.assertEqual(manifest.sha256, backup_digest)
        self.assertEqual(payload["sha256"], manifest.sha256)
        self.assertGreater(payload["row_counts"]["project"], 0)
        self.assertEqual(payload["source_integrity_check"], ["ok"])
        self.assertEqual(payload["backup_integrity_check"], ["ok"])
        self.assertEqual(payload["source_foreign_key_issue_count"], 0)
        self.assertEqual(payload["backup_foreign_key_issue_count"], 0)
        self.assertNotIn(secret, payload_text)
        self.assertEqual(markdown_after, markdown_before)
        self.assertEqual(backup_files, {"harness.db", "backup-manifest.json"})
        self.assertEqual(restored_reason, secret)
        self.assertEqual(backup_version, 29)

    def test_backup_rejects_schema_version_cas_mismatch_without_partial_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            init_schema29_fixture(root)
            with self.assertRaisesRegex(SchemaLifecycleError, "source schema changed before backup"):
                backup_sqlite_database(
                    root,
                    expected_source_version=28,
                    created_at="2026-07-11T01:02:03Z",
                )
            backups = list((root / ".ai-team/backups").glob("**/*")) if (root / ".ai-team/backups").exists() else []

        self.assertEqual(backups, [])


class Schema30StagingTests(unittest.TestCase):
    def test_schema29_native_submitted_context_survives_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            init_schema29_fixture(root)
            source = root / ".ai-team/state/harness.db"
            staging = root / ".ai-team/runtime/migration/schema30-context.db"
            with closing(sqlite3.connect(source)) as conn:
                conn.execute(
                    """
                    insert into tasks
                    (id, cycle_id, task, owner, status, submitted_context_id, updated_at)
                    values ('T-context', 'CYCLE-current', 'preserve producer context',
                            'developer', 'submitted', 'ctx-must-survive', 'now')
                    """
                )
                conn.commit()

            stage_schema29_to_schema30(source, staging)
            with closing(sqlite3.connect(staging)) as conn:
                task = conn.execute(
                    """
                    select status, submitted_context_id from tasks
                    where id='T-context' and cycle_id='CYCLE-current'
                    """
                ).fetchone()

        self.assertEqual(task, ("submitted", "ctx-must-survive"))

    def test_retained_legacy_event_gets_complete_compact_audit_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            init_schema29_fixture(root)
            source = root / ".ai-team/state/harness.db"
            with closing(sqlite3.connect(source)) as conn:
                conn.execute(
                    """
                    insert into events
                    (id, schema_version, type, source, target, correlation_id, payload_json, created_at)
                    values ('E-local', 29, 'requirement_recorded', 'runtime',
                            'requirement:R1', '', '{}', '2026-07-11T00:00:00Z')
                    """
                )
                conn.commit()
            staging = root / ".ai-team/runtime/migration/events.schema30.db"

            stage_schema29_to_schema30(source, staging)

            with closing(sqlite3.connect(staging)) as conn:
                conn.row_factory = sqlite3.Row
                event = conn.execute(
                    "select entity_type, entity_id, actor, command, correlation_id from events where id='E-local'"
                ).fetchone()
                issues = validate_audit_events(conn)

        self.assertEqual(event["entity_type"], "requirement")
        self.assertEqual(event["entity_id"], "R1")
        self.assertEqual(event["actor"], "schema-migration")
        self.assertEqual(event["command"], "import legacy local audit event")
        self.assertTrue(event["correlation_id"])
        self.assertEqual(issues, [])

    def test_legacy_failed_and_skipped_tasks_map_without_data_loss(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            init_schema29_fixture(root)
            source = root / ".ai-team/state/harness.db"
            staging = root / ".ai-team/runtime/migration/schema30-tasks.db"
            with closing(sqlite3.connect(source)) as conn:
                add_retired_schema29_fixture_surface(conn)
                conn.executemany(
                    """
                    insert into tasks (id, cycle_id, task, owner, status, updated_at)
                    values (?, 'CYCLE-current', ?, 'developer', ?, 'now')
                    """,
                    [
                        ("T-failed", "failed task", "failed"),
                        ("T-skipped", "skipped task", "skipped"),
                    ],
                )
                conn.commit()

            stage_schema29_to_schema30(source, staging)
            with closing(sqlite3.connect(staging)) as conn:
                rows = conn.execute("select id, status from tasks order by id").fetchall()

        self.assertEqual(rows, [("T-failed", "blocked"), ("T-skipped", "cancelled")])

    def test_schema29_local_facts_stage_side_by_side_without_external_rows(self) -> None:
        external_secret = "external-connector-secret-must-stay-in-schema29"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            init_schema29_fixture(root)
            source = root / ".ai-team/state/harness.db"
            with closing(sqlite3.connect(source)) as conn:
                add_retired_schema29_fixture_surface(conn)
                conn.execute(
                    """
                    insert into requirements
                    (id, cycle_id, kind, body, priority, status, tool_link, updated_at)
                    values ('R1', 'CYCLE-current', 'functional', 'local requirement', 'must', 'active', ?, 'now')
                    """,
                    (external_secret,),
                )
                conn.execute(
                    """
                    insert into acceptance
                    (id, cycle_id, criterion, priority, tool_link, status)
                    values ('AC1', 'CYCLE-current', 'local acceptance', 'must', ?, 'active')
                    """,
                    (external_secret,),
                )
                conn.execute(
                    "insert into requirement_acceptance (cycle_id, requirement_id, acceptance_id) values ('CYCLE-current', 'R1', 'AC1')"
                )
                conn.execute(
                    """
                    insert into failure_modes
                    (id, cycle_id, feature, scenario, trigger, expected_behavior, risk, status)
                    values ('FM1', 'CYCLE-current', 'migration', 'copy', 'upgrade', 'preserve', 'high', 'active')
                    """
                )
                conn.execute(
                    "insert into failure_mode_acceptance (cycle_id, failure_mode_id, acceptance_id) values ('CYCLE-current', 'FM1', 'AC1')"
                )
                conn.execute(
                    """
                    insert into agent_sessions
                    (session_id, agent_id, role, context_id, status, started_at)
                    values ('S-producer', 'developer', 'developer', 'ctx-producer', 'closed', 'now')
                    """
                )
                conn.execute(
                    """
                    insert into tasks
                    (id, cycle_id, task, owner, status, evidence, tool_link, submitted_session_id, accepted_by, updated_at)
                    values ('T1', 'CYCLE-current', 'local task', 'developer', 'review', 'implemented', ?, 'S-producer', 'qa-reviewer', 'now')
                    """,
                    (external_secret,),
                )
                conn.execute(
                    "insert into task_acceptance (cycle_id, task_id, acceptance_id) values ('CYCLE-current', 'T1', 'AC1')"
                )
                conn.execute(
                    "insert into task_failure_modes (cycle_id, task_id, failure_mode_id) values ('CYCLE-current', 'T1', 'FM1')"
                )
                conn.execute(
                    """
                    insert into test_targets
                    (id, kind, command_template, description, created_at, updated_at)
                    values ('UNIT', 'unit', 'python3 -m unittest', 'local target', 'now', 'now')
                    """
                )
                conn.execute(
                    "insert into task_test_targets (cycle_id, task_id, target_id) values ('CYCLE-current', 'T1', 'UNIT')"
                )
                snapshot = json.dumps(
                    {"requirements": [{"id": "R1", "body": "local requirement", "tool_link": external_secret}]},
                    sort_keys=True,
                )
                conn.execute(
                    """
                    insert into baselines
                    (id, summary, snapshot_json, digest, project_revision, created_by, created_at)
                    values ('B1', 'local baseline', ?, 'legacy-digest', 1, 'project-manager', 'now')
                    """,
                    (snapshot,),
                )
                conn.execute(
                    """
                    insert into connector_profiles
                    (id, tool, project_key, status, scope_json, created_at, updated_at)
                    values ('CP1', 'github', 'local-project', 'active', ?, 'now', 'now')
                    """,
                    (json.dumps({"token": external_secret}),),
                )
                conn.execute(
                    """
                    insert into events
                    (id, schema_version, type, source, target, payload_json, created_at)
                    values ('E-external', 29, 'connector_profile_set', 'connector', 'project', ?, 'now')
                    """,
                    (json.dumps({"token": external_secret}),),
                )
                conn.commit()

            source_digest_before = sha256(source)
            staging = root / ".ai-team/backups/test/harness.schema30.new.db"
            report = stage_schema29_to_schema30(source, staging)
            source_digest_after = sha256(source)
            staging_bytes = staging.read_bytes()
            with closing(sqlite3.connect(f"file:{staging.as_posix()}?mode=ro", uri=True)) as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "select name from sqlite_master where type='table' and name not like 'sqlite_%'"
                    )
                }
                project = conn.execute("select schema_version, runtime_version from project where id=1").fetchone()
                requirement = conn.execute("select body from requirements where id='R1'").fetchone()[0]
                requirement_columns = {row[1] for row in conn.execute("pragma table_info(requirements)")}
                task = conn.execute(
                    "select status, submitted_context_id from tasks where id='T1'"
                ).fetchone()
                task_columns = {row[1] for row in conn.execute("pragma table_info(tasks)")}
                baseline = json.loads(conn.execute("select snapshot_json from baselines where id='B1'").fetchone()[0])
                external_event_count = int(
                    conn.execute("select count(*) from events where event_type='connector_profile_set'").fetchone()[0]
                )
                foreign_key_issues = conn.execute("pragma foreign_key_check").fetchall()

        self.assertEqual(source_digest_after, source_digest_before)
        self.assertEqual(report.source_version, 29)
        self.assertEqual(report.target_version, 30)
        self.assertEqual(len(tables), 27)
        self.assertEqual(project, (30, "5.0.0"))
        self.assertEqual(requirement, "local requirement")
        self.assertNotIn("tool_link", requirement_columns)
        self.assertEqual(task, ("submitted", "ctx-producer"))
        self.assertFalse({"lease_token", "lease_agent", "fence"} & task_columns)
        self.assertNotIn("tool_link", baseline["requirements"][0])
        self.assertNotIn(external_secret.encode("utf-8"), staging_bytes)
        self.assertNotIn("connector_profiles", tables)
        self.assertGreaterEqual(report.retired_row_counts["connector_profiles"], 1)
        self.assertGreaterEqual(report.dropped_event_count, 1)
        self.assertEqual(external_event_count, 0)
        self.assertEqual(foreign_key_issues, [])

    def test_schema29_degraded_review_is_not_promoted_by_distinct_session_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            init_schema29_fixture(root)
            source = root / ".ai-team/state/harness.db"
            with closing(sqlite3.connect(source)) as conn:
                add_retired_schema29_fixture_surface(conn)
                conn.executemany(
                    """
                    insert into agent_sessions
                    (session_id, agent_id, role, context_id, status, started_at)
                    values (?, ?, ?, ?, 'closed', 'now')
                    """,
                    (
                        ("S-producer", "developer", "developer", "ctx-producer"),
                        ("S-reviewer", "qa", "qa", "ctx-reviewer"),
                    ),
                )
                conn.execute(
                    """
                    insert into tasks
                    (id, cycle_id, task, owner, status, submitted_session_id, updated_at)
                    values ('T1', 'CYCLE-current', 'candidate', 'developer', 'review',
                            'S-producer', 'now')
                    """
                )
                conn.executemany(
                    """
                    insert into quality_gates
                    (id, sequence, cycle_id, candidate_sha, gate_status, gate,
                     reviewed_commit, reviewer_context, result, project_revision,
                     reviewer_session_id, created_at)
                    values (?, ?, 'CYCLE-current', 'candidate', 'active',
                            'independent_qa', 'candidate', ?, 'pass', 1,
                            'S-reviewer', 'now')
                    """,
                    (
                        ("G-degraded", 1, "same-context-degraded"),
                        ("G-fresh", 2, "fresh"),
                        ("G-spaced-fresh", 3, " fresh "),
                    ),
                )
                conn.commit()

            staging = root / ".ai-team/backups/test/harness.schema30.new.db"
            stage_schema29_to_schema30(source, staging)
            with closing(sqlite3.connect(staging)) as conn:
                gates = conn.execute(
                    """
                    select id, producer_context_id, reviewer_context_id, review_status
                    from quality_gates order by sequence
                    """
                ).fetchall()

        self.assertEqual(
            gates,
            [
                ("G-degraded", "ctx-producer", "ctx-reviewer", "same-context-degraded"),
                ("G-fresh", "ctx-producer", "ctx-reviewer", "reviewed-local"),
                (
                    "G-spaced-fresh",
                    "ctx-producer",
                    "ctx-reviewer",
                    "same-context-degraded",
                ),
            ],
        )

    def test_schema29_staging_rejects_non_positive_or_non_integer_trust_revisions(self) -> None:
        cases = (
            ("project", 1.9),
            ("project", "not-an-integer"),
            ("project", 0),
            ("project", -1),
            ("quality-gate", 1.9),
            ("quality-gate", "not-an-integer"),
            ("quality-gate", 0),
            ("quality-gate", -1),
        )
        for surface, revision in cases:
            with self.subTest(surface=surface, revision=revision), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                init_schema29_fixture(root)
                source = root / ".ai-team/state/harness.db"
                with closing(sqlite3.connect(source)) as conn:
                    if surface == "project":
                        conn.execute("update project set revision = ? where id = 1", (revision,))
                    else:
                        conn.execute(
                            """
                            insert into quality_gates
                            (id, sequence, cycle_id, candidate_sha, gate_status, gate,
                             reviewed_commit, reviewer_context, result, project_revision,
                             created_at)
                            values ('G-invalid-revision', 1, 'CYCLE-current', 'candidate',
                                    'active', 'independent_qa', 'candidate',
                                    'same-context-degraded', 'pass', ?, 'now')
                            """,
                            (revision,),
                        )
                    conn.commit()

                staging = root / ".ai-team/backups/test/harness.schema30.new.db"
                with self.assertRaisesRegex(
                    LocalCoreMigrationError,
                    "positive SQLite integer",
                ):
                    stage_schema29_to_schema30(source, staging)
                self.assertFalse(staging.exists())

    def test_empty_session_contexts_never_become_reviewed_local(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            init_schema29_fixture(root)
            source = root / ".ai-team/state/harness.db"
            with closing(sqlite3.connect(source)) as conn:
                add_retired_schema29_fixture_surface(conn)
                conn.executemany(
                    """
                    insert into agent_sessions
                    (session_id, agent_id, role, context_id, status, started_at)
                    values (?, ?, ?, '', 'closed', 'now')
                    """,
                    (
                        ("S-empty-producer", "developer", "developer"),
                        ("S-empty-reviewer", "qa", "qa"),
                    ),
                )
                conn.execute(
                    """
                    insert into tasks
                    (id, cycle_id, task, owner, status, submitted_session_id, updated_at)
                    values ('T-empty-context', 'CYCLE-current', 'candidate', 'developer',
                            'review', 'S-empty-producer', 'now')
                    """
                )
                conn.execute(
                    """
                    insert into quality_gates
                    (id, sequence, cycle_id, candidate_sha, gate_status, gate,
                     reviewed_commit, reviewer_context, result, project_revision,
                     reviewer_session_id, created_at)
                    values ('G-empty-context', 1, 'CYCLE-current', 'candidate', 'active',
                            'independent_qa', 'candidate', 'fresh', 'pass', 1,
                            'S-empty-reviewer', 'now')
                    """
                )
                conn.commit()

            staging = root / ".ai-team/backups/test/harness.schema30.new.db"
            stage_schema29_to_schema30(source, staging)
            with closing(sqlite3.connect(staging)) as conn:
                task_context = conn.execute(
                    "select submitted_context_id from tasks where id='T-empty-context'"
                ).fetchone()[0]
                gate = conn.execute(
                    """
                    select producer_context_id, reviewer_context_id, review_status
                    from quality_gates where id='G-empty-context'
                    """
                ).fetchone()

        self.assertEqual(task_context, "")
        self.assertEqual(gate, ("", "", "same-context-degraded"))

    def test_invalidated_validations_preserve_valid_supersession_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            init_schema29_fixture(root)
            source = root / ".ai-team/state/harness.db"
            with closing(sqlite3.connect(source)) as conn:
                conn.executemany(
                    """
                    insert into validations
                    (id, cycle_id, candidate_sha, validation_status, superseded_by,
                     surface, findings, result, residual_risk, created_at)
                    values (?, 'CYCLE-current', 'candidate', ?, ?, 'unit', '', 'pass', '', ?)
                    """,
                    (
                        ("V-new", "active", "", "2026-07-11T00:00:01Z"),
                        ("V-old", "superseded", "V-new", "2026-07-11T00:00:00Z"),
                    ),
                )
                conn.commit()

            staging = root / ".ai-team/backups/test/harness.schema30.new.db"
            stage_schema29_to_schema30(source, staging)
            with closing(sqlite3.connect(staging)) as conn:
                validations = conn.execute(
                    "select id, validation_status, superseded_by from validations order by id"
                ).fetchall()

        self.assertEqual(
            validations,
            [
                ("V-new", "invalidated", None),
                ("V-old", "invalidated", "V-new"),
            ],
        )

    def test_only_controller_artifacts_become_immutable_executions(self) -> None:
        candidate_sha = "a" * 64
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            init_schema29_fixture(root)
            artifact = root / ".ai-team/runtime/executions/eligible/stdout.txt"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text("Ran 1 test in 0.001s\nOK\n", encoding="utf-8")
            artifact_sha = sha256(artifact)
            source = root / ".ai-team/state/harness.db"
            with closing(sqlite3.connect(source)) as conn:
                add_retired_schema29_fixture_surface(conn)
                conn.execute(
                    "insert into acceptance (id, cycle_id, criterion, status) values ('AC1', 'CYCLE-current', 'verified', 'active')"
                )
                conn.execute(
                    """
                    insert into failure_modes
                    (id, cycle_id, feature, scenario, trigger, expected_behavior, risk, status)
                    values ('FM1', 'CYCLE-current', 'migration', 'evidence', 'upgrade', 'preserve', 'medium', 'active')
                    """
                )
                conn.execute(
                    """
                    insert into test_targets
                    (id, kind, command_template, description, gateable, result_format, created_at, updated_at)
                    values ('UNIT', 'unit', 'python3 -m unittest', 'eligible', 1, 'regex', 'now', 'now')
                    """
                )
                conn.execute(
                    """
                    insert into evidence
                    (id, kind, summary, command, exit_code, stdout_sha256, artifact_path, source_tree_hash,
                     target_id, executed_count, executed_count_source, result_format, semantic_status,
                     policy_status, sandbox_profile, sandbox_status, verified_by, trust_anchor, created_at)
                    values ('EV-controller', 'command', 'controller', 'python3 -m unittest', 0, ?, ?, ?,
                            'UNIT', 1, 'parsed', 'regex', '', 'allowed', 'none', '', 'controller-local',
                            'local-only', 'now')
                    """,
                    (artifact_sha, artifact.relative_to(root).as_posix(), candidate_sha),
                )
                conn.execute(
                    """
                    insert into evidence
                    (id, kind, summary, command, exit_code, stdout_sha256, artifact_path, source_tree_hash,
                     target_id, executed_count, executed_count_source, result_format, semantic_status,
                     policy_status, sandbox_profile, sandbox_status, verified_by, trust_anchor, trust_anchor_id, created_at)
                    values ('EV-manual', 'command', 'forged manual', 'python3 -m unittest', 0, ?, ?, ?,
                            'UNIT', 1, 'manual', 'regex', 'pass', 'manual', 'none', '', '',
                            'external-session', 'looks-like-hmac', 'now')
                    """,
                    (artifact_sha, artifact.relative_to(root).as_posix(), candidate_sha),
                )
                for validation_id in ("V-controller", "V-manual", "V-unbound"):
                    conn.execute(
                        """
                        insert into validations
                        (id, cycle_id, candidate_sha, validation_status, surface, acceptance_id,
                         findings, result, residual_risk, source_tree_hash, created_at)
                        values (?, 'CYCLE-current', ?, 'active', 'migration', 'AC1', 'legacy judgment',
                                'pass', '', ?, 'now')
                        """,
                        (validation_id, candidate_sha, candidate_sha),
                    )
                conn.execute(
                    "insert into validation_evidence (validation_id, evidence_id) values ('V-controller', 'EV-controller')"
                )
                conn.execute(
                    "insert into validation_evidence (validation_id, evidence_id) values ('V-manual', 'EV-manual')"
                )
                conn.execute(
                    """
                    insert into validation_failure_modes (validation_id, cycle_id, failure_mode_id)
                    values ('V-controller', 'CYCLE-current', 'FM1')
                    """
                )
                conn.commit()

            staging = root / ".ai-team/backups/test/harness.schema30.new.db"
            report = stage_schema29_to_schema30(source, staging)
            with closing(sqlite3.connect(staging)) as conn:
                execution = conn.execute(
                    "select candidate_sha, target_id, command, exit_code, stdout_sha256, executed_count, semantic_status, runner, sandbox_status from executions"
                ).fetchone()
                validation_rows = conn.execute(
                    "select id, validation_status from validations order by id"
                ).fetchall()
                links = conn.execute(
                    "select v.id, ve.execution_id from validations v join validation_executions ve on ve.validation_id=v.id"
                ).fetchall()
                failure_links = conn.execute(
                    "select validation_id, failure_mode_id from validation_failure_modes"
                ).fetchall()
                invalidated = {
                    row[0]
                    for row in conn.execute(
                        "select target_id from invalidations where target_type='validation'"
                    )
                }
                tables = {row[0] for row in conn.execute("select name from sqlite_master where type='table'")}
                with self.assertRaises(sqlite3.DatabaseError):
                    conn.execute("update executions set command='false'")

        self.assertEqual(
            execution,
            (
                candidate_sha,
                "UNIT",
                "python3 -m unittest",
                0,
                artifact_sha,
                1,
                "pass",
                "local",
                "",
            ),
        )
        self.assertEqual(
            validation_rows,
            [("V-controller", "active"), ("V-manual", "invalidated"), ("V-unbound", "invalidated")],
        )
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0][0], "V-controller")
        self.assertEqual(failure_links, [("V-controller", "FM1")])
        self.assertEqual(invalidated, {"V-manual", "V-unbound"})
        self.assertNotIn("evidence", tables)
        self.assertNotIn("tests", tables)
        self.assertEqual(report.converted_execution_count, 1)
        self.assertEqual(report.converted_validation_count, 1)
        self.assertEqual(report.invalidated_validation_count, 2)

    def test_fractional_execution_metadata_cannot_become_immutable_execution(self) -> None:
        cases = (
            ("gateable", 1.9),
            ("requires_sandbox", 1.9),
            ("requires_no_network", 1.9),
            ("exit_code", 0.9),
            ("executed_count", 1.9),
            ("no_network", 1.9),
        )
        for field, invalid_value in cases:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                init_schema29_fixture(root)
                artifact = root / ".ai-team/runtime/executions/fractional/stdout.txt"
                artifact.parent.mkdir(parents=True, exist_ok=True)
                artifact.write_text("Ran 1 test in 0.001s\nOK\n", encoding="utf-8")
                candidate_sha = "a" * 64
                target_values = {
                    "gateable": 1,
                    "requires_sandbox": 0,
                    "requires_no_network": 0,
                }
                evidence_values = {
                    "exit_code": 0,
                    "executed_count": 1,
                    "no_network": 1,
                }
                if field in target_values:
                    target_values[field] = invalid_value
                else:
                    evidence_values[field] = invalid_value

                source = root / ".ai-team/state/harness.db"
                with closing(sqlite3.connect(source)) as conn:
                    conn.execute(
                        """
                        insert into test_targets
                        (id, kind, command_template, description, gateable,
                         requires_sandbox, requires_no_network, result_format,
                         created_at, updated_at)
                        values ('UNIT-fractional', 'unit', 'python3 -m unittest',
                                'fractional metadata must not be trusted', ?, ?, ?,
                                'regex', 'now', 'now')
                        """,
                        (
                            target_values["gateable"],
                            target_values["requires_sandbox"],
                            target_values["requires_no_network"],
                        ),
                    )
                    conn.execute(
                        """
                        insert into evidence
                        (id, kind, summary, command, exit_code, stdout_sha256,
                         artifact_path, source_tree_hash, target_id, executed_count,
                         executed_count_source, result_format, semantic_status,
                         no_network, sandbox_status, policy_status, verified_by,
                         created_at)
                        values ('EV-fractional', 'command', 'fractional metadata',
                                'python3 -m unittest', ?, ?, ?, ?, 'UNIT-fractional',
                                ?, 'parsed', 'regex', 'pass', ?, 'available',
                                'allowed', 'controller-local', 'now')
                        """,
                        (
                            evidence_values["exit_code"],
                            sha256(artifact),
                            artifact.relative_to(root).as_posix(),
                            candidate_sha,
                            evidence_values["executed_count"],
                            evidence_values["no_network"],
                        ),
                    )
                    conn.execute(
                        """
                        insert into validations
                        (id, cycle_id, candidate_sha, validation_status, surface,
                         findings, result, residual_risk, source_tree_hash, created_at)
                        values ('V-fractional', 'CYCLE-current', ?, 'active',
                                'migration', '', 'pass', '', ?, 'now')
                        """,
                        (candidate_sha, candidate_sha),
                    )
                    conn.execute(
                        """
                        insert into validation_evidence (validation_id, evidence_id)
                        values ('V-fractional', 'EV-fractional')
                        """
                    )
                    conn.commit()

                staging = root / ".ai-team/backups/test/harness.schema30.new.db"
                if field in target_values:
                    with self.assertRaisesRegex(
                        LocalCoreMigrationError,
                        "SQLite flag",
                    ):
                        stage_schema29_to_schema30(source, staging)
                    self.assertFalse(staging.exists())
                    continue

                report = stage_schema29_to_schema30(source, staging)
                with closing(sqlite3.connect(staging)) as conn:
                    execution_count = conn.execute(
                        "select count(*) from executions"
                    ).fetchone()[0]
                    validation_status = conn.execute(
                        "select validation_status from validations where id='V-fractional'"
                    ).fetchone()[0]

                self.assertEqual(execution_count, 0)
                self.assertEqual(validation_status, "invalidated")
                self.assertEqual(report.converted_execution_count, 0)
                self.assertEqual(report.invalidated_validation_count, 1)


class Schema30LegacyStagingTests(unittest.TestCase):
    def _assert_legacy_fixture_migrates(self, source_version: int) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (_create_schema27_fixture if source_version == 27 else create_schema28_fixture)(root)
            source = root / ".ai-team/state/harness.db"
            with closing(sqlite3.connect(source)) as conn:
                source_inventory = (
                    conn.execute("select count(*) from sqlite_master where type='table' and name not like 'sqlite_%'").fetchone()[0],
                    conn.execute("select count(*) from sqlite_master where type='index'").fetchone()[0],
                )
            source_digest_before = sha256(source)
            staging = root / ".ai-team/backups/test/harness.schema30.new.db"

            report = stage_supported_schema_to_schema30(source, staging)

            source_digest_after = sha256(source)
            with closing(sqlite3.connect(staging)) as conn:
                project = conn.execute(
                    "select schema_version, runtime_version, current_cycle_id from project where id=1"
                ).fetchone()
                requirements = conn.execute(
                    "select cycle_id, id, body from requirements order by cycle_id, id"
                ).fetchall()
                tasks = conn.execute(
                    "select cycle_id, id, status from tasks order by cycle_id, id"
                ).fetchall()
                gates = conn.execute(
                    "select id, sequence, result, review_status from quality_gates order by sequence"
                ).fetchall()
                migrations = conn.execute(
                    "select from_version, to_version, status from migrations order by id"
                ).fetchall()
                tables = {
                    row[0]
                    for row in conn.execute(
                        "select name from sqlite_master where type='table' and name not like 'sqlite_%'"
                    )
                }
                foreign_key_issues = conn.execute("pragma foreign_key_check").fetchall()
                invalidations = conn.execute(
                    "select source_type, target_type from invalidations order by id"
                ).fetchall()

        self.assertEqual(source_digest_after, source_digest_before)
        self.assertEqual(report.source_version, source_version)
        self.assertEqual(report.target_version, 30)
        self.assertEqual(project, (30, "5.0.0", "CYCLE-current"))
        self.assertEqual(requirements, [("CYCLE-current", "R1", "Preserve requirement")])
        expected_tasks = [("CYCLE-current", "T0", "accepted"), ("CYCLE-current", "T1", "submitted")] if source_version == 27 else [("CYCLE-current", "T1", "planned")]
        expected_gates = [("G1", "pass")] if source_version == 27 else [("z-old-pass", "pass"), ("a-new-fail", "fail")]
        self.assertEqual(tasks, expected_tasks)
        self.assertEqual([(row[0], row[2]) for row in gates], expected_gates)
        self.assertEqual([row[1] for row in gates], list(range(1, len(gates) + 1)))
        self.assertTrue(all(row[3] in {"reviewed-local", "same-context-degraded"} for row in gates))
        self.assertEqual(source_inventory, (53, 60) if source_version == 27 else (17, 17))
        self.assertIn((source_version, 29, "legacy-history"), migrations)
        self.assertIn((29, 30, "staged"), migrations)
        self.assertEqual(len(tables), 27)
        self.assertNotIn("session_attestations", tables)
        self.assertNotIn("ci_verifications", tables)
        self.assertNotIn("external_session_verifications", tables)
        self.assertEqual(foreign_key_issues, [])
        self.assertTrue(
            all(
                source_type in {"requirement", "acceptance", "failure_mode"}
                and target_type in {"acceptance", "task", "validation", "quality_gate"}
                for source_type, target_type in invalidations
            )
        )
        if source_version == 27:
            self.assertEqual(
                (report.converted_execution_count, report.converted_validation_count),
                (1, 1),
            )
            self.assertEqual(report.retired_row_counts["adapter_actions"], 1)

    def test_published_schema27_uses_isolated_legacy_stage(self) -> None:
        self._assert_legacy_fixture_migrates(27)

    def test_development_schema28_uses_isolated_legacy_stage(self) -> None:
        self._assert_legacy_fixture_migrates(28)

    def test_legacy_staging_rejects_non_positive_or_non_integer_trust_revisions(
        self,
    ) -> None:
        invalid_revisions = (1.9, "not-an-integer", 0, -1)
        for source_version, fixture in (
            (27, _create_schema27_fixture),
            (28, create_schema28_fixture),
        ):
            for surface in ("project", "quality-gate"):
                for revision in invalid_revisions:
                    with (
                        self.subTest(
                            source_version=source_version,
                            surface=surface,
                            revision=revision,
                        ),
                        tempfile.TemporaryDirectory() as temp,
                    ):
                        root = Path(temp)
                        fixture(root)
                        source = root / ".ai-team/state/harness.db"
                        with closing(sqlite3.connect(source)) as conn:
                            if surface == "project":
                                conn.execute(
                                    "update project set revision = ? where id = 1",
                                    (revision,),
                                )
                            else:
                                updated = conn.execute(
                                    "update quality_gates set project_revision = ?",
                                    (revision,),
                                )
                                self.assertGreater(updated.rowcount, 0)
                            conn.commit()

                        staging = root / ".ai-team/backups/test/harness.schema30.new.db"
                        with self.assertRaisesRegex(
                            LocalCoreMigrationError,
                            "positive SQLite integer",
                        ):
                            stage_supported_schema_to_schema30(source, staging)
                        self.assertFalse(staging.exists())

    def test_legacy_session_ids_do_not_replace_shared_context_identity(self) -> None:
        for source_version, fixture in (
            (27, _create_schema27_fixture),
            (28, create_schema28_fixture),
        ):
            with self.subTest(source_version=source_version), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                fixture(root)
                source = root / ".ai-team/state/harness.db"
                with closing(sqlite3.connect(source)) as conn:
                    conn.executemany(
                        """
                        insert into agent_sessions
                        (session_id, agent_id, role, context_id, origin, trust_level,
                         status, started_at)
                        values (?, ?, ?, 'ctx-shared', 'manual', 'local-only', 'closed', 'now')
                        """,
                        (
                            ("S-context-producer", "developer", "developer"),
                            ("S-context-reviewer", "qa", "qa"),
                        ),
                    )
                    conn.execute(
                        """
                        insert into tasks
                        (id, cycle_id, task, owner, status, submitted_session_id, updated_at)
                        values ('T-context', 'CYCLE-current', 'candidate', 'developer',
                                'review', 'S-context-producer', 'now')
                        """
                    )
                    conn.execute(
                        """
                        insert into quality_gates
                        (id, cycle_id, candidate_sha, gate, reviewed_commit,
                         reviewer_context, result, project_revision,
                         reviewer_session_id, created_at)
                        values ('G-context', 'CYCLE-current', 'candidate', 'independent_qa',
                                'candidate', 'fresh', 'pass', 1,
                                'S-context-reviewer', 'now')
                        """
                    )
                    conn.commit()

                staging = root / ".ai-team/backups/test/harness.schema30.new.db"
                stage_supported_schema_to_schema30(source, staging)
                with closing(sqlite3.connect(staging)) as conn:
                    task_context = conn.execute(
                        "select submitted_context_id from tasks where id='T-context'"
                    ).fetchone()[0]
                    gate = conn.execute(
                        """
                        select producer_context_id, reviewer_context_id, review_status
                        from quality_gates where id='G-context'
                        """
                    ).fetchone()

                self.assertEqual(task_context, "ctx-shared")
                self.assertEqual(
                    gate,
                    ("ctx-shared", "ctx-shared", "same-context-degraded"),
                )


class ProjectionPathContractTests(unittest.TestCase):
    def test_projection_and_rollback_path_inventories_are_exact_and_unique(self) -> None:
        expected = (
            Path(".ai-team/control/project-state.yaml"),
            Path(".ai-team/requirements/requirements.md"),
            Path(".ai-team/requirements/traceability.md"),
            Path(".ai-team/requirements/acceptance.md"),
            Path(".ai-team/requirements/failure-modes.md"),
            Path(".ai-team/planning/task-board.md"),
            Path(".ai-team/control/test-targets.md"),
            Path("docs/harness/validation.md"),
            Path("docs/harness/executions.md"),
            Path("docs/harness/findings.md"),
            Path("docs/harness/quality-gates.md"),
            Path("docs/harness/delivery.md"),
            Path(".ai-team/control/decision-log.md"),
        )
        self.assertEqual(PROJECTION_PATHS, expected)
        self.assertEqual(len(PROJECTION_PATHS), len(set(PROJECTION_PATHS)))
        self.assertEqual(
            PROJECTION_ROLLBACK_PATHS,
            (*expected, Path("docs/harness/evidence.md")),
        )
        self.assertEqual(len(PROJECTION_ROLLBACK_PATHS), len(set(PROJECTION_ROLLBACK_PATHS)))

    def test_windows_safe_file_mode_uses_pinned_attributes_without_path_stat(self) -> None:
        from core.project_fs import _PathIdentity, _PathSnapshot

        relative = Path("docs/harness/executions.md")

        class PinnedAttributesFS:
            def __init__(self, snapshot):
                self.snapshot = snapshot

            def _snapshot(self, requested, *, allow_missing):
                self.requested = (requested, allow_missing)
                return self.snapshot

            def absolute(self, _requested):
                raise AssertionError("pathname stat must not authorize Windows mode")

        for attributes, expected_mode in ((0x00000001, 0o444), (0x00000080, 0o666)):
            with self.subTest(attributes=attributes):
                snapshot = _PathSnapshot(
                    True,
                    _PathIdentity(
                        volume=7,
                        file_id=b"projection",
                        kind="file",
                        mode_or_attributes=attributes,
                        nlink=1,
                    ),
                )
                project_fs = PinnedAttributesFS(snapshot)
                with patch.object(local_core_migration.os, "name", "nt"):
                    actual_mode = local_core_migration._safe_file_mode(
                        project_fs,
                        relative,
                        expected=snapshot,
                    )
                self.assertEqual(actual_mode, expected_mode)
                self.assertEqual(project_fs.requested, (relative, False))


class Schema30ActivationRollbackTests(unittest.TestCase):
    def _prepare_source(self, root: Path) -> Path:
        init_schema29_fixture(root)
        source = root / ".ai-team/state/harness.db"
        with closing(sqlite3.connect(source)) as conn:
            conn.execute(
                "insert into decisions (id, decision, reason, created_at) values ('D-sentinel', 'keep', 'rollback sentinel', 'now')"
            )
            conn.commit()
        return source

    def test_manifest_rewrite_race_uses_fallback_without_overwriting_replacement(
        self,
    ) -> None:
        from core.project_fs import ProjectFS

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._prepare_source(root)
            original_atomic_write = ProjectFS.atomic_write
            raced = False
            attacker_payload = b'{"attacker":"must-remain"}\n'
            raced_manifest: Path | None = None
            parked_manifest: Path | None = None

            def race_manifest_then_write(
                active_fs,
                relative,
                data,
                *,
                mode=0o600,
                expected_destination=None,
            ):
                nonlocal raced, raced_manifest, parked_manifest
                relative = Path(relative)
                if (
                    relative.name == "migration-manifest.json"
                    and expected_destination is not None
                    and not raced
                ):
                    raced_manifest = active_fs.absolute(relative)
                    parked_manifest = raced_manifest.with_name(
                        "migration-manifest.before-race.json"
                    )
                    replacement = raced_manifest.with_name(
                        "migration-manifest.attacker.json"
                    )
                    replacement.write_bytes(attacker_payload)
                    raced_manifest.rename(parked_manifest)
                    replacement.rename(raced_manifest)
                    raced = True
                return original_atomic_write(
                    active_fs,
                    relative,
                    data,
                    mode=mode,
                    expected_destination=expected_destination,
                )

            with patch.object(
                ProjectFS,
                "atomic_write",
                autospec=True,
                side_effect=race_manifest_then_write,
            ):
                with self.assertRaisesRegex(
                    Exception,
                    "path-identity-changed",
                ):
                    migrate_project_to_schema30(root)

            self.assertTrue(raced)
            assert raced_manifest is not None
            assert parked_manifest is not None
            self.assertEqual(raced_manifest.read_bytes(), attacker_payload)
            self.assertTrue(parked_manifest.is_file())
            fallback_manifests = tuple(
                (root / ".ai-team/state").glob(
                    "migration-recovery-*.json"
                )
            )
            self.assertEqual(len(fallback_manifests), 1)
            fallback = json.loads(
                fallback_manifests[0].read_text(encoding="utf-8")
            )
            self.assertEqual(fallback["status"], "rollback-incomplete")
            self.assertEqual(
                fallback["failed_manifest_path"],
                str(raced_manifest),
            )
            with closing(sqlite3.connect(active)) as conn:
                self.assertEqual(
                    conn.execute(
                        "select schema_version from project where id=1"
                    ).fetchone()[0],
                    29,
                )

    def test_final_manifest_receipt_is_held_until_success_cleanup(self) -> None:
        from core.project_fs import ProjectFS

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._prepare_source(root)
            original_atomic_write = ProjectFS.atomic_write
            attacker_payload = b'{"attacker":"must-remain"}\n'
            raced = False
            raced_manifest: Path | None = None
            parked_manifest: Path | None = None

            def replace_final_manifest_after_write(
                active_fs,
                relative,
                data,
                *,
                mode=0o600,
                expected_destination=None,
            ):
                nonlocal raced, raced_manifest, parked_manifest
                written = original_atomic_write(
                    active_fs,
                    relative,
                    data,
                    mode=mode,
                    expected_destination=expected_destination,
                )
                if (
                    Path(relative).name == "migration-manifest.json"
                    and b'"status": "activated"' in bytes(data)
                    and not raced
                ):
                    raced_manifest = active_fs.absolute(Path(relative))
                    parked_manifest = raced_manifest.with_name(
                        "migration-manifest.verified-before-final-race.json"
                    )
                    replacement = raced_manifest.with_name(
                        "migration-manifest.final-attacker.json"
                    )
                    replacement.write_bytes(attacker_payload)
                    raced_manifest.rename(parked_manifest)
                    replacement.rename(raced_manifest)
                    raced = True
                return written

            with patch.object(
                ProjectFS,
                "atomic_write",
                autospec=True,
                side_effect=replace_final_manifest_after_write,
            ):
                with self.assertRaisesRegex(Exception, "path-identity-changed"):
                    migrate_project_to_schema30(root)

            self.assertTrue(raced)
            assert raced_manifest is not None
            assert parked_manifest is not None
            self.assertEqual(raced_manifest.read_bytes(), attacker_payload)
            self.assertTrue(parked_manifest.is_file())
            self.assertTrue(
                (root / ".ai-team/state/local-core-migration.lock").is_file()
            )
            with closing(sqlite3.connect(active)) as conn:
                self.assertEqual(
                    conn.execute(
                        "select schema_version from project where id=1"
                    ).fetchone()[0],
                    29,
                )

    def test_success_projection_receipts_survive_final_manifest_write(self) -> None:
        from core.project_fs import ProjectFS

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._prepare_source(root)
            projection = root / PROJECTION_PATHS[0]
            projection.parent.mkdir(parents=True, exist_ok=True)
            original_projection = b"schema_version: 29\nstate: original\n"
            projection.write_bytes(original_projection)
            original_atomic_write = ProjectFS.atomic_write
            raced = False

            def replace_projection_after_final_manifest(
                active_fs,
                relative,
                data,
                *,
                mode=0o600,
                expected_destination=None,
            ):
                nonlocal raced
                written = original_atomic_write(
                    active_fs,
                    relative,
                    data,
                    mode=mode,
                    expected_destination=expected_destination,
                )
                if (
                    Path(relative).name == "migration-manifest.json"
                    and b'"status": "activated"' in bytes(data)
                    and not raced
                ):
                    parked = projection.with_name(
                        "project-state.published-before-final-race.yaml"
                    )
                    replacement = projection.with_name(
                        "project-state.success-attacker.yaml"
                    )
                    replacement.write_bytes(b"attacker-projection\n")
                    projection.rename(parked)
                    replacement.rename(projection)
                    raced = True
                return written

            with patch.object(
                ProjectFS,
                "atomic_write",
                autospec=True,
                side_effect=replace_projection_after_final_manifest,
            ):
                with self.assertRaisesRegex(Exception, "path-identity-changed"):
                    migrate_project_to_schema30(root)

            self.assertTrue(raced)
            with closing(sqlite3.connect(active)) as conn:
                self.assertEqual(
                    conn.execute(
                        "select schema_version from project where id=1"
                    ).fetchone()[0],
                    29,
                )
            self.assertEqual(projection.read_bytes(), original_projection)

    def test_operation_lock_remains_held_through_sentinel_cleanup(self) -> None:
        from core.project_fs import ProjectFS

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._prepare_source(root)
            original_operation = local_core_migration.project_db_operation
            original_unlink = ProjectFS.unlink_regular
            operation_active = False
            cleanup_observed = False

            @contextmanager
            def tracked_operation(*args, **kwargs):
                nonlocal operation_active
                with original_operation(*args, **kwargs) as project_fs:
                    operation_active = True
                    try:
                        yield project_fs
                    finally:
                        operation_active = False

            def assert_lock_during_cleanup(
                active_fs,
                relative,
                *,
                missing_ok=False,
                expected=None,
            ):
                nonlocal cleanup_observed
                if Path(relative).name == "local-core-migration.lock":
                    cleanup_observed = True
                    self.assertTrue(
                        operation_active,
                        "operation lock released before migration sentinel cleanup",
                    )
                return original_unlink(
                    active_fs,
                    relative,
                    missing_ok=missing_ok,
                    expected=expected,
                )

            with (
                patch.object(
                    local_core_migration,
                    "project_db_operation",
                    side_effect=tracked_operation,
                ),
                patch.object(
                    ProjectFS,
                    "unlink_regular",
                    autospec=True,
                    side_effect=assert_lock_during_cleanup,
                ),
            ):
                migrate_project_to_schema30(root)

            self.assertTrue(cleanup_observed)

    def test_guard_only_failure_publishes_consistent_recovery_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._prepare_source(root)
            original_result_type = local_core_migration.LocalCoreMigrationResult
            projection = root / PROJECTION_PATHS[0]
            raced = False

            def replace_projection_during_result_construction(*args, **kwargs):
                nonlocal raced
                parked = projection.with_name(
                    "project-state.before-guard-only-race.yaml"
                )
                replacement = projection.with_name(
                    "project-state.guard-only-attacker.yaml"
                )
                replacement.write_bytes(b"guard-only-attacker\n")
                projection.rename(parked)
                replacement.rename(projection)
                raced = True
                return original_result_type(*args, **kwargs)

            with patch.object(
                local_core_migration,
                "LocalCoreMigrationResult",
                side_effect=replace_projection_during_result_construction,
            ):
                with self.assertRaises(Exception):
                    migrate_project_to_schema30(root)

            self.assertTrue(raced)
            sentinel_path = root / ".ai-team/state/local-core-migration.lock"
            sentinel = json.loads(
                sentinel_path.read_text(encoding="utf-8")
            )
            self.assertEqual(sentinel["status"], "rollback-incomplete")
            recovery_manifest = Path(str(sentinel["manifest_path"]))
            manifest = json.loads(
                recovery_manifest.read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "rollback-incomplete")
            self.assertEqual(
                manifest["previous_terminal_status"],
                "activated",
            )
            self.assertIn(
                "path-identity-changed",
                manifest["terminal_authority_error"],
            )

    def test_success_pins_absent_retired_projection_and_database_sidecars(
        self,
    ) -> None:
        from core.project_fs import ProjectFS

        scenarios = (
            Path("docs/harness/evidence.md"),
            Path(".ai-team/state/harness.db-journal"),
        )
        for raced_relative in scenarios:
            with self.subTest(path=raced_relative), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                active = self._prepare_source(root)
                original_atomic_write = ProjectFS.atomic_write
                raced = False

                def create_absent_authority_after_final_manifest(
                    active_fs,
                    relative,
                    data,
                    *,
                    mode=0o600,
                    expected_destination=None,
                ):
                    nonlocal raced
                    written = original_atomic_write(
                        active_fs,
                        relative,
                        data,
                        mode=mode,
                        expected_destination=expected_destination,
                    )
                    if (
                        Path(relative).name == "migration-manifest.json"
                        and b'"status": "activated"' in bytes(data)
                        and not raced
                    ):
                        target = root / raced_relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(b"unexpected-terminal-authority\n")
                        raced = True
                    return written

                with patch.object(
                    ProjectFS,
                    "atomic_write",
                    autospec=True,
                    side_effect=create_absent_authority_after_final_manifest,
                ):
                    with self.assertRaises(Exception):
                        migrate_project_to_schema30(root)

                self.assertTrue(raced)
                self.assertFalse((root / raced_relative).exists())
                with closing(sqlite3.connect(active)) as conn:
                    self.assertEqual(
                        conn.execute(
                            "select schema_version from project where id=1"
                        ).fetchone()[0],
                        29,
                    )

    def test_rollback_pins_absent_database_sidecars_through_manifest_write(
        self,
    ) -> None:
        from core.project_fs import ProjectFS

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._prepare_source(root)
            journal = active.with_name(active.name + "-journal")
            original_atomic_write = ProjectFS.atomic_write
            raced = False

            def fail_validation(_active_path: Path) -> None:
                raise RuntimeError("validator-failed-before-sidecar-race")

            def create_sidecar_after_rollback_manifest(
                active_fs,
                relative,
                data,
                *,
                mode=0o600,
                expected_destination=None,
            ):
                nonlocal raced
                written = original_atomic_write(
                    active_fs,
                    relative,
                    data,
                    mode=mode,
                    expected_destination=expected_destination,
                )
                if (
                    Path(relative).name == "migration-manifest.json"
                    and b'"status": "rolled-back"' in bytes(data)
                    and not raced
                ):
                    journal.write_bytes(b"unexpected-rollback-journal\n")
                    raced = True
                return written

            with patch.object(
                ProjectFS,
                "atomic_write",
                autospec=True,
                side_effect=create_sidecar_after_rollback_manifest,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "validator-failed-before-sidecar-race",
                ):
                    _core_migrate_project_to_schema30(
                        root,
                        active_validator=fail_validation,
                    )

            self.assertTrue(raced)
            self.assertTrue(journal.is_file())
            backup_dir = next(
                (root / ".ai-team/backups").glob(
                    "schema-29-before-local-core-*"
                )
            )
            manifest = json.loads(
                (backup_dir / "migration-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["status"], "rollback-incomplete")
            self.assertEqual(manifest["database_restore_status"], "failed")
            self.assertTrue(
                (root / ".ai-team/state/local-core-migration.lock").is_file()
            )

    def test_success_pins_complete_recovery_bundle(self) -> None:
        from core.project_fs import ProjectFS

        for target_kind in ("database", "projection"):
            with self.subTest(target=target_kind), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                self._prepare_source(root)
                original_projection = root / PROJECTION_PATHS[0]
                original_projection.parent.mkdir(parents=True, exist_ok=True)
                original_projection.write_bytes(b"pre-migration-projection\n")
                original_atomic_write = ProjectFS.atomic_write
                raced = False
                raced_target: Path | None = None
                parked: Path | None = None

                def replace_recovery_file_after_final_manifest(
                    active_fs,
                    relative,
                    data,
                    *,
                    mode=0o600,
                    expected_destination=None,
                ):
                    nonlocal raced, raced_target, parked
                    written = original_atomic_write(
                        active_fs,
                        relative,
                        data,
                        mode=mode,
                        expected_destination=expected_destination,
                    )
                    if (
                        Path(relative).name == "migration-manifest.json"
                        and b'"status": "activated"' in bytes(data)
                        and not raced
                    ):
                        backup_dir = active_fs.absolute(Path(relative)).parent
                        raced_target = (
                            backup_dir / "harness.db"
                            if target_kind == "database"
                            else backup_dir
                            / "projections/00-project-state.yaml.bin"
                        )
                        parked = raced_target.with_name(
                            raced_target.name + ".verified-before-race"
                        )
                        replacement = raced_target.with_name(
                            raced_target.name + ".attacker"
                        )
                        replacement.write_bytes(b"attacker-recovery-file\n")
                        raced_target.rename(parked)
                        replacement.rename(raced_target)
                        raced = True
                    return written

                with patch.object(
                    ProjectFS,
                    "atomic_write",
                    autospec=True,
                    side_effect=replace_recovery_file_after_final_manifest,
                ):
                    with self.assertRaises(Exception):
                        migrate_project_to_schema30(root)

                self.assertTrue(raced)
                assert raced_target is not None
                assert parked is not None
                self.assertEqual(
                    raced_target.read_bytes(),
                    b"attacker-recovery-file\n",
                )
                self.assertTrue(parked.is_file())
                self.assertTrue(
                    (root / ".ai-team/state/local-core-migration.lock").is_file()
                )

    def test_raced_fallback_manifest_is_not_published_as_diagnostic_authority(
        self,
    ) -> None:
        from core.project_fs import ProjectFS

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._prepare_source(root)
            original_atomic_write = ProjectFS.atomic_write
            original_create_exclusive = ProjectFS.create_exclusive
            canonical_raced = False
            fallback_raced = False
            raced_fallback: Path | None = None

            def race_canonical_manifest(
                active_fs,
                relative,
                data,
                *,
                mode=0o600,
                expected_destination=None,
            ):
                nonlocal canonical_raced
                relative = Path(relative)
                if (
                    relative.name == "migration-manifest.json"
                    and expected_destination is not None
                    and not canonical_raced
                ):
                    canonical = active_fs.absolute(relative)
                    parked = canonical.with_name(
                        "migration-manifest.canonical-verified.json"
                    )
                    attacker = canonical.with_name(
                        "migration-manifest.canonical-attacker.json"
                    )
                    attacker.write_bytes(b'{"attacker":"canonical"}\n')
                    canonical.rename(parked)
                    attacker.rename(canonical)
                    canonical_raced = True
                return original_atomic_write(
                    active_fs,
                    relative,
                    data,
                    mode=mode,
                    expected_destination=expected_destination,
                )

            def race_first_fallback(
                active_fs,
                relative,
                data=b"",
                *,
                mode=0o600,
            ):
                nonlocal fallback_raced, raced_fallback
                snapshot = original_create_exclusive(
                    active_fs,
                    relative,
                    data,
                    mode=mode,
                )
                relative = Path(relative)
                if (
                    relative.name.startswith("migration-recovery-")
                    and not fallback_raced
                ):
                    raced_fallback = active_fs.absolute(relative)
                    parked = raced_fallback.with_name(
                        raced_fallback.name + ".verified"
                    )
                    attacker = raced_fallback.with_name(
                        raced_fallback.name + ".attacker"
                    )
                    attacker.write_bytes(b'{"attacker":"fallback"}\n')
                    raced_fallback.rename(parked)
                    attacker.rename(raced_fallback)
                    fallback_raced = True
                return snapshot

            with (
                patch.object(
                    ProjectFS,
                    "atomic_write",
                    autospec=True,
                    side_effect=race_canonical_manifest,
                ),
                patch.object(
                    ProjectFS,
                    "create_exclusive",
                    autospec=True,
                    side_effect=race_first_fallback,
                ),
            ):
                with self.assertRaises(Exception):
                    migrate_project_to_schema30(root)

            self.assertTrue(canonical_raced)
            self.assertTrue(fallback_raced)
            assert raced_fallback is not None
            self.assertEqual(
                raced_fallback.read_bytes(),
                b'{"attacker":"fallback"}\n',
            )
            sentinel = json.loads(
                (
                    root / ".ai-team/state/local-core-migration.lock"
                ).read_text(encoding="utf-8")
            )
            trusted_fallback = Path(str(sentinel["manifest_path"]))
            self.assertNotEqual(trusted_fallback, raced_fallback)
            trusted_payload = json.loads(
                trusted_fallback.read_text(encoding="utf-8")
            )
            self.assertEqual(trusted_payload["status"], "rollback-incomplete")

    @unittest.skipIf(os.name == "nt", "POSIX cleanup divergence detection")
    def test_cleanup_compensation_divergence_is_recovery_required(self) -> None:
        from core import project_fs as project_fs_module

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._prepare_source(root)
            real_noreplace = project_fs_module._posix_rename_noreplace
            injected = False
            attacker_payload = b"cleanup-divergence-attacker\n"

            def replace_cleanup_source(*args):
                nonlocal injected
                if (
                    not injected
                    and args[1] == "harness.schema30.new.db"
                    and ".kafa-delete-" in args[3]
                ):
                    staging = next(
                        (root / ".ai-team/backups").glob(
                            "schema-29-before-local-core-*/harness.schema30.new.db"
                        )
                    )
                    parked = staging.with_name("harness.schema29.parked.db")
                    attacker = staging.with_name("harness.staging.attacker.db")
                    attacker.write_bytes(attacker_payload)
                    staging.rename(parked)
                    attacker.rename(staging)
                    injected = True
                return real_noreplace(*args)

            with patch.object(
                project_fs_module,
                "_posix_rename_noreplace",
                side_effect=replace_cleanup_source,
            ):
                with self.assertRaises(Exception):
                    migrate_project_to_schema30(root)

            self.assertTrue(injected)
            active = root / ".ai-team/state/harness.db"
            self.assertEqual(active.read_bytes(), attacker_payload)
            backup_dir = next(
                (root / ".ai-team/backups").glob(
                    "schema-29-before-local-core-*"
                )
            )
            manifest = json.loads(
                (backup_dir / "migration-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                manifest["activation_detection_status"],
                "activation-state-diverged-recovery-required",
            )
            self.assertEqual(manifest["status"], "rollback-incomplete")
            self.assertEqual(
                manifest["database_restore_status"],
                "unknown-active-preserved",
            )
            self.assertTrue(
                (root / ".ai-team/state/local-core-migration.lock").is_file()
            )

    def test_missing_active_after_publication_is_recovery_required(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._prepare_source(root)
            parked = active.with_name("harness.published-before-unlink.db")
            original_activate = local_core_migration._activate_staging_database

            def remove_active_after_publication(
                project_fs,
                source,
                destination,
                *,
                staging_snapshot,
                active_snapshot,
            ):
                original_activate(
                    project_fs,
                    source,
                    destination,
                    staging_snapshot=staging_snapshot,
                    active_snapshot=active_snapshot,
                )
                active.rename(parked)
                raise KeyboardInterrupt("active-removed-after-publication")

            with patch.object(
                local_core_migration,
                "_activate_staging_database",
                side_effect=remove_active_after_publication,
            ):
                with self.assertRaisesRegex(
                    LocalCoreMigrationError,
                    "activation state diverged",
                ):
                    migrate_project_to_schema30(root)

            self.assertFalse(active.exists())
            self.assertTrue(parked.is_file())
            backup_dir = next(
                (root / ".ai-team/backups").glob(
                    "schema-29-before-local-core-*"
                )
            )
            manifest = json.loads(
                (backup_dir / "migration-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                manifest["activation_detection_status"],
                "activation-state-diverged-recovery-required",
            )
            self.assertEqual(manifest["status"], "rollback-incomplete")
            self.assertTrue(
                (root / ".ai-team/state/local-core-migration.lock").is_file()
            )

    def test_projection_restore_holds_exact_receipt_through_final_verification(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_relative = PROJECTION_ROLLBACK_PATHS[0]
            target = root / target_relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"mutated-projection\n")

            backup_directory = root / ".ai-team/backups/projection-receipt"
            backup_directory.mkdir(parents=True)
            backup_path = backup_directory / "project-state.yaml"
            expected_content = b"original-projection\n"
            expected_mode = 0o666 if os.name == "nt" else 0o640
            backup_path.write_bytes(expected_content)
            backup_path.chmod(expected_mode)
            expected_digest = hashlib.sha256(expected_content).hexdigest()
            entries: list[dict[str, object]] = []
            for relative in PROJECTION_ROLLBACK_PATHS:
                if relative == target_relative:
                    entries.append(
                        {
                            "path": relative.as_posix(),
                            "existed": True,
                            "mode": expected_mode,
                            "sha256": expected_digest,
                            "backup_path": str(backup_path),
                        }
                    )
                else:
                    entries.append(
                        {
                            "path": relative.as_posix(),
                            "existed": False,
                        }
                    )
            projection_backup: dict[str, object] = {
                "directory": str(backup_directory),
                "entries": entries,
            }
            original_safe_mode = local_core_migration._safe_file_mode
            raced = False
            parked = target.with_name("project-state.restored-before-race.yaml")

            def replace_after_immediate_verification(
                project_fs,
                relative,
                *,
                expected=None,
            ):
                nonlocal raced
                mode = original_safe_mode(
                    project_fs,
                    relative,
                    expected=expected,
                )
                if (
                    Path(relative) == target_relative
                    and expected is not None
                    and not raced
                    and target.read_bytes() == expected_content
                ):
                    replacement = target.with_name(
                        "project-state.same-bytes-attacker.yaml"
                    )
                    replacement.write_bytes(expected_content)
                    replacement.chmod(expected_mode)
                    target.rename(parked)
                    replacement.rename(target)
                    raced = True
                return mode

            with patch.object(
                local_core_migration,
                "_safe_file_mode",
                side_effect=replace_after_immediate_verification,
            ):
                with self.assertRaisesRegex(Exception, "path-identity-changed"):
                    local_core_migration._restore_projection_backup(
                        root,
                        projection_backup,
                    )

            self.assertTrue(raced)
            self.assertTrue(parked.is_file())
            self.assertEqual(target.read_bytes(), expected_content)

    def test_rollback_projection_receipt_survives_terminal_manifest_write(
        self,
    ) -> None:
        from core.project_fs import ProjectFS

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._prepare_source(root)
            projection = root / PROJECTION_ROLLBACK_PATHS[0]
            projection.parent.mkdir(parents=True, exist_ok=True)
            original_projection = b"schema_version: 29\nstate: original\n"
            projection.write_bytes(original_projection)
            original_atomic_write = ProjectFS.atomic_write
            attacker_projection = b"attacker-projection\n"
            parked = projection.with_name(
                "project-state.verified-before-terminal-manifest.yaml"
            )
            raced = False

            def fail_after_partial_projection(_active_path: Path) -> None:
                projection.write_bytes(b"schema_version: 30\nstate: partial\n")
                raise RuntimeError("validator-failed-after-partial-projection")

            def replace_projection_during_rolled_back_manifest(
                active_fs,
                relative,
                data,
                *,
                mode=0o600,
                expected_destination=None,
            ):
                nonlocal raced
                written = original_atomic_write(
                    active_fs,
                    relative,
                    data,
                    mode=mode,
                    expected_destination=expected_destination,
                )
                if (
                    Path(relative).name == "migration-manifest.json"
                    and b'"status": "rolled-back"' in bytes(data)
                    and not raced
                ):
                    replacement = projection.with_name(
                        "project-state.terminal-attacker.yaml"
                    )
                    replacement.write_bytes(attacker_projection)
                    projection.rename(parked)
                    replacement.rename(projection)
                    raced = True
                return written

            with patch.object(
                ProjectFS,
                "atomic_write",
                autospec=True,
                side_effect=replace_projection_during_rolled_back_manifest,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "validator-failed-after-partial-projection",
                ):
                    _core_migrate_project_to_schema30(
                        root,
                        active_validator=fail_after_partial_projection,
                    )

            self.assertTrue(raced)
            self.assertEqual(projection.read_bytes(), attacker_projection)
            self.assertEqual(parked.read_bytes(), original_projection)
            with closing(sqlite3.connect(active)) as conn:
                self.assertEqual(
                    conn.execute(
                        "select schema_version from project where id=1"
                    ).fetchone()[0],
                    29,
                )
            backup_dir = next(
                (root / ".ai-team/backups").glob(
                    "schema-29-before-local-core-*"
                )
            )
            manifest = json.loads(
                (backup_dir / "migration-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["status"], "rollback-incomplete")
            self.assertEqual(manifest["projection_restore_status"], "failed")
            self.assertTrue(
                (root / ".ai-team/state/local-core-migration.lock").is_file()
            )

    def test_rollback_database_receipt_survives_terminal_manifest_write(
        self,
    ) -> None:
        from core.project_fs import ProjectFS

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._prepare_source(root)
            original_atomic_write = ProjectFS.atomic_write
            attacker_database = b"attacker-database\n"
            parked = active.with_name(
                "harness.verified-before-terminal-manifest.db"
            )
            raced = False

            def fail_validation(_active_path: Path) -> None:
                raise RuntimeError("validator-failed-before-db-race")

            def replace_database_during_rolled_back_manifest(
                active_fs,
                relative,
                data,
                *,
                mode=0o600,
                expected_destination=None,
            ):
                nonlocal raced
                written = original_atomic_write(
                    active_fs,
                    relative,
                    data,
                    mode=mode,
                    expected_destination=expected_destination,
                )
                if (
                    Path(relative).name == "migration-manifest.json"
                    and b'"status": "rolled-back"' in bytes(data)
                    and not raced
                ):
                    replacement = active.with_name(
                        "harness.terminal-attacker.db"
                    )
                    replacement.write_bytes(attacker_database)
                    active.rename(parked)
                    replacement.rename(active)
                    raced = True
                return written

            with patch.object(
                ProjectFS,
                "atomic_write",
                autospec=True,
                side_effect=replace_database_during_rolled_back_manifest,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "validator-failed-before-db-race",
                ):
                    _core_migrate_project_to_schema30(
                        root,
                        active_validator=fail_validation,
                    )

            self.assertTrue(raced)
            self.assertEqual(active.read_bytes(), attacker_database)
            with closing(sqlite3.connect(parked)) as conn:
                self.assertEqual(
                    conn.execute(
                        "select schema_version from project where id=1"
                    ).fetchone()[0],
                    29,
                )
            backup_dir = next(
                (root / ".ai-team/backups").glob(
                    "schema-29-before-local-core-*"
                )
            )
            manifest = json.loads(
                (backup_dir / "migration-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["status"], "rollback-incomplete")
            self.assertEqual(manifest["database_restore_status"], "failed")
            self.assertTrue(
                (root / ".ai-team/state/local-core-migration.lock").is_file()
            )

    def test_active_replacement_during_final_manifest_cannot_report_success(
        self,
    ) -> None:
        from core.project_fs import ProjectFS

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._prepare_source(root)
            parked = active.with_name("harness.before-final-manifest.db")
            replacement = active.with_name("harness.raced-final.db")
            original_atomic_write = ProjectFS.atomic_write
            raced = False

            def race_active_then_write_manifest(
                active_fs,
                relative,
                data,
                *,
                mode=0o600,
                expected_destination=None,
            ):
                nonlocal raced
                if (
                    Path(relative).name == "migration-manifest.json"
                    and b'"status": "activated"' in bytes(data)
                    and not raced
                ):
                    replacement.write_bytes(active.read_bytes())
                    active.rename(parked)
                    replacement.rename(active)
                    raced = True
                return original_atomic_write(
                    active_fs,
                    relative,
                    data,
                    mode=mode,
                    expected_destination=expected_destination,
                )

            with patch.object(
                ProjectFS,
                "atomic_write",
                autospec=True,
                side_effect=race_active_then_write_manifest,
            ):
                with self.assertRaisesRegex(
                    LocalCoreMigrationError,
                    "rollback failed",
                ):
                    migrate_project_to_schema30(root)

            self.assertTrue(raced)
            self.assertTrue(active.is_file())
            self.assertTrue(parked.is_file())
            with closing(sqlite3.connect(active)) as conn:
                self.assertEqual(
                    conn.execute(
                        "select schema_version from project where id=1"
                    ).fetchone()[0],
                    30,
                )
            backup_dir = next(
                (root / ".ai-team/backups").glob(
                    "schema-29-before-local-core-*"
                )
            )
            manifest = json.loads(
                (backup_dir / "migration-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["status"], "rollback-incomplete")
            self.assertEqual(manifest["database_restore_status"], "failed")
            self.assertTrue(
                (root / ".ai-team/state/local-core-migration.lock").is_file()
            )

    @unittest.skipIf(os.name == "nt", "POSIX cleanup rollback detection")
    def test_cleanup_rollback_failure_detects_activated_candidate_by_receipt(
        self,
    ) -> None:
        from core import project_fs as project_fs_module

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._prepare_source(root)
            real_exchange = project_fs_module._posix_rename_exchange
            real_noreplace = project_fs_module._posix_rename_noreplace
            exchange_calls = 0
            injected = False
            parked: Path | None = None

            def fail_publication_rollback_exchange(*args):
                nonlocal exchange_calls
                if {
                    args[1],
                    args[3],
                } == {"harness.schema30.new.db", "harness.db"}:
                    exchange_calls += 1
                    if exchange_calls == 2:
                        raise OSError(
                            5,
                            "injected publication rollback failure",
                        )
                return real_exchange(*args)

            def replace_cleanup_source(*args):
                nonlocal injected, parked
                source_name = args[1]
                destination_name = args[3]
                if (
                    not injected
                    and source_name == "harness.schema30.new.db"
                    and ".kafa-delete-" in destination_name
                ):
                    staging = next(
                        (root / ".ai-team/backups").glob(
                            "schema-29-before-local-core-*/harness.schema30.new.db"
                        )
                    )
                    parked = staging.with_name("harness.schema29.parked.db")
                    attacker = staging.with_name("harness.staging.attacker.db")
                    attacker.write_bytes(b"attacker-staging\n")
                    staging.rename(parked)
                    attacker.rename(staging)
                    injected = True
                return real_noreplace(*args)

            with (
                patch.object(
                    project_fs_module,
                    "_posix_rename_exchange",
                    side_effect=fail_publication_rollback_exchange,
                ),
                patch.object(
                    project_fs_module,
                    "_posix_rename_noreplace",
                    side_effect=replace_cleanup_source,
                ),
            ):
                with self.assertRaisesRegex(
                    Exception,
                    "path-identity-changed",
                ):
                    migrate_project_to_schema30(root)

            self.assertTrue(injected)
            self.assertEqual(exchange_calls, 2)
            assert parked is not None
            self.assertTrue(parked.is_file())
            backup_dir = parked.parent
            manifest = json.loads(
                (backup_dir / "migration-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                manifest["activation_detection_status"],
                "matched-staging-identity",
            )
            self.assertEqual(manifest["status"], "rolled-back")
            self.assertEqual(sha256(active), manifest["backup"]["sha256"])
            with closing(sqlite3.connect(active)) as conn:
                self.assertEqual(
                    conn.execute(
                        "select schema_version from project where id=1"
                    ).fetchone()[0],
                    29,
                )

    def test_hard_exit_after_activation_leaves_durable_recovery_required_sentinel(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._prepare_source(root)
            projection = root / ".ai-team/control/project-state.yaml"
            projection.parent.mkdir(parents=True, exist_ok=True)
            projection.write_text("schema_version: 29\n", encoding="utf-8")
            context = multiprocessing.get_context("spawn")
            process = context.Process(
                target=_run_hard_exit_after_schema30_activation,
                args=(str(root),),
            )
            process.start()
            process.join(30)
            if process.is_alive():
                process.terminate()
                process.join(5)
                self.fail("hard-exit migration child did not terminate")

            sentinel = root / ".ai-team/state/local-core-migration.lock"
            payload = json.loads(sentinel.read_text(encoding="utf-8"))
            with closing(sqlite3.connect(active)) as conn:
                active_version = int(
                    conn.execute("select schema_version from project where id=1").fetchone()[0]
                )

            self.assertEqual(process.exitcode, 17)
            self.assertEqual(active_version, 30)
            self.assertEqual(
                projection.read_text(encoding="utf-8"),
                "schema_version: 29\n",
            )
            self.assertEqual(payload["status"], "recovery-required")
            manifest_path = Path(str(payload["manifest_path"]))
            self.assertTrue(manifest_path.is_file())
            with self.assertRaisesRegex(
                ProjectOperationLockError,
                "recovery-required.*do not remove",
            ):
                with SqliteStore(root).connection():
                    self.fail("Store opened a split-authority hard-exit migration")

    def test_core_migration_cannot_report_success_without_projection_validator(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._prepare_source(root)
            projection = root / ".ai-team/control/project-state.yaml"
            projection.parent.mkdir(parents=True, exist_ok=True)
            projection.write_text("schema_version: 29\n", encoding="utf-8")

            with self.assertRaisesRegex(
                LocalCoreMigrationError,
                "projection.*validator",
            ):
                _core_migrate_project_to_schema30(root)

            with closing(sqlite3.connect(active)) as conn:
                active_version = int(
                    conn.execute("select schema_version from project where id=1").fetchone()[0]
                )
            self.assertEqual(active_version, 29)
            self.assertEqual(
                projection.read_text(encoding="utf-8"),
                "schema_version: 29\n",
            )

    def test_migration_root_replacement_fails_closed_without_writing_new_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            active = self._prepare_source(root)
            source_digest = sha256(active)
            detached = base / "detached-project"
            replacement_marker = b"replacement-root-must-remain-untouched\n"
            original_checkpoint = local_core_migration._checkpoint_active_database
            blocked_replacements: list[OSError] = []

            def replace_root_then_checkpoint(
                active_path: Path,
                *,
                pinned_fs=None,
            ) -> None:
                try:
                    root.rename(detached)
                except OSError as exc:
                    if os.name != "nt":
                        raise
                    if not isinstance(exc, PermissionError) or getattr(
                        exc,
                        "winerror",
                        None,
                    ) not in {5, 32}:
                        raise
                    blocked_replacements.append(exc)
                    original_checkpoint(active_path, pinned_fs=pinned_fs)
                    return
                root.mkdir()
                (root / "replacement-marker.txt").write_bytes(replacement_marker)
                original_checkpoint(active_path, pinned_fs=pinned_fs)

            if os.name == "nt":
                with patch.object(
                    local_core_migration,
                    "_checkpoint_active_database",
                    side_effect=replace_root_then_checkpoint,
                ):
                    migrate_project_to_schema30(root)

                self.assertEqual(len(blocked_replacements), 1)
                self.assertTrue(root.is_dir())
                self.assertFalse(detached.exists())
                self.assertFalse((root / "replacement-marker.txt").exists())
                self.assertFalse(
                    (root / ".ai-team/state/local-core-migration.lock").exists()
                )
                with closing(sqlite3.connect(active)) as conn:
                    self.assertEqual(
                        conn.execute(
                            "select schema_version from project where id=1"
                        ).fetchone()[0],
                        30,
                    )
                return

            with patch.object(
                local_core_migration,
                "_checkpoint_active_database",
                side_effect=replace_root_then_checkpoint,
            ):
                with self.assertRaisesRegex(Exception, "path-identity-changed"):
                    migrate_project_to_schema30(root)

            self.assertEqual(
                sorted(path.name for path in root.iterdir()),
                ["replacement-marker.txt"],
            )
            self.assertEqual(
                (root / "replacement-marker.txt").read_bytes(),
                replacement_marker,
            )
            detached_active = detached / ".ai-team/state/harness.db"
            self.assertEqual(sha256(detached_active), source_digest)
            with closing(sqlite3.connect(detached_active)) as conn:
                self.assertEqual(
                    conn.execute(
                        "select schema_version from project where id=1"
                    ).fetchone()[0],
                    29,
                )
            self.assertTrue(
                (detached / ".ai-team/state/local-core-migration.lock").is_file()
            )

    def test_failure_injection_preserves_or_restores_active_database(self) -> None:
        points = (
            "before_copy",
            "during_relation_copy",
            "during_invariant_validation",
            "before_atomic_replace",
            "after_atomic_replace",
        )
        for point in points:
            with self.subTest(point=point), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                active = self._prepare_source(root)
                source_digest = sha256(active)

                with self.assertRaises(InjectedLocalCoreMigrationFailure):
                    migrate_project_to_schema30(root, fail_at=point)

                with closing(sqlite3.connect(active)) as conn:
                    version = int(conn.execute("select schema_version from project where id=1").fetchone()[0])
                    sentinel = conn.execute("select decision from decisions where id='D-sentinel'").fetchone()[0]
                    integrity = conn.execute("pragma integrity_check").fetchone()[0]
                    foreign_key_issues = conn.execute("pragma foreign_key_check").fetchall()
                backup_dirs = sorted((root / ".ai-team/backups").glob("schema-29-before-local-core-*"))
                self.assertEqual(len(backup_dirs), 1)
                manifest_path = backup_dirs[0] / "migration-manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                backup_path = Path(manifest["backup"]["backup_path"])
                backup_digest = manifest["backup"]["sha256"]
                active_digest = sha256(active)
                lock_exists = (root / ".ai-team/state/local-core-migration.lock").exists()

                self.assertEqual(version, 29)
                self.assertEqual(sentinel, "keep")
                self.assertEqual(integrity, "ok")
                self.assertEqual(foreign_key_issues, [])
                self.assertTrue(backup_path.is_file())
                self.assertEqual(sha256(backup_path), backup_digest)
                self.assertFalse(lock_exists)
                if point == "after_atomic_replace":
                    self.assertEqual(manifest["status"], "rolled-back")
                    self.assertEqual(active_digest, backup_digest)
                    failed = Path(manifest["failed_schema30_path"])
                    self.assertTrue(failed.is_file())
                    with closing(sqlite3.connect(failed)) as conn:
                        self.assertEqual(
                            int(conn.execute("select schema_version from project where id=1").fetchone()[0]),
                            30,
                        )
                else:
                    self.assertEqual(manifest["status"], "failed-before-activation")
                    self.assertEqual(
                        manifest["database_restore_status"],
                        "unchanged-verified",
                    )
                    self.assertEqual(
                        manifest["projection_restore_status"],
                        "restored",
                    )
                    self.assertEqual(active_digest, source_digest)

    def test_unverified_projection_backup_failure_retains_diagnostic_sentinel(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._prepare_source(root)
            with patch.object(
                local_core_migration,
                "_create_projection_backup",
                side_effect=OSError("projection-backup-unavailable"),
            ):
                with self.assertRaisesRegex(OSError, "projection-backup-unavailable"):
                    migrate_project_to_schema30(root)

            backup_dir = next(
                (root / ".ai-team/backups").glob("schema-29-before-local-core-*")
            )
            manifest_path = backup_dir / "migration-manifest.json"
            sentinel = root / ".ai-team/state/local-core-migration.lock"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

            self.assertEqual(manifest["status"], "failed-before-activation")
            self.assertTrue(sentinel.is_file())
            sentinel_payload = json.loads(sentinel.read_text(encoding="utf-8"))
            self.assertEqual(sentinel_payload["status"], "migration-failed")
            self.assertEqual(
                Path(sentinel_payload["manifest_path"]).resolve(),
                manifest_path.resolve(),
            )

    def test_post_activation_cancellation_restores_database_and_projections(self) -> None:
        cancellation_types = (KeyboardInterrupt, SystemExit, asyncio.CancelledError)
        for cancellation_type in cancellation_types:
            with self.subTest(cancellation_type=cancellation_type.__name__), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                active = self._prepare_source(root)
                projection = root / ".ai-team/control/project-state.yaml"
                projection.parent.mkdir(parents=True, exist_ok=True)
                projection.write_bytes(b"schema_version: 29\nstate: before-cancellation\n")
                projection.chmod(0o640)
                projection_bytes = projection.read_bytes()
                projection_mode = projection.stat().st_mode & 0o7777
                cancellation = cancellation_type("interrupt-after-activation")

                def interrupt_after_activation(_active_path: Path) -> None:
                    raise cancellation

                with self.assertRaises(cancellation_type) as raised:
                    migrate_project_to_schema30(
                        root,
                        active_validator=interrupt_after_activation,
                    )

                backup_dir = next(
                    (root / ".ai-team/backups").glob("schema-29-before-local-core-*")
                )
                manifest = json.loads(
                    (backup_dir / "migration-manifest.json").read_text(encoding="utf-8")
                )
                with closing(sqlite3.connect(active)) as conn:
                    active_version = int(
                        conn.execute(
                            "select schema_version from project where id=1"
                        ).fetchone()[0]
                    )
                failed_schema30 = Path(manifest["failed_schema30_path"])
                with closing(sqlite3.connect(failed_schema30)) as conn:
                    failed_version = int(
                        conn.execute(
                            "select schema_version from project where id=1"
                        ).fetchone()[0]
                    )

                self.assertIs(raised.exception, cancellation)
                self.assertEqual(active_version, 29)
                self.assertEqual(sha256(active), manifest["backup"]["sha256"])
                self.assertEqual(projection.read_bytes(), projection_bytes)
                self.assertEqual(projection.stat().st_mode & 0o7777, projection_mode)
                self.assertEqual(failed_version, 30)
                self.assertEqual(manifest["status"], "rolled-back")
                self.assertEqual(manifest["database_restore_status"], "restored")
                self.assertEqual(manifest["projection_restore_status"], "restored")
                self.assertIn("interrupt-after-activation", manifest["error"])
                self.assertFalse(
                    (root / ".ai-team/state/local-core-migration.lock").exists()
                )

    def test_interrupt_between_atomic_replace_and_activation_flag_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._prepare_source(root)
            projection = root / ".ai-team/control/project-state.yaml"
            projection.parent.mkdir(parents=True, exist_ok=True)
            projection.write_bytes(b"schema_version: 29\nstate: before-replace\n")
            original_activate = (
                local_core_migration._activate_staging_database
            )
            interrupted = False

            def interrupt_after_replace(
                project_fs,
                source: Path,
                destination: Path,
                *,
                staging_snapshot,
                active_snapshot,
            ) -> None:
                nonlocal interrupted
                if not interrupted:
                    original_activate(
                        project_fs,
                        source,
                        destination,
                        staging_snapshot=staging_snapshot,
                        active_snapshot=active_snapshot,
                    )
                    interrupted = True
                    raise KeyboardInterrupt(
                        "interrupt-between-replace-and-activated-flag"
                    )
                original_activate(
                    project_fs,
                    source,
                    destination,
                    staging_snapshot=staging_snapshot,
                    active_snapshot=active_snapshot,
                )

            with patch.object(
                local_core_migration,
                "_activate_staging_database",
                side_effect=interrupt_after_replace,
            ):
                with self.assertRaisesRegex(
                    KeyboardInterrupt,
                    "interrupt-between-replace-and-activated-flag",
                ):
                    migrate_project_to_schema30(root)

            backup_dir = next(
                (root / ".ai-team/backups").glob("schema-29-before-local-core-*")
            )
            manifest = json.loads(
                (backup_dir / "migration-manifest.json").read_text(encoding="utf-8")
            )
            with closing(sqlite3.connect(active)) as conn:
                active_version = int(
                    conn.execute(
                        "select schema_version from project where id=1"
                    ).fetchone()[0]
                )

            self.assertTrue(interrupted)
            self.assertEqual(active_version, 29)
            self.assertEqual(sha256(active), manifest["backup"]["sha256"])
            self.assertEqual(
                projection.read_bytes(),
                b"schema_version: 29\nstate: before-replace\n",
            )
            self.assertEqual(manifest["status"], "rolled-back")
            self.assertEqual(manifest["database_restore_status"], "restored")
            self.assertEqual(manifest["projection_restore_status"], "restored")
            self.assertEqual(
                manifest["activation_detection_status"],
                "matched-staging-identity",
            )
            self.assertIn(
                "interrupt-between-replace-and-activated-flag",
                manifest["error"],
            )
            self.assertFalse(
                (root / ".ai-team/state/local-core-migration.lock").exists()
            )

    def test_public_migrate_interrupt_during_projection_publish_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._prepare_source(root)
            (root / ".gitignore").write_text(
                "\n".join(harness_db.RUNTIME_GITIGNORE_PATTERNS) + "\n",
                encoding="utf-8",
            )
            projection = root / ".ai-team/control/project-state.yaml"
            projection.parent.mkdir(parents=True, exist_ok=True)
            projection.write_bytes(b"schema_version: 29\nstate: public-before\n")
            original_projection = projection.read_bytes()
            render_calls = 0

            def interrupt_live_projection(render_root: Path) -> None:
                nonlocal render_calls
                render_calls += 1
                if Path(render_root).resolve() != root.resolve():
                    return
                projection.write_bytes(
                    b"schema_version: 30\nstate: partial-publication\n"
                )
                raise KeyboardInterrupt("interrupt-during-live-projection")

            with patch.object(
                harness_db,
                "render_all",
                side_effect=interrupt_live_projection,
            ):
                with self.assertRaisesRegex(
                    KeyboardInterrupt,
                    "interrupt-during-live-projection",
                ):
                    harness_db.migrate(root, "29", 30)

            backup_dir = next(
                (root / ".ai-team/backups").glob("schema-29-before-local-core-*")
            )
            manifest = json.loads(
                (backup_dir / "migration-manifest.json").read_text(encoding="utf-8")
            )
            with closing(sqlite3.connect(active)) as conn:
                active_version = int(
                    conn.execute(
                        "select schema_version from project where id=1"
                    ).fetchone()[0]
                )

            self.assertGreaterEqual(render_calls, 2)
            self.assertEqual(active_version, 29)
            self.assertEqual(sha256(active), manifest["backup"]["sha256"])
            self.assertEqual(projection.read_bytes(), original_projection)
            self.assertEqual(manifest["status"], "rolled-back")
            self.assertEqual(manifest["database_restore_status"], "restored")
            self.assertEqual(manifest["projection_restore_status"], "restored")
            self.assertIn("interrupt-during-live-projection", manifest["error"])
            self.assertFalse(
                (root / ".ai-team/state/local-core-migration.lock").exists()
            )

    def test_database_restore_cancellation_keeps_normal_store_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._prepare_source(root)
            with patch.object(
                local_core_migration,
                "_restore_verified_backup",
                side_effect=SystemExit("cancel-during-db-restore"),
            ):
                with self.assertRaisesRegex(
                    LocalCoreMigrationError,
                    "database rollback failed",
                ):
                    migrate_project_to_schema30(
                        root,
                        fail_at="after_atomic_replace",
                    )

            backup_dir = next(
                (root / ".ai-team/backups").glob("schema-29-before-local-core-*")
            )
            manifest_path = backup_dir / "migration-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            sentinel = root / ".ai-team/state/local-core-migration.lock"

            self.assertFalse(active.exists())
            self.assertEqual(manifest["status"], "rollback-incomplete")
            self.assertEqual(manifest["database_restore_status"], "failed")
            self.assertIn(
                "cancel-during-db-restore",
                manifest["database_restore_error"],
            )
            self.assertTrue(sentinel.is_file())
            sentinel_payload = json.loads(sentinel.read_text(encoding="utf-8"))
            self.assertEqual(sentinel_payload["status"], "rollback-incomplete")
            self.assertEqual(
                Path(sentinel_payload["manifest_path"]).resolve(),
                manifest_path.resolve(),
            )
            with self.assertRaisesRegex(
                ProjectOperationLockError,
                "rollback-incomplete",
            ) as blocked:
                with SqliteStore(root).connection():
                    self.fail("normal Store opened while recovery-required sentinel existed")
            self.assertIn(str(manifest_path.resolve()), str(blocked.exception))
            self.assertFalse(active.exists())

    def test_rollback_manifest_write_failure_keeps_normal_store_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._prepare_source(root)
            original_write_json = local_core_migration._write_json_atomic

            def fail_terminal_manifest(
                path: Path,
                payload: dict[str, object],
                **kwargs: object,
            ):
                if (
                    Path(path).name == "migration-manifest.json"
                    and payload.get("status") == "rollback-incomplete"
                ):
                    raise OSError("manifest-write-failed-during-rollback")
                return original_write_json(path, payload, **kwargs)

            with (
                patch.object(
                    local_core_migration,
                    "_restore_verified_backup",
                    side_effect=SystemExit("cancel-during-db-restore"),
                ),
                patch.object(
                    local_core_migration,
                    "_write_json_atomic",
                    side_effect=fail_terminal_manifest,
                ),
            ):
                with self.assertRaisesRegex(
                    LocalCoreMigrationError,
                    "cancel-during-db-restore.*migration-recovery-",
                ):
                    migrate_project_to_schema30(
                        root,
                        fail_at="after_atomic_replace",
                    )

            sentinel = root / ".ai-team/state/local-core-migration.lock"
            fallback_paths = tuple(
                (root / ".ai-team/state").glob(
                    "migration-recovery-*.json"
                )
            )
            self.assertEqual(len(fallback_paths), 1)
            fallback_payload = json.loads(
                fallback_paths[0].read_text(encoding="utf-8")
            )
            self.assertEqual(
                fallback_payload["database_restore_error"],
                "cancel-during-db-restore",
            )
            self.assertIn(
                "manifest-write-failed-during-rollback",
                fallback_payload["manifest_write_error"],
            )
            self.assertFalse(active.exists())
            self.assertTrue(sentinel.is_file())
            sentinel_payload = json.loads(sentinel.read_text(encoding="utf-8"))
            self.assertEqual(
                Path(sentinel_payload["manifest_path"]).resolve(),
                fallback_paths[0].resolve(),
            )
            with self.assertRaisesRegex(
                ProjectOperationLockError,
                "rollback-incomplete",
            ):
                with SqliteStore(root).connection():
                    self.fail("normal Store recreated a DB after manifest-write failure")
            self.assertFalse(active.exists())

    def test_second_cancellation_during_recovery_keeps_normal_store_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._prepare_source(root)
            original_preserve = local_core_migration._preserve_failed_schema30

            def preserve_then_interrupt(
                active_path: Path,
                failed_path: Path,
                **kwargs: object,
            ) -> tuple[str, str]:
                original_preserve(active_path, failed_path, **kwargs)
                raise KeyboardInterrupt("cancel-after-schema30-preservation")

            with patch.object(
                local_core_migration,
                "_preserve_failed_schema30",
                side_effect=preserve_then_interrupt,
            ):
                with self.assertRaisesRegex(
                    KeyboardInterrupt,
                    "cancel-after-schema30-preservation",
                ):
                    migrate_project_to_schema30(
                        root,
                        fail_at="after_atomic_replace",
                    )

            sentinel = root / ".ai-team/state/local-core-migration.lock"
            self.assertFalse(active.exists())
            self.assertTrue(sentinel.is_file())
            with self.assertRaisesRegex(
                ProjectOperationLockError,
                "rollback-incomplete",
            ):
                with SqliteStore(root).connection():
                    self.fail("normal Store recreated a DB after recovery cancellation")
            self.assertFalse(active.exists())

    def test_post_activation_projection_failure_restores_verified_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._prepare_source(root)
            (root / ".gitignore").write_text(
                "\n".join(harness_db.RUNTIME_GITIGNORE_PATTERNS) + "\n", encoding="utf-8"
            )
            with patch.object(
                harness_db,
                "render_all",
                side_effect=(None, harness_db.HarnessError("projection failed")),
            ):
                with self.assertRaisesRegex(harness_db.HarnessError, "projection failed"):
                    harness_db.migrate(root, "29", 30)
            backup_dir = next((root / ".ai-team/backups").glob("schema-29-before-local-core-*"))
            manifest = json.loads((backup_dir / "migration-manifest.json").read_text(encoding="utf-8"))
            with closing(sqlite3.connect(active)) as conn:
                version = conn.execute("select schema_version from project where id=1").fetchone()[0]
            self.assertEqual(
                (version, sha256(active), manifest["status"]),
                (29, manifest["backup"]["sha256"], "rolled-back"),
            )

    def test_final_doctor_failure_does_not_publish_schema30_projections(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._prepare_source(root)
            state = root / ".ai-team/control/project-state.yaml"
            delivery = root / "docs/harness/delivery.md"
            state.parent.mkdir(parents=True, exist_ok=True)
            delivery.parent.mkdir(parents=True, exist_ok=True)
            state.write_bytes(b"schema: 29\nstate: original\n")
            delivery.write_bytes(b"# Original schema 29 delivery view\n")

            def render_with_visible_side_effect(render_root: Path) -> None:
                if render_root.resolve() != root.resolve():
                    return
                state.write_bytes(b"schema: 30\nstate: published-too-early\n")
                delivery.write_bytes(b"# Incorrect schema 30 delivery view\n")

            with (
                patch.object(harness_db, "render_all", side_effect=render_with_visible_side_effect),
                patch.object(harness_db, "doctor", return_value=["forced final doctor failure"]),
            ):
                with self.assertRaisesRegex(harness_db.HarnessError, "forced final doctor failure"):
                    harness_db.migrate(root, "29", 30)

            backup_dir = next((root / ".ai-team/backups").glob("schema-29-before-local-core-*"))
            manifest = json.loads((backup_dir / "migration-manifest.json").read_text(encoding="utf-8"))
            with closing(sqlite3.connect(active)) as conn:
                version = conn.execute("select schema_version from project where id=1").fetchone()[0]

            self.assertEqual(version, 29)
            self.assertEqual(state.read_bytes(), b"schema: 29\nstate: original\n")
            self.assertEqual(delivery.read_bytes(), b"# Original schema 29 delivery view\n")
            self.assertIn("projection_backup", manifest)
            self.assertEqual(manifest["projection_restore_status"], "restored")

    def test_partial_projection_failure_restores_exact_pre_migration_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._prepare_source(root)
            original = root / ".ai-team/control/project-state.yaml"
            created_during_failure = root / "docs/harness/executions.md"
            retired_evidence = root / "docs/harness/evidence.md"
            original.parent.mkdir(parents=True, exist_ok=True)
            retired_evidence.parent.mkdir(parents=True, exist_ok=True)
            original.write_bytes(b"schema: 29\noriginal: true\n")
            retired_evidence.write_bytes(b"# Legacy evidence view\n")
            original.chmod(0o640)
            retired_evidence.chmod(0o444)
            original_mode = original.stat().st_mode & 0o7777
            retired_mode = retired_evidence.stat().st_mode & 0o7777
            original_digest = sha256(original)
            retired_digest = sha256(retired_evidence)

            def partially_render(render_root: Path) -> None:
                if render_root.resolve() != root.resolve():
                    return
                original.write_bytes(b"schema: 30\npartial: true\n")
                render_executions(render_root)
                raise harness_db.HarnessError("partial projection failure")

            with patch.object(harness_db, "render_all", side_effect=partially_render):
                with self.assertRaisesRegex(harness_db.HarnessError, "partial projection failure"):
                    harness_db.migrate(root, "29", 30)

            backup_dir = next((root / ".ai-team/backups").glob("schema-29-before-local-core-*"))
            manifest = json.loads((backup_dir / "migration-manifest.json").read_text(encoding="utf-8"))
            with closing(sqlite3.connect(active)) as conn:
                version = conn.execute("select schema_version from project where id=1").fetchone()[0]

            self.assertEqual(version, 29)
            self.assertEqual(original.read_bytes(), b"schema: 29\noriginal: true\n")
            self.assertEqual(original.stat().st_mode & 0o7777, original_mode)
            self.assertEqual(sha256(original), original_digest)
            self.assertFalse(created_during_failure.exists())
            self.assertEqual(retired_evidence.read_bytes(), b"# Legacy evidence view\n")
            self.assertEqual(retired_evidence.stat().st_mode & 0o7777, retired_mode)
            self.assertEqual(sha256(retired_evidence), retired_digest)
            self.assertEqual(manifest["projection_restore_status"], "restored")
            entries = {
                entry["path"]: entry for entry in manifest["projection_backup"]["entries"]
            }
            self.assertEqual(
                entries[PROJECTION_PATHS[0].as_posix()]["sha256"], original_digest
            )
            self.assertEqual(entries[PROJECTION_PATHS[0].as_posix()]["mode"], original_mode)
            self.assertFalse(
                entries[Path("docs/harness/executions.md").as_posix()]["existed"]
            )
            self.assertEqual(
                entries[Path("docs/harness/evidence.md").as_posix()]["sha256"],
                retired_digest,
            )

    def test_projection_restore_failure_is_never_reported_as_complete_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._prepare_source(root)
            projection = root / ".ai-team/control/project-state.yaml"
            projection.parent.mkdir(parents=True, exist_ok=True)
            projection.write_text("schema: 29\n", encoding="utf-8")

            with (
                patch.object(
                    harness_db,
                    "render_all",
                    side_effect=(None, harness_db.HarnessError("projection publish failed")),
                ),
                patch.object(
                    local_core_migration,
                    "_restore_projection_backup",
                    side_effect=local_core_migration.LocalCoreMigrationError(
                        "projection restore failed"
                    ),
                ),
            ):
                with self.assertRaisesRegex(Exception, "projection restore failed"):
                    harness_db.migrate(root, "29", 30)

            backup_dir = next((root / ".ai-team/backups").glob("schema-29-before-local-core-*"))
            manifest = json.loads((backup_dir / "migration-manifest.json").read_text(encoding="utf-8"))
            with closing(sqlite3.connect(active)) as conn:
                version = conn.execute("select schema_version from project where id=1").fetchone()[0]
            self.assertEqual(version, 29)
            self.assertEqual(sha256(active), manifest["backup"]["sha256"])
            self.assertEqual(manifest["status"], "rollback-incomplete")
            self.assertEqual(manifest["projection_restore_status"], "failed")
            self.assertIn("projection publish failed", manifest["error"])
            self.assertIn("projection restore failed", manifest["projection_restore_error"])

    def test_failed_schema30_move_uses_verified_copy_before_authority_restore(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._prepare_source(root)
            def reject_failed_database_move(
                _project_fs,
                _source: Path,
                _destination: Path,
                **_kwargs,
            ) -> None:
                raise PermissionError(
                    "injected failed-schema30 move denial"
                )

            with patch.object(
                local_core_migration,
                "_move_failed_schema30",
                side_effect=reject_failed_database_move,
            ):
                with self.assertRaises(InjectedLocalCoreMigrationFailure):
                    migrate_project_to_schema30(root, fail_at="after_atomic_replace")

            backup_dir = next((root / ".ai-team/backups").glob("schema-29-before-local-core-*"))
            manifest = json.loads((backup_dir / "migration-manifest.json").read_text(encoding="utf-8"))
            failed = Path(manifest["failed_schema30_path"])
            with closing(sqlite3.connect(active)) as conn:
                active_version = conn.execute("select schema_version from project where id=1").fetchone()[0]
            with closing(sqlite3.connect(failed)) as conn:
                failed_version = conn.execute("select schema_version from project where id=1").fetchone()[0]

            self.assertEqual(active_version, 29)
            self.assertEqual(sha256(active), manifest["backup"]["sha256"])
            self.assertEqual(failed_version, 30)
            self.assertEqual(manifest["status"], "rolled-back")
            self.assertEqual(
                manifest["failed_schema30_preservation_status"],
                "copied-after-move-failure",
            )
            self.assertIn("move denial", manifest["failed_schema30_preservation_error"])

    def test_failed_schema30_diagnostic_loss_cannot_prevent_authority_restore(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._prepare_source(root)
            def reject_failed_database_move(
                _project_fs,
                _source: Path,
                _destination: Path,
                **_kwargs,
            ) -> None:
                raise PermissionError(
                    "injected failed-schema30 move denial"
                )

            def reject_failed_database_copy(
                _project_fs,
                _source: Path,
                _destination: Path,
                **_kwargs,
            ) -> None:
                raise PermissionError(
                    "injected failed-schema30 copy denial"
                )

            with (
                patch.object(
                    local_core_migration,
                    "_move_failed_schema30",
                    side_effect=reject_failed_database_move,
                ),
                patch.object(
                    local_core_migration,
                    "_copy_failed_schema30",
                    side_effect=reject_failed_database_copy,
                ),
            ):
                with self.assertRaisesRegex(
                    LocalCoreMigrationError,
                    "diagnostic preservation was incomplete",
                ):
                    migrate_project_to_schema30(root, fail_at="after_atomic_replace")

            backup_dir = next((root / ".ai-team/backups").glob("schema-29-before-local-core-*"))
            manifest = json.loads((backup_dir / "migration-manifest.json").read_text(encoding="utf-8"))
            with closing(sqlite3.connect(active)) as conn:
                active_version = conn.execute("select schema_version from project where id=1").fetchone()[0]

            self.assertEqual(active_version, 29)
            self.assertEqual(sha256(active), manifest["backup"]["sha256"])
            self.assertEqual(manifest["status"], "rollback-incomplete")
            self.assertEqual(manifest["database_restore_status"], "restored")
            self.assertEqual(manifest["projection_restore_status"], "restored")
            self.assertEqual(manifest["failed_schema30_preservation_status"], "failed")
            self.assertIn("move denial", manifest["failed_schema30_preservation_error"])
            self.assertIn("copy denial", manifest["failed_schema30_preservation_error"])

    def test_failed_schema30_hash_denial_cannot_prevent_authority_restore(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._prepare_source(root)
            original_digest = (
                local_core_migration._diagnostic_database_digest
            )

            def reject_failed_active_digest(
                project_fs,
                relative: Path,
            ) -> str:
                resolved = project_fs.absolute(relative)
                if resolved == active.resolve():
                    with closing(sqlite3.connect(resolved)) as conn:
                        version = int(
                            conn.execute(
                                "select schema_version from project where id=1"
                            ).fetchone()[0]
                        )
                    if version == 30:
                        raise PermissionError("injected active-schema30 read denial")
                return original_digest(project_fs, relative)

            with patch.object(
                local_core_migration,
                "_diagnostic_database_digest",
                side_effect=reject_failed_active_digest,
            ):
                with self.assertRaisesRegex(
                    LocalCoreMigrationError,
                    "diagnostic preservation was incomplete",
                ):
                    migrate_project_to_schema30(root, fail_at="after_atomic_replace")

            backup_dir = next(
                (root / ".ai-team/backups").glob("schema-29-before-local-core-*")
            )
            manifest = json.loads(
                (backup_dir / "migration-manifest.json").read_text(encoding="utf-8")
            )
            with closing(sqlite3.connect(active)) as conn:
                active_version = int(
                    conn.execute("select schema_version from project where id=1").fetchone()[0]
                )

            self.assertEqual(active_version, 29)
            self.assertEqual(sha256(active), manifest["backup"]["sha256"])
            self.assertEqual(manifest["status"], "rollback-incomplete")
            self.assertEqual(manifest["database_restore_status"], "restored")
            self.assertEqual(manifest["projection_restore_status"], "restored")
            self.assertEqual(manifest["failed_schema30_preservation_status"], "failed")
            self.assertIn(
                "active-schema30 read denial",
                manifest["failed_schema30_preservation_error"],
            )

    def test_empty_cancellation_during_failed_schema30_preservation_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._prepare_source(root)
            original_digest = (
                local_core_migration._diagnostic_database_digest
            )

            def interrupt_failed_active_digest(
                project_fs,
                relative: Path,
            ) -> str:
                resolved = project_fs.absolute(relative)
                if resolved == active.resolve() and resolved.is_file():
                    with closing(sqlite3.connect(resolved)) as conn:
                        version = int(
                            conn.execute(
                                "select schema_version from project where id=1"
                            ).fetchone()[0]
                        )
                    if version == 30:
                        raise KeyboardInterrupt()
                return original_digest(project_fs, relative)

            with patch.object(
                local_core_migration,
                "_diagnostic_database_digest",
                side_effect=interrupt_failed_active_digest,
            ):
                with self.assertRaisesRegex(
                    LocalCoreMigrationError,
                    "KeyboardInterrupt",
                ):
                    migrate_project_to_schema30(
                        root,
                        fail_at="after_atomic_replace",
                    )

            backup_dir = next(
                (root / ".ai-team/backups").glob("schema-29-before-local-core-*")
            )
            manifest = json.loads(
                (backup_dir / "migration-manifest.json").read_text(encoding="utf-8")
            )
            with closing(sqlite3.connect(active)) as conn:
                active_version = int(
                    conn.execute(
                        "select schema_version from project where id=1"
                    ).fetchone()[0]
                )

            self.assertEqual(active_version, 29)
            self.assertEqual(manifest["status"], "rollback-incomplete")
            self.assertEqual(manifest["database_restore_status"], "restored")
            self.assertEqual(manifest["projection_restore_status"], "restored")
            self.assertEqual(
                manifest["failed_schema30_preservation_status"],
                "failed",
            )
            self.assertIn(
                "KeyboardInterrupt",
                manifest["failed_schema30_preservation_error"],
            )
            self.assertTrue(
                (root / ".ai-team/state/local-core-migration.lock").is_file()
            )

    def test_failed_schema30_cleanup_denial_cannot_prevent_authority_restore(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._prepare_source(root)
            def reject_failed_database_move(
                _project_fs,
                _source: Path,
                _destination: Path,
            ) -> None:
                raise PermissionError(
                    "injected failed-schema30 move denial"
                )

            def reject_failed_database_copy(
                _project_fs,
                _source: Path,
                _destination: Path,
            ) -> None:
                raise PermissionError(
                    "injected failed-schema30 copy denial"
                )

            def reject_failed_database_cleanup(
                _project_fs,
                _relative: Path,
                _expected,
            ) -> None:
                raise PermissionError(
                    "injected failed-schema30 cleanup denial"
                )

            with (
                patch.object(
                    local_core_migration,
                    "_move_failed_schema30",
                    side_effect=reject_failed_database_move,
                ),
                patch.object(
                    local_core_migration,
                    "_copy_failed_schema30",
                    side_effect=reject_failed_database_copy,
                ),
                patch.object(
                    local_core_migration,
                    "_cleanup_failed_schema30",
                    side_effect=reject_failed_database_cleanup,
                ),
            ):
                with self.assertRaisesRegex(
                    LocalCoreMigrationError,
                    "diagnostic preservation was incomplete",
                ):
                    migrate_project_to_schema30(root, fail_at="after_atomic_replace")

            backup_dir = next(
                (root / ".ai-team/backups").glob("schema-29-before-local-core-*")
            )
            manifest = json.loads(
                (backup_dir / "migration-manifest.json").read_text(encoding="utf-8")
            )
            with closing(sqlite3.connect(active)) as conn:
                active_version = int(
                    conn.execute("select schema_version from project where id=1").fetchone()[0]
                )

            self.assertEqual(active_version, 29)
            self.assertEqual(sha256(active), manifest["backup"]["sha256"])
            self.assertEqual(manifest["status"], "rollback-incomplete")
            self.assertEqual(manifest["database_restore_status"], "restored")
            self.assertEqual(manifest["projection_restore_status"], "restored")
            self.assertIn(
                "cleanup denial",
                manifest["failed_schema30_preservation_error"],
            )

    def test_projection_dry_run_failure_preserves_source_before_activation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._prepare_source(root)
            with patch.object(
                harness_db, "render_all", side_effect=harness_db.HarnessError("projection dry-run failed")
            ):
                with self.assertRaisesRegex(harness_db.HarnessError, "projection dry-run failed"):
                    harness_db.migrate(root, "29", 30)
            backup_dir = next((root / ".ai-team/backups").glob("schema-29-before-local-core-*"))
            manifest = json.loads((backup_dir / "migration-manifest.json").read_text(encoding="utf-8"))
            with closing(sqlite3.connect(active)) as conn:
                version = conn.execute("select schema_version from project where id=1").fetchone()[0]
            self.assertEqual((version, manifest["status"]), (29, "failed-before-activation"))

    def test_successful_activation_keeps_backup_and_valid_schema30(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._prepare_source(root)

            result = migrate_project_to_schema30(root)

            manifest = json.loads(Path(result.migration_manifest_path).read_text(encoding="utf-8"))
            with closing(sqlite3.connect(active)) as conn:
                version = int(conn.execute("select schema_version from project where id=1").fetchone()[0])
                tables = {
                    row[0]
                    for row in conn.execute(
                        "select name from sqlite_master where type='table' and name not like 'sqlite_%'"
                    )
                }
                sentinel = conn.execute("select decision from decisions where id='D-sentinel'").fetchone()[0]
                migration_status = conn.execute(
                    "select status from migrations where to_version=30 order by id desc limit 1"
                ).fetchone()[0]
                foreign_key_issues = conn.execute("pragma foreign_key_check").fetchall()
            with closing(sqlite3.connect(result.backup.backup_path)) as conn:
                backup_version = int(conn.execute("select schema_version from project where id=1").fetchone()[0])
            active_digest = sha256(active)
            migration_sentinel_exists = (
                root / ".ai-team/state/local-core-migration.lock"
            ).exists()

        self.assertEqual(result.source_version, 29)
        self.assertEqual(result.target_version, 30)
        self.assertEqual(result.active_sha256, active_digest)
        self.assertEqual(manifest["status"], "activated")
        self.assertEqual(manifest["projection_restore_status"], "not-needed")
        self.assertEqual(manifest["projection_backup"]["live_projection_count"], 13)
        self.assertEqual(manifest["projection_backup"]["rollback_path_count"], 14)
        self.assertEqual(len(manifest["projection_backup"]["entries"]), 14)
        self.assertEqual(version, 30)
        self.assertEqual(len(tables), 27)
        self.assertEqual(sentinel, "keep")
        self.assertEqual(migration_status, "activated")
        self.assertEqual(foreign_key_issues, [])
        self.assertEqual(backup_version, 29)
        self.assertFalse(migration_sentinel_exists)

    def test_successful_runtime_migration_publishes_and_verifies_all_projections(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._prepare_source(root)
            (root / ".gitignore").write_text(
                "\n".join(harness_db.RUNTIME_GITIGNORE_PATTERNS) + "\n",
                encoding="utf-8",
            )
            retired_evidence = root / "docs/harness/evidence.md"
            retired_evidence.parent.mkdir(parents=True)
            retired_evidence.write_text("# Retired evidence view\n", encoding="utf-8")

            harness_db.migrate(root, "29", 30)

            backup_dir = next((root / ".ai-team/backups").glob("schema-29-before-local-core-*"))
            manifest = json.loads((backup_dir / "migration-manifest.json").read_text(encoding="utf-8"))
            with closing(sqlite3.connect(active)) as conn:
                version = conn.execute("select schema_version from project where id=1").fetchone()[0]

            self.assertEqual(version, 30)
            self.assertEqual(harness_db.doctor(root), [])
            self.assertTrue(all((root / path).is_file() for path in PROJECTION_PATHS))
            self.assertFalse(retired_evidence.exists())
            self.assertEqual(manifest["status"], "activated")
            self.assertEqual(manifest["projection_restore_status"], "not-needed")
            evidence_entry = next(
                entry
                for entry in manifest["projection_backup"]["entries"]
                if entry["path"] == "docs/harness/evidence.md"
            )
            self.assertTrue(evidence_entry["existed"])
            self.assertEqual(len(evidence_entry["sha256"]), 64)

    def test_public_migration_rejects_silent_stale_projection_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._prepare_source(root)
            (root / ".gitignore").write_text(
                "\n".join(harness_db.RUNTIME_GITIGNORE_PATTERNS) + "\n",
                encoding="utf-8",
            )
            stale = b"STALE-SCHEMA-29\n"
            for relative_path in PROJECTION_PATHS:
                projection = root / relative_path
                projection.parent.mkdir(parents=True, exist_ok=True)
                projection.write_bytes(stale)

            with patch.object(harness_db, "render_all", return_value=None):
                with self.assertRaisesRegex(
                    harness_db.HarnessError,
                    "projection verification",
                ):
                    harness_db.migrate(root, "29", 30)

            with closing(sqlite3.connect(active)) as conn:
                version = int(
                    conn.execute("select schema_version from project where id=1").fetchone()[0]
                )
            backup_dir = next(
                (root / ".ai-team/backups").glob("schema-29-before-local-core-*")
            )
            manifest = json.loads(
                (backup_dir / "migration-manifest.json").read_text(encoding="utf-8")
            )

            self.assertEqual(version, 29)
            self.assertEqual(manifest["status"], "rolled-back")
            self.assertEqual(manifest["projection_restore_status"], "restored")
            self.assertTrue(
                all((root / path).read_bytes() == stale for path in PROJECTION_PATHS)
            )

    def test_core_migration_independently_rejects_noop_projection_validator(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._prepare_source(root)
            stale = b"STALE-SCHEMA-29\n"
            for relative_path in PROJECTION_PATHS:
                projection = root / relative_path
                projection.parent.mkdir(parents=True, exist_ok=True)
                projection.write_bytes(stale)

            with self.assertRaisesRegex(
                LocalCoreMigrationError,
                "projection.*verification",
            ):
                _core_migrate_project_to_schema30(
                    root,
                    active_validator=lambda _active_path: None,
                )

            with closing(sqlite3.connect(active)) as conn:
                version = int(
                    conn.execute("select schema_version from project where id=1").fetchone()[0]
                )
            backup_dir = next(
                (root / ".ai-team/backups").glob("schema-29-before-local-core-*")
            )
            manifest = json.loads(
                (backup_dir / "migration-manifest.json").read_text(encoding="utf-8")
            )

            self.assertEqual(version, 29)
            self.assertEqual(manifest["status"], "rolled-back")
            self.assertEqual(manifest["projection_restore_status"], "restored")
            self.assertTrue(
                all((root / path).read_bytes() == stale for path in PROJECTION_PATHS)
            )
            self.assertFalse(
                (root / ".ai-team/state/local-core-migration.lock").exists()
            )

    def test_core_migration_rejects_callback_database_fact_injection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._prepare_source(root)
            stale = b"STALE-SCHEMA-29\n"
            for relative_path in PROJECTION_PATHS:
                projection = root / relative_path
                projection.parent.mkdir(parents=True, exist_ok=True)
                projection.write_bytes(stale)

            def mutate_and_publish(active_path: Path) -> None:
                with closing(sqlite3.connect(active_path)) as conn:
                    conn.execute(
                        "update project set current_owner='callback-injected' where id=1"
                    )
                    conn.commit()
                harness_db.render_all(root)

            with self.assertRaisesRegex(
                LocalCoreMigrationError,
                "callback.*database",
            ):
                _core_migrate_project_to_schema30(
                    root,
                    active_validator=mutate_and_publish,
                )

            with closing(sqlite3.connect(active)) as conn:
                version, owner = conn.execute(
                    "select schema_version, current_owner from project where id=1"
                ).fetchone()
            backup_dir = next(
                (root / ".ai-team/backups").glob("schema-29-before-local-core-*")
            )
            manifest = json.loads(
                (backup_dir / "migration-manifest.json").read_text(encoding="utf-8")
            )

            self.assertEqual((int(version), str(owner)), (29, "project-manager"))
            self.assertEqual(manifest["status"], "rolled-back")
            self.assertEqual(manifest["projection_restore_status"], "restored")
            self.assertTrue(
                all((root / path).read_bytes() == stale for path in PROJECTION_PATHS)
            )
            self.assertFalse(
                (root / ".ai-team/state/local-core-migration.lock").exists()
            )

    def test_live_schema30_wal_cannot_corrupt_reported_complete_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._prepare_source(root)
            leaked: list[sqlite3.Connection] = []

            def fail_with_live_wal(active_path: Path) -> None:
                conn = sqlite3.connect(active_path)
                leaked.append(conn)
                conn.execute("pragma journal_mode = wal")
                conn.execute(
                    "update project set updated_at='schema30-live-wal' where id=1"
                )
                conn.commit()
                self.assertGreater(
                    Path(str(active_path) + "-wal").stat().st_size,
                    0,
                )
                raise RuntimeError("schema30-live-wal-validator-failure")

            caught: BaseException | None = None
            try:
                try:
                    _core_migrate_project_to_schema30(
                        root,
                        active_validator=fail_with_live_wal,
                    )
                except BaseException as exc:  # cancellation/handle behavior is platform-specific
                    caught = exc

                sentinel = root / ".ai-team/state/local-core-migration.lock"
                wal = Path(str(active) + "-wal")
                shm = Path(str(active) + "-shm")
                self.assertIsNotNone(caught)
                if sentinel.exists():
                    payload = json.loads(sentinel.read_text(encoding="utf-8"))
                    self.assertEqual(payload["status"], "rollback-incomplete")
                    with self.assertRaisesRegex(
                        ProjectOperationLockError,
                        "rollback-incomplete",
                    ):
                        with SqliteStore(root).connection():
                            self.fail("Store opened an incomplete WAL recovery state")
                else:
                    self.assertFalse(wal.exists(), "schema-30 WAL survived complete rollback")
                    self.assertFalse(shm.exists(), "schema-30 SHM survived complete rollback")
                    with closing(sqlite3.connect(active)) as conn:
                        version = int(
                            conn.execute(
                                "select schema_version from project where id=1"
                            ).fetchone()[0]
                        )
                        integrity = conn.execute("pragma integrity_check").fetchone()[0]
                        foreign_keys = conn.execute("pragma foreign_key_check").fetchall()
                    self.assertEqual((version, integrity, foreign_keys), (29, "ok", []))
            finally:
                for conn in leaked:
                    conn.close()


class Schema30FactPreservationTests(unittest.TestCase):
    def test_cycle_candidate_gate_risk_finding_invalidation_and_delivery_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            init_schema29_fixture(root)
            source = root / ".ai-team/state/harness.db"
            with closing(sqlite3.connect(source)) as conn:
                conn.execute("pragma foreign_keys = on")
                conn.execute(
                    """
                    update delivery_cycles
                    set status='delivered', candidate_sha='candidate-old', closed_at='2026-07-10T00:00:00Z'
                    where id='CYCLE-current'
                    """
                )
                conn.execute(
                    """
                    insert into delivery_cycles
                    (id, name, goal, status, phase, base_ref, candidate_sha, started_at, closed_at, created_at, updated_at)
                    values ('CYCLE-next', 'Next', 'Current candidate', 'active', 'qa', 'main', 'candidate-new',
                            '2026-07-11T00:00:00Z', '', '2026-07-11T00:00:00Z', '2026-07-11T00:00:00Z')
                    """
                )
                for cycle_id, body, criterion, task_status in (
                    ("CYCLE-current", "old requirement", "old acceptance", "accepted"),
                    ("CYCLE-next", "new requirement", "new acceptance", "planned"),
                ):
                    conn.execute(
                        """
                        insert into requirements (id, cycle_id, kind, body, status, updated_at)
                        values ('R1', ?, 'functional', ?, 'active', 'now')
                        """,
                        (cycle_id, body),
                    )
                    conn.execute(
                        "insert into acceptance (id, cycle_id, criterion, status) values ('AC1', ?, ?, 'active')",
                        (cycle_id, criterion),
                    )
                    conn.execute(
                        """
                        insert into failure_modes
                        (id, cycle_id, feature, scenario, trigger, expected_behavior, risk, status,
                         accepted_by, acceptance_reason, acceptance_scope, accepted_revision, expires_at)
                        values ('FM1', ?, 'delivery', 'risk', 'migration', 'preserve', 'high', ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            cycle_id,
                            "resolved" if cycle_id == "CYCLE-current" else "accepted",
                            "" if cycle_id == "CYCLE-current" else "user",
                            "" if cycle_id == "CYCLE-current" else "temporary acceptance",
                            "" if cycle_id == "CYCLE-current" else "candidate",
                            None if cycle_id == "CYCLE-current" else 9,
                            None if cycle_id == "CYCLE-current" else "2000-01-01T00:00:00Z",
                        ),
                    )
                    conn.execute(
                        """
                        insert into tasks (id, cycle_id, task, owner, status, updated_at)
                        values ('T1', ?, 'cycle task', 'developer', ?, 'now')
                        """,
                        (cycle_id, task_status),
                    )
                    conn.execute(
                        "insert into requirement_acceptance (cycle_id, requirement_id, acceptance_id) values (?, 'R1', 'AC1')",
                        (cycle_id,),
                    )
                    conn.execute(
                        "insert into failure_mode_acceptance (cycle_id, failure_mode_id, acceptance_id) values (?, 'FM1', 'AC1')",
                        (cycle_id,),
                    )
                    conn.execute(
                        "insert into task_acceptance (cycle_id, task_id, acceptance_id) values (?, 'T1', 'AC1')",
                        (cycle_id,),
                    )
                    conn.execute(
                        "insert into task_failure_modes (cycle_id, task_id, failure_mode_id) values (?, 'T1', 'FM1')",
                        (cycle_id,),
                    )
                conn.execute(
                    """
                    insert into findings
                    (id, cycle_id, candidate_sha, surface, severity, status, summary, created_at)
                    values ('F-old', 'CYCLE-current', 'candidate-old', 'delivery', 'high', 'resolved', 'old resolved', 'now')
                    """
                )
                conn.execute(
                    """
                    insert into findings
                    (id, cycle_id, candidate_sha, surface, severity, status, summary, waiver_expires_at, created_at)
                    values ('F-new', 'CYCLE-next', 'candidate-new', 'delivery', 'critical', 'open', 'current blocker',
                            '2000-01-01T00:00:00Z', 'now')
                    """
                )
                gate_rows = (
                    ("G-old", 1, "CYCLE-current", "candidate-old", "active", "", "pass", "F-old"),
                    ("G-pass", 2, "CYCLE-next", "candidate-new", "superseded", "G-fail", "pass", ""),
                    ("G-fail", 3, "CYCLE-next", "candidate-new", "active", "", "fail", "F-new"),
                )
                for gate_id, sequence, cycle_id, candidate, gate_status, superseded_by, result, finding_id in gate_rows:
                    conn.execute(
                        """
                        insert into quality_gates
                        (id, sequence, cycle_id, candidate_sha, gate_status, superseded_by, gate,
                         reviewed_commit, reviewer_context, result, project_revision, created_at)
                        values (?, ?, ?, ?, ?, ?, 'independent_qa', ?, 'same-context-degraded', ?, 9, 'now')
                        """,
                        (gate_id, sequence, cycle_id, candidate, gate_status, superseded_by, candidate, result),
                    )
                    if finding_id:
                        conn.execute(
                            "insert into quality_gate_findings (gate_id, finding_id) values (?, ?)",
                            (gate_id, finding_id),
                        )
                conn.execute(
                    """
                    insert into deliveries
                    (id, cycle_id, candidate_sha, scope, acceptance, validation, qa, quality_gate, created_at)
                    values ('D-old', 'CYCLE-current', 'candidate-old', 'historical delivery', 'AC1',
                            'verified', 'reviewed', 'G-old', '2026-07-10T00:00:00Z')
                    """
                )
                conn.execute(
                    """
                    insert into delivery_acceptance (delivery_id, cycle_id, acceptance_id)
                    values ('D-old', 'CYCLE-current', 'AC1')
                    """
                )
                conn.execute(
                    """
                    insert into invalidations
                    (id, cycle_id, source_type, source_id, target_type, target_id, reason, created_at)
                    values ('I1', 'CYCLE-next', 'acceptance', 'AC1', 'task', 'T1', 'acceptance changed', 'now')
                    """
                )
                conn.execute(
                    """
                    update project set current_cycle_id='CYCLE-next', phase='qa', revision=9, updated_at='now'
                    where id=1
                    """
                )
                conn.commit()

            staging = root / ".ai-team/backups/test/harness.schema30.new.db"
            stage_schema29_to_schema30(source, staging)
            with closing(sqlite3.connect(staging)) as conn:
                current = conn.execute(
                    "select current_cycle_id, phase, revision from project where id=1"
                ).fetchone()
                cycles = conn.execute(
                    "select id, status, candidate_sha from delivery_cycles order by id"
                ).fetchall()
                requirements = conn.execute(
                    "select cycle_id, id, body from requirements where id='R1' order by cycle_id"
                ).fetchall()
                tasks = conn.execute(
                    "select cycle_id, id, status from tasks where id='T1' order by cycle_id"
                ).fetchall()
                risks = conn.execute(
                    """
                    select cycle_id, status, accepted_by, acceptance_reason, acceptance_scope,
                           accepted_revision, expires_at
                    from failure_modes where id='FM1' order by cycle_id
                    """
                ).fetchall()
                gates = conn.execute(
                    "select id, sequence, gate_status, superseded_by, result from quality_gates order by sequence"
                ).fetchall()
                findings = conn.execute(
                    "select id, cycle_id, candidate_sha, severity, status, summary from findings order by id"
                ).fetchall()
                gate_findings = conn.execute(
                    "select gate_id, finding_id from quality_gate_findings order by gate_id"
                ).fetchall()
                invalidations = conn.execute(
                    "select id, cycle_id, source_type, source_id, target_type, target_id, reason from invalidations"
                ).fetchall()
                deliveries = conn.execute(
                    "select id, cycle_id, candidate_sha, scope, decision_status from deliveries"
                ).fetchall()
                delivery_links = conn.execute(
                    "select delivery_id, cycle_id, acceptance_id from delivery_acceptance"
                ).fetchall()
                foreign_key_issues = conn.execute("pragma foreign_key_check").fetchall()

        self.assertEqual(current, ("CYCLE-next", "qa", 9))
        self.assertEqual(
            cycles,
            [
                ("CYCLE-current", "delivered", "candidate-old"),
                ("CYCLE-next", "active", "candidate-new"),
            ],
        )
        self.assertEqual(
            requirements,
            [
                ("CYCLE-current", "R1", "old requirement"),
                ("CYCLE-next", "R1", "new requirement"),
            ],
        )
        self.assertEqual(
            tasks,
            [
                ("CYCLE-current", "T1", "accepted"),
                ("CYCLE-next", "T1", "planned"),
            ],
        )
        self.assertEqual(risks[1], (
            "CYCLE-next",
            "accepted",
            "user",
            "temporary acceptance",
            "candidate",
            9,
            "2000-01-01T00:00:00Z",
        ))
        self.assertEqual(
            gates,
            [
                ("G-old", 1, "active", None, "pass"),
                ("G-pass", 2, "superseded", "G-fail", "pass"),
                ("G-fail", 3, "active", None, "fail"),
            ],
        )
        self.assertEqual(
            findings,
            [
                ("F-new", "CYCLE-next", "candidate-new", "critical", "open", "current blocker"),
                ("F-old", "CYCLE-current", "candidate-old", "high", "resolved", "old resolved"),
            ],
        )
        self.assertEqual(gate_findings, [("G-fail", "F-new"), ("G-old", "F-old")])
        self.assertEqual(
            invalidations,
            [("I1", "CYCLE-next", "acceptance", "AC1", "task", "T1", "acceptance changed")],
        )
        self.assertEqual(
            deliveries,
            [("D-old", "CYCLE-current", "candidate-old", "historical delivery", "historical-migrated")],
        )
        self.assertEqual(delivery_links, [("D-old", "CYCLE-current", "AC1")])
        self.assertEqual(foreign_key_issues, [])


if __name__ == "__main__":
    unittest.main()

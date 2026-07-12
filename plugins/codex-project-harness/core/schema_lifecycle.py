"""Transactional SQLite schema creation and compatibility columns."""

from __future__ import annotations

import json
import hashlib
import os
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from harness_lib import now_iso
from . import RUNTIME_VERSION, SCHEMA_VERSION
from .errors import HarnessError
from .store import project_db_operation


class SchemaLifecycleError(HarnessError):
    """Raised when schema SQL cannot be applied transactionally."""


DEFAULT_EXECUTOR_PREFIXES = [
    "python3 -m unittest",
    "python3 -B -m unittest",
    "python3 -m pytest",
    "pytest",
    "npm test",
    "pnpm test",
    "yarn test",
    "make test",
    "make lint",
    "go test",
    "cargo test",
    "dotnet test",
]


SCHEMA30_VERSION = SCHEMA_VERSION
SCHEMA30_RUNTIME_VERSION = RUNTIME_VERSION
SCHEMA30_TABLES = frozenset(
    {
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
)
SCHEMA30_JSON_SCHEMAS = frozenset(
    {
        "project-state.schema.json",
        "delivery-cycle.schema.json",
        "requirement.schema.json",
        "acceptance.schema.json",
        "failure-mode.schema.json",
        "baseline.schema.json",
        "task.schema.json",
        "task-test-target.schema.json",
        "test-target.schema.json",
        "execution.schema.json",
        "validation.schema.json",
        "finding.schema.json",
        "quality-gate.schema.json",
        "delivery.schema.json",
        "invalidation.schema.json",
        "event.schema.json",
    }
)


SCHEMA30_DDL = """
create table project (
    id integer primary key check (id = 1),
    project_id text not null,
    schema_version integer not null check (schema_version = __SCHEMA_VERSION__),
    runtime_version text not null,
    phase text not null,
    current_cycle_id text not null default '',
    status text not null,
    scope_status text not null,
    current_owner text not null,
    revision integer not null check (revision > 0),
    updated_at text not null
);
create table delivery_cycles (
    id text primary key,
    name text not null,
    goal text not null,
    status text not null,
    phase text not null,
    base_ref text not null default '',
    candidate_sha text not null default '',
    started_at text not null,
    closed_at text not null default '',
    created_at text not null,
    updated_at text not null
);
create table requirements (
    uid text primary key default (lower(hex(randomblob(16)))),
    id text not null,
    cycle_id text not null,
    kind text not null,
    body text not null,
    priority text not null default '',
    status text not null default 'active',
    revision integer not null default 1 check (revision > 0),
    updated_at text not null,
    unique(cycle_id, id),
    foreign key (cycle_id) references delivery_cycles(id) on delete cascade
);
create table acceptance (
    uid text primary key default (lower(hex(randomblob(16)))),
    id text not null,
    cycle_id text not null,
    criterion text not null,
    priority text not null default '',
    status text not null default 'active',
    revision integer not null default 1 check (revision > 0),
    unique(cycle_id, id),
    foreign key (cycle_id) references delivery_cycles(id) on delete cascade
);
create table requirement_acceptance (
    cycle_id text not null,
    requirement_id text not null,
    acceptance_id text not null,
    primary key (cycle_id, requirement_id, acceptance_id),
    foreign key (cycle_id, requirement_id) references requirements(cycle_id, id) on delete cascade,
    foreign key (cycle_id, acceptance_id) references acceptance(cycle_id, id) on delete cascade
);
create table failure_modes (
    uid text primary key default (lower(hex(randomblob(16)))),
    id text not null,
    cycle_id text not null,
    feature text not null,
    scenario text not null,
    trigger text not null,
    expected_behavior text not null,
    recovery text not null default '',
    data_safety text not null default '',
    risk text not null check (risk in ('low', 'medium', 'high', 'critical')),
    status text not null default 'active',
    accepted_by text not null default '',
    acceptance_reason text not null default '',
    acceptance_scope text not null default '',
    accepted_revision integer,
    expires_at text not null default '',
    revision integer not null default 1 check (revision > 0),
    unique(cycle_id, id),
    foreign key (cycle_id) references delivery_cycles(id) on delete cascade
);
create table failure_mode_acceptance (
    cycle_id text not null,
    failure_mode_id text not null,
    acceptance_id text not null,
    primary key (cycle_id, failure_mode_id, acceptance_id),
    foreign key (cycle_id, failure_mode_id) references failure_modes(cycle_id, id) on delete cascade,
    foreign key (cycle_id, acceptance_id) references acceptance(cycle_id, id) on delete cascade
);
create table baselines (
    id text primary key,
    cycle_id text not null,
    summary text not null,
    snapshot_json text not null,
    digest text not null,
    project_revision integer not null,
    created_by text not null default '',
    created_at text not null,
    foreign key (cycle_id) references delivery_cycles(id) on delete cascade
);
create table tasks (
    uid text primary key default (lower(hex(randomblob(16)))),
    id text not null,
    cycle_id text not null,
    task text not null,
    owner text not null default '',
    status text not null default 'planned'
        check (status in ('planned', 'active', 'submitted', 'accepted', 'blocked', 'cancelled')),
    evidence text not null default '',
    submitted_context_id text not null default '',
    accepted_by text not null default '',
    revision integer not null default 1 check (revision > 0),
    updated_at text not null,
    unique(cycle_id, id),
    foreign key (cycle_id) references delivery_cycles(id) on delete cascade
);
create table task_acceptance (
    cycle_id text not null,
    task_id text not null,
    acceptance_id text not null,
    primary key (cycle_id, task_id, acceptance_id),
    foreign key (cycle_id, task_id) references tasks(cycle_id, id) on delete cascade,
    foreign key (cycle_id, acceptance_id) references acceptance(cycle_id, id) on delete cascade
);
create table task_failure_modes (
    cycle_id text not null,
    task_id text not null,
    failure_mode_id text not null,
    primary key (cycle_id, task_id, failure_mode_id),
    foreign key (cycle_id, task_id) references tasks(cycle_id, id) on delete cascade,
    foreign key (cycle_id, failure_mode_id) references failure_modes(cycle_id, id) on delete cascade
);
create table task_dependencies (
    cycle_id text not null,
    task_id text not null,
    depends_on text not null,
    primary key (cycle_id, task_id, depends_on),
    foreign key (cycle_id, task_id) references tasks(cycle_id, id) on delete cascade,
    foreign key (cycle_id, depends_on) references tasks(cycle_id, id) on delete restrict,
    check (task_id != depends_on)
);
create table test_targets (
    id text primary key,
    kind text not null,
    command_template text not null,
    description text not null default '',
    gateable integer not null default 1 check (gateable in (0, 1)),
    gate_block_reason text not null default '',
    stack_profile text not null default 'python',
    container_image text not null default '',
    requires_sandbox integer not null default 0 check (requires_sandbox in (0, 1)),
    requires_no_network integer not null default 0 check (requires_no_network in (0, 1)),
    result_format text not null default 'regex',
    result_path text not null default '',
    created_at text not null,
    updated_at text not null
);
create table task_test_targets (
    cycle_id text not null,
    task_id text not null,
    target_id text not null,
    primary key (cycle_id, task_id, target_id),
    foreign key (cycle_id, task_id) references tasks(cycle_id, id) on delete cascade,
    foreign key (target_id) references test_targets(id) on delete cascade
);
create table executions (
    id text primary key,
    cycle_id text not null,
    candidate_sha text not null,
    target_id text,
    command text not null,
    exit_code integer not null,
    stdout_sha256 text not null,
    artifact_path text not null default '',
    executed_count integer not null check (executed_count >= 0),
    result_format text not null,
    semantic_status text not null,
    runner text not null check (runner in ('local', 'container')),
    sandbox_status text not null,
    no_network integer not null default 0 check (no_network in (0, 1)),
    policy_status text not null,
    created_at text not null,
    foreign key (cycle_id) references delivery_cycles(id) on delete cascade,
    foreign key (target_id) references test_targets(id) on delete restrict,
    unique (id, cycle_id, candidate_sha)
);
create trigger executions_no_update
before update on executions
begin
    select raise(abort, 'executions are immutable');
end;
create trigger executions_no_delete
before delete on executions
begin
    select raise(abort, 'executions are immutable');
end;
create table validations (
    id text primary key,
    cycle_id text not null,
    candidate_sha text not null,
    acceptance_id text,
    surface text not null,
    result text not null,
    validation_status text not null default 'active',
    superseded_by text,
    findings text not null default '',
    residual_risk text not null default '',
    created_at text not null,
    foreign key (cycle_id) references delivery_cycles(id) on delete cascade,
    foreign key (cycle_id, acceptance_id) references acceptance(cycle_id, id) on delete restrict,
    foreign key (superseded_by) references validations(id) on delete set null,
    unique (id, cycle_id, candidate_sha)
);
create table validation_executions (
    validation_id text not null,
    execution_id text not null,
    cycle_id text not null,
    candidate_sha text not null,
    primary key (validation_id, execution_id),
    foreign key (validation_id, cycle_id, candidate_sha)
        references validations(id, cycle_id, candidate_sha) on delete cascade,
    foreign key (execution_id, cycle_id, candidate_sha)
        references executions(id, cycle_id, candidate_sha) on delete restrict
);
create table validation_failure_modes (
    validation_id text not null,
    cycle_id text not null,
    failure_mode_id text not null,
    primary key (validation_id, cycle_id, failure_mode_id),
    foreign key (validation_id) references validations(id) on delete cascade,
    foreign key (cycle_id, failure_mode_id) references failure_modes(cycle_id, id) on delete cascade
);
create table findings (
    id text primary key,
    cycle_id text not null,
    candidate_sha text not null default '',
    surface text not null,
    severity text not null check (severity in ('low', 'medium', 'high', 'critical')),
    status text not null,
    summary text not null,
    waived_by text not null default '',
    waiver_reason text not null default '',
    waiver_scope text not null default '',
    waived_revision integer,
    waiver_expires_at text not null default '',
    created_at text not null,
    foreign key (cycle_id) references delivery_cycles(id) on delete cascade
);
create table quality_gates (
    id text primary key,
    sequence integer not null unique,
    cycle_id text not null,
    candidate_sha text not null,
    gate_status text not null default 'active',
    superseded_by text,
    gate text not null,
    producer_context_id text not null default '',
    reviewer_context_id text not null default '',
    review_status text not null default 'same-context-degraded',
    result text not null,
    blocking_findings text not null default '',
    residual_risk text not null default '',
    reviewed_revision integer not null default 0,
    created_at text not null,
    foreign key (cycle_id) references delivery_cycles(id) on delete cascade,
    foreign key (superseded_by) references quality_gates(id) on delete set null
);
create table quality_gate_findings (
    gate_id text not null,
    finding_id text not null,
    primary key (gate_id, finding_id),
    foreign key (gate_id) references quality_gates(id) on delete cascade,
    foreign key (finding_id) references findings(id) on delete cascade
);
create table deliveries (
    id text primary key,
    cycle_id text not null,
    candidate_sha text not null,
    scope text not null,
    acceptance text not null default '',
    changed_files text not null default '',
    validation text not null default '',
    qa text not null default '',
    failure_mode_coverage text not null default '',
    quality_gate text not null default '',
    data_config_notes text not null default '',
    known_gaps text not null default '',
    handoff text not null default '',
    decision_status text not null default 'delivered',
    created_at text not null,
    foreign key (cycle_id) references delivery_cycles(id) on delete cascade
);
create table delivery_acceptance (
    delivery_id text not null,
    cycle_id text not null,
    acceptance_id text not null,
    primary key (delivery_id, cycle_id, acceptance_id),
    foreign key (delivery_id) references deliveries(id) on delete cascade,
    foreign key (cycle_id, acceptance_id) references acceptance(cycle_id, id) on delete restrict
);
create table decisions (
    id text primary key,
    cycle_id text not null default '',
    candidate_sha text not null default '',
    decision text not null,
    reason text not null,
    created_at text not null
);
create table invalidations (
    id text primary key,
    cycle_id text not null,
    source_type text not null,
    source_id text not null,
    target_type text not null,
    target_id text not null,
    reason text not null,
    resolved_at text,
    created_at text not null,
    foreign key (cycle_id) references delivery_cycles(id) on delete cascade
);
create table migrations (
    id integer primary key autoincrement,
    from_version integer not null,
    to_version integer not null,
    source_sha256 text not null default '',
    backup_path text not null default '',
    manifest_path text not null default '',
    row_counts_json text not null default '{}',
    dropped_table_count integer not null default 0,
    status text not null,
    applied_at text not null
);
create table events (
    sequence integer primary key autoincrement,
    id text not null unique,
    schema_version integer not null check (schema_version = __SCHEMA_VERSION__),
    event_type text not null,
    entity_type text not null default '',
    entity_id text not null default '',
    actor text not null default '',
    command text not null default '',
    before_json text not null default '{}',
    after_json text not null default '{}',
    correlation_id text not null default '',
    created_at text not null
);
create trigger events_no_update
before update on events
begin
    select raise(abort, 'events are append-only');
end;
create trigger events_no_delete
before delete on events
begin
    select raise(abort, 'events are append-only');
end;
create index requirements_cycle_status on requirements(cycle_id, status);
create index acceptance_cycle_status on acceptance(cycle_id, status);
create index failure_modes_cycle_risk_status on failure_modes(cycle_id, risk, status);
create index tasks_cycle_status on tasks(cycle_id, status);
create index executions_cycle_candidate on executions(cycle_id, candidate_sha, created_at);
create index validations_cycle_candidate on validations(cycle_id, candidate_sha, validation_status);
create index findings_cycle_candidate_status on findings(cycle_id, candidate_sha, status, severity);
create index quality_gates_cycle_candidate_sequence on quality_gates(cycle_id, candidate_sha, sequence desc);
create index deliveries_cycle_candidate on deliveries(cycle_id, candidate_sha);
create index invalidations_cycle_target on invalidations(cycle_id, target_type, target_id);
create index events_entity on events(entity_type, entity_id, sequence);
""".replace("__SCHEMA_VERSION__", str(SCHEMA30_VERSION))


def create_schema30(conn: sqlite3.Connection) -> None:
    """Create only the schema 30 local delivery Kernel in an empty staging DB."""

    existing = {
        str(row[0])
        for row in conn.execute(
            "select name from sqlite_master where type = 'table' and name not like 'sqlite_%'"
        )
    }
    if existing:
        raise SchemaLifecycleError(
            "schema 30 creation requires an empty staging database; found: "
            + ", ".join(sorted(existing))
        )
    execute_transactional_script(conn, SCHEMA30_DDL)
    actual = {
        str(row[0])
        for row in conn.execute(
            "select name from sqlite_master where type = 'table' and name not like 'sqlite_%'"
        )
    }
    if actual != SCHEMA30_TABLES:
        missing = sorted(SCHEMA30_TABLES - actual)
        extra = sorted(actual - SCHEMA30_TABLES)
        raise SchemaLifecycleError(f"schema 30 table inventory mismatch: missing={missing} extra={extra}")


@dataclass(frozen=True)
class SQLiteBackupManifest:
    source_version: int
    target_version: int
    created_at: str
    backup_path: str
    sha256: str
    row_counts: dict[str, int]
    source_integrity_check: tuple[str, ...]
    source_foreign_key_issue_count: int
    backup_integrity_check: tuple[str, ...]
    backup_foreign_key_issue_count: int
    manifest_path: str

    def safe_payload(self) -> dict[str, object]:
        payload = asdict(self)
        payload["source_integrity_check"] = list(self.source_integrity_check)
        payload["backup_integrity_check"] = list(self.backup_integrity_check)
        return payload


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _user_tables(conn: sqlite3.Connection) -> tuple[str, ...]:
    return tuple(
        str(row[0])
        for row in conn.execute(
            "select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name"
        )
    )


def _database_row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(conn.execute(f"select count(*) from {_quote_identifier(table)}").fetchone()[0])
        for table in _user_tables(conn)
    }


def _database_integrity(conn: sqlite3.Connection) -> tuple[tuple[str, ...], int]:
    integrity = tuple(str(row[0]) for row in conn.execute("pragma integrity_check"))
    foreign_key_issue_count = sum(1 for _ in conn.execute("pragma foreign_key_check"))
    return integrity, foreign_key_issue_count


def _schema_version(conn: sqlite3.Connection) -> int:
    project_exists = conn.execute(
        "select 1 from sqlite_master where type='table' and name='project'"
    ).fetchone()
    if project_exists is None:
        raise SchemaLifecycleError("cannot back up database without project schema metadata")
    row = conn.execute("select schema_version from project where id = 1").fetchone()
    if row is None:
        raise SchemaLifecycleError("cannot back up database without project row")
    return int(row[0])


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            pass
    finally:
        os.close(descriptor)


def _unique_backup_directory(backups_root: Path, source_version: int, timestamp: str) -> Path:
    stem = f"schema-{source_version}-before-local-core-{timestamp}"
    candidate = backups_root / stem
    suffix = 0
    while candidate.exists():
        suffix += 1
        candidate = backups_root / f"{stem}-{suffix}"
    candidate.mkdir(parents=True, mode=0o700)
    return candidate


def backup_sqlite_database(
    root: Path,
    *,
    source_path: Path | None = None,
    expected_source_version: int | None = None,
    created_at: str | None = None,
) -> SQLiteBackupManifest:
    """Create a consistent, digested recovery backup without exporting row payloads."""

    with project_db_operation(root):
        return _backup_sqlite_database_locked(
            root,
            source_path=source_path,
            expected_source_version=expected_source_version,
            created_at=created_at,
        )


def _backup_sqlite_database_locked(
    root: Path,
    *,
    source_path: Path | None = None,
    expected_source_version: int | None = None,
    created_at: str | None = None,
) -> SQLiteBackupManifest:
    """Implement backup while the caller owns the project operation lock."""

    root = root.resolve()
    source = (source_path or (root / ".ai-team/state/harness.db")).resolve()
    if not source.is_file():
        raise SchemaLifecycleError(f"runtime database is missing: {source}")

    created_at_value = created_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    timestamp = (
        created_at_value.replace("-", "")
        .replace(":", "")
        .replace("T", "T")
        .removesuffix("Z")
        + "Z"
    )
    backups_root = root / ".ai-team/backups"
    backups_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup_dir: Path | None = None
    partial_path: Path | None = None
    try:
        source_uri = f"file:{source.as_posix()}?mode=ro"
        with closing(sqlite3.connect(source_uri, uri=True, timeout=5.0)) as source_conn:
            source_conn.execute("pragma query_only = on")
            source_version = _schema_version(source_conn)
            if expected_source_version is not None and source_version != expected_source_version:
                raise SchemaLifecycleError(
                    f"source schema changed before backup: expected {expected_source_version}, actual {source_version}"
                )
            source_integrity, source_fk_issues = _database_integrity(source_conn)
            if source_integrity != ("ok",) or source_fk_issues:
                raise SchemaLifecycleError(
                    "source database failed pre-backup validation: "
                    f"integrity={list(source_integrity)} foreign_key_issues={source_fk_issues}"
                )
            source_counts = _database_row_counts(source_conn)
            backup_dir = _unique_backup_directory(backups_root, source_version, timestamp)
            partial_path = backup_dir / "harness.db.partial"
            with closing(sqlite3.connect(partial_path)) as destination_conn:
                source_conn.backup(destination_conn)
                destination_conn.execute("pragma journal_mode = delete")
                destination_conn.commit()

        final_path = backup_dir / "harness.db"
        os.replace(partial_path, final_path)
        partial_path = None
        os.chmod(final_path, 0o600)
        _fsync_file(final_path)

        backup_uri = f"file:{final_path.as_posix()}?mode=ro"
        with closing(sqlite3.connect(backup_uri, uri=True, timeout=5.0)) as backup_conn:
            backup_integrity, backup_fk_issues = _database_integrity(backup_conn)
            backup_counts = _database_row_counts(backup_conn)
            backup_version = _schema_version(backup_conn)
        if backup_integrity != ("ok",) or backup_fk_issues:
            raise SchemaLifecycleError(
                "backup database failed validation: "
                f"integrity={list(backup_integrity)} foreign_key_issues={backup_fk_issues}"
            )
        if backup_version != source_version or backup_counts != source_counts:
            raise SchemaLifecycleError(
                "backup database does not match source metadata: "
                f"version={backup_version}/{source_version} rows_equal={backup_counts == source_counts}"
            )

        manifest_path = backup_dir / "backup-manifest.json"
        manifest = SQLiteBackupManifest(
            source_version=source_version,
            target_version=SCHEMA30_VERSION,
            created_at=created_at_value,
            backup_path=str(final_path),
            sha256=_sha256_file(final_path),
            row_counts=source_counts,
            source_integrity_check=source_integrity,
            source_foreign_key_issue_count=source_fk_issues,
            backup_integrity_check=backup_integrity,
            backup_foreign_key_issue_count=backup_fk_issues,
            manifest_path=str(manifest_path),
        )
        manifest_path.write_text(
            json.dumps(manifest.safe_payload(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(manifest_path, 0o600)
        _fsync_file(manifest_path)
        _fsync_directory(backup_dir)
        _fsync_directory(backups_root)
        return manifest
    except Exception:
        if partial_path is not None:
            partial_path.unlink(missing_ok=True)
        if backup_dir is not None and backup_dir.exists() and not any(backup_dir.iterdir()):
            backup_dir.rmdir()
        raise


def execute_transactional_script(conn: sqlite3.Connection, script: str) -> None:
    if not conn.in_transaction:
        raise SchemaLifecycleError("schema SQL requires an active transaction")
    statement = ""
    for character in script:
        statement += character
        if character != ";" or not sqlite3.complete_statement(statement):
            continue
        sql = statement.strip()
        if sql:
            conn.execute(sql)
        statement = ""
    if statement.strip():
        raise SchemaLifecycleError("incomplete schema SQL statement")


def ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    columns = {row["name"] for row in conn.execute(f"pragma table_info({table})")}
    if column not in columns:
        conn.execute(f"alter table {table} add column {column} {ddl}")


def ensure_default_executor_allowlist(conn: sqlite3.Connection) -> None:
    for prefix in DEFAULT_EXECUTOR_PREFIXES:
        conn.execute(
            """
            insert into executor_allowlist (id, prefix, reason, created_at)
            values (?, ?, ?, ?)
            on conflict(prefix) do nothing
            """,
            (f"default-{hashlib.sha256(prefix.encode('utf-8')).hexdigest()[:12]}", prefix, "default safe test prefix", now_iso()),
        )


def create_schema(conn: sqlite3.Connection) -> None:
    execute_transactional_script(
        conn,
        """
        create table if not exists project (
            id integer primary key check (id = 1),
            project_id text not null,
            schema_version integer not null,
            runtime_version text not null,
            phase text not null,
            current_cycle_id text not null default '',
            status text not null,
            scope_status text not null,
            current_owner text not null,
            revision integer not null,
            updated_at text not null
        );
        create table if not exists delivery_cycles (
            id text primary key,
            name text not null,
            goal text not null,
            status text not null,
            phase text not null,
            base_ref text not null default '',
            candidate_sha text not null default '',
            started_at text not null,
            closed_at text not null default '',
            created_at text not null,
            updated_at text not null
        );
        create table if not exists acceptance (
            uid text primary key default (lower(hex(randomblob(16)))),
            id text not null,
            cycle_id text not null default '',
            criterion text not null,
            priority text not null default '',
            status text not null default 'active',
            revision integer not null default 1,
            unique(cycle_id, id)
        );
        create table if not exists requirements (
            uid text primary key default (lower(hex(randomblob(16)))),
            id text not null,
            cycle_id text not null default '',
            kind text not null,
            body text not null,
            priority text not null default '',
            status text not null default 'active',
            revision integer not null default 1,
            updated_at text not null,
            unique(cycle_id, id)
        );
        create table if not exists failure_modes (
            uid text primary key default (lower(hex(randomblob(16)))),
            id text not null,
            cycle_id text not null default '',
            feature text not null,
            scenario text not null,
            trigger text not null,
            expected_behavior text not null,
            recovery text not null default '',
            data_safety text not null default '',
            risk text not null,
            status text not null,
            accepted_by text,
            acceptance_reason text,
            acceptance_scope text not null default '',
            accepted_revision integer,
            expires_at text,
            revision integer not null default 1,
            unique(cycle_id, id)
        );
        create table if not exists requirement_acceptance (
            cycle_id text not null,
            requirement_id text not null,
            acceptance_id text not null,
            primary key (cycle_id, requirement_id, acceptance_id),
            foreign key (cycle_id, requirement_id) references requirements(cycle_id, id) on delete cascade,
            foreign key (cycle_id, acceptance_id) references acceptance(cycle_id, id) on delete cascade
        );
        create table if not exists failure_mode_acceptance (
            cycle_id text not null,
            failure_mode_id text not null,
            acceptance_id text not null,
            primary key (cycle_id, failure_mode_id, acceptance_id),
            foreign key (cycle_id, failure_mode_id) references failure_modes(cycle_id, id) on delete cascade,
            foreign key (cycle_id, acceptance_id) references acceptance(cycle_id, id) on delete cascade
        );
        create table if not exists baselines (
            id text primary key,
            summary text not null,
            snapshot_json text not null,
            digest text not null,
            project_revision integer not null,
            created_by text not null default '',
            created_at text not null
        );
        create table if not exists tasks (
            uid text primary key default (lower(hex(randomblob(16)))),
            id text not null,
            cycle_id text not null default '',
            task text not null,
            owner text not null default '',
            status text not null default 'planned'
                check (status in ('planned', 'active', 'submitted', 'accepted', 'blocked', 'cancelled')),
            evidence text not null default '',
            submitted_context_id text not null default '',
            accepted_by text not null default '',
            revision integer not null default 1 check (revision > 0),
            updated_at text not null,
            unique(cycle_id, id)
        );
        create table if not exists task_acceptance (
            cycle_id text not null,
            task_id text not null,
            acceptance_id text not null,
            primary key (cycle_id, task_id, acceptance_id),
            foreign key (cycle_id, task_id) references tasks(cycle_id, id) on delete cascade,
            foreign key (cycle_id, acceptance_id) references acceptance(cycle_id, id) on delete cascade
        );
        create table if not exists task_failure_modes (
            cycle_id text not null,
            task_id text not null,
            failure_mode_id text not null,
            primary key (cycle_id, task_id, failure_mode_id),
            foreign key (cycle_id, task_id) references tasks(cycle_id, id) on delete cascade,
            foreign key (cycle_id, failure_mode_id) references failure_modes(cycle_id, id) on delete cascade
        );
        create table if not exists task_dependencies (
            cycle_id text not null,
            task_id text not null,
            depends_on text not null,
            primary key (cycle_id, task_id, depends_on),
            foreign key (cycle_id, task_id) references tasks(cycle_id, id) on delete cascade,
            foreign key (cycle_id, depends_on) references tasks(cycle_id, id) on delete restrict,
            check (task_id != depends_on)
        );
        create table if not exists task_test_targets (
            cycle_id text not null,
            task_id text not null,
            target_id text not null references test_targets(id) on delete cascade,
            primary key (cycle_id, task_id, target_id),
            foreign key (cycle_id, task_id) references tasks(cycle_id, id) on delete cascade
        );
        create table if not exists task_attempts (
            id text primary key,
            run_id text not null,
            cycle_id text not null default '',
            task_id text not null,
            agent_id text not null,
            fence integer not null default 0,
            base_commit_sha text not null default '',
            head_commit_sha text not null default '',
            tree_sha text not null default '',
            branch_name text not null default '',
            target_id text not null default '',
            status text not null,
            provider_session_id text not null default '',
            agent_session_id text not null default '',
            report_id text not null default '',
            evidence_id text not null default '',
            started_at text not null default '',
            finished_at text not null default '',
            foreign key (cycle_id, task_id) references tasks(cycle_id, id) on delete cascade
        );
        create table if not exists validations (
            id text primary key,
            cycle_id text not null default '',
            candidate_sha text not null default '',
            validation_status text not null default 'active',
            superseded_by text not null default '',
            surface text not null,
            acceptance_id text not null default '',
            commands text not null default '',
            command text not null default '',
            exit_code integer,
            stdout_sha256 text not null default '',
            artifact_path text not null default '',
            target_id text not null default '',
            executed_count integer not null default 0,
            executed_count_source text not null default '',
            result_format text not null default 'regex',
            result_path text not null default '',
            semantic_status text not null default '',
            allow_unlisted integer not null default 0,
            no_network integer not null default 0,
            sandbox_profile text not null default 'none',
            sandbox_status text not null default '',
            sandbox_execution_id text not null default '',
            sandbox_engine text not null default '',
            container_image text not null default '',
            allow_unlisted_reason text not null default '',
            policy_status text not null default '',
            policy_reason text not null default '',
            findings text not null,
            result text not null,
            residual_risk text not null default '',
            head_commit text not null default '',
            source_tree_hash text not null default '',
            attempt_id text not null default '',
            tree_sha text not null default '',
            code_ref text not null default '',
            verified_by text not null default '',
            tracked_diff_hash text not null default '',
            project_revision integer not null default 0,
            created_at text not null
        );
        create table if not exists validation_failure_modes (
            validation_id text not null references validations(id) on delete cascade,
            cycle_id text not null,
            failure_mode_id text not null,
            primary key (validation_id, cycle_id, failure_mode_id),
            foreign key (cycle_id, failure_mode_id) references failure_modes(cycle_id, id) on delete cascade
        );
        create table if not exists validation_tests (
            validation_id text not null references validations(id) on delete cascade,
            test_id text not null references tests(id) on delete cascade,
            primary key (validation_id, test_id)
        );
        create table if not exists validation_evidence (
            validation_id text not null references validations(id) on delete cascade,
            evidence_id text not null references evidence(id) on delete cascade,
            primary key (validation_id, evidence_id)
        );
        create table if not exists test_targets (
            id text primary key,
            kind text not null,
            command_template text not null,
            description text not null default '',
            gateable integer not null default 1,
            gate_block_reason text not null default '',
            stack_profile text not null default 'python',
            container_image text not null default '',
            requires_sandbox integer not null default 0,
            requires_no_network integer not null default 0,
            result_format text not null default 'regex',
            result_path text not null default '',
            created_at text not null,
            updated_at text not null
        );
        create table if not exists quality_gates (
            id text primary key,
            sequence integer not null default 0 unique,
            cycle_id text not null default '',
            candidate_sha text not null default '',
            gate_status text not null default 'active',
            superseded_by text not null default '',
            gate text not null,
            reviewed_commit text not null,
            evidence_commit text not null default '',
            diff_hash text not null default '',
            base_commit text not null default '',
            head_commit text not null default '',
            tracked_diff_hash text not null default '',
            project_revision integer not null default 0,
            reviewer_context text not null,
            result text not null,
            blocking_findings text not null default '',
            commands text not null default '',
            evidence text not null default '',
            residual_risk text not null default '',
            reviewer_session_id text not null default '',
            created_at text not null
        );
        create table if not exists quality_gate_findings (
            gate_id text not null references quality_gates(id) on delete cascade,
            finding_id text not null references findings(id) on delete cascade,
            primary key (gate_id, finding_id)
        );
        create table if not exists deliveries (
            id text primary key,
            cycle_id text not null default '',
            candidate_sha text not null default '',
            scope text not null,
            acceptance text not null default '',
            changed_files text not null default '',
            validation text not null default '',
            qa text not null default '',
            failure_mode_coverage text not null default '',
            quality_gate text not null default '',
            data_config_notes text not null default '',
            known_gaps text not null default '',
            handoff text not null default '',
            created_at text not null
        );
        create table if not exists delivery_acceptance (
            delivery_id text not null references deliveries(id) on delete cascade,
            cycle_id text not null,
            acceptance_id text not null,
            primary key (delivery_id, cycle_id, acceptance_id),
            foreign key (cycle_id, acceptance_id) references acceptance(cycle_id, id) on delete cascade
        );
        create table if not exists evidence (
            id text primary key,
            kind text not null,
            summary text not null,
            uri text not null default '',
            hash text not null default '',
            command text not null default '',
            exit_code integer,
            stdout_sha256 text not null default '',
            artifact_path text not null default '',
            source_tree_hash text not null default '',
            target_id text not null default '',
            executed_count integer not null default 0,
            executed_count_source text not null default '',
            result_format text not null default 'regex',
            result_path text not null default '',
            semantic_status text not null default '',
            allow_unlisted integer not null default 0,
            no_network integer not null default 0,
            sandbox_profile text not null default 'none',
            sandbox_status text not null default '',
            sandbox_execution_id text not null default '',
            sandbox_engine text not null default '',
            container_image text not null default '',
            allow_unlisted_reason text not null default '',
            policy_status text not null default '',
            policy_reason text not null default '',
            attempt_id text not null default '',
            tree_sha text not null default '',
            code_ref text not null default '',
            verified_by text not null default '',
            created_at text not null
        );
        create table if not exists tests (
            id text primary key,
            surface text not null,
            command text not null default '',
            result text not null,
            evidence_id text not null default '',
            created_at text not null
        );
        create table if not exists findings (
            id text primary key,
            cycle_id text not null default '',
            candidate_sha text not null default '',
            surface text not null,
            severity text not null,
            status text not null,
            summary text not null,
            evidence_id text not null default '',
            waived_by text not null default '',
            waiver_reason text not null default '',
            waiver_scope text not null default '',
            waived_revision integer,
            waiver_expires_at text not null default '',
            created_at text not null
        );
        create table if not exists decisions (
            id text primary key,
            decision text not null,
            reason text not null,
            created_at text not null
        );
        create table if not exists invalidations (
            id text primary key,
            cycle_id text not null default '',
            source_type text not null,
            source_id text not null,
            target_type text not null,
            target_id text not null,
            reason text not null,
            resolved_at text,
            created_at text not null
        );
        create table if not exists agents (
            id text primary key,
            role text not null,
            template_path text not null,
            status text not null,
            tool_permissions text not null default '',
            session_id text not null default '',
            lease_task_id text not null default '',
            updated_at text not null
        );
        create table if not exists agent_sessions (
            session_id text primary key,
            agent_id text not null,
            role text not null,
            context_id text not null,
            provider_session_id text not null default '',
            origin text not null default 'manual',
            trust_level text not null default 'local-only',
            effective_trust text not null default 'local-only',
            receipt_provenance text not null default '',
            status text not null default 'active',
            started_at text not null,
            ended_at text not null default ''
        );
        create table if not exists agent_capabilities (
            agent_id text not null references agents(id) on delete cascade,
            capability text not null,
            primary key (agent_id, capability)
        );
        create table if not exists executor_allowlist (
            id text primary key,
            prefix text not null unique,
            reason text not null,
            created_at text not null
        );
        create table if not exists dispatch_runs (
            id text primary key,
            cycle_id text not null default '',
            scope text not null,
            status text not null,
            created_at text not null,
            updated_at text not null
        );
        create table if not exists dispatch_assignments (
            run_id text not null references dispatch_runs(id) on delete cascade,
            cycle_id text not null default '',
            task_id text not null,
            agent_id text not null default '',
            capability text not null default '',
            status text not null,
            evidence text not null default '',
            provider_session_id text not null default '',
            claimed_at text,
            heartbeat_at text,
            lease_expires_at text,
            updated_at text not null,
            primary key (run_id, task_id),
            foreign key (cycle_id, task_id) references tasks(cycle_id, id) on delete cascade
        );
        create table if not exists dispatch_worktrees (
            id text primary key,
            run_id text not null,
            task_id text not null,
            agent_id text not null,
            branch_name text not null,
            worktree_path text not null,
            status text not null,
            created_at text not null,
            cleaned_at text not null default ''
        );
        create table if not exists task_file_claims (
            id text primary key,
            run_id text not null,
            task_id text not null,
            agent_id text not null,
            path text not null,
            worktree_path text not null default '',
            branch_name text not null default '',
            status text not null,
            created_at text not null,
            released_at text not null default ''
        );
        create unique index if not exists task_file_claims_active_path
            on task_file_claims(path) where status = 'active';
        create table if not exists agent_reports (
            id text primary key,
            run_id text not null,
            task_id text not null,
            provider_session_id text not null default '',
            job_id text not null default '',
            status text not null,
            last_error text not null default '',
            result_json text not null,
            created_at text not null
        );
        create table if not exists agent_provider_sessions (
            id text primary key,
            run_id text not null,
            task_id text not null,
            provider text not null,
            provider_session_id text not null default '',
            provider_job_id text not null default '',
            agent_id text not null default '',
            status text not null,
            fence integer not null default 0,
            agent_session_id text not null default '',
            branch_name text not null default '',
            worktree_path text not null default '',
            input_json text not null default '',
            report_id text not null default '',
            attempt_id text not null default '',
            last_error text not null default '',
            spawned_at text not null default '',
            heartbeat_at text not null default '',
            lease_expires_at text not null default '',
            collected_at text not null default '',
            cancelled_at text not null default '',
            finished_at text not null default '',
            unique(run_id, task_id, provider)
        );
        create table if not exists agent_provider_events (
            id text primary key,
            session_id text not null,
            run_id text not null,
            task_id text not null,
            provider text not null,
            event_type text not null,
            payload_json text not null default '',
            created_at text not null
        );
        create table if not exists sandbox_executions (
            id text primary key,
            runner text not null,
            engine text not null default '',
            image text not null default '',
            command text not null,
            target_id text not null default '',
            source_ref text not null default '',
            tree_sha text not null default '',
            network_mode text not null default '',
            timeout_seconds integer not null default 0,
            resource_limits text not null default '',
            exit_code integer,
            artifact_path text not null default '',
            artifact_sha256 text not null default '',
            sandbox_status text not null,
            started_at text not null,
            finished_at text not null default ''
        );
        create table if not exists integration_attempts (
            id text primary key,
            run_id text not null,
            target_branch text not null,
            integration_worktree text not null default '',
            base_ref text not null default '',
            merged_branches text not null default '',
            status text not null,
            validation_result text not null default '',
            finding_id text not null default '',
            started_at text not null,
            finished_at text not null default ''
        );
        create table if not exists codex_fanout_exports (
            id text primary key,
            run_id text not null,
            input_csv_path text not null,
            instruction_path text not null,
            output_schema_path text not null,
            spawn_config_path text not null,
            max_concurrency integer not null,
            max_runtime_seconds integer not null,
            status text not null,
            created_at text not null,
            imported_at text not null default ''
        );
        create table if not exists runtime_snapshots (
            id text primary key,
            label text not null,
            event_sequence integer not null,
            snapshot_json text not null,
            created_at text not null
        );
        create table if not exists command_log (
            request_id text primary key,
            command text not null,
            args_hash text not null,
            result_json text not null default '',
            created_at text not null
        );
        create table if not exists migrations (
            id integer primary key autoincrement,
            from_version integer not null,
            to_version integer not null,
            applied_at text not null
        );
        create table if not exists events (
            sequence integer primary key autoincrement,
            id text not null unique,
            schema_version integer not null,
            type text not null,
            source text not null,
            target text not null,
            correlation_id text not null default '',
            causation_id text not null default '',
            idempotency_key text not null default '',
            payload_json text not null,
            created_at text not null
        );
        """
    )
    ensure_column(conn, "project", "current_cycle_id", "text not null default ''")
    ensure_column(conn, "acceptance", "cycle_id", "text not null default ''")
    ensure_column(conn, "requirements", "cycle_id", "text not null default ''")
    ensure_column(conn, "failure_modes", "cycle_id", "text not null default ''")
    ensure_column(conn, "tasks", "cycle_id", "text not null default ''")
    for relation in [
        "requirement_acceptance",
        "failure_mode_acceptance",
        "task_acceptance",
        "task_failure_modes",
        "task_dependencies",
        "task_test_targets",
        "validation_failure_modes",
        "delivery_acceptance",
    ]:
        ensure_column(conn, relation, "cycle_id", "text not null default ''")
    ensure_column(conn, "task_attempts", "cycle_id", "text not null default ''")
    ensure_column(conn, "dispatch_assignments", "cycle_id", "text not null default ''")
    ensure_column(conn, "invalidations", "cycle_id", "text not null default ''")
    ensure_column(conn, "dispatch_runs", "cycle_id", "text not null default ''")
    ensure_column(conn, "failure_modes", "acceptance_scope", "text not null default ''")
    ensure_column(conn, "failure_modes", "accepted_revision", "integer")
    ensure_column(conn, "tasks", "submitted_context_id", "text not null default ''")
    ensure_column(conn, "tasks", "accepted_by", "text not null default ''")
    ensure_column(conn, "quality_gates", "base_commit", "text not null default ''")
    ensure_column(conn, "quality_gates", "cycle_id", "text not null default ''")
    ensure_column(conn, "quality_gates", "candidate_sha", "text not null default ''")
    ensure_column(conn, "quality_gates", "sequence", "integer not null default 0")
    ensure_column(conn, "quality_gates", "gate_status", "text not null default 'active'")
    ensure_column(conn, "quality_gates", "superseded_by", "text not null default ''")
    ensure_column(conn, "quality_gates", "head_commit", "text not null default ''")
    ensure_column(conn, "quality_gates", "tracked_diff_hash", "text not null default ''")
    ensure_column(conn, "quality_gates", "project_revision", "integer not null default 0")
    ensure_column(conn, "quality_gates", "reviewer_session_id", "text not null default ''")
    ensure_column(conn, "findings", "cycle_id", "text not null default ''")
    ensure_column(conn, "findings", "candidate_sha", "text not null default ''")
    ensure_column(conn, "findings", "waived_by", "text not null default ''")
    ensure_column(conn, "findings", "waiver_reason", "text not null default ''")
    ensure_column(conn, "findings", "waiver_scope", "text not null default ''")
    ensure_column(conn, "findings", "waived_revision", "integer")
    ensure_column(conn, "findings", "waiver_expires_at", "text not null default ''")
    ensure_column(conn, "validations", "head_commit", "text not null default ''")
    ensure_column(conn, "validations", "cycle_id", "text not null default ''")
    ensure_column(conn, "validations", "candidate_sha", "text not null default ''")
    ensure_column(conn, "validations", "validation_status", "text not null default 'active'")
    ensure_column(conn, "validations", "superseded_by", "text not null default ''")
    ensure_column(conn, "validations", "source_tree_hash", "text not null default ''")
    ensure_column(conn, "validations", "attempt_id", "text not null default ''")
    ensure_column(conn, "validations", "tree_sha", "text not null default ''")
    ensure_column(conn, "validations", "code_ref", "text not null default ''")
    ensure_column(conn, "validations", "verified_by", "text not null default ''")
    ensure_column(conn, "validations", "tracked_diff_hash", "text not null default ''")
    ensure_column(conn, "validations", "project_revision", "integer not null default 0")
    ensure_column(conn, "validations", "command", "text not null default ''")
    ensure_column(conn, "validations", "exit_code", "integer")
    ensure_column(conn, "validations", "stdout_sha256", "text not null default ''")
    ensure_column(conn, "validations", "artifact_path", "text not null default ''")
    ensure_column(conn, "validations", "target_id", "text not null default ''")
    ensure_column(conn, "validations", "executed_count", "integer not null default 0")
    ensure_column(conn, "validations", "executed_count_source", "text not null default ''")
    ensure_column(conn, "validations", "result_format", "text not null default 'regex'")
    ensure_column(conn, "validations", "result_path", "text not null default ''")
    ensure_column(conn, "validations", "semantic_status", "text not null default ''")
    ensure_column(conn, "validations", "allow_unlisted", "integer not null default 0")
    ensure_column(conn, "validations", "no_network", "integer not null default 0")
    ensure_column(conn, "validations", "sandbox_profile", "text not null default 'none'")
    ensure_column(conn, "validations", "sandbox_status", "text not null default ''")
    ensure_column(conn, "validations", "sandbox_execution_id", "text not null default ''")
    ensure_column(conn, "validations", "sandbox_engine", "text not null default ''")
    ensure_column(conn, "validations", "container_image", "text not null default ''")
    ensure_column(conn, "validations", "allow_unlisted_reason", "text not null default ''")
    ensure_column(conn, "validations", "policy_status", "text not null default ''")
    ensure_column(conn, "validations", "policy_reason", "text not null default ''")
    ensure_column(conn, "test_targets", "gateable", "integer not null default 1")
    ensure_column(conn, "test_targets", "gate_block_reason", "text not null default ''")
    ensure_column(conn, "test_targets", "stack_profile", "text not null default 'python'")
    ensure_column(conn, "test_targets", "container_image", "text not null default ''")
    ensure_column(conn, "test_targets", "requires_sandbox", "integer not null default 0")
    ensure_column(conn, "test_targets", "requires_no_network", "integer not null default 0")
    ensure_column(conn, "test_targets", "result_format", "text not null default 'regex'")
    ensure_column(conn, "test_targets", "result_path", "text not null default ''")
    ensure_column(conn, "evidence", "command", "text not null default ''")
    ensure_column(conn, "evidence", "exit_code", "integer")
    ensure_column(conn, "evidence", "stdout_sha256", "text not null default ''")
    ensure_column(conn, "evidence", "artifact_path", "text not null default ''")
    ensure_column(conn, "evidence", "source_tree_hash", "text not null default ''")
    ensure_column(conn, "evidence", "attempt_id", "text not null default ''")
    ensure_column(conn, "evidence", "tree_sha", "text not null default ''")
    ensure_column(conn, "evidence", "code_ref", "text not null default ''")
    ensure_column(conn, "evidence", "verified_by", "text not null default ''")
    ensure_column(conn, "evidence", "target_id", "text not null default ''")
    ensure_column(conn, "evidence", "executed_count", "integer not null default 0")
    ensure_column(conn, "evidence", "executed_count_source", "text not null default ''")
    ensure_column(conn, "evidence", "result_format", "text not null default 'regex'")
    ensure_column(conn, "evidence", "result_path", "text not null default ''")
    ensure_column(conn, "evidence", "semantic_status", "text not null default ''")
    ensure_column(conn, "evidence", "allow_unlisted", "integer not null default 0")
    ensure_column(conn, "evidence", "no_network", "integer not null default 0")
    ensure_column(conn, "evidence", "sandbox_profile", "text not null default 'none'")
    ensure_column(conn, "evidence", "sandbox_status", "text not null default ''")
    ensure_column(conn, "evidence", "sandbox_execution_id", "text not null default ''")
    ensure_column(conn, "evidence", "sandbox_engine", "text not null default ''")
    ensure_column(conn, "evidence", "container_image", "text not null default ''")
    ensure_column(conn, "evidence", "allow_unlisted_reason", "text not null default ''")
    ensure_column(conn, "evidence", "policy_status", "text not null default ''")
    ensure_column(conn, "evidence", "policy_reason", "text not null default ''")
    ensure_column(conn, "deliveries", "cycle_id", "text not null default ''")
    ensure_column(conn, "deliveries", "candidate_sha", "text not null default ''")
    ensure_column(conn, "agent_sessions", "effective_trust", "text not null default 'local-only'")
    ensure_column(conn, "agent_sessions", "receipt_provenance", "text not null default ''")
    ensure_column(conn, "dispatch_assignments", "heartbeat_at", "text")
    ensure_column(conn, "dispatch_assignments", "lease_expires_at", "text")
    ensure_column(conn, "dispatch_assignments", "provider_session_id", "text not null default ''")
    ensure_column(conn, "task_attempts", "provider_session_id", "text not null default ''")
    ensure_column(conn, "task_attempts", "agent_session_id", "text not null default ''")
    ensure_column(conn, "agent_reports", "provider_session_id", "text not null default ''")
    ensure_column(conn, "agent_provider_sessions", "agent_session_id", "text not null default ''")
    ensure_default_executor_allowlist(conn)

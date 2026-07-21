"""Side-by-side conversion from the schema 29 runtime to the local schema 30 Kernel."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from .execution import command_matches_template
from .errors import HarnessError, exception_text as _exception_text
from .project_fs import ProjectFS, ProjectPathSafetyError, _PathSnapshot
from .projections import PROJECTION_PATHS, PROJECTION_ROLLBACK_PATHS
from .schema_guard import (
    ACCEPTANCE_STATUSES,
    FAILURE_MODE_STATUSES,
    REQUIREMENT_STATUSES,
)
from .schema_lifecycle import (
    ACTIVE_RUNTIME_VERSION,
    ACTIVE_SCHEMA_CATALOG_TABLES,
    ACTIVE_SCHEMA_TABLES,
    ACTIVE_SCHEMA_VERSION,
    SCHEMA30_RUNTIME_VERSION,
    SCHEMA30_CATALOG_TABLES,
    SCHEMA30_TABLES,
    SCHEMA30_VERSION,
    SQLiteBackupManifest,
    backup_sqlite_database,
    create_active_schema,
    create_schema30,
)
from .store import (
    _apply_sqlite_teardown_errors,
    _temporary_sqlite_family_cleanup_errors,
    _verified_sqlite_connection,
    project_db_operation,
)


_WINDOWS_FILE_ATTRIBUTE_READONLY = 0x00000001


class LocalCoreMigrationError(HarnessError):
    """Raised when a local-core staging conversion is unsafe or incomplete."""


class InjectedLocalCoreMigrationFailure(LocalCoreMigrationError):
    """Deterministic failure used to prove migration rollback boundaries."""


@dataclass(frozen=True)
class _FileAuthorityReceipt:
    snapshot: _PathSnapshot
    sha256: str
    mode: int | None


@dataclass
class _MigrationGuard:
    lock_path: Path
    project_fs: ProjectFS
    lock_snapshot: _PathSnapshot
    target_schema: int = SCHEMA30_VERSION
    recovery_required: bool = False
    manifest_path: Path | None = None
    clear_allowed: bool = False
    clear_verifier: Callable[[], None] | None = None
    clear_failure_publisher: Callable[[BaseException], None] | None = None
    recovery_manifest_relative: Path | None = None
    recovery_manifest_snapshot: _PathSnapshot | None = None
    recovery_manifest_sha256: str = ""

    def record_manifest(self, manifest_path: Path) -> None:
        self.manifest_path = manifest_path

    def record_recovery_manifest(
        self,
        manifest_path: Path,
        snapshot: _PathSnapshot,
    ) -> None:
        relative = self.project_fs.relative_to_root(manifest_path)
        self.project_fs._assert_unchanged(relative, snapshot)
        digest = _safe_file_sha256(
            self.project_fs,
            relative,
            expected=snapshot,
        )
        self.project_fs._assert_unchanged(relative, snapshot)
        self.manifest_path = manifest_path
        self.recovery_manifest_relative = relative
        self.recovery_manifest_snapshot = snapshot
        self.recovery_manifest_sha256 = digest

    def verify_recovery_manifest(self, *, required: bool) -> None:
        relative = self.recovery_manifest_relative
        snapshot = self.recovery_manifest_snapshot
        digest = self.recovery_manifest_sha256
        if relative is None or snapshot is None or not digest:
            if required:
                raise LocalCoreMigrationError(
                    "verified recovery manifest receipt is unavailable"
                )
            return
        self.project_fs._assert_unchanged(relative, snapshot)
        if (
            _safe_file_sha256(
                self.project_fs,
                relative,
                expected=snapshot,
            )
            != digest
        ):
            raise LocalCoreMigrationError(
                "recovery manifest changed before sentinel publication"
            )
        self.project_fs._assert_unchanged(relative, snapshot)

    def write_sentinel(self, payload: dict[str, object]) -> None:
        self.lock_snapshot = _write_json_atomic(
            self.lock_path,
            payload,
            project_fs=self.project_fs,
            expected_destination=self.lock_snapshot,
        )

    def require_recovery(
        self,
        manifest_path: Path,
        *,
        snapshot: _PathSnapshot,
    ) -> None:
        self.recovery_required = True
        self.manifest_path = manifest_path
        self.clear_allowed = False
        self.record_recovery_manifest(manifest_path, snapshot)
        self.verify_recovery_manifest(required=True)
        payload: dict[str, object] = {
            "pid": os.getpid(),
            "created_at": _timestamp(),
            "target_schema": self.target_schema,
            "status": "recovery-required",
            "manifest_path": str(manifest_path),
        }
        self.write_sentinel(payload)
        try:
            self.verify_recovery_manifest(required=True)
        except BaseException as manifest_exc:
            payload.pop("manifest_path", None)
            payload["manifest_status"] = "changed"
            payload["manifest_error"] = _exception_text(manifest_exc)
            self.write_sentinel(payload)
            raise
    def arm_verified_clear(
        self,
        verifier: Callable[[], None],
        failure_publisher: Callable[[BaseException], None],
    ) -> None:
        self.clear_verifier = verifier
        self.clear_failure_publisher = failure_publisher

    def verify_clear_authorities(self, *, required: bool) -> None:
        if self.clear_verifier is None:
            if required:
                raise LocalCoreMigrationError(
                    "migration success authority receipts are unavailable"
                )
            return
        self.clear_verifier()

    def mark_safe(
        self,
        verifier: Callable[[], None] | None = None,
        failure_publisher: Callable[[BaseException], None] | None = None,
    ) -> None:
        self.recovery_required = False
        self.clear_allowed = True
        self.clear_verifier = verifier
        self.clear_failure_publisher = failure_publisher


def _sqlite_integer(value: object) -> int | None:
    return value if type(value) is int else None


def _positive_sqlite_integer(
    value: object,
    *,
    field: str,
    source_schema: int = 29,
) -> int:
    integer = _sqlite_integer(value)
    if integer is None or integer <= 0:
        raise LocalCoreMigrationError(
            f"schema {source_schema} {field} must be a positive SQLite integer: {value!r}"
        )
    return integer


def _sqlite_flag(value: object, *, field: str) -> int:
    integer = _sqlite_integer(value)
    if integer not in {0, 1}:
        raise LocalCoreMigrationError(
            f"schema 29 {field} must be an exact SQLite flag (0 or 1): {value!r}"
        )
    return integer


@dataclass(frozen=True)
class LocalCoreStagingReport:
    source_version: int
    target_version: int
    source_path: str
    staging_path: str
    source_sha256: str
    staging_sha256: str
    source_row_counts: dict[str, int]
    staging_row_counts: dict[str, int]
    retired_row_counts: dict[str, int]
    dropped_event_count: int
    converted_execution_count: int
    converted_validation_count: int
    invalidated_validation_count: int
    normalized_failure_mode_count: int = 0


@dataclass(frozen=True)
class LocalCoreMigrationResult:
    source_version: int
    target_version: int
    active_path: str
    active_sha256: str
    backup: SQLiteBackupManifest
    staging: LocalCoreStagingReport
    migration_manifest_path: str


MIGRATION_FAILURE_POINTS = frozenset(
    {
        "before_copy",
        "during_relation_copy",
        "during_invariant_validation",
        "before_atomic_replace",
        "after_atomic_replace",
    }
)


def _inject_failure(fail_at: str | None, point: str) -> None:
    if fail_at == point:
        raise InjectedLocalCoreMigrationFailure(f"injected local-core migration failure at {point}")


def _project_root_for_internal_path(path: Path) -> Path:
    absolute = Path(path).expanduser().absolute()
    for parent in absolute.parents:
        if parent.name == ".ai-team":
            return parent.parent
    raise LocalCoreMigrationError(
        f"migration path is outside project-owned authority: {path}"
    )


def _project_relative(root: Path, path: Path) -> Path:
    with ProjectFS.open(root) as project_fs:
        return project_fs.relative_to_root(path)


@contextmanager
def _project_fs_scope(
    root: Path,
    project_fs: ProjectFS | None = None,
) -> Iterator[ProjectFS]:
    if project_fs is not None:
        yield project_fs
        return
    with ProjectFS.open(root) as opened:
        yield opened


def _safe_file_sha256(
    project_fs: ProjectFS,
    relative: Path,
    *,
    expected: _PathSnapshot | None = None,
) -> str:
    return hashlib.sha256(
        project_fs.read_bytes(relative, expected=expected)
    ).hexdigest()


def _safe_file_mode(
    project_fs: ProjectFS,
    relative: Path,
    *,
    expected: _PathSnapshot | None = None,
) -> int:
    snapshot = project_fs._snapshot(relative, allow_missing=False)
    if expected is not None and snapshot != expected:
        raise ProjectPathSafetyError(relative, "path-identity-changed")
    assert snapshot.identity is not None
    if os.name == "nt":
        return (
            0o444
            if snapshot.identity.mode_or_attributes
            & _WINDOWS_FILE_ATTRIBUTE_READONLY
            else 0o666
        )
    return stat.S_IMODE(snapshot.identity.mode_or_attributes)


def _database_family(relative: Path) -> tuple[Path, Path, Path, Path]:
    value = relative.as_posix()
    return (
        relative,
        Path(f"{value}-wal"),
        Path(f"{value}-shm"),
        Path(f"{value}-journal"),
    )


def _capture_database_family_receipts(
    project_fs: ProjectFS,
    active_relative: Path,
    *,
    expected_main: _PathSnapshot,
    expected_digest: str,
) -> dict[Path, _PathSnapshot]:
    family = _database_family(active_relative)
    receipts: dict[Path, _PathSnapshot] = {}
    for index, relative in enumerate(family):
        snapshot = project_fs._snapshot(
            relative,
            allow_missing=index != 0,
        )
        if index == 0:
            if snapshot != expected_main:
                raise ProjectPathSafetyError(
                    relative,
                    "path-identity-changed",
                )
        elif snapshot.exists:
            raise LocalCoreMigrationError(
                "database sidecar remained at an authority boundary: "
                f"{relative}"
            )
        receipts[relative] = snapshot
    if (
        _safe_database_digest(
            project_fs,
            active_relative,
            expected=expected_main,
        )
        != expected_digest
    ):
        raise LocalCoreMigrationError(
            "database digest changed at an authority boundary"
        )
    project_fs._assert_unchanged(active_relative, expected_main)
    return receipts


def _assert_database_family_receipts(
    project_fs: ProjectFS,
    active_relative: Path,
    receipts: dict[Path, _PathSnapshot],
    *,
    expected_digest: str,
) -> None:
    family = _database_family(active_relative)
    if set(receipts) != set(family):
        raise LocalCoreMigrationError(
            "database family receipts do not match the canonical inventory"
        )
    for index, relative in enumerate(family):
        snapshot = receipts[relative]
        project_fs._assert_unchanged(relative, snapshot)
        if index == 0 and not snapshot.exists:
            raise LocalCoreMigrationError(
                "database authority is missing at completion"
            )
        if index != 0 and snapshot.exists:
            raise LocalCoreMigrationError(
                f"database sidecar receipt must be absent: {relative}"
            )
    main_snapshot = receipts[active_relative]
    if (
        _safe_database_digest(
            project_fs,
            active_relative,
            expected=main_snapshot,
        )
        != expected_digest
    ):
        raise LocalCoreMigrationError(
            "database authority changed before migration completion"
        )
    for relative in family:
        project_fs._assert_unchanged(relative, receipts[relative])


def _safe_database_fingerprint(
    project_fs: ProjectFS,
    relative: Path,
) -> dict[str, str]:
    """Hash committed authority and a non-empty WAL without following links."""

    family = _database_family(relative)
    project_fs.audit(family, allow_missing=True)
    fingerprint: dict[str, str] = {}
    for candidate in family[:2]:
        snapshot = project_fs._snapshot(candidate, allow_missing=True)
        if not snapshot.exists:
            continue
        payload = project_fs.read_bytes(candidate)
        if candidate == relative or payload:
            fingerprint[candidate.name] = hashlib.sha256(payload).hexdigest()
    return fingerprint


def _safe_database_digest(
    project_fs: ProjectFS,
    relative: Path,
    *,
    expected: _PathSnapshot | None = None,
) -> str:
    return hashlib.sha256(
        project_fs.read_bytes(relative, expected=expected)
    ).hexdigest()


TASK_STATUS_MAP = {
    "ready": "planned",
    "planned": "planned",
    "queued": "planned",
    "open": "planned",
    "claimed": "active",
    "in_progress": "active",
    "active": "active",
    "submitted": "submitted",
    "review": "submitted",
    "accepted": "accepted",
    "complete": "accepted",
    "completed": "accepted",
    "done": "accepted",
    "blocked": "blocked",
    "failed": "blocked",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "skipped": "cancelled",
}

RETIRED_EVENT_MARKERS = (
    "adapter",
    "connector",
    "provider",
    "dispatch",
    "worktree",
    "fanout",
    "agent_session",
    "session_attest",
    "external_session",
    "ci_verification",
    "sandbox_execution",
    "checkpoint",
    "snapshot",
)

LOCAL_ENTITY_TYPES = {
    "project",
    "cycle",
    "delivery_cycle",
    "requirement",
    "acceptance",
    "failure_mode",
    "baseline",
    "task",
    "test_target",
    "execution",
    "validation",
    "finding",
    "quality_gate",
    "delivery",
    "decision",
    "invalidation",
}
LOCAL_INVALIDATION_SOURCE_TYPES = {"requirement", "acceptance", "failure_mode"}
LOCAL_INVALIDATION_TARGET_TYPES = {
    "acceptance",
    "task",
    "validation",
    "quality_gate",
}

RETIRED_METADATA_KEYS = {
    "tool_link",
    "collaboration_links",
    "connector_project_key",
    "provider_session_id",
    "agent_session_id",
    "reviewer_attestation_id",
    "verification_token",
    "receipt_provenance",
    "trust_anchor_id",
}


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _table_names(conn: sqlite3.Connection) -> tuple[str, ...]:
    return tuple(
        str(row[0])
        for row in conn.execute(
            "select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name"
        )
    )


def _catalog_table_names(conn: sqlite3.Connection) -> tuple[str, ...]:
    return tuple(
        str(row[0])
        for row in conn.execute(
            "select name from sqlite_master where type='table' order by name"
        )
    )


def _row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(conn.execute(f"select count(*) from {_quote_identifier(table)}").fetchone()[0])
        for table in _table_names(conn)
    }


def _columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(str(row[1]) for row in conn.execute(f"pragma table_info({_quote_identifier(table)})"))


def _rows(conn: sqlite3.Connection, table: str, *, order_by: str = "rowid") -> list[dict[str, object]]:
    if table not in _table_names(conn):
        raise LocalCoreMigrationError(f"schema 29 source is missing required table: {table}")
    return [
        {str(key): row[key] for key in row.keys()}
        for row in conn.execute(
            f"select * from {_quote_identifier(table)} order by {order_by}"
        ).fetchall()
    ]


def _insert(conn: sqlite3.Connection, table: str, values: dict[str, object]) -> None:
    table_columns = _columns(conn, table)
    columns = [column for column in table_columns if column in values]
    if not columns:
        raise LocalCoreMigrationError(f"no compatible columns for {table}")
    placeholders = ",".join("?" for _ in columns)
    column_sql = ",".join(_quote_identifier(column) for column in columns)
    conn.execute(
        f"insert into {_quote_identifier(table)} ({column_sql}) values ({placeholders})",
        tuple(values[column] for column in columns),
    )


def _copy_intersection(
    source: sqlite3.Connection,
    destination: sqlite3.Connection,
    table: str,
    *,
    transform: Callable[[dict[str, object]], dict[str, object] | None] | None = None,
) -> int:
    source_columns = set(_columns(source, table))
    target_columns = set(_columns(destination, table))
    count = 0
    for row in _rows(source, table):
        values = {key: value for key, value in row.items() if key in source_columns & target_columns}
        if transform is not None:
            values = transform(values)
        if values is None:
            continue
        _insert(destination, table, values)
        count += 1
    return count


def _sanitize_local_json(value: object) -> object:
    if isinstance(value, dict):
        return {
            str(key): _sanitize_local_json(item)
            for key, item in value.items()
            if str(key) not in RETIRED_METADATA_KEYS
        }
    if isinstance(value, list):
        return [_sanitize_local_json(item) for item in value]
    return value


def _stable_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stable_digest(value: object) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _normalize_cycle_id(value: object, current_cycle_id: str) -> str:
    normalized = str(value or "").strip()
    return normalized or current_cycle_id


def _source_project(source: sqlite3.Connection) -> dict[str, object]:
    rows = _rows(source, "project")
    if len(rows) != 1 or int(rows[0].get("id", 0)) != 1:
        raise LocalCoreMigrationError("schema 29 source must contain exactly one project row")
    if int(rows[0].get("schema_version", 0)) != 29:
        raise LocalCoreMigrationError(
            f"schema 29 staging requires source version 29, actual {rows[0].get('schema_version')}"
        )
    return rows[0]


def _session_contexts(source: sqlite3.Connection) -> dict[str, str]:
    if "agent_sessions" not in _table_names(source):
        return {}
    contexts: dict[str, str] = {}
    for row in _rows(source, "agent_sessions"):
        session_id = str(row.get("session_id") or "").strip()
        context_id = str(row.get("context_id") or "").strip()
        if session_id and context_id:
            contexts[session_id] = context_id
    return contexts


def _copy_delivery_cycles(
    source: sqlite3.Connection,
    destination: sqlite3.Connection,
    project: dict[str, object],
) -> str:
    current_cycle_id = str(project.get("current_cycle_id") or "CYCLE-current")
    cycles = _rows(source, "delivery_cycles")
    for cycle in cycles:
        _insert(destination, "delivery_cycles", cycle)
    existing = {str(cycle["id"]) for cycle in cycles}
    if current_cycle_id not in existing:
        timestamp = str(project.get("updated_at") or "migration")
        _insert(
            destination,
            "delivery_cycles",
            {
                "id": current_cycle_id,
                "name": "Migrated delivery cycle",
                "goal": "Preserve schema 29 local delivery facts",
                "status": "active",
                "phase": str(project.get("phase") or "intake"),
                "base_ref": "",
                "candidate_sha": "",
                "started_at": timestamp,
                "closed_at": "",
                "created_at": timestamp,
                "updated_at": timestamp,
            },
        )
    return current_cycle_id


def _copy_project(
    destination: sqlite3.Connection,
    project: dict[str, object],
    current_cycle_id: str,
) -> None:
    _insert(
        destination,
        "project",
        {
            "id": 1,
            "project_id": str(project["project_id"]),
            "schema_version": SCHEMA30_VERSION,
            "runtime_version": SCHEMA30_RUNTIME_VERSION,
            "phase": str(project["phase"]),
            "current_cycle_id": current_cycle_id,
            "status": str(project["status"]),
            "scope_status": str(project["scope_status"]),
            "current_owner": str(project["current_owner"]),
            "revision": _positive_sqlite_integer(
                project["revision"],
                field="project.revision",
            ),
            "updated_at": str(project["updated_at"]),
        },
    )


def _copy_baselines(
    source: sqlite3.Connection,
    destination: sqlite3.Connection,
    current_cycle_id: str,
) -> int:
    count = 0
    for row in _rows(source, "baselines"):
        try:
            snapshot = _sanitize_local_json(json.loads(str(row["snapshot_json"])))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise LocalCoreMigrationError(f"baseline {row.get('id')} has invalid snapshot JSON") from exc
        _insert(
            destination,
            "baselines",
            {
                "id": row["id"],
                "cycle_id": current_cycle_id,
                "summary": row["summary"],
                "snapshot_json": _stable_json(snapshot),
                "digest": _stable_digest(snapshot),
                "project_revision": row["project_revision"],
                "created_by": row.get("created_by") or "",
                "created_at": row["created_at"],
            },
        )
        count += 1
    return count


def _copy_tasks(
    source: sqlite3.Connection,
    destination: sqlite3.Connection,
    current_cycle_id: str,
    session_contexts: dict[str, str],
) -> int:
    count = 0
    for row in _rows(source, "tasks"):
        source_status = str(row.get("status") or "")
        if source_status not in TASK_STATUS_MAP:
            raise LocalCoreMigrationError(
                f"task {row.get('id')} has unsupported schema 29 status: {source_status}"
            )
        submitted_context_id = str(row.get("submitted_context_id") or "").strip()
        if not submitted_context_id:
            submitted_session_id = str(row.get("submitted_session_id") or "").strip()
            submitted_context_id = session_contexts.get(submitted_session_id, "")
        _insert(
            destination,
            "tasks",
            {
                "uid": row.get("uid"),
                "id": row["id"],
                "cycle_id": _normalize_cycle_id(row.get("cycle_id"), current_cycle_id),
                "task": row["task"],
                "owner": row.get("owner") or "",
                "status": TASK_STATUS_MAP[source_status],
                "evidence": row.get("evidence") or "",
                "submitted_context_id": submitted_context_id,
                "accepted_by": row.get("accepted_by") or "",
                "revision": _positive_sqlite_integer(
                    row.get("revision"),
                    field=f"task {row.get('id')}.revision",
                ),
                "updated_at": row["updated_at"],
            },
        )
        count += 1
    return count


def _legacy_gate_candidate(row: dict[str, object]) -> str:
    candidate_sha = str(row.get("candidate_sha") or "").strip()
    return candidate_sha or str(row.get("reviewed_commit") or "").strip()


def _copy_quality_gates(
    source: sqlite3.Connection,
    destination: sqlite3.Connection,
    current_cycle_id: str,
    session_contexts: dict[str, str],
) -> int:
    tasks_by_cycle: dict[str, set[str]] = {}
    for task in _rows(destination, "tasks"):
        context_id = str(task.get("submitted_context_id") or "")
        if context_id:
            tasks_by_cycle.setdefault(str(task["cycle_id"]), set()).add(context_id)

    rows = _rows(source, "quality_gates", order_by="sequence, rowid")
    used_sequences: set[int] = set()
    pending_supersession: list[tuple[str, str]] = []
    next_sequence = 1
    for row in rows:
        cycle_id = _normalize_cycle_id(row.get("cycle_id"), current_cycle_id)
        source_sequence = int(row.get("sequence") or 0)
        sequence = source_sequence if source_sequence > 0 and source_sequence not in used_sequences else next_sequence
        used_sequences.add(sequence)
        next_sequence = max(next_sequence, sequence + 1)
        producer_contexts = tasks_by_cycle.get(cycle_id, set())
        producer_context_id = next(iter(producer_contexts)) if len(producer_contexts) == 1 else ""
        reviewer_session_id = str(row.get("reviewer_session_id") or "")
        reviewer_context_id = session_contexts.get(reviewer_session_id, "")
        source_review_context = str(row.get("reviewer_context") or "")
        if not reviewer_context_id and source_review_context == "same-context-degraded":
            reviewer_context_id = producer_context_id
        review_status = (
            "reviewed-local"
            if source_review_context == "fresh"
            and reviewer_context_id
            and producer_context_id
            and reviewer_context_id != producer_context_id
            else "same-context-degraded"
        )
        _insert(
            destination,
            "quality_gates",
            {
                "id": row["id"],
                "sequence": sequence,
                "cycle_id": cycle_id,
                "candidate_sha": _legacy_gate_candidate(row),
                "gate_status": row.get("gate_status") or "active",
                "superseded_by": None,
                "gate": row.get("gate") or "independent_qa",
                "producer_context_id": producer_context_id,
                "reviewer_context_id": reviewer_context_id,
                "review_status": review_status,
                "result": row["result"],
                "blocking_findings": row.get("blocking_findings") or "",
                "residual_risk": row.get("residual_risk") or "",
                "reviewed_revision": _positive_sqlite_integer(
                    row.get("project_revision"),
                    field=f"quality gate {row.get('id')}.project_revision",
                ),
                "created_at": row["created_at"],
            },
        )
        if row.get("superseded_by"):
            pending_supersession.append((str(row["id"]), str(row["superseded_by"])))
    gate_ids = {str(row[0]) for row in destination.execute("select id from quality_gates")}
    for gate_id, superseded_by in pending_supersession:
        if superseded_by in gate_ids:
            destination.execute(
                "update quality_gates set superseded_by = ? where id = ?",
                (superseded_by, gate_id),
            )
    return len(rows)


def _project_root_for_database(source_path: Path) -> Path:
    try:
        return _project_root_for_internal_path(source_path)
    except LocalCoreMigrationError:
        return Path(source_path).expanduser().absolute().parent


def _artifact_matches(
    project_root: Path,
    artifact_path: object,
    expected_sha256: object,
    *,
    pinned_fs: ProjectFS | None = None,
) -> bool:
    relative = str(artifact_path or "").strip()
    expected = str(expected_sha256 or "").strip().lower()
    if not relative or len(expected) != 64:
        return False
    candidate = Path(relative)
    try:
        with _project_fs_scope(project_root, pinned_fs) as project_fs:
            artifact_relative = project_fs.relative_to_root(candidate)
            snapshot = project_fs._snapshot(
                artifact_relative,
                allow_missing=True,
            )
            return (
                snapshot.exists
                and _safe_file_sha256(project_fs, artifact_relative)
                == expected
            )
    except ProjectPathSafetyError:
        return False


def _execution_id(evidence_id: str) -> str:
    return "MIG-EXEC-" + hashlib.sha256(evidence_id.encode("utf-8")).hexdigest()[:20]


def _invalidation_id(validation_id: str) -> str:
    return "MIG-INVALID-" + hashlib.sha256(validation_id.encode("utf-8")).hexdigest()[:20]


def _eligible_execution(
    evidence: dict[str, object],
    target: dict[str, object] | None,
    *,
    project_root: Path,
    candidate_sha: str,
    pinned_fs: ProjectFS | None = None,
) -> tuple[bool, str]:
    if str(evidence.get("kind") or "") != "command":
        return False, "evidence is not a command execution"
    if not str(evidence.get("verified_by") or "").startswith("controller"):
        return False, "evidence was not written by the controller executor"
    if target is None:
        return False, "evidence target is missing or not gateable"
    gateable = _sqlite_integer(target.get("gateable"))
    if gateable != 1:
        return False, "evidence target gateable flag is not the exact SQLite integer 1"
    command = str(evidence.get("command") or "")
    if not command or not command_matches_template(command, str(target.get("command_template") or "")):
        return False, "evidence command does not match target template"
    exit_code = _sqlite_integer(evidence.get("exit_code"))
    if exit_code != 0:
        return False, "evidence exit code is not the exact SQLite integer zero"
    executed_count = _sqlite_integer(evidence.get("executed_count"))
    if executed_count is None or executed_count <= 0:
        return False, "evidence executed count is not a positive SQLite integer"
    if str(evidence.get("executed_count_source") or "") not in {"parsed", "structured"}:
        return False, "evidence count was not parsed from executor output"
    if str(evidence.get("policy_status") or "") not in {"allowed", "pass"}:
        return False, "evidence execution policy was not allowed"
    result_format = str(evidence.get("result_format") or target.get("result_format") or "regex")
    semantic_status = str(evidence.get("semantic_status") or "")
    if result_format != "regex" and semantic_status != "pass":
        return False, "structured execution did not report semantic pass"
    if str(evidence.get("source_tree_hash") or "") != candidate_sha or not candidate_sha:
        return False, "evidence candidate does not match validation candidate"
    if not _artifact_matches(
        project_root,
        evidence.get("artifact_path"),
        evidence.get("stdout_sha256"),
        pinned_fs=pinned_fs,
    ):
        return False, "evidence artifact is missing or has a digest mismatch"
    requires_sandbox = _sqlite_integer(target.get("requires_sandbox"))
    requires_no_network = _sqlite_integer(target.get("requires_no_network"))
    if requires_sandbox not in {0, 1} or requires_no_network not in {0, 1}:
        return False, "evidence target policy flags are not exact SQLite flags"
    sandbox_status = str(evidence.get("sandbox_status") or "")
    no_network = _sqlite_integer(evidence.get("no_network"))
    if no_network not in {0, 1}:
        return False, "evidence no_network is not an exact SQLite flag"
    if requires_sandbox == 1 and sandbox_status != "available":
        return False, "target requires an available sandbox"
    if requires_no_network == 1 and (sandbox_status != "available" or no_network != 1):
        return False, "target requires an available no-network sandbox"
    return True, "controller execution is migration-eligible"


def _copy_execution_validation_facts(
    source: sqlite3.Connection,
    destination: sqlite3.Connection,
    current_cycle_id: str,
    project_root: Path,
    pinned_fs: ProjectFS | None = None,
) -> tuple[int, int, int]:
    evidence_rows = {str(row["id"]): row for row in _rows(source, "evidence")}
    target_rows = {str(row["id"]): row for row in _rows(source, "test_targets")}
    validation_rows = _rows(source, "validations", order_by="created_at, rowid")
    validation_ids = {str(row["id"]) for row in validation_rows}
    evidence_links: dict[str, list[str]] = {validation_id: [] for validation_id in validation_ids}
    for link in _rows(source, "validation_evidence"):
        validation_id = str(link.get("validation_id") or "")
        evidence_id = str(link.get("evidence_id") or "")
        if validation_id in evidence_links and evidence_id in evidence_rows:
            evidence_links[validation_id].append(evidence_id)
    evidence_scopes: dict[str, set[tuple[str, str]]] = {}
    for validation in validation_rows:
        validation_id = str(validation["id"])
        cycle_id = _normalize_cycle_id(validation.get("cycle_id"), current_cycle_id)
        candidate_sha = str(validation.get("candidate_sha") or validation.get("source_tree_hash") or "")
        for evidence_id in evidence_links.get(validation_id, []):
            evidence_scopes.setdefault(evidence_id, set()).add((cycle_id, candidate_sha))

    execution_bindings: dict[str, str] = {}
    converted_execution_count = 0
    converted_validation_count = 0
    invalidated_validation_count = 0
    pending_supersession: list[tuple[str, str]] = []
    accepted_ids = {
        (str(row[0]), str(row[1]))
        for row in destination.execute("select cycle_id, id from acceptance")
    }
    for validation in validation_rows:
        validation_id = str(validation["id"])
        cycle_id = _normalize_cycle_id(validation.get("cycle_id"), current_cycle_id)
        candidate_sha = str(validation.get("candidate_sha") or validation.get("source_tree_hash") or "")
        linked_execution_ids: list[str] = []
        rejection_reasons: list[str] = []
        for evidence_id in evidence_links.get(validation_id, []):
            evidence = evidence_rows[evidence_id]
            if evidence_scopes.get(evidence_id) != {(cycle_id, candidate_sha)}:
                rejection_reasons.append(
                    f"{evidence_id}: evidence is ambiguously linked across cycle or candidate scopes"
                )
                continue
            target_id = str(evidence.get("target_id") or validation.get("target_id") or "")
            target = target_rows.get(target_id)
            eligible, reason = _eligible_execution(
                evidence,
                target,
                project_root=project_root,
                candidate_sha=candidate_sha,
                pinned_fs=pinned_fs,
            )
            if not eligible:
                rejection_reasons.append(f"{evidence_id}: {reason}")
                continue
            execution_id = execution_bindings.get(evidence_id)
            if execution_id is None:
                execution_id = _execution_id(evidence_id)
                execution_bindings[evidence_id] = execution_id
                result_format = str(evidence.get("result_format") or target.get("result_format") or "regex")
                semantic_status = str(evidence.get("semantic_status") or "") or "pass"
                sandbox_status = str(evidence.get("sandbox_status") or "")
                runner = (
                    "container"
                    if sandbox_status == "available" or evidence.get("no_network") == 1
                    else "local"
                )
                _insert(
                    destination,
                    "executions",
                    {
                        "id": execution_id,
                        "cycle_id": cycle_id,
                        "candidate_sha": candidate_sha,
                        "target_id": target_id,
                        "command": evidence["command"],
                        "exit_code": evidence["exit_code"],
                        "stdout_sha256": str(evidence["stdout_sha256"]),
                        "artifact_path": str(evidence["artifact_path"]),
                        "executed_count": evidence["executed_count"],
                        "result_format": result_format,
                        "semantic_status": semantic_status,
                        "runner": runner,
                        "sandbox_status": sandbox_status,
                        "no_network": evidence.get("no_network"),
                        "policy_status": str(evidence.get("policy_status") or "allowed"),
                        "created_at": str(evidence["created_at"]),
                    },
                )
                converted_execution_count += 1
            linked_execution_ids.append(execution_id)

        acceptance_id_value = str(validation.get("acceptance_id") or "")
        acceptance_id: str | None = acceptance_id_value or None
        acceptance_valid = acceptance_id is None or (cycle_id, acceptance_id) in accepted_ids
        if not acceptance_valid:
            rejection_reasons.append(f"missing acceptance {acceptance_id}")
            acceptance_id = None
        bound = bool(linked_execution_ids) and acceptance_valid
        validation_status = str(validation.get("validation_status") or "active") if bound else "invalidated"
        findings = str(validation.get("findings") or "")
        if not bound:
            invalidated_validation_count += 1
            reason = "; ".join(rejection_reasons) or "validation has no controller execution link"
            findings = (findings + "; " if findings else "") + f"migration invalidated: {reason}"
        else:
            converted_validation_count += 1
        _insert(
            destination,
            "validations",
            {
                "id": validation_id,
                "cycle_id": cycle_id,
                "candidate_sha": candidate_sha,
                "acceptance_id": acceptance_id,
                "surface": validation.get("surface") or "legacy validation",
                "result": validation.get("result") or "fail",
                "validation_status": validation_status,
                "superseded_by": None,
                "findings": findings,
                "residual_risk": validation.get("residual_risk") or "",
                "created_at": validation["created_at"],
            },
        )
        for execution_id in sorted(set(linked_execution_ids)):
            _insert(
                destination,
                "validation_executions",
                {
                    "validation_id": validation_id,
                    "execution_id": execution_id,
                    "cycle_id": cycle_id,
                    "candidate_sha": candidate_sha,
                },
            )
        if not bound:
            _insert(
                destination,
                "invalidations",
                {
                    "id": _invalidation_id(validation_id),
                    "cycle_id": cycle_id,
                    "source_type": "validation",
                    "source_id": validation_id,
                    "target_type": "validation",
                    "target_id": validation_id,
                    "reason": "legacy validation has no migration-eligible controller execution",
                    "resolved_at": None,
                    "created_at": validation["created_at"],
                },
            )
        superseded_by = str(validation.get("superseded_by") or "").strip()
        if superseded_by:
            pending_supersession.append((validation_id, superseded_by))

    for validation_id, superseded_by in pending_supersession:
        if superseded_by not in validation_ids:
            raise LocalCoreMigrationError(
                f"validation {validation_id} references missing supersession target: {superseded_by}"
            )
        destination.execute(
            "update validations set superseded_by = ? where id = ?",
            (superseded_by, validation_id),
        )

    failure_mode_ids = {
        (str(row[0]), str(row[1]))
        for row in destination.execute("select cycle_id, id from failure_modes")
    }
    for link in _rows(source, "validation_failure_modes"):
        validation_id = str(link.get("validation_id") or "")
        cycle_id = _normalize_cycle_id(link.get("cycle_id"), current_cycle_id)
        failure_mode_id = str(link.get("failure_mode_id") or "")
        if validation_id in validation_ids and (cycle_id, failure_mode_id) in failure_mode_ids:
            _insert(
                destination,
                "validation_failure_modes",
                {
                    "validation_id": validation_id,
                    "cycle_id": cycle_id,
                    "failure_mode_id": failure_mode_id,
                },
            )
    return converted_execution_count, converted_validation_count, invalidated_validation_count


def _copy_events(source: sqlite3.Connection, destination: sqlite3.Connection) -> tuple[int, int]:
    copied = 0
    dropped = 0
    for row in _rows(source, "events", order_by="sequence"):
        event_type = str(row.get("type") or "")
        lowered = event_type.lower()
        if any(marker in lowered for marker in RETIRED_EVENT_MARKERS):
            dropped += 1
            continue
        event_id = str(row.get("id") or uuid.uuid4())
        target = str(row.get("target") or "legacy_event")
        target_parts = target.split(":", 1)
        entity_type = target_parts[0] or "legacy_event"
        entity_id = target_parts[1] if len(target_parts) == 2 else event_id
        _insert(
            destination,
            "events",
            {
                "id": event_id,
                "schema_version": SCHEMA30_VERSION,
                "event_type": event_type or "legacy_audit_event",
                "entity_type": entity_type,
                "entity_id": entity_id or event_id,
                "actor": "schema-migration",
                "command": "import legacy local audit event",
                "before_json": "{}",
                "after_json": "{}",
                "correlation_id": row.get("correlation_id") or str(uuid.uuid4()),
                "created_at": row["created_at"],
            },
        )
        copied += 1
    return copied, dropped


def _copy_schema29_local_facts(
    source: sqlite3.Connection,
    destination: sqlite3.Connection,
    source_sha256: str,
    staging_path: Path,
    project_root: Path,
    fail_at: str | None,
    pinned_fs: ProjectFS | None = None,
) -> tuple[int, int, int, int]:
    project = _source_project(source)
    current_cycle_id = _copy_delivery_cycles(source, destination, project)
    _copy_project(destination, project, current_cycle_id)
    session_contexts = _session_contexts(source)

    def normalize_cycle(values: dict[str, object]) -> dict[str, object]:
        values["cycle_id"] = _normalize_cycle_id(values.get("cycle_id"), current_cycle_id)
        return values

    _copy_intersection(source, destination, "requirements", transform=normalize_cycle)
    _copy_intersection(source, destination, "acceptance", transform=normalize_cycle)

    def normalize_failure_mode(values: dict[str, object]) -> dict[str, object]:
        values = normalize_cycle(values)
        for key in ("accepted_by", "acceptance_reason", "acceptance_scope", "expires_at"):
            values[key] = values.get(key) or ""
        return values

    _copy_intersection(source, destination, "failure_modes", transform=normalize_failure_mode)
    _inject_failure(fail_at, "during_relation_copy")
    for relation in ("requirement_acceptance", "failure_mode_acceptance"):
        _copy_intersection(source, destination, relation, transform=normalize_cycle)
    _copy_baselines(source, destination, current_cycle_id)
    _copy_tasks(source, destination, current_cycle_id, session_contexts)
    for relation in ("task_acceptance", "task_failure_modes", "task_dependencies"):
        _copy_intersection(source, destination, relation, transform=normalize_cycle)
    def validate_test_target_flags(values: dict[str, object]) -> dict[str, object]:
        target_id = values.get("id")
        for field in ("gateable", "requires_sandbox", "requires_no_network"):
            values[field] = _sqlite_flag(
                values.get(field),
                field=f"test target {target_id}.{field}",
            )
        return values

    _copy_intersection(
        source,
        destination,
        "test_targets",
        transform=validate_test_target_flags,
    )
    _copy_intersection(source, destination, "task_test_targets", transform=normalize_cycle)
    (
        converted_execution_count,
        converted_validation_count,
        invalidated_validation_count,
    ) = _copy_execution_validation_facts(
        source,
        destination,
        current_cycle_id,
        project_root,
        pinned_fs,
    )

    source_finding_rows = {
        str(row.get("id") or ""): row for row in _rows(source, "findings")
    }
    evidence_candidates = {
        str(row.get("id") or ""): str(row.get("source_tree_hash") or "").strip()
        for row in _rows(source, "evidence")
    }
    gate_scopes = {
        str(row.get("id") or ""): (
            _normalize_cycle_id(row.get("cycle_id"), current_cycle_id),
            _legacy_gate_candidate(row),
        )
        for row in _rows(source, "quality_gates")
    }
    finding_gate_scopes: dict[str, set[tuple[str, str]]] = {}
    for link in _rows(source, "quality_gate_findings"):
        finding_id = str(link.get("finding_id") or "")
        gate_scope = gate_scopes.get(str(link.get("gate_id") or ""))
        if finding_id and gate_scope is not None:
            finding_gate_scopes.setdefault(finding_id, set()).add(gate_scope)

    def normalize_finding(values: dict[str, object]) -> dict[str, object]:
        values = normalize_cycle(values)
        finding_id = str(values.get("id") or "")
        source_row = source_finding_rows.get(finding_id, {})
        candidate_sources = {
            candidate
            for candidate in (
                str(values.get("candidate_sha") or "").strip(),
                evidence_candidates.get(str(source_row.get("evidence_id") or ""), ""),
                *(candidate for _, candidate in finding_gate_scopes.get(finding_id, set())),
            )
            if candidate
        }
        linked_cycles = {
            cycle_id for cycle_id, _ in finding_gate_scopes.get(finding_id, set())
        }
        if linked_cycles and linked_cycles != {str(values["cycle_id"])}:
            raise LocalCoreMigrationError(
                "finding is linked to quality gates from a different cycle during "
                f"migration: {finding_id}"
            )
        if len(candidate_sources) > 1:
            raise LocalCoreMigrationError(
                "finding has conflicting candidate provenance during migration: "
                f"{finding_id}"
            )
        values["candidate_sha"] = next(iter(candidate_sources), "")
        for key in ("waived_by", "waiver_reason", "waiver_scope", "waiver_expires_at"):
            values[key] = values.get(key) or ""
        return values

    _copy_intersection(source, destination, "findings", transform=normalize_finding)
    _copy_quality_gates(source, destination, current_cycle_id, session_contexts)

    gate_ids = {str(row[0]) for row in destination.execute("select id from quality_gates")}
    finding_ids = {str(row[0]) for row in destination.execute("select id from findings")}

    def valid_gate_finding(values: dict[str, object]) -> dict[str, object] | None:
        if str(values.get("gate_id") or "") not in gate_ids:
            return None
        if str(values.get("finding_id") or "") not in finding_ids:
            return None
        return values

    _copy_intersection(source, destination, "quality_gate_findings", transform=valid_gate_finding)

    def normalize_delivery(values: dict[str, object]) -> dict[str, object]:
        values = normalize_cycle(values)
        values["candidate_sha"] = values.get("candidate_sha") or ""
        values["decision_status"] = "historical-migrated"
        return values

    _copy_intersection(source, destination, "deliveries", transform=normalize_delivery)
    delivery_ids = {str(row[0]) for row in destination.execute("select id from deliveries")}

    def valid_delivery_acceptance(values: dict[str, object]) -> dict[str, object] | None:
        values = normalize_cycle(values)
        return values if str(values.get("delivery_id") or "") in delivery_ids else None

    _copy_intersection(source, destination, "delivery_acceptance", transform=valid_delivery_acceptance)
    _copy_intersection(source, destination, "decisions")

    def local_invalidation(values: dict[str, object]) -> dict[str, object] | None:
        values = normalize_cycle(values)
        source_type = str(values.get("source_type") or "").replace("-", "_")
        target_type = str(values.get("target_type") or "").replace("-", "_")
        if (
            source_type not in LOCAL_INVALIDATION_SOURCE_TYPES
            or target_type not in LOCAL_INVALIDATION_TARGET_TYPES
        ):
            return None
        values["source_type"] = source_type
        values["target_type"] = target_type
        return values

    _copy_intersection(source, destination, "invalidations", transform=local_invalidation)

    for row in _rows(source, "migrations"):
        _insert(
            destination,
            "migrations",
            {
                "from_version": row["from_version"],
                "to_version": row["to_version"],
                "source_sha256": "",
                "backup_path": "",
                "manifest_path": "",
                "row_counts_json": "{}",
                "dropped_table_count": 0,
                "status": "legacy-history",
                "applied_at": row["applied_at"],
            },
        )
    _insert(
        destination,
        "migrations",
        {
            "from_version": 29,
            "to_version": SCHEMA30_VERSION,
            "source_sha256": source_sha256,
            "backup_path": "",
            "manifest_path": str(staging_path),
            "row_counts_json": "{}",
            "dropped_table_count": 0,
            "status": "staged",
            "applied_at": str(project.get("updated_at") or "migration"),
        },
    )
    _, dropped_events = _copy_events(source, destination)
    return (
        dropped_events,
        converted_execution_count,
        converted_validation_count,
        invalidated_validation_count,
    )


def _validate_staging_database(conn: sqlite3.Connection, *, fail_at: str | None = None) -> None:
    _inject_failure(fail_at, "during_invariant_validation")
    tables = set(_catalog_table_names(conn))
    if tables != SCHEMA30_CATALOG_TABLES:
        raise LocalCoreMigrationError(
            "staging table inventory mismatch: "
            f"missing={sorted(SCHEMA30_CATALOG_TABLES - tables)} "
            f"extra={sorted(tables - SCHEMA30_CATALOG_TABLES)}"
        )
    integrity = [str(row[0]) for row in conn.execute("pragma integrity_check")]
    if integrity != ["ok"]:
        raise LocalCoreMigrationError(f"staging integrity check failed: {integrity}")
    foreign_keys = conn.execute("pragma foreign_key_check").fetchall()
    if foreign_keys:
        raise LocalCoreMigrationError(f"staging foreign key check failed: {len(foreign_keys)} issue(s)")
    project = conn.execute("select schema_version, runtime_version from project where id = 1").fetchone()
    if project is None or int(project[0]) != SCHEMA30_VERSION or str(project[1]) != SCHEMA30_RUNTIME_VERSION:
        raise LocalCoreMigrationError(f"staging project metadata is invalid: {tuple(project) if project else None}")


def stage_schema29_to_schema30(
    source_path: Path,
    staging_path: Path,
    *,
    project_root: Path | None = None,
    fail_at: str | None = None,
    source_fs: ProjectFS | None = None,
    destination_fs: ProjectFS | None = None,
) -> LocalCoreStagingReport:
    """Create a validated schema 30 staging DB without modifying the schema 29 source."""

    source_root = _project_root_for_database(source_path)
    destination_root = project_root or _project_root_for_internal_path(staging_path)
    created_staging = False
    with _project_fs_scope(
        source_root,
        source_fs,
    ) as active_source_fs, _project_fs_scope(
        destination_root,
        destination_fs,
    ) as active_destination_fs:
        source_relative = active_source_fs.relative_to_root(source_path)
        staging_relative = active_destination_fs.relative_to_root(staging_path)
        source_snapshot = active_source_fs._snapshot(
            source_relative,
            allow_missing=True,
        )
        if not source_snapshot.exists:
            raise LocalCoreMigrationError(
                "schema 29 source database is missing: "
                f"{active_source_fs.absolute(source_relative)}"
            )
        if (
            active_source_fs.root_identity_key
            == active_destination_fs.root_identity_key
            and source_relative == staging_relative
        ):
            raise LocalCoreMigrationError(
                "staging database must not replace the active source"
            )
        staging_snapshot = active_destination_fs._snapshot(
            staging_relative,
            allow_missing=True,
        )
        if staging_snapshot.exists:
            raise LocalCoreMigrationError(
                "staging database already exists: "
                f"{active_destination_fs.absolute(staging_relative)}"
            )
        active_source_fs.audit(
            _database_family(source_relative),
            allow_missing=True,
        )
        active_destination_fs.audit(
            _database_family(staging_relative),
            allow_missing=True,
        )
        active_destination_fs.create_exclusive(
            staging_relative,
            b"",
            mode=0o600,
        )
        created_staging = True
        staging_snapshot = active_destination_fs._snapshot(
            staging_relative,
            allow_missing=False,
        )
        source_fingerprint = _safe_database_fingerprint(
            active_source_fs,
            source_relative,
        )
        source_sha256 = _safe_database_digest(
            active_source_fs,
            source_relative,
        )
        source_absolute = active_source_fs.absolute(source_relative)
        staging_absolute = active_destination_fs.absolute(staging_relative)
        try:
            with _verified_sqlite_connection(
                active_source_fs,
                source_relative,
                access="ro",
            ) as source:
                source.row_factory = sqlite3.Row
                source.execute("pragma query_only = on")
                active_source_fs._assert_unchanged(
                    source_relative,
                    source_snapshot,
                )
                integrity = [
                    str(row[0])
                    for row in source.execute("pragma integrity_check")
                ]
                foreign_keys = source.execute(
                    "pragma foreign_key_check"
                ).fetchall()
                if integrity != ["ok"] or foreign_keys:
                    raise LocalCoreMigrationError(
                        "schema 29 source failed validation: "
                        f"integrity={integrity} "
                        f"foreign_keys={len(foreign_keys)}"
                    )
                project = _source_project(source)
                source_counts = _row_counts(source)
                with _verified_sqlite_connection(
                    active_destination_fs,
                    staging_relative,
                    access="rw",
                    journal_mode="memory",
                ) as destination:
                    destination.row_factory = sqlite3.Row
                    destination.execute("pragma foreign_keys = on")
                    destination.execute("begin immediate")
                    create_schema30(destination)
                    (
                        dropped_event_count,
                        converted_execution_count,
                        converted_validation_count,
                        invalidated_validation_count,
                    ) = _copy_schema29_local_facts(
                        source,
                        destination,
                        source_sha256,
                        staging_absolute,
                        Path(destination_root).expanduser().absolute(),
                        fail_at,
                        active_destination_fs,
                    )
                    destination.commit()
                    destination.execute("pragma journal_mode = delete")
                    _validate_staging_database(
                        destination,
                        fail_at=fail_at,
                    )
                    staging_counts = _row_counts(destination)
                    active_destination_fs._assert_unchanged(
                        staging_relative,
                        staging_snapshot,
                    )
                active_source_fs._assert_unchanged(
                    source_relative,
                    source_snapshot,
                )
                if int(project["schema_version"]) != 29:
                    raise LocalCoreMigrationError(
                        "schema version changed during staging conversion"
                    )

            active_destination_fs.audit(
                _database_family(staging_relative),
                allow_missing=True,
            )
            if (
                _safe_database_fingerprint(
                    active_source_fs,
                    source_relative,
                )
                != source_fingerprint
            ):
                raise LocalCoreMigrationError(
                    "active schema 29 database changed during staging conversion"
                )
            retired_counts = {
                table: count
                for table, count in source_counts.items()
                if table not in SCHEMA30_TABLES
            }
            return LocalCoreStagingReport(
                source_version=29,
                target_version=SCHEMA30_VERSION,
                source_path=str(source_absolute),
                staging_path=str(staging_absolute),
                source_sha256=source_sha256,
                staging_sha256=_safe_database_digest(
                    active_destination_fs,
                    staging_relative,
                ),
                source_row_counts=source_counts,
                staging_row_counts=staging_counts,
                retired_row_counts=retired_counts,
                dropped_event_count=dropped_event_count,
                converted_execution_count=converted_execution_count,
                converted_validation_count=converted_validation_count,
                invalidated_validation_count=invalidated_validation_count,
            )
        except BaseException as exc:
            if created_staging:
                _apply_sqlite_teardown_errors(
                    exc,
                    _temporary_sqlite_family_cleanup_errors(
                        active_destination_fs,
                        staging_relative,
                        staging_snapshot,
                        published=False,
                    ),
                )
            raise


def _read_source_version(
    path: Path,
    *,
    pinned_fs: ProjectFS | None = None,
) -> int:
    with _project_fs_scope(
        _project_root_for_database(path),
        pinned_fs,
    ) as project_fs:
        relative = project_fs.relative_to_root(path)
        snapshot = project_fs._snapshot(relative, allow_missing=False)
        project_fs.audit(_database_family(relative), allow_missing=True)
        with _verified_sqlite_connection(
            project_fs,
            relative,
            access="ro",
        ) as conn:
            row = conn.execute(
                "select schema_version from project where id=1"
            ).fetchone()
            project_fs._assert_unchanged(relative, snapshot)
    if row is None:
        raise LocalCoreMigrationError("legacy source is missing project schema metadata")
    return int(row[0])


def _sqlite_backup_copy(
    source_path: Path,
    destination_path: Path,
    *,
    source_fs: ProjectFS | None = None,
    destination_fs: ProjectFS | None = None,
) -> None:
    with _project_fs_scope(
        _project_root_for_database(source_path),
        source_fs,
    ) as active_source_fs, _project_fs_scope(
        _project_root_for_database(destination_path),
        destination_fs,
    ) as active_destination_fs:
        source_relative = active_source_fs.relative_to_root(source_path)
        destination_relative = active_destination_fs.relative_to_root(
            destination_path
        )
        source_snapshot = active_source_fs._snapshot(
            source_relative,
            allow_missing=False,
        )
        destination_snapshot = active_destination_fs._snapshot(
            destination_relative,
            allow_missing=True,
        )
        if destination_snapshot.exists:
            raise LocalCoreMigrationError(
                f"isolated backup destination already exists: {destination_path}"
            )
        active_source_fs.audit(
            _database_family(source_relative),
            allow_missing=True,
        )
        active_destination_fs.audit(
            _database_family(destination_relative),
            allow_missing=True,
        )
        active_destination_fs.create_exclusive(
            destination_relative,
            b"",
            mode=0o600,
        )
        destination_snapshot = active_destination_fs._snapshot(
            destination_relative,
            allow_missing=False,
        )
        try:
            with _verified_sqlite_connection(
                active_source_fs,
                source_relative,
                access="ro",
            ) as source:
                source.execute("pragma query_only = on")
                active_source_fs._assert_unchanged(
                    source_relative,
                    source_snapshot,
                )
                with _verified_sqlite_connection(
                    active_destination_fs,
                    destination_relative,
                    access="rw",
                    journal_mode="memory",
                ) as destination:
                    source.backup(destination)
                    destination.execute("pragma journal_mode = delete")
                    destination.commit()
                    active_destination_fs._assert_unchanged(
                        destination_relative,
                        destination_snapshot,
                    )
                active_source_fs._assert_unchanged(
                    source_relative,
                    source_snapshot,
                )
        except BaseException as exc:
            _apply_sqlite_teardown_errors(
                exc,
                _temporary_sqlite_family_cleanup_errors(
                    active_destination_fs,
                    destination_relative,
                    destination_snapshot,
                    published=False,
                ),
            )
            raise


def _validate_legacy_trust_revisions(
    source_path: Path,
    source_version: int,
    *,
    pinned_fs: ProjectFS | None = None,
) -> None:
    """Reject malformed trust revisions before legacy SQLite arithmetic can coerce them."""

    with _project_fs_scope(
        _project_root_for_database(source_path),
        pinned_fs,
    ) as project_fs:
        relative = project_fs.relative_to_root(source_path)
        snapshot = project_fs._snapshot(relative, allow_missing=False)
        project_fs.audit(_database_family(relative), allow_missing=True)
        with _verified_sqlite_connection(
            project_fs,
            relative,
            access="ro",
        ) as source:
            project = source.execute(
                "select revision from project where id=1"
            ).fetchone()
            if project is None:
                raise LocalCoreMigrationError(
                    f"schema {source_version} source is missing project revision metadata"
                )
            _positive_sqlite_integer(
                project[0],
                field="project.revision",
                source_schema=source_version,
            )

            if (
                "quality_gates" in _table_names(source)
                and "project_revision"
                in _columns(source, "quality_gates")
            ):
                for gate_id, revision in source.execute(
                    "select id, project_revision from quality_gates order by rowid"
                ):
                    _positive_sqlite_integer(
                        revision,
                        field=(
                            f"quality gate {gate_id}.project_revision"
                        ),
                        source_schema=source_version,
                    )
            project_fs._assert_unchanged(relative, snapshot)


def stage_supported_schema_to_schema30(
    source_path: Path,
    staging_path: Path,
    *,
    project_root: Path | None = None,
    fail_at: str | None = None,
    pinned_fs: ProjectFS | None = None,
) -> LocalCoreStagingReport:
    """Stage schema 27/28 through an isolated schema 29 copy, or convert schema 29 directly."""
    source_version = _read_source_version(
        source_path,
        pinned_fs=pinned_fs,
    )
    if source_version == 29:
        return stage_schema29_to_schema30(
            source_path,
            staging_path,
            project_root=project_root,
            fail_at=fail_at,
            source_fs=pinned_fs,
            destination_fs=pinned_fs,
        )
    if source_version not in {27, 28}:
        raise LocalCoreMigrationError(
            f"unsupported local-core migration source schema {source_version}; "
            "install the last v1 release and migrate to schema 27, 28, or 29 first"
        )

    _validate_legacy_trust_revisions(
        source_path,
        source_version,
        pinned_fs=pinned_fs,
    )
    source_root = _project_root_for_database(source_path)
    with _project_fs_scope(source_root, pinned_fs) as source_fs:
        source_relative = source_fs.relative_to_root(source_path)
        source_snapshot = source_fs._snapshot(
            source_relative,
            allow_missing=False,
        )
        source_fingerprint = _safe_database_fingerprint(
            source_fs,
            source_relative,
        )
        source_sha256 = _safe_database_digest(source_fs, source_relative)
        with _verified_sqlite_connection(
            source_fs,
            source_relative,
            access="ro",
        ) as original:
            original_counts = _row_counts(original)
            source_fs._assert_unchanged(
                source_relative,
                source_snapshot,
            )

    with tempfile.TemporaryDirectory(
        prefix=f"schema{source_version}-legacy-stage-"
    ) as temp:
        legacy_root = Path(temp)
        legacy_db = legacy_root / ".ai-team/state/harness.db"
        _sqlite_backup_copy(
            source_path,
            legacy_db,
            source_fs=pinned_fs,
        )
        try:
            import harness_db as legacy_runtime
        except ImportError as exc:
            raise LocalCoreMigrationError("isolated v1 migration runtime is unavailable") from exc
        try:
            legacy_runtime.migrate_legacy_to_schema29(legacy_root, source_version)
        except Exception as exc:
            raise LocalCoreMigrationError(
                f"isolated schema {source_version}->29 conversion failed: {exc}"
            ) from exc
        if _read_source_version(legacy_db) != 29:
            raise LocalCoreMigrationError(
                f"isolated schema {source_version}->29 conversion did not produce schema 29"
            )
        report = stage_schema29_to_schema30(
            legacy_db,
            staging_path,
            project_root=project_root or _project_root_for_database(source_path),
            fail_at=fail_at,
            destination_fs=pinned_fs,
        )

    with _project_fs_scope(
        source_root,
        pinned_fs,
    ) as source_fs, _project_fs_scope(
        project_root or _project_root_for_internal_path(staging_path),
        pinned_fs,
    ) as destination_fs:
        source_relative = source_fs.relative_to_root(source_path)
        staging_relative = destination_fs.relative_to_root(staging_path)
        source_absolute = source_fs.absolute(source_relative)
        staging_snapshot = destination_fs._snapshot(
            staging_relative,
            allow_missing=False,
        )
        if (
            _safe_database_fingerprint(source_fs, source_relative)
            != source_fingerprint
        ):
            error = LocalCoreMigrationError(
                f"active schema {source_version} database changed during isolated legacy conversion"
            )
            _apply_sqlite_teardown_errors(
                error,
                _temporary_sqlite_family_cleanup_errors(
                    destination_fs,
                    staging_relative,
                    staging_snapshot,
                    published=False,
                ),
            )
            raise error
    return replace(
        report,
        source_version=source_version,
        source_path=str(source_absolute),
        source_sha256=source_sha256,
        source_row_counts=original_counts,
    )


SCHEMA30_TO31_COPY_ORDER = (
    "delivery_cycles",
    "project",
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
)


def _validate_schema30_source_contract(conn: sqlite3.Connection) -> int:
    tables = set(_catalog_table_names(conn))
    if tables != SCHEMA30_CATALOG_TABLES:
        raise LocalCoreMigrationError(
            "schema 30 source table inventory mismatch: "
            f"missing={sorted(SCHEMA30_CATALOG_TABLES - tables)} "
            f"extra={sorted(tables - SCHEMA30_CATALOG_TABLES)}"
        )
    integrity = [str(row[0]) for row in conn.execute("pragma integrity_check")]
    foreign_keys = conn.execute("pragma foreign_key_check").fetchall()
    if integrity != ["ok"] or foreign_keys:
        raise LocalCoreMigrationError(
            "schema 30 source failed validation: "
            f"integrity={integrity} foreign_keys={len(foreign_keys)}"
        )
    project = conn.execute(
        "select schema_version, runtime_version from project where id=1"
    ).fetchone()
    if (
        project is None
        or int(project[0]) != SCHEMA30_VERSION
        or str(project[1]) != SCHEMA30_RUNTIME_VERSION
    ):
        raise LocalCoreMigrationError(
            "schema 30 source project metadata is invalid: "
            f"{tuple(project) if project else None}"
        )

    state_contracts = (
        ("requirements", "requirement", REQUIREMENT_STATUSES),
        ("acceptance", "acceptance", ACCEPTANCE_STATUSES),
        (
            "failure_modes",
            "failure-mode",
            FAILURE_MODE_STATUSES | {"active"},
        ),
    )
    for table, label, allowed in state_contracts:
        for row in conn.execute(
            f"select id, status from {_quote_identifier(table)} order by rowid"
        ):
            value = str(row[1])
            if value not in allowed:
                raise LocalCoreMigrationError(
                    f"invalid {label} status: {table}:{row[0]}.status={value!r}"
                )
    return int(
        conn.execute(
            "select count(*) from failure_modes where status='active'"
        ).fetchone()[0]
    )


def preflight_schema30_to_active(
    source_path: Path,
    *,
    pinned_fs: ProjectFS | None = None,
) -> int:
    """Validate schema-30 state domains before backup or migration publication."""

    with _project_fs_scope(
        _project_root_for_database(source_path),
        pinned_fs,
    ) as project_fs:
        relative = project_fs.relative_to_root(source_path)
        snapshot = project_fs._snapshot(relative, allow_missing=False)
        project_fs.audit(_database_family(relative), allow_missing=True)
        with _verified_sqlite_connection(
            project_fs,
            relative,
            access="ro",
        ) as source:
            source.row_factory = sqlite3.Row
            source.execute("pragma query_only = on")
            normalized = _validate_schema30_source_contract(source)
            project_fs._assert_unchanged(relative, snapshot)
        project_fs._assert_unchanged(relative, snapshot)
        return normalized


def _copy_schema30_facts_to_schema31(
    source: sqlite3.Connection,
    destination: sqlite3.Connection,
    *,
    source_sha256: str,
    staging_path: Path,
    fail_at: str | None,
) -> tuple[int, int]:
    normalized_failure_modes = _validate_schema30_source_contract(source)

    def transform(
        table: str,
        values: dict[str, object],
    ) -> dict[str, object]:
        if table == "project":
            values["schema_version"] = ACTIVE_SCHEMA_VERSION
            values["runtime_version"] = ACTIVE_RUNTIME_VERSION
        elif table == "failure_modes" and values.get("status") == "active":
            values["status"] = "identified"
        elif table == "executions":
            values.update(
                {
                    "target_definition_sha256": "",
                    "platform": "",
                    "runtime_executable": "",
                    "runtime_version": "",
                    "runtime_executable_sha256": "",
                    "policy_version": "",
                    "container_engine": "",
                    "container_engine_version": "",
                    "container_image_requested": "",
                    "container_image_digest": "",
                    "provenance_status": "legacy-incomplete",
                }
            )
        elif table == "validations":
            values["qualification_id"] = None
        elif table == "events":
            values["schema_version"] = ACTIVE_SCHEMA_VERSION
        return values

    for table in SCHEMA30_TO31_COPY_ORDER:
        if table == "requirement_acceptance":
            _inject_failure(fail_at, "during_relation_copy")
        for row in _rows(source, table):
            values = transform(table, dict(row))
            _insert(destination, table, values)

    project = source.execute(
        "select updated_at from project where id=1"
    ).fetchone()
    _insert(
        destination,
        "migrations",
        {
            "from_version": SCHEMA30_VERSION,
            "to_version": ACTIVE_SCHEMA_VERSION,
            "source_sha256": source_sha256,
            "backup_path": "",
            "manifest_path": str(staging_path),
            "row_counts_json": "{}",
            "dropped_table_count": 0,
            "status": "staged",
            "applied_at": str(project[0] if project else "migration"),
        },
    )
    return (
        int(source.execute("select count(*) from executions").fetchone()[0]),
        normalized_failure_modes,
    )


def _validate_active_staging_database(
    conn: sqlite3.Connection,
    *,
    fail_at: str | None = None,
) -> None:
    _inject_failure(fail_at, "during_invariant_validation")
    tables = set(_catalog_table_names(conn))
    if tables != ACTIVE_SCHEMA_CATALOG_TABLES:
        raise LocalCoreMigrationError(
            "active staging table inventory mismatch: "
            f"missing={sorted(ACTIVE_SCHEMA_CATALOG_TABLES - tables)} "
            f"extra={sorted(tables - ACTIVE_SCHEMA_CATALOG_TABLES)}"
        )
    integrity = [str(row[0]) for row in conn.execute("pragma integrity_check")]
    if integrity != ["ok"]:
        raise LocalCoreMigrationError(
            f"active staging integrity check failed: {integrity}"
        )
    foreign_keys = conn.execute("pragma foreign_key_check").fetchall()
    if foreign_keys:
        raise LocalCoreMigrationError(
            "active staging foreign key check failed: "
            f"{len(foreign_keys)} issue(s)"
        )
    project = conn.execute(
        "select schema_version, runtime_version from project where id=1"
    ).fetchone()
    if (
        project is None
        or int(project[0]) != ACTIVE_SCHEMA_VERSION
        or str(project[1]) != ACTIVE_RUNTIME_VERSION
    ):
        raise LocalCoreMigrationError(
            "active staging project metadata is invalid: "
            f"{tuple(project) if project else None}"
        )


def stage_schema30_to_schema31(
    source_path: Path,
    staging_path: Path,
    *,
    project_root: Path | None = None,
    fail_at: str | None = None,
    source_fs: ProjectFS | None = None,
    destination_fs: ProjectFS | None = None,
) -> LocalCoreStagingReport:
    """Create a validated schema-31 staging DB without mutating schema 30."""

    source_root = _project_root_for_database(source_path)
    destination_root = project_root or _project_root_for_internal_path(
        staging_path
    )
    created_staging = False
    with _project_fs_scope(
        source_root,
        source_fs,
    ) as active_source_fs, _project_fs_scope(
        destination_root,
        destination_fs,
    ) as active_destination_fs:
        source_relative = active_source_fs.relative_to_root(source_path)
        staging_relative = active_destination_fs.relative_to_root(staging_path)
        source_snapshot = active_source_fs._snapshot(
            source_relative,
            allow_missing=True,
        )
        if not source_snapshot.exists:
            raise LocalCoreMigrationError(
                f"schema 30 source database is missing: {source_path}"
            )
        if (
            active_source_fs.root_identity_key
            == active_destination_fs.root_identity_key
            and source_relative == staging_relative
        ):
            raise LocalCoreMigrationError(
                "staging database must not replace the active source"
            )
        staging_snapshot = active_destination_fs._snapshot(
            staging_relative,
            allow_missing=True,
        )
        if staging_snapshot.exists:
            raise LocalCoreMigrationError(
                f"staging database already exists: {staging_path}"
            )
        active_source_fs.audit(
            _database_family(source_relative),
            allow_missing=True,
        )
        active_destination_fs.audit(
            _database_family(staging_relative),
            allow_missing=True,
        )
        active_destination_fs.create_exclusive(
            staging_relative,
            b"",
            mode=0o600,
        )
        created_staging = True
        staging_snapshot = active_destination_fs._snapshot(
            staging_relative,
            allow_missing=False,
        )
        source_fingerprint = _safe_database_fingerprint(
            active_source_fs,
            source_relative,
        )
        source_sha256 = _safe_database_digest(
            active_source_fs,
            source_relative,
        )
        source_absolute = active_source_fs.absolute(source_relative)
        staging_absolute = active_destination_fs.absolute(staging_relative)
        try:
            with _verified_sqlite_connection(
                active_source_fs,
                source_relative,
                access="ro",
            ) as source:
                source.row_factory = sqlite3.Row
                source.execute("pragma query_only = on")
                normalized_failure_modes = _validate_schema30_source_contract(
                    source
                )
                source_counts = _row_counts(source)
                with _verified_sqlite_connection(
                    active_destination_fs,
                    staging_relative,
                    access="rw",
                    journal_mode="memory",
                ) as destination:
                    destination.row_factory = sqlite3.Row
                    destination.execute("pragma foreign_keys = on")
                    destination.execute("begin immediate")
                    destination.execute("pragma defer_foreign_keys = on")
                    create_active_schema(destination)
                    (
                        converted_execution_count,
                        copied_normalized_failure_modes,
                    ) = _copy_schema30_facts_to_schema31(
                        source,
                        destination,
                        source_sha256=source_sha256,
                        staging_path=staging_absolute,
                        fail_at=fail_at,
                    )
                    if (
                        copied_normalized_failure_modes
                        != normalized_failure_modes
                    ):
                        raise LocalCoreMigrationError(
                            "failure-mode normalization preflight changed during copy"
                        )
                    destination.commit()
                    destination.execute("pragma journal_mode = delete")
                    _validate_active_staging_database(
                        destination,
                        fail_at=fail_at,
                    )
                    staging_counts = _row_counts(destination)
                    active_destination_fs._assert_unchanged(
                        staging_relative,
                        staging_snapshot,
                    )
                active_source_fs._assert_unchanged(
                    source_relative,
                    source_snapshot,
                )

            active_destination_fs.audit(
                _database_family(staging_relative),
                allow_missing=True,
            )
            if (
                _safe_database_fingerprint(
                    active_source_fs,
                    source_relative,
                )
                != source_fingerprint
            ):
                raise LocalCoreMigrationError(
                    "active schema 30 database changed during staging conversion"
                )
            for table in SCHEMA30_TABLES:
                expected = source_counts[table] + (
                    1 if table == "migrations" else 0
                )
                actual = staging_counts[table]
                if actual != expected:
                    raise LocalCoreMigrationError(
                        "schema 30 to 31 row count mismatch: "
                        f"{table} expected={expected} actual={actual}"
                    )
            for table in ACTIVE_SCHEMA_TABLES - SCHEMA30_TABLES:
                if staging_counts[table] != 0:
                    raise LocalCoreMigrationError(
                        f"schema 31 migration invented {table} facts"
                    )
            return LocalCoreStagingReport(
                source_version=SCHEMA30_VERSION,
                target_version=ACTIVE_SCHEMA_VERSION,
                source_path=str(source_absolute),
                staging_path=str(staging_absolute),
                source_sha256=source_sha256,
                staging_sha256=_safe_database_digest(
                    active_destination_fs,
                    staging_relative,
                ),
                source_row_counts=source_counts,
                staging_row_counts=staging_counts,
                retired_row_counts={},
                dropped_event_count=0,
                converted_execution_count=converted_execution_count,
                converted_validation_count=0,
                invalidated_validation_count=0,
                normalized_failure_mode_count=normalized_failure_modes,
            )
        except BaseException as exc:
            if created_staging:
                _apply_sqlite_teardown_errors(
                    exc,
                    _temporary_sqlite_family_cleanup_errors(
                        active_destination_fs,
                        staging_relative,
                        staging_snapshot,
                        published=False,
                    ),
                )
            raise


def stage_supported_schema_to_active(
    source_path: Path,
    staging_path: Path,
    *,
    project_root: Path | None = None,
    fail_at: str | None = None,
    pinned_fs: ProjectFS | None = None,
) -> LocalCoreStagingReport:
    """Stage every supported legacy source to the one active schema contract."""

    source_version = _read_source_version(
        source_path,
        pinned_fs=pinned_fs,
    )
    if source_version == SCHEMA30_VERSION:
        return stage_schema30_to_schema31(
            source_path,
            staging_path,
            project_root=project_root,
            fail_at=fail_at,
            source_fs=pinned_fs,
            destination_fs=pinned_fs,
        )
    if source_version not in {27, 28, 29}:
        raise LocalCoreMigrationError(
            f"unsupported active-schema migration source {source_version}"
        )

    destination_root = project_root or _project_root_for_internal_path(
        staging_path
    )
    intermediate_path = staging_path.with_name(
        f".{staging_path.name}.schema30-intermediate-{uuid.uuid4().hex}.db"
    )
    intermediate_snapshot: _PathSnapshot | None = None
    operation_error: BaseException | None = None
    try:
        legacy_report = stage_supported_schema_to_schema30(
            source_path,
            intermediate_path,
            project_root=destination_root,
            fail_at=fail_at,
            pinned_fs=pinned_fs,
        )
        with _project_fs_scope(destination_root, pinned_fs) as destination_fs:
            intermediate_relative = destination_fs.relative_to_root(
                intermediate_path
            )
            intermediate_snapshot = destination_fs._snapshot(
                intermediate_relative,
                allow_missing=False,
            )
        active_report = stage_schema30_to_schema31(
            intermediate_path,
            staging_path,
            project_root=destination_root,
            fail_at=fail_at,
            source_fs=pinned_fs,
            destination_fs=pinned_fs,
        )
        return replace(
            active_report,
            source_version=legacy_report.source_version,
            source_path=legacy_report.source_path,
            source_sha256=legacy_report.source_sha256,
            source_row_counts=legacy_report.source_row_counts,
            retired_row_counts=legacy_report.retired_row_counts,
            dropped_event_count=legacy_report.dropped_event_count,
            converted_validation_count=legacy_report.converted_validation_count,
            invalidated_validation_count=legacy_report.invalidated_validation_count,
        )
    except BaseException as exc:
        operation_error = exc
        raise
    finally:
        if intermediate_snapshot is not None:
            with _project_fs_scope(destination_root, pinned_fs) as destination_fs:
                intermediate_relative = destination_fs.relative_to_root(
                    intermediate_path
                )
                _apply_sqlite_teardown_errors(
                    operation_error,
                    _temporary_sqlite_family_cleanup_errors(
                        destination_fs,
                        intermediate_relative,
                        intermediate_snapshot,
                        published=False,
                    ),
                )


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json_atomic(
    path: Path,
    payload: dict[str, object],
    *,
    project_fs: ProjectFS | None = None,
    expected_destination: _PathSnapshot | None = None,
) -> _PathSnapshot:
    root = _project_root_for_internal_path(path)
    with _project_fs_scope(root, project_fs) as active_project_fs:
        relative = active_project_fs.relative_to_root(path)
        legacy_temporary = relative.with_name(relative.name + ".tmp")
        active_project_fs.audit(
            (relative, legacy_temporary),
            allow_missing=True,
        )
        return active_project_fs.atomic_write(
            relative,
            (
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8"),
            mode=0o600,
            expected_destination=expected_destination,
        )


def _create_fallback_recovery_manifest(
    canonical_path: Path,
    payload: dict[str, object],
    *,
    manifest_write_error: BaseException,
    project_fs: ProjectFS,
) -> tuple[Path, _PathSnapshot]:
    project_fs.relative_to_root(canonical_path)
    fallback_parent = Path(".ai-team/state")
    for _ in range(128):
        fallback_relative = fallback_parent / (
            f"migration-recovery-{uuid.uuid4().hex}.json"
        )
        fallback_path = project_fs.absolute(fallback_relative)
        fallback_payload = dict(payload)
        fallback_payload.update(
            {
                "status": "rollback-incomplete",
                "canonical_manifest_path": str(canonical_path),
                "failed_manifest_path": str(canonical_path),
                "manifest_write_error": _exception_text(
                    manifest_write_error
                ),
                "recovery_manifest_path": str(fallback_path),
            }
        )
        encoded = (
            json.dumps(
                fallback_payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        try:
            fallback_snapshot = project_fs.create_exclusive(
                fallback_relative,
                encoded,
                mode=0o600,
            )
        except FileExistsError:
            continue
        try:
            project_fs._assert_unchanged(
                fallback_relative,
                fallback_snapshot,
            )
            if (
                _safe_file_sha256(
                    project_fs,
                    fallback_relative,
                    expected=fallback_snapshot,
                )
                != hashlib.sha256(encoded).hexdigest()
            ):
                raise LocalCoreMigrationError(
                    "fallback recovery manifest changed during publication"
                )
            project_fs._assert_unchanged(
                fallback_relative,
                fallback_snapshot,
            )
        except (ProjectPathSafetyError, LocalCoreMigrationError):
            # The raced entry is not ours to remove. Reserve a new unique
            # diagnostic path and keep the changed entry fail-closed.
            continue
        return fallback_path, fallback_snapshot
    raise LocalCoreMigrationError(
        "cannot reserve a unique fallback migration recovery manifest for "
        f"{canonical_path}"
    )


def _create_projection_backup(
    root: Path,
    backup_dir: Path,
    *,
    pinned_fs: ProjectFS | None = None,
) -> dict[str, object]:
    with _project_fs_scope(root, pinned_fs) as project_fs:
        backup_relative = project_fs.relative_to_root(backup_dir)
        projection_dir = backup_relative / "projections"
        project_fs.create_directory_exclusive(projection_dir, mode=0o700)
        entries: list[dict[str, object]] = []
        for index, relative_path in enumerate(PROJECTION_ROLLBACK_PATHS):
            snapshot = project_fs._snapshot(
                relative_path,
                allow_missing=True,
            )
            if not snapshot.exists:
                entries.append(
                    {
                        "path": relative_path.as_posix(),
                        "existed": False,
                        "mode": None,
                        "sha256": "",
                        "backup_path": "",
                    }
                )
                continue

            mode = _safe_file_mode(
                project_fs,
                relative_path,
                expected=snapshot,
            )
            content = project_fs.read_bytes(
                relative_path,
                expected=snapshot,
            )
            digest = hashlib.sha256(content).hexdigest()
            projection_copy = (
                projection_dir / f"{index:02d}-{relative_path.name}.bin"
            )
            projection_copy_snapshot = project_fs.create_exclusive(
                projection_copy,
                content,
                mode=0o600,
            )
            if (
                _safe_file_sha256(
                    project_fs,
                    projection_copy,
                    expected=projection_copy_snapshot,
                )
                != digest
            ):
                raise LocalCoreMigrationError(
                    f"projection backup digest mismatch: {relative_path}"
                )
            project_fs._assert_unchanged(relative_path, snapshot)
            if (
                _safe_file_sha256(
                    project_fs,
                    relative_path,
                    expected=snapshot,
                )
                != digest
                or _safe_file_mode(
                    project_fs,
                    relative_path,
                    expected=snapshot,
                )
                != mode
            ):
                raise LocalCoreMigrationError(
                    "projection changed while its rollback backup was created: "
                    f"{relative_path}"
                )
            entries.append(
                {
                    "path": relative_path.as_posix(),
                    "existed": True,
                    "mode": mode,
                    "sha256": digest,
                    "backup_path": str(project_fs.absolute(projection_copy)),
                }
            )

        return {
            "directory": str(project_fs.absolute(projection_dir)),
            "live_projection_count": len(PROJECTION_PATHS),
            "rollback_path_count": len(PROJECTION_ROLLBACK_PATHS),
            "entries": entries,
        }


def _restore_projection_backup(
    root: Path,
    projection_backup: dict[str, object],
    *,
    pinned_fs: ProjectFS | None = None,
) -> dict[Path, _PathSnapshot]:
    entries = projection_backup.get("entries")
    if not isinstance(entries, list):
        raise LocalCoreMigrationError("projection rollback metadata is missing its entries")
    expected_paths = tuple(path.as_posix() for path in PROJECTION_ROLLBACK_PATHS)
    actual_paths = tuple(
        str(entry.get("path", "")) if isinstance(entry, dict) else ""
        for entry in entries
    )
    if actual_paths != expected_paths:
        raise LocalCoreMigrationError(
            f"projection rollback inventory mismatch: expected={expected_paths} actual={actual_paths}"
        )

    backup_directory_value = projection_backup.get("directory")
    if not isinstance(backup_directory_value, str) or not backup_directory_value:
        raise LocalCoreMigrationError("projection rollback metadata is missing its backup directory")
    with _project_fs_scope(root, pinned_fs) as project_fs:
        backup_directory = project_fs.relative_to_root(
            Path(backup_directory_value)
        )
        project_fs.audit_directory(backup_directory, allow_missing=False)
        restored_receipts: dict[Path, _PathSnapshot] = {}

        for relative_path, entry in zip(
            PROJECTION_ROLLBACK_PATHS,
            entries,
            strict=True,
        ):
            if not isinstance(entry, dict):
                raise LocalCoreMigrationError(
                    f"invalid projection rollback entry: {relative_path}"
                )
            existed = entry.get("existed")
            if existed is False:
                snapshot = project_fs._snapshot(
                    relative_path,
                    allow_missing=True,
                )
                if snapshot.exists:
                    project_fs.unlink_regular(
                        relative_path,
                        expected=snapshot,
                    )
                    snapshot = project_fs._snapshot(
                        relative_path,
                        allow_missing=True,
                    )
                restored_receipts[relative_path] = snapshot
                continue
            if existed is not True:
                raise LocalCoreMigrationError(
                    f"invalid projection existence metadata: {relative_path}"
                )

            mode = entry.get("mode")
            digest = entry.get("sha256")
            backup_path_value = entry.get("backup_path")
            if (
                not isinstance(mode, int)
                or not 0 <= mode <= 0o7777
                or not isinstance(digest, str)
                or len(digest) != 64
                or not isinstance(backup_path_value, str)
                or not backup_path_value
            ):
                raise LocalCoreMigrationError(
                    f"invalid projection restore metadata: {relative_path}"
                )
            backup_path = project_fs.relative_to_root(Path(backup_path_value))
            if not backup_path.is_relative_to(backup_directory):
                raise LocalCoreMigrationError(
                    f"projection recovery copy is missing or invalid: {relative_path}"
                )
            backup_snapshot = project_fs._snapshot(
                backup_path,
                allow_missing=False,
            )
            backup_content = project_fs.read_bytes(
                backup_path,
                expected=backup_snapshot,
            )
            if hashlib.sha256(backup_content).hexdigest() != digest:
                raise LocalCoreMigrationError(
                    f"projection recovery copy is missing or invalid: {relative_path}"
                )

            current = project_fs._snapshot(
                relative_path,
                allow_missing=True,
            )
            if (
                current.exists
                and _safe_file_sha256(
                    project_fs,
                    relative_path,
                    expected=current,
                )
                == digest
                and _safe_file_mode(
                    project_fs,
                    relative_path,
                    expected=current,
                )
                == mode
            ):
                restored_receipts[relative_path] = current
                continue
            restored_snapshot = project_fs.atomic_write(
                relative_path,
                backup_content,
                mode=mode,
                expected_destination=current,
            )
            if (
                _safe_file_sha256(
                    project_fs,
                    relative_path,
                    expected=restored_snapshot,
                )
                != digest
                or _safe_file_mode(
                    project_fs,
                    relative_path,
                    expected=restored_snapshot,
                )
                != mode
            ):
                raise LocalCoreMigrationError(
                    f"restored projection failed verification: {relative_path}"
                )
            restored_receipts[relative_path] = restored_snapshot

        for relative_path, entry in zip(
            PROJECTION_ROLLBACK_PATHS,
            entries,
            strict=True,
        ):
            snapshot = restored_receipts[relative_path]
            project_fs._assert_unchanged(relative_path, snapshot)
            if entry["existed"] is False:
                if snapshot.exists:
                    raise LocalCoreMigrationError(
                        f"projection should be absent after rollback: {relative_path}"
                    )
                continue
            if (
                not snapshot.exists
                or _safe_file_sha256(
                    project_fs,
                    relative_path,
                    expected=snapshot,
                )
                != entry["sha256"]
                or _safe_file_mode(
                    project_fs,
                    relative_path,
                    expected=snapshot,
                )
                != entry["mode"]
            ):
                raise LocalCoreMigrationError(
                    "projection rollback bundle failed final verification: "
                    f"{relative_path}"
                )
            project_fs._assert_unchanged(relative_path, snapshot)
        return restored_receipts


def _assert_projection_receipts(
    project_fs: ProjectFS,
    receipts: dict[Path, _PathSnapshot],
    projection_backup: dict[str, object],
) -> None:
    expected_paths = set(PROJECTION_ROLLBACK_PATHS)
    if set(receipts) != expected_paths:
        raise LocalCoreMigrationError(
            "projection rollback receipts do not match the canonical inventory"
        )
    entries = projection_backup.get("entries")
    if not isinstance(entries, list) or len(entries) != len(
        PROJECTION_ROLLBACK_PATHS
    ):
        raise LocalCoreMigrationError(
            "projection rollback metadata is missing its canonical entries"
        )
    for relative_path, entry in zip(
        PROJECTION_ROLLBACK_PATHS,
        entries,
        strict=True,
    ):
        if not isinstance(entry, dict) or entry.get("path") != relative_path.as_posix():
            raise LocalCoreMigrationError(
                f"invalid projection rollback receipt metadata: {relative_path}"
            )
        receipt = receipts[relative_path]
        project_fs._assert_unchanged(
            relative_path,
            receipt,
        )
        if entry.get("existed") is False:
            if receipt.exists:
                raise LocalCoreMigrationError(
                    f"projection should remain absent after rollback: {relative_path}"
                )
            continue
        if (
            entry.get("existed") is not True
            or not receipt.exists
            or _safe_file_sha256(
                project_fs,
                relative_path,
                expected=receipt,
            )
            != entry.get("sha256")
            or _safe_file_mode(
                project_fs,
                relative_path,
                expected=receipt,
            )
            != entry.get("mode")
        ):
            raise LocalCoreMigrationError(
                f"projection rollback receipt content changed: {relative_path}"
            )
        project_fs._assert_unchanged(relative_path, receipt)


def _capture_published_projection_receipts(
    project_fs: ProjectFS,
) -> dict[Path, _FileAuthorityReceipt]:
    receipts: dict[Path, _FileAuthorityReceipt] = {}
    for relative_path in PROJECTION_ROLLBACK_PATHS:
        canonical = relative_path in PROJECTION_PATHS
        snapshot = project_fs._snapshot(
            relative_path,
            allow_missing=not canonical,
        )
        if not snapshot.exists:
            receipts[relative_path] = _FileAuthorityReceipt(
                snapshot=snapshot,
                sha256="",
                mode=None,
            )
            continue
        if not canonical:
            raise LocalCoreMigrationError(
                f"retired projection remains present: {relative_path}"
            )
        digest = _safe_file_sha256(
            project_fs,
            relative_path,
            expected=snapshot,
        )
        mode = _safe_file_mode(
            project_fs,
            relative_path,
            expected=snapshot,
        )
        project_fs._assert_unchanged(relative_path, snapshot)
        receipts[relative_path] = _FileAuthorityReceipt(
            snapshot=snapshot,
            sha256=digest,
            mode=mode,
        )
    return receipts


def _assert_published_projection_receipts(
    project_fs: ProjectFS,
    receipts: dict[Path, _FileAuthorityReceipt],
) -> None:
    if set(receipts) != set(PROJECTION_ROLLBACK_PATHS):
        raise LocalCoreMigrationError(
            "published projection receipts do not match the canonical inventory"
        )
    for relative_path in PROJECTION_ROLLBACK_PATHS:
        receipt = receipts[relative_path]
        project_fs._assert_unchanged(
            relative_path,
            receipt.snapshot,
        )
        if not receipt.snapshot.exists:
            if receipt.sha256 or receipt.mode is not None:
                raise LocalCoreMigrationError(
                    f"invalid absent projection receipt: {relative_path}"
                )
            continue
        if (
            _safe_file_sha256(
                project_fs,
                relative_path,
                expected=receipt.snapshot,
            )
            != receipt.sha256
            or _safe_file_mode(
                project_fs,
                relative_path,
                expected=receipt.snapshot,
            )
            != receipt.mode
        ):
            raise LocalCoreMigrationError(
                f"published projection changed before migration completion: {relative_path}"
            )
        project_fs._assert_unchanged(
            relative_path,
            receipt.snapshot,
        )


def _capture_recovery_bundle_receipts(
    project_fs: ProjectFS,
    backup: SQLiteBackupManifest,
    projection_backup: dict[str, object] | None = None,
) -> dict[Path, _FileAuthorityReceipt]:
    receipts: dict[Path, _FileAuthorityReceipt] = {}

    def capture(
        absolute_path: Path,
        *,
        expected_digest: str,
        expected_mode: int | None = None,
    ) -> None:
        relative = project_fs.relative_to_root(absolute_path)
        snapshot = project_fs._snapshot(relative, allow_missing=False)
        digest = _safe_file_sha256(
            project_fs,
            relative,
            expected=snapshot,
        )
        mode = _safe_file_mode(
            project_fs,
            relative,
            expected=snapshot,
        )
        if digest != expected_digest or (
            expected_mode is not None and mode != expected_mode
        ):
            raise LocalCoreMigrationError(
                f"recovery bundle file failed receipt validation: {relative}"
            )
        project_fs._assert_unchanged(relative, snapshot)
        receipts[relative] = _FileAuthorityReceipt(
            snapshot=snapshot,
            sha256=digest,
            mode=mode,
        )

    capture(
        Path(backup.backup_path),
        expected_digest=backup.sha256,
    )
    manifest_path = Path(backup.manifest_path)
    manifest_relative = project_fs.relative_to_root(manifest_path)
    manifest_snapshot = project_fs._snapshot(
        manifest_relative,
        allow_missing=False,
    )
    manifest_payload = project_fs.read_bytes(
        manifest_relative,
        expected=manifest_snapshot,
    )
    try:
        parsed_manifest = json.loads(manifest_payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LocalCoreMigrationError(
            "verified backup manifest is not valid JSON"
        ) from exc
    if parsed_manifest != backup.safe_payload():
        raise LocalCoreMigrationError(
            "verified backup manifest does not match the backup receipt"
        )
    manifest_mode = _safe_file_mode(
        project_fs,
        manifest_relative,
        expected=manifest_snapshot,
    )
    project_fs._assert_unchanged(
        manifest_relative,
        manifest_snapshot,
    )
    receipts[manifest_relative] = _FileAuthorityReceipt(
        snapshot=manifest_snapshot,
        sha256=hashlib.sha256(manifest_payload).hexdigest(),
        mode=manifest_mode,
    )

    if projection_backup is not None:
        entries = projection_backup.get("entries")
        if not isinstance(entries, list):
            raise LocalCoreMigrationError(
                "projection recovery bundle entries are unavailable"
            )
        for entry in entries:
            if not isinstance(entry, dict):
                raise LocalCoreMigrationError(
                    "projection recovery bundle entry is invalid"
                )
            if entry.get("existed") is not True:
                continue
            backup_path = entry.get("backup_path")
            digest = entry.get("sha256")
            mode = entry.get("mode")
            if (
                not isinstance(backup_path, str)
                or not backup_path
                or not isinstance(digest, str)
                or len(digest) != 64
                or not isinstance(mode, int)
            ):
                raise LocalCoreMigrationError(
                    "projection recovery bundle metadata is invalid"
                )
            capture(
                Path(backup_path),
                expected_digest=digest,
            )
    return receipts


def _assert_recovery_bundle_receipts(
    project_fs: ProjectFS,
    receipts: dict[Path, _FileAuthorityReceipt],
) -> None:
    if not receipts:
        raise LocalCoreMigrationError(
            "recovery bundle receipts are unavailable"
        )
    for relative, receipt in receipts.items():
        project_fs._assert_unchanged(relative, receipt.snapshot)
        if (
            _safe_file_sha256(
                project_fs,
                relative,
                expected=receipt.snapshot,
            )
            != receipt.sha256
            or _safe_file_mode(
                project_fs,
                relative,
                expected=receipt.snapshot,
            )
            != receipt.mode
        ):
            raise LocalCoreMigrationError(
                f"recovery bundle authority changed: {relative}"
            )
        project_fs._assert_unchanged(relative, receipt.snapshot)


@contextmanager
def _project_migration_lock(
    root: Path,
    *,
    target_schema: int = SCHEMA30_VERSION,
) -> Iterator[tuple[_MigrationGuard, ProjectFS]]:
    relative_lock = Path(".ai-team/state/local-core-migration.lock")
    with ProjectFS.open(root) as project_fs:
        root = project_fs.root
        lock_path = project_fs.absolute(relative_lock)
        payload = json.dumps(
            {
                "pid": os.getpid(),
                "created_at": _timestamp(),
                "target_schema": target_schema,
            },
            sort_keys=True,
        ).encode("utf-8")
        try:
            lock_snapshot = project_fs.create_exclusive(
                relative_lock,
                payload,
                mode=0o600,
            )
        except FileExistsError as exc:
            raise LocalCoreMigrationError(
                f"local-core migration lock already exists: {lock_path}; inspect the active migration before retrying"
            ) from exc
        guard = _MigrationGuard(
            lock_path=lock_path,
            project_fs=project_fs,
            lock_snapshot=lock_snapshot,
            target_schema=target_schema,
        )
        completed = False

        def finalize_guard() -> None:
            def publish_failure_sentinel(status: str) -> None:
                payload: dict[str, object] = {
                    "pid": os.getpid(),
                    "created_at": _timestamp(),
                    "target_schema": target_schema,
                    "status": status,
                }
                manifest_verified = False
                try:
                    guard.verify_recovery_manifest(required=True)
                    manifest_verified = True
                except BaseException as manifest_exc:
                    payload["manifest_status"] = "untrusted"
                    payload["manifest_error"] = _exception_text(
                        manifest_exc
                    )
                if manifest_verified and guard.manifest_path is not None:
                    payload["manifest_path"] = str(guard.manifest_path)
                guard.write_sentinel(payload)
                if manifest_verified:
                    try:
                        guard.verify_recovery_manifest(required=True)
                    except BaseException as manifest_exc:
                        payload.pop("manifest_path", None)
                        payload["manifest_status"] = "changed"
                        payload["manifest_error"] = _exception_text(
                            manifest_exc
                        )
                        guard.write_sentinel(payload)

            if completed or guard.clear_allowed:
                try:
                    receipts_required = (
                        completed and guard.manifest_path is not None
                    )
                    guard.verify_clear_authorities(
                        required=receipts_required
                    )
                    project_fs.unlink_regular(
                        relative_lock,
                        missing_ok=True,
                        expected=guard.lock_snapshot,
                    )
                    guard.lock_snapshot = _PathSnapshot(False)
                    guard.verify_clear_authorities(
                        required=receipts_required
                    )
                except BaseException as clear_exc:
                    guard.recovery_required = True
                    guard.clear_allowed = False
                    if guard.clear_failure_publisher is not None:
                        try:
                            guard.clear_failure_publisher(clear_exc)
                        except BaseException:
                            # Never point the sentinel at a diagnostic artifact
                            # whose terminal failure publication was not itself
                            # verified.
                            guard.recovery_manifest_relative = None
                            guard.recovery_manifest_snapshot = None
                            guard.recovery_manifest_sha256 = ""
                    try:
                        current_lock_snapshot = project_fs._snapshot(
                            relative_lock,
                            allow_missing=True,
                        )
                        if (
                            not current_lock_snapshot.exists
                            or current_lock_snapshot == guard.lock_snapshot
                        ):
                            guard.lock_snapshot = current_lock_snapshot
                            publish_failure_sentinel(
                                "rollback-incomplete"
                            )
                    except BaseException:
                        # An existing changed entry remains fail-closed.
                        pass
                    raise
            elif guard.recovery_required:
                try:
                    publish_failure_sentinel("rollback-incomplete")
                except BaseException:
                    # The exclusive original sentinel remains fail-closed.
                    pass
            elif guard.manifest_path is not None:
                try:
                    publish_failure_sentinel("migration-failed")
                except BaseException:
                    # Preserve the original diagnostic sentinel.
                    pass

        with project_db_operation(
            root,
            purpose="migration",
            project_fs=project_fs,
        ) as locked_project_fs:
            try:
                yield guard, locked_project_fs
                completed = True
            finally:
                # Authority verification and sentinel cleanup are part of the
                # migration operation itself.  Releasing the operation lock
                # first would let an ordinary writer commit in the gap and be
                # misclassified as migration corruption.
                finalize_guard()


def _checkpoint_active_database(
    active_path: Path,
    *,
    pinned_fs: ProjectFS | None = None,
) -> None:
    """Merge committed WAL pages before source identity and backup are read."""

    with _project_fs_scope(
        _project_root_for_database(active_path),
        pinned_fs,
    ) as project_fs:
        relative = project_fs.relative_to_root(active_path)
        snapshot = project_fs._snapshot(relative, allow_missing=False)
        project_fs.audit(_database_family(relative), allow_missing=True)
        checkpoint_result: list[object] = []

        def checkpoint_database(conn: sqlite3.Connection) -> None:
            conn.execute("pragma busy_timeout = 5000")
            project_fs._assert_unchanged(relative, snapshot)
            current_mode = conn.execute("pragma journal_mode").fetchone()
            if current_mode is not None and str(current_mode[0]).lower() == "wal":
                checkpoint_result.append(
                    conn.execute("pragma wal_checkpoint(truncate)").fetchone()
                )
            else:
                checkpoint_result.append((0, 0, 0))
            project_fs._assert_unchanged(relative, snapshot)

        with _verified_sqlite_connection(
            project_fs,
            relative,
            access="rw",
            setup=checkpoint_database,
        ):
            pass
        project_fs.audit(_database_family(relative), allow_missing=True)
        result = checkpoint_result[0] if checkpoint_result else None
    if result is None or int(result[0]) != 0:
        raise LocalCoreMigrationError(
            f"active database WAL checkpoint did not complete before migration: {result}"
        )


def _finalize_staging_metadata(
    staging_path: Path,
    report: LocalCoreStagingReport,
    backup: SQLiteBackupManifest,
    migration_manifest_path: Path,
    *,
    target_version: int = SCHEMA30_VERSION,
    schema_validator: Callable[[sqlite3.Connection], None] | None = None,
    pinned_fs: ProjectFS | None = None,
) -> None:
    with _project_fs_scope(
        _project_root_for_database(staging_path),
        pinned_fs,
    ) as project_fs:
        relative = project_fs.relative_to_root(staging_path)
        snapshot = project_fs._snapshot(relative, allow_missing=False)
        project_fs.audit(_database_family(relative), allow_missing=True)
        with _verified_sqlite_connection(
            project_fs,
            relative,
            access="rw",
            journal_mode="memory",
        ) as conn:
            project_fs._assert_unchanged(relative, snapshot)
            conn.execute("pragma foreign_keys = on")
            conn.execute("begin immediate")
            updated = conn.execute(
                """
                update migrations
                set backup_path=?, manifest_path=?, row_counts_json=?, dropped_table_count=?,
                    status='activated', applied_at=?
                where id=(select max(id) from migrations where to_version=?)
                """,
                (
                    backup.backup_path,
                    str(migration_manifest_path),
                    _stable_json(report.staging_row_counts),
                    sum(report.retired_row_counts.values()),
                    _timestamp(),
                    target_version,
                ),
            )
            if updated.rowcount != 1:
                conn.rollback()
                raise LocalCoreMigrationError(
                    f"schema {target_version} staging database is missing its migration record"
                )
            conn.execute(
                """
                insert into events
                (id, schema_version, event_type, entity_type, entity_id, actor, command,
                before_json, after_json, correlation_id, created_at)
                values (?, ?, 'local_core_migration_activated', 'project', '1', 'root-controller',
                        'migrate local-core', '{}', ?, lower(hex(randomblob(16))), ?)
                """,
                (
                    str(uuid.uuid4()),
                    target_version,
                    _stable_json(
                        {
                            "source_version": report.source_version,
                            "target_version": report.target_version,
                            "backup_sha256": backup.sha256,
                        }
                    ),
                    _timestamp(),
                ),
            )
            conn.commit()
            conn.execute("pragma journal_mode = delete")
            (schema_validator or _validate_staging_database)(conn)
            project_fs._assert_unchanged(relative, snapshot)
        project_fs.audit(_database_family(relative), allow_missing=True)


def _schema30_doctor(
    path: Path,
    *,
    pinned_fs: ProjectFS | None = None,
) -> None:
    with _project_fs_scope(
        _project_root_for_database(path),
        pinned_fs,
    ) as project_fs:
        relative = project_fs.relative_to_root(path)
        snapshot = project_fs._snapshot(relative, allow_missing=False)
        project_fs.audit(_database_family(relative), allow_missing=True)
        with _verified_sqlite_connection(
            project_fs,
            relative,
            access="ro",
            immutable=True,
        ) as conn:
            project_fs._assert_unchanged(relative, snapshot)
            _validate_staging_database(conn)
            triggers = {
                str(row[0])
                for row in conn.execute(
                    "select name from sqlite_master where type='trigger' order by name"
                )
            }
            required = {
                "executions_no_update",
                "executions_no_delete",
                "events_no_update",
                "events_no_delete",
            }
            if not required.issubset(triggers):
                raise LocalCoreMigrationError(
                    "schema 30 immutable trigger contract is incomplete: "
                    f"{sorted(triggers)}"
                )
            migration = conn.execute(
                "select status from migrations where to_version=? order by id desc limit 1",
                (SCHEMA30_VERSION,),
            ).fetchone()
            if migration is None or str(migration[0]) != "activated":
                raise LocalCoreMigrationError(
                    "schema 30 activation record is missing or incomplete"
                )
            project_fs._assert_unchanged(relative, snapshot)


def _active_schema_doctor(
    path: Path,
    *,
    pinned_fs: ProjectFS | None = None,
) -> None:
    with _project_fs_scope(
        _project_root_for_database(path),
        pinned_fs,
    ) as project_fs:
        relative = project_fs.relative_to_root(path)
        snapshot = project_fs._snapshot(relative, allow_missing=False)
        project_fs.audit(_database_family(relative), allow_missing=True)
        with _verified_sqlite_connection(
            project_fs,
            relative,
            access="ro",
            immutable=True,
        ) as conn:
            project_fs._assert_unchanged(relative, snapshot)
            _validate_active_staging_database(conn)
            triggers = {
                str(row[0])
                for row in conn.execute(
                    "select name from sqlite_master where type='trigger' order by name"
                )
            }
            required = {
                "acceptance_target_qualifications_no_update",
                "acceptance_target_qualifications_no_delete",
                "quality_gate_qualifications_no_update",
                "quality_gate_qualifications_no_delete",
                "outcome_observations_no_update",
                "outcome_observations_no_delete",
                "executions_no_update",
                "executions_no_delete",
                "events_no_update",
                "events_no_delete",
            }
            if not required.issubset(triggers):
                raise LocalCoreMigrationError(
                    "active immutable trigger contract is incomplete: "
                    f"missing={sorted(required - triggers)}"
                )
            migration = conn.execute(
                "select status from migrations where to_version=? order by id desc limit 1",
                (ACTIVE_SCHEMA_VERSION,),
            ).fetchone()
            if migration is None or str(migration[0]) != "activated":
                raise LocalCoreMigrationError(
                    "active schema activation record is missing or incomplete"
                )
            project_fs._assert_unchanged(relative, snapshot)


def _database_sidecars(path: Path) -> tuple[Path, Path, Path]:
    return (
        Path(str(path) + "-wal"),
        Path(str(path) + "-shm"),
        Path(str(path) + "-journal"),
    )


def _quarantine_failed_database_sidecars(
    active_path: Path,
    failed_path: Path,
    *,
    pinned_fs: ProjectFS | None = None,
) -> tuple[Path, ...]:
    root = _project_root_for_database(active_path)
    with _project_fs_scope(root, pinned_fs) as project_fs:
        sources = tuple(
            project_fs.relative_to_root(path)
            for path in _database_sidecars(active_path)
        )
        destinations = tuple(
            project_fs.relative_to_root(path)
            for path in _database_sidecars(failed_path)
        )
        source_snapshots = tuple(
            project_fs._snapshot(path, allow_missing=True)
            for path in sources
        )
        destination_snapshots = tuple(
            project_fs._snapshot(path, allow_missing=True)
            for path in destinations
        )
        for destination, snapshot in zip(
            destinations,
            destination_snapshots,
            strict=True,
        ):
            if snapshot.exists:
                raise LocalCoreMigrationError(
                    "failed schema30 sidecar quarantine target already exists: "
                    f"{project_fs.absolute(destination)}"
                )

        quarantined: list[Path] = []
        for source, destination, source_snapshot, destination_snapshot in zip(
            sources,
            destinations,
            source_snapshots,
            destination_snapshots,
            strict=True,
        ):
            if not source_snapshot.exists:
                continue
            project_fs._assert_unchanged(source, source_snapshot)
            project_fs.replace_file(
                source,
                destination,
                expected_source=source_snapshot,
                expected_destination=destination_snapshot,
            )
            if project_fs._snapshot(source, allow_missing=True).exists:
                raise LocalCoreMigrationError(
                    "failed schema30 sidecar remained active after quarantine: "
                    f"{project_fs.absolute(source)}"
                )
            project_fs._snapshot(destination, allow_missing=False)
            quarantined.append(project_fs.absolute(destination))
        return tuple(quarantined)


def _restore_verified_backup(
    active_path: Path,
    backup: SQLiteBackupManifest,
    *,
    expected_active_snapshot: _PathSnapshot,
    expected_backup_snapshot: _PathSnapshot,
    pinned_fs: ProjectFS | None = None,
) -> dict[Path, _PathSnapshot]:
    root = _project_root_for_database(active_path)
    with _project_fs_scope(root, pinned_fs) as project_fs:
        active_relative = project_fs.relative_to_root(active_path)
        backup_relative = project_fs.relative_to_root(Path(backup.backup_path))
        restore_relative = active_relative.with_name(
            active_relative.name + ".restore"
        )
        sidecars = _database_family(active_relative)[1:]
        project_fs._assert_unchanged(
            backup_relative,
            expected_backup_snapshot,
        )
        backup_snapshot = expected_backup_snapshot
        active_snapshot = project_fs._snapshot(
            active_relative,
            allow_missing=True,
        )
        if active_snapshot != expected_active_snapshot:
            raise ProjectPathSafetyError(
                active_relative,
                "path-identity-changed",
            )
        restore_destination_snapshot = project_fs._snapshot(
            restore_relative,
            allow_missing=True,
        )
        sidecar_snapshots = tuple(
            project_fs._snapshot(path, allow_missing=True)
            for path in sidecars
        )
        remaining_sidecars = [
            str(project_fs.absolute(path))
            for path, snapshot in zip(
                sidecars,
                sidecar_snapshots,
                strict=True,
            )
            if snapshot.exists
        ]
        if remaining_sidecars:
            raise LocalCoreMigrationError(
                "failed schema30 sidecars were not quarantined before authority restore: "
                + ", ".join(remaining_sidecars)
            )

        backup_payload = project_fs.read_bytes(
            backup_relative,
            expected=backup_snapshot,
        )
        if hashlib.sha256(backup_payload).hexdigest() != backup.sha256:
            raise LocalCoreMigrationError(
                "verified migration backup is missing or has a digest mismatch"
            )
        restore_snapshot = project_fs.atomic_write(
            restore_relative,
            backup_payload,
            mode=0o600,
            expected_destination=restore_destination_snapshot,
        )
        if (
            _safe_database_digest(
                project_fs,
                restore_relative,
                expected=restore_snapshot,
            )
            != backup.sha256
        ):
            raise LocalCoreMigrationError(
                "temporary restored database does not match the verified backup digest"
            )
        project_fs._assert_unchanged(restore_relative, restore_snapshot)
        project_fs.replace_file(
            restore_relative,
            active_relative,
            expected_source=restore_snapshot,
            expected_destination=active_snapshot,
        )
        active_snapshot = project_fs._snapshot(
            active_relative,
            allow_missing=False,
        )
        if (
            _safe_database_digest(
                project_fs,
                active_relative,
                expected=active_snapshot,
            )
            != backup.sha256
        ):
            raise LocalCoreMigrationError(
                "restored active database does not match the verified backup digest"
            )
        with _verified_sqlite_connection(
            project_fs,
            active_relative,
            access="ro",
        ) as conn:
            integrity = [
                str(row[0])
                for row in conn.execute("pragma integrity_check")
            ]
            foreign_keys = conn.execute(
                "pragma foreign_key_check"
            ).fetchall()
            version = conn.execute(
                "select schema_version from project where id=1"
            ).fetchone()
            project_fs._assert_unchanged(
                active_relative,
                active_snapshot,
            )
        if any(
            project_fs._snapshot(path, allow_missing=True).exists
            for path in sidecars
        ):
            raise LocalCoreMigrationError(
                "restored database validation left an active WAL/SHM/journal sidecar"
            )
        if (
            integrity != ["ok"]
            or foreign_keys
            or version is None
            or int(version[0]) != backup.source_version
        ):
            raise LocalCoreMigrationError(
                "automatic backup restore failed validation: "
                f"integrity={integrity} "
                f"foreign_keys={len(foreign_keys)} version={version}"
            )
        project_fs._assert_unchanged(active_relative, active_snapshot)
        if (
            _safe_database_digest(
                project_fs,
                active_relative,
                expected=active_snapshot,
            )
            != backup.sha256
        ):
            raise LocalCoreMigrationError(
                "restored active database changed after validation"
            )
        project_fs._assert_unchanged(active_relative, active_snapshot)
        return _capture_database_family_receipts(
            project_fs,
            active_relative,
            expected_main=active_snapshot,
            expected_digest=backup.sha256,
        )


def _diagnostic_database_digest(
    project_fs: ProjectFS,
    relative: Path,
) -> str:
    return _safe_database_digest(project_fs, relative)


def _move_failed_schema30(
    project_fs: ProjectFS,
    source: Path,
    destination: Path,
    *,
    expected_source: _PathSnapshot,
    expected_destination: _PathSnapshot,
) -> None:
    project_fs.replace_file(
        source,
        destination,
        expected_source=expected_source,
        expected_destination=expected_destination,
    )


def _copy_failed_schema30(
    project_fs: ProjectFS,
    source: Path,
    destination: Path,
    *,
    expected_source: _PathSnapshot,
    expected_destination: _PathSnapshot,
) -> _PathSnapshot:
    return project_fs.atomic_write(
        destination,
        project_fs.read_bytes(source, expected=expected_source),
        mode=0o600,
        expected_destination=expected_destination,
    )


def _cleanup_failed_schema30(
    project_fs: ProjectFS,
    relative: Path,
    expected: _PathSnapshot,
) -> None:
    project_fs.unlink_regular(
        relative,
        missing_ok=True,
        expected=expected,
    )


def _preserve_failed_schema30(
    active_path: Path,
    failed_path: Path,
    *,
    expected_active_snapshot: _PathSnapshot,
    pinned_fs: ProjectFS | None = None,
) -> tuple[str, str, _PathSnapshot]:
    """Best-effort diagnostic preservation that must never block authority restore."""
    root = _project_root_for_database(active_path)
    with _project_fs_scope(root, pinned_fs) as project_fs:
        active_relative = project_fs.relative_to_root(active_path)
        failed_relative = project_fs.relative_to_root(failed_path)
        active_snapshot = project_fs._snapshot(
            active_relative,
            allow_missing=False,
        )
        if active_snapshot != expected_active_snapshot:
            raise ProjectPathSafetyError(
                active_relative,
                "path-identity-changed",
            )
        failed_snapshot = project_fs._snapshot(
            failed_relative,
            allow_missing=True,
        )
        if failed_snapshot.exists:
            raise LocalCoreMigrationError(
                f"failed schema30 destination already exists: {failed_path}"
            )
        for source, destination in zip(
            _database_family(active_relative)[1:],
            _database_family(failed_relative)[1:],
            strict=True,
        ):
            project_fs._snapshot(source, allow_missing=True)
            destination_snapshot = project_fs._snapshot(
                destination,
                allow_missing=True,
            )
            if destination_snapshot.exists:
                raise LocalCoreMigrationError(
                    "failed schema30 sidecar quarantine target already exists: "
                    f"{project_fs.absolute(destination)}"
                )

        try:
            active_digest = _diagnostic_database_digest(
                project_fs,
                active_relative,
            )
            project_fs._assert_unchanged(
                active_relative,
                active_snapshot,
            )
        except ProjectPathSafetyError:
            raise
        except BaseException as digest_exc:
            return (
                "failed",
                "failed schema30 digest unavailable: "
                f"{_exception_text(digest_exc)}",
                active_snapshot,
            )

        try:
            _move_failed_schema30(
                project_fs,
                active_relative,
                failed_relative,
                expected_source=active_snapshot,
                expected_destination=failed_snapshot,
            )
        except ProjectPathSafetyError:
            raise
        except BaseException as move_exc:
            copied_snapshot: _PathSnapshot | None = None
            try:
                copied_snapshot = _copy_failed_schema30(
                    project_fs,
                    active_relative,
                    failed_relative,
                    expected_source=active_snapshot,
                    expected_destination=failed_snapshot,
                )
                if (
                    _diagnostic_database_digest(
                        project_fs,
                        failed_relative,
                    )
                    != active_digest
                ):
                    raise LocalCoreMigrationError(
                        "fallback failed-schema30 copy digest mismatch"
                    )
            except ProjectPathSafetyError:
                raise
            except BaseException as copy_exc:
                cleanup_error = ""
                try:
                    _cleanup_failed_schema30(
                        project_fs,
                        failed_relative,
                        copied_snapshot or failed_snapshot,
                    )
                except BaseException as cleanup_exc:
                    cleanup_error = (
                        "; partial-copy cleanup failed: "
                        f"{_exception_text(cleanup_exc)}"
                    )
                return (
                    "failed",
                    f"atomic move failed: {_exception_text(move_exc)}; "
                    f"fallback copy failed: {_exception_text(copy_exc)}"
                    f"{cleanup_error}",
                    active_snapshot,
                )
            preservation_status = "copied-after-move-failure"
            preservation_error = _exception_text(move_exc)
            active_after = project_fs._snapshot(
                active_relative,
                allow_missing=False,
            )
            if active_after != active_snapshot:
                raise ProjectPathSafetyError(
                    active_relative,
                    "path-identity-changed",
                )
        else:
            active_after = project_fs._snapshot(
                active_relative,
                allow_missing=True,
            )
            if active_after.exists:
                raise ProjectPathSafetyError(
                    active_relative,
                    "path-identity-changed",
                )
            try:
                if (
                    _diagnostic_database_digest(
                        project_fs,
                        failed_relative,
                    )
                    != active_digest
                ):
                    raise LocalCoreMigrationError(
                        "moved failed-schema30 digest mismatch"
                    )
            except BaseException as verify_exc:
                return (
                    "failed",
                    "atomic move completed but verification failed: "
                    f"{_exception_text(verify_exc)}",
                    active_after,
                )
            preservation_status = "moved"
            preservation_error = ""

    try:
        _quarantine_failed_database_sidecars(
            active_path,
            failed_path,
            pinned_fs=pinned_fs,
        )
    except BaseException as sidecar_exc:
        return (
            "failed",
            f"{preservation_error + '; ' if preservation_error else ''}"
            "failed schema30 sidecar quarantine failed: "
            f"{_exception_text(sidecar_exc)}",
            active_after,
        )
    with _project_fs_scope(root, pinned_fs) as project_fs:
        active_relative = project_fs.relative_to_root(active_path)
        if project_fs._snapshot(
            active_relative,
            allow_missing=not active_after.exists,
        ) != active_after:
            raise ProjectPathSafetyError(
                active_relative,
                "path-identity-changed",
            )
    return preservation_status, preservation_error, active_after


def _remove_empty_active_sidecars(
    active_path: Path,
    *,
    pinned_fs: ProjectFS | None = None,
) -> None:
    with _project_fs_scope(
        _project_root_for_database(active_path),
        pinned_fs,
    ) as project_fs:
        active_relative = project_fs.relative_to_root(active_path)
        sidecars = _database_family(active_relative)[1:]
        removable: dict[Path, _PathSnapshot] = {}
        for index, sidecar in enumerate(sidecars):
            snapshot = project_fs._snapshot(sidecar, allow_missing=True)
            if not snapshot.exists:
                continue
            # A checkpointed WAL and rollback journal must be empty.  SQLite's
            # shared-memory index is expected to remain non-empty after a
            # successful checkpoint and is safe to remove once all handles
            # are closed.
            if index != 1 and project_fs.read_bytes(sidecar):
                raise LocalCoreMigrationError(
                    "active database has a non-empty SQLite sidecar; "
                    "stop project writers and checkpoint SQLite before activation: "
                    f"{project_fs.absolute(sidecar)}"
                )
            removable[sidecar] = snapshot
        for sidecar, snapshot in removable.items():
            project_fs.unlink_regular(
                sidecar,
                expected=snapshot,
            )


def _activate_staging_database(
    project_fs: ProjectFS,
    staging_relative: Path,
    active_relative: Path,
    *,
    staging_snapshot: _PathSnapshot,
    active_snapshot: _PathSnapshot,
) -> _PathSnapshot:
    project_fs.replace_file(
        staging_relative,
        active_relative,
        expected_source=staging_snapshot,
        expected_destination=active_snapshot,
    )
    activated_snapshot = project_fs._snapshot(
        active_relative,
        allow_missing=False,
    )
    if activated_snapshot != staging_snapshot:
        raise ProjectPathSafetyError(
            active_relative,
            "path-identity-changed",
        )
    return activated_snapshot


def _migrate_project_to_target(
    root: Path,
    *,
    target_version: int,
    supported_source_versions: frozenset[int],
    stage_function: Callable[..., LocalCoreStagingReport],
    doctor_function: Callable[..., None],
    schema_validator: Callable[[sqlite3.Connection], None],
    target_label: str,
    source_preflight: Callable[[Path, int, ProjectFS], None] | None = None,
    fail_at: str | None = None,
    staging_validator: Callable[[Path], None] | None = None,
    active_validator: Callable[[Path], None] | None = None,
) -> LocalCoreMigrationResult:
    """Back up, stage, atomically activate, and roll back one target contract."""

    if active_validator is None:
        raise LocalCoreMigrationError(
            "post-activation projection publication and validator callback is required"
        )
    if fail_at is not None and fail_at not in MIGRATION_FAILURE_POINTS:
        raise LocalCoreMigrationError(
            f"unknown migration failure point {fail_at!r}; expected one of {sorted(MIGRATION_FAILURE_POINTS)}"
        )
    active_relative = Path(".ai-team/state/harness.db")
    with _project_migration_lock(
        root,
        target_schema=target_version,
    ) as (
        migration_guard,
        project_fs,
    ):
        root = project_fs.root
        active_path = project_fs.absolute(active_relative)
        active_snapshot = project_fs._snapshot(
            active_relative,
            allow_missing=True,
        )
        if not active_snapshot.exists:
            migration_guard.mark_safe()
            raise LocalCoreMigrationError(
                f"runtime database is missing: {active_path}"
            )
        source_active_snapshot = active_snapshot
        project_fs.audit(
            _database_family(active_relative),
            allow_missing=True,
        )
        _checkpoint_active_database(
            active_path,
            pinned_fs=project_fs,
        )
        source_version = _read_source_version(
            active_path,
            pinned_fs=project_fs,
        )
        if source_version not in supported_source_versions:
            migration_guard.mark_safe()
            raise LocalCoreMigrationError(
                f"unsupported local-core migration source schema {source_version}"
            )
        if source_preflight is not None:
            try:
                source_preflight(active_path, source_version, project_fs)
            except BaseException:
                migration_guard.mark_safe()
                raise
        source_fingerprint = _safe_database_fingerprint(
            project_fs,
            active_relative,
        )
        backup = backup_sqlite_database(
            root,
            source_path=active_path,
            expected_source_version=source_version,
            target_version=target_version,
            preserve_physical_bytes=(target_version == ACTIVE_SCHEMA_VERSION),
            project_fs=project_fs,
        )
        recovery_bundle_receipts = _capture_recovery_bundle_receipts(
            project_fs,
            backup,
        )
        backup_dir = Path(backup.backup_path).parent
        staging_path = backup_dir / f"harness.{target_label}.new.db"
        migration_manifest_path = backup_dir / "migration-manifest.json"
        backup_relative = project_fs.relative_to_root(backup_dir)
        staging_relative = project_fs.relative_to_root(staging_path)
        manifest_relative = project_fs.relative_to_root(
            migration_manifest_path
        )
        project_fs.audit_directory(backup_relative, allow_missing=False)
        project_fs.audit(
            (
                staging_relative,
                *(_database_family(staging_relative)[1:]),
                manifest_relative,
                manifest_relative.with_name(manifest_relative.name + ".tmp"),
            ),
            allow_missing=True,
        )
        manifest_payload: dict[str, object] = {
            "status": "backup-created",
            "source_version": source_version,
            "target_version": target_version,
            "backup": backup.safe_payload(),
            "projection_backup": {"status": "pending"},
            "projection_restore_status": "not-needed",
            "failure_point": fail_at or "",
        }
        manifest_snapshot = _write_json_atomic(
            migration_manifest_path,
            manifest_payload,
            project_fs=project_fs,
        )
        migration_guard.record_manifest(migration_manifest_path)
        migration_guard.record_recovery_manifest(
            migration_manifest_path,
            manifest_snapshot,
        )
        recovery_manifest_path = migration_manifest_path
        recovery_manifest_snapshot = manifest_snapshot
        manifest_fallback_used = False

        def write_canonical_manifest() -> _PathSnapshot:
            nonlocal manifest_snapshot, recovery_manifest_snapshot
            manifest_snapshot = _write_json_atomic(
                migration_manifest_path,
                manifest_payload,
                project_fs=project_fs,
                expected_destination=manifest_snapshot,
            )
            if recovery_manifest_path == migration_manifest_path:
                recovery_manifest_snapshot = manifest_snapshot
            return manifest_snapshot

        def write_failure_manifest() -> Path:
            nonlocal recovery_manifest_path
            nonlocal recovery_manifest_snapshot
            nonlocal manifest_fallback_used
            try:
                recovery_manifest_snapshot = _write_json_atomic(
                    recovery_manifest_path,
                    manifest_payload,
                    project_fs=project_fs,
                    expected_destination=recovery_manifest_snapshot,
                )
                migration_guard.record_recovery_manifest(
                    recovery_manifest_path,
                    recovery_manifest_snapshot,
                )
            except BaseException as manifest_exc:
                failed_manifest_path = recovery_manifest_path
                manifest_payload["status"] = "rollback-incomplete"
                manifest_payload["failed_manifest_path"] = str(
                    failed_manifest_path
                )
                manifest_payload["manifest_write_error"] = _exception_text(
                    manifest_exc
                )
                for _ in range(128):
                    (
                        recovery_manifest_path,
                        recovery_manifest_snapshot,
                    ) = _create_fallback_recovery_manifest(
                        migration_manifest_path,
                        manifest_payload,
                        manifest_write_error=manifest_exc,
                        project_fs=project_fs,
                    )
                    try:
                        migration_guard.record_recovery_manifest(
                            recovery_manifest_path,
                            recovery_manifest_snapshot,
                        )
                    except (
                        ProjectPathSafetyError,
                        LocalCoreMigrationError,
                    ) as recovery_race_exc:
                        manifest_payload[
                            "recovery_manifest_race_error"
                        ] = _exception_text(recovery_race_exc)
                        continue
                    break
                else:
                    raise LocalCoreMigrationError(
                        "cannot publish a stable fallback recovery manifest"
                    )
                manifest_fallback_used = True
                migration_guard.record_manifest(recovery_manifest_path)
                migration_guard.require_recovery(
                    recovery_manifest_path,
                    snapshot=recovery_manifest_snapshot,
                )
            return recovery_manifest_path

        def publish_terminal_authority_failure(
            failure: BaseException,
        ) -> None:
            previous_status = str(manifest_payload.get("status") or "")
            manifest_payload["status"] = "rollback-incomplete"
            manifest_payload["previous_terminal_status"] = previous_status
            manifest_payload["terminal_authority_error"] = (
                _exception_text(failure)
            )
            manifest_payload.setdefault(
                "error",
                _exception_text(failure),
            )
            write_failure_manifest()
            migration_guard.require_recovery(
                recovery_manifest_path,
                snapshot=recovery_manifest_snapshot,
            )

        try:
            projection_backup = _create_projection_backup(
                root,
                backup_dir,
                pinned_fs=project_fs,
            )
        except BaseException as exc:
            manifest_payload["status"] = "failed-before-activation"
            manifest_payload["error"] = _exception_text(exc)
            manifest_payload["projection_backup"] = {"status": "failed"}
            write_failure_manifest()
            raise
        manifest_payload["projection_backup"] = projection_backup
        try:
            recovery_bundle_receipts = _capture_recovery_bundle_receipts(
                project_fs,
                backup,
                projection_backup,
            )
            write_canonical_manifest()
        except BaseException as manifest_exc:
            manifest_payload["status"] = "rollback-incomplete"
            manifest_payload["error"] = _exception_text(manifest_exc)
            manifest_payload["projection_restore_status"] = "not-needed"
            write_failure_manifest()
            migration_guard.require_recovery(
                recovery_manifest_path,
                snapshot=recovery_manifest_snapshot,
            )
            raise
        activated = False
        activation_attempted = False
        expected_active_sha256 = ""
        report: LocalCoreStagingReport | None = None
        staging_snapshot: _PathSnapshot | None = None
        activated_snapshot: _PathSnapshot | None = None
        try:
            _inject_failure(fail_at, "before_copy")
            report = stage_function(
                active_path,
                staging_path,
                project_root=root,
                fail_at=fail_at,
                pinned_fs=project_fs,
            )
            manifest_payload["staging"] = asdict(report)
            manifest_payload["status"] = "staged"
            write_canonical_manifest()
            _finalize_staging_metadata(
                staging_path,
                report,
                backup,
                migration_manifest_path,
                target_version=target_version,
                schema_validator=schema_validator,
                pinned_fs=project_fs,
            )
            if staging_validator:
                staging_validator(staging_path)
            if (
                _safe_database_fingerprint(project_fs, active_relative)
                != source_fingerprint
            ):
                raise LocalCoreMigrationError(
                    "active source changed after staging and before activation"
                )
            project_fs._assert_unchanged(
                active_relative,
                source_active_snapshot,
            )
            _inject_failure(fail_at, "before_atomic_replace")
            _remove_empty_active_sidecars(
                active_path,
                pinned_fs=project_fs,
            )
            staging_snapshot = project_fs._snapshot(
                staging_relative,
                allow_missing=False,
            )
            expected_active_sha256 = _safe_database_digest(
                project_fs,
                staging_relative,
            )
            project_fs._assert_unchanged(
                staging_relative,
                staging_snapshot,
            )
            migration_guard.require_recovery(
                migration_manifest_path,
                snapshot=manifest_snapshot,
            )
            activation_attempted = True
            activated_snapshot = _activate_staging_database(
                project_fs,
                staging_relative,
                active_relative,
                staging_snapshot=staging_snapshot,
                active_snapshot=source_active_snapshot,
            )
            activated = True
            _inject_failure(fail_at, "after_atomic_replace")
            project_fs._assert_unchanged(
                active_relative,
                activated_snapshot,
            )
            if (
                _safe_database_digest(project_fs, active_relative)
                != expected_active_sha256
            ):
                raise LocalCoreMigrationError(
                    "activated database digest does not match verified staging"
                )
            project_fs._assert_unchanged(
                active_relative,
                activated_snapshot,
            )
            doctor_function(active_path, pinned_fs=project_fs)
            project_fs._assert_unchanged(
                active_relative,
                activated_snapshot,
            )
            active_snapshot = activated_snapshot
            project_fs.audit(
                _database_family(active_relative),
                allow_missing=True,
            )
            def prepare_callback_database(callback_prep: sqlite3.Connection) -> None:
                checkpoint = callback_prep.execute(
                    "pragma wal_checkpoint(truncate)"
                ).fetchone()
                if checkpoint is None or int(checkpoint[0]) != 0:
                    raise LocalCoreMigrationError(
                        f"cannot stabilize callback database WAL: {checkpoint}"
                    )

            with _verified_sqlite_connection(
                project_fs,
                active_relative,
                access="rw",
                journal_mode="wal",
                setup=prepare_callback_database,
            ):
                project_fs._assert_unchanged(
                    active_relative,
                    active_snapshot,
                )
            project_fs._assert_unchanged(
                active_relative,
                active_snapshot,
            )
            project_fs.audit(
                _database_family(active_relative),
                allow_missing=True,
            )
            pre_callback_fingerprint = _safe_database_fingerprint(
                project_fs,
                active_relative,
            )
            project_fs._assert_unchanged(
                active_relative,
                active_snapshot,
            )
            if active_validator:
                active_validator(active_path)
            project_fs._assert_unchanged(
                active_relative,
                active_snapshot,
            )
            post_callback_fingerprint = _safe_database_fingerprint(
                project_fs,
                active_relative,
            )
            if post_callback_fingerprint != pre_callback_fingerprint:
                raise LocalCoreMigrationError(
                    "projection callback mutated the active database authority"
                )
            project_fs._assert_unchanged(
                active_relative,
                active_snapshot,
            )
            doctor_function(active_path, pinned_fs=project_fs)
            project_fs._assert_unchanged(
                active_relative,
                active_snapshot,
            )
            from core.projections import projection_content_issues

            published_projection_receipts = (
                _capture_published_projection_receipts(project_fs)
            )
            projection_issues = projection_content_issues(root)
            if projection_issues:
                raise LocalCoreMigrationError(
                    "post-activation projection content verification failed: "
                    + "; ".join(projection_issues)
                )
            _assert_published_projection_receipts(
                project_fs,
                published_projection_receipts,
            )
            if (
                _safe_database_fingerprint(project_fs, active_relative)
                != pre_callback_fingerprint
            ):
                raise LocalCoreMigrationError(
                    "post-validation database authority changed"
                )
            project_fs._assert_unchanged(
                active_relative,
                active_snapshot,
            )
            active_sha256 = _safe_database_digest(
                project_fs,
                active_relative,
            )
            project_fs._assert_unchanged(
                active_relative,
                active_snapshot,
            )
            success_database_receipts = (
                _capture_database_family_receipts(
                    project_fs,
                    active_relative,
                    expected_main=active_snapshot,
                    expected_digest=active_sha256,
                )
            )
            _assert_recovery_bundle_receipts(
                project_fs,
                recovery_bundle_receipts,
            )
            manifest_payload["status"] = "activated"
            manifest_payload["active_sha256"] = active_sha256
            final_manifest_snapshot = write_canonical_manifest()
            project_fs._assert_unchanged(
                active_relative,
                active_snapshot,
            )
            if (
                _safe_database_digest(
                    project_fs,
                    active_relative,
                    expected=active_snapshot,
                )
                != active_sha256
            ):
                raise LocalCoreMigrationError(
                    "active database changed during final manifest publication"
                )
            project_fs._assert_unchanged(
                active_relative,
                active_snapshot,
            )
            _assert_published_projection_receipts(
                project_fs,
                published_projection_receipts,
            )
            _assert_database_family_receipts(
                project_fs,
                active_relative,
                success_database_receipts,
                expected_digest=active_sha256,
            )
            _assert_recovery_bundle_receipts(
                project_fs,
                recovery_bundle_receipts,
            )
            project_fs._assert_unchanged(
                manifest_relative,
                final_manifest_snapshot,
            )
            final_manifest_sha256 = _safe_file_sha256(
                project_fs,
                manifest_relative,
                expected=final_manifest_snapshot,
            )
            project_fs._assert_unchanged(
                manifest_relative,
                final_manifest_snapshot,
            )

            def verify_success_authorities(
                *,
                expected_active: _PathSnapshot = active_snapshot,
                expected_active_digest: str = active_sha256,
                expected_database_family: dict[
                    Path,
                    _PathSnapshot,
                ] = dict(success_database_receipts),
                expected_projections: dict[
                    Path,
                    _FileAuthorityReceipt,
                ] = dict(published_projection_receipts),
                expected_recovery_bundle: dict[
                    Path,
                    _FileAuthorityReceipt,
                ] = dict(recovery_bundle_receipts),
                expected_manifest: _PathSnapshot = final_manifest_snapshot,
                expected_manifest_digest: str = final_manifest_sha256,
            ) -> None:
                project_fs._assert_unchanged(
                    active_relative,
                    expected_active,
                )
                if (
                    _safe_database_digest(
                        project_fs,
                        active_relative,
                        expected=expected_active,
                    )
                    != expected_active_digest
                ):
                    raise LocalCoreMigrationError(
                        "active database changed before migration sentinel cleanup"
                    )
                project_fs._assert_unchanged(
                    active_relative,
                    expected_active,
                )
                _assert_database_family_receipts(
                    project_fs,
                    active_relative,
                    expected_database_family,
                    expected_digest=expected_active_digest,
                )
                _assert_published_projection_receipts(
                    project_fs,
                    expected_projections,
                )
                _assert_recovery_bundle_receipts(
                    project_fs,
                    expected_recovery_bundle,
                )
                project_fs._assert_unchanged(
                    manifest_relative,
                    expected_manifest,
                )
                if (
                    _safe_file_sha256(
                        project_fs,
                        manifest_relative,
                        expected=expected_manifest,
                    )
                    != expected_manifest_digest
                ):
                    raise LocalCoreMigrationError(
                        "migration manifest changed before sentinel cleanup"
                    )
                project_fs._assert_unchanged(
                    manifest_relative,
                    expected_manifest,
                )

            migration_guard.arm_verified_clear(
                verify_success_authorities,
                publish_terminal_authority_failure,
            )
            migration_guard.record_recovery_manifest(
                migration_manifest_path,
                final_manifest_snapshot,
            )
            assert report is not None
            return LocalCoreMigrationResult(
                source_version=source_version,
                target_version=target_version,
                active_path=str(active_path),
                active_sha256=active_sha256,
                backup=backup,
                staging=report,
                migration_manifest_path=str(migration_manifest_path),
            )
        except BaseException as exc:
            primary_error = _exception_text(exc)
            rollback_active_snapshot: _PathSnapshot | None = None
            rollback_active_sha256 = ""
            rollback_database_receipts: dict[
                Path,
                _PathSnapshot,
            ] | None = None
            projection_restore_receipts: dict[
                Path,
                _PathSnapshot,
            ] | None = None
            activation_state_diverged = False
            if not activated and activation_attempted:
                try:
                    detected_active_snapshot = project_fs._snapshot(
                        active_relative,
                        allow_missing=True,
                    )
                    detected_staging_snapshot = project_fs._snapshot(
                        staging_relative,
                        allow_missing=True,
                    )
                except BaseException as detection_exc:
                    activated = True
                    manifest_payload["activation_detection_status"] = (
                        "activation-state-unsafe-assume-replacement"
                    )
                    manifest_payload["activation_detection_error"] = _exception_text(
                        detection_exc
                    )
                else:
                    active_matches_staging_identity = (
                        staging_snapshot is not None
                        and detected_active_snapshot == staging_snapshot
                    )
                    active_matches_staging_digest = False
                    if detected_active_snapshot.exists:
                        try:
                            active_matches_staging_digest = (
                                _safe_database_digest(
                                    project_fs,
                                    active_relative,
                                )
                                == expected_active_sha256
                            )
                        except BaseException as detection_exc:
                            manifest_payload["activation_detection_error"] = (
                                _exception_text(detection_exc)
                            )
                    activated = detected_active_snapshot.exists and (
                        not detected_staging_snapshot.exists
                        or active_matches_staging_identity
                        or active_matches_staging_digest
                    )
                    activation_state_diverged = (
                        not activated
                        and detected_active_snapshot
                        != source_active_snapshot
                    )
                    manifest_payload["activation_detection_status"] = (
                        "activation-state-diverged-recovery-required"
                        if activation_state_diverged
                        else "matched-staging-identity"
                        if active_matches_staging_identity
                        else "matched-staging-digest"
                        if active_matches_staging_digest
                        else "staging-missing-assume-replacement"
                        if activated
                        else "not-activated"
                    )
                if activated and "activation_detection_status" not in manifest_payload:
                    activated = True
                    try:
                        active_digest = _safe_database_digest(
                            project_fs,
                            active_relative,
                        )
                    except BaseException as detection_exc:
                        manifest_payload["activation_detection_status"] = (
                            "staging-missing-active-digest-unavailable"
                        )
                        manifest_payload["activation_detection_error"] = (
                            _exception_text(detection_exc)
                        )
                    else:
                        manifest_payload["activation_detection_status"] = (
                            "matched-staging-digest"
                            if active_digest == expected_active_sha256
                            else "staging-missing-active-digest-mismatch"
                        )
            if activated:
                failed_path = backup_dir / (
                    f"harness.{target_label}.failed-after-activation.db"
                )
                try:
                    expected_failed_active_snapshot = (
                        activated_snapshot
                        if activated_snapshot is not None
                        else staging_snapshot
                    )
                    if expected_failed_active_snapshot is None:
                        raise LocalCoreMigrationError(
                            "activated database receipt is unavailable"
                        )
                    restore_destination_snapshot = (
                        expected_failed_active_snapshot
                    )
                    failed_relative = project_fs.relative_to_root(failed_path)
                    while project_fs._snapshot(
                        failed_relative,
                        allow_missing=True,
                    ).exists:
                        failed_path = backup_dir / (
                            f"harness.{target_label}.failed-after-activation-"
                            f"{uuid.uuid4().hex[:8]}.db"
                        )
                        failed_relative = project_fs.relative_to_root(
                            failed_path
                        )
                    (
                        preservation_status,
                        preservation_error,
                        restore_destination_snapshot,
                    ) = _preserve_failed_schema30(
                        active_path,
                        failed_path,
                        expected_active_snapshot=(
                            expected_failed_active_snapshot
                        ),
                        pinned_fs=project_fs,
                    )
                except Exception as preserve_exc:
                    preservation_status = "failed"
                    preservation_error = _exception_text(preserve_exc)
                failure_key = f"failed_{target_label}"
                manifest_payload[f"{failure_key}_preservation_status"] = preservation_status
                manifest_payload[f"{failure_key}_path"] = str(failed_path)
                if preservation_error:
                    manifest_payload[f"{failure_key}_preservation_error"] = preservation_error
                try:
                    rollback_database_receipts = _restore_verified_backup(
                        active_path,
                        backup,
                        expected_active_snapshot=(
                            restore_destination_snapshot
                        ),
                        expected_backup_snapshot=(
                            recovery_bundle_receipts[
                                project_fs.relative_to_root(
                                    Path(backup.backup_path)
                                )
                            ].snapshot
                        ),
                        pinned_fs=project_fs,
                    )
                    rollback_active_snapshot = (
                        rollback_database_receipts[active_relative]
                    )
                    rollback_active_sha256 = backup.sha256
                except BaseException as restore_exc:
                    restore_error = _exception_text(restore_exc)
                    manifest_payload["status"] = "rollback-incomplete"
                    manifest_payload["database_restore_status"] = "failed"
                    manifest_payload["database_restore_error"] = restore_error
                    manifest_payload["projection_restore_status"] = "failed"
                    manifest_payload["projection_restore_error"] = (
                        "not attempted because database restore failed"
                    )
                    manifest_payload["error"] = primary_error
                    write_failure_manifest()
                    raise LocalCoreMigrationError(
                        f"migration failed after activation and database rollback failed: {restore_error}"
                        f"; recovery manifest: {recovery_manifest_path}"
                    ) from restore_exc
                manifest_payload["database_restore_status"] = "restored"
                try:
                    projection_restore_receipts = _restore_projection_backup(
                        root,
                        projection_backup,
                        pinned_fs=project_fs,
                    )
                except BaseException as restore_exc:
                    restore_error = _exception_text(restore_exc)
                    manifest_payload["status"] = "rollback-incomplete"
                    manifest_payload["projection_restore_status"] = "failed"
                    manifest_payload["projection_restore_error"] = restore_error
                    manifest_payload["error"] = primary_error
                    write_failure_manifest()
                    raise LocalCoreMigrationError(
                        f"{primary_error}; database restored but projection restore failed: {restore_error}"
                        f"; recovery manifest: {recovery_manifest_path}"
                    ) from restore_exc
                manifest_payload["status"] = "rolled-back"
                manifest_payload["projection_restore_status"] = "restored"
                if preservation_status == "failed":
                    manifest_payload["status"] = "rollback-incomplete"
                    manifest_payload["error"] = primary_error
                    write_failure_manifest()
                    raise LocalCoreMigrationError(
                        f"{primary_error}; database and projections restored but failed {target_label} diagnostic "
                        f"preservation was incomplete: {preservation_error}; "
                        f"recovery manifest: {recovery_manifest_path}"
                    ) from exc
            else:
                try:
                    current_staging_snapshot = project_fs._snapshot(
                        staging_relative,
                        allow_missing=True,
                    )
                    if current_staging_snapshot.exists:
                        failed_path = backup_dir / (
                            f"harness.{target_label}.failed-before-activation.db"
                        )
                        failed_relative = project_fs.relative_to_root(
                            failed_path
                        )
                        failed_destination_snapshot = project_fs._snapshot(
                            failed_relative,
                            allow_missing=True,
                        )
                        if failed_destination_snapshot.exists:
                            failed_path = backup_dir / (
                                f"harness.{target_label}.failed-before-activation-"
                                f"{uuid.uuid4().hex[:8]}.db"
                            )
                            failed_relative = project_fs.relative_to_root(
                                failed_path
                            )
                            failed_destination_snapshot = project_fs._snapshot(
                                failed_relative,
                                allow_missing=True,
                            )
                        expected_staging_snapshot = (
                            staging_snapshot
                            if staging_snapshot is not None
                            else current_staging_snapshot
                        )
                        project_fs.replace_file(
                            staging_relative,
                            failed_relative,
                            expected_source=expected_staging_snapshot,
                            expected_destination=failed_destination_snapshot,
                        )
                        _quarantine_failed_database_sidecars(
                            staging_path,
                            failed_path,
                            pinned_fs=project_fs,
                        )
                        manifest_payload[f"failed_{target_label}_path"] = str(
                            failed_path
                        )
                except BaseException as preservation_exc:
                    manifest_payload[
                        f"failed_{target_label}_preservation_status"
                    ] = "failed"
                    manifest_payload[
                        f"failed_{target_label}_preservation_error"
                    ] = _exception_text(preservation_exc)
                try:
                    _remove_empty_active_sidecars(
                        active_path,
                        pinned_fs=project_fs,
                    )
                    current_active_snapshot = project_fs._snapshot(
                        active_relative,
                        allow_missing=False,
                    )
                    active_fingerprint = _safe_database_fingerprint(
                        project_fs,
                        active_relative,
                    )
                except BaseException as active_check_exc:
                    manifest_payload["active_source_check_error"] = (
                        _exception_text(active_check_exc)
                    )
                    current_active_snapshot = _PathSnapshot(False)
                    active_fingerprint = None
                if (
                    current_active_snapshot != source_active_snapshot
                    or active_fingerprint != source_fingerprint
                ):
                    manifest_payload["status"] = (
                        "rollback-incomplete"
                        if activation_state_diverged
                        else "source-changed-before-activation"
                    )
                    if activation_state_diverged:
                        manifest_payload["database_restore_status"] = (
                            "unknown-active-preserved"
                        )
                        manifest_payload["projection_restore_status"] = (
                            "not-attempted"
                        )
                    manifest_payload["error"] = primary_error
                    write_failure_manifest()
                    if activation_state_diverged:
                        migration_guard.require_recovery(
                            recovery_manifest_path,
                            snapshot=recovery_manifest_snapshot,
                        )
                    raise LocalCoreMigrationError(
                        (
                            "activation state diverged after publication attempt; "
                            "preserving the unknown active authority for recovery"
                            if activation_state_diverged
                            else "active source changed before activation; refusing to overwrite concurrent facts"
                        )
                    ) from exc
                manifest_payload["database_restore_status"] = "unchanged-verified"
                rollback_active_snapshot = current_active_snapshot
                rollback_active_sha256 = str(
                    source_fingerprint.get(active_relative.name) or ""
                )
                rollback_database_receipts = (
                    _capture_database_family_receipts(
                        project_fs,
                        active_relative,
                        expected_main=current_active_snapshot,
                        expected_digest=rollback_active_sha256,
                    )
                )
                try:
                    projection_restore_receipts = _restore_projection_backup(
                        root,
                        projection_backup,
                        pinned_fs=project_fs,
                    )
                except BaseException as restore_exc:
                    restore_error = _exception_text(restore_exc)
                    manifest_payload["status"] = "rollback-incomplete"
                    manifest_payload["projection_restore_status"] = "failed"
                    manifest_payload["projection_restore_error"] = restore_error
                    manifest_payload["error"] = primary_error
                    write_failure_manifest()
                    migration_guard.require_recovery(
                        recovery_manifest_path,
                        snapshot=recovery_manifest_snapshot,
                    )
                    raise LocalCoreMigrationError(
                        f"{primary_error}; pre-activation projection rollback failed: "
                        f"{restore_error}; recovery manifest: {recovery_manifest_path}"
                    ) from restore_exc
                manifest_payload["projection_restore_status"] = "restored"
                manifest_payload["status"] = "failed-before-activation"
            manifest_payload["error"] = primary_error
            write_failure_manifest()
            if (
                not manifest_fallback_used
                and manifest_payload["status"]
                in {"rolled-back", "failed-before-activation"}
            ):
                rollback_receipt_phase = "database"
                try:
                    if (
                        rollback_active_snapshot is None
                        or not rollback_active_sha256
                        or rollback_database_receipts is None
                        or projection_restore_receipts is None
                    ):
                        raise LocalCoreMigrationError(
                            "terminal rollback receipts are incomplete"
                        )
                    project_fs._assert_unchanged(
                        active_relative,
                        rollback_active_snapshot,
                    )
                    _assert_database_family_receipts(
                        project_fs,
                        active_relative,
                        rollback_database_receipts,
                        expected_digest=rollback_active_sha256,
                    )
                    rollback_receipt_phase = "projection"
                    _assert_projection_receipts(
                        project_fs,
                        projection_restore_receipts,
                        projection_backup,
                    )
                    rollback_receipt_phase = "recovery-bundle"
                    _assert_recovery_bundle_receipts(
                        project_fs,
                        recovery_bundle_receipts,
                    )
                    rollback_receipt_phase = "manifest"
                    terminal_manifest_relative = (
                        project_fs.relative_to_root(
                            recovery_manifest_path
                        )
                    )
                    terminal_manifest_snapshot = (
                        recovery_manifest_snapshot
                    )
                    project_fs._assert_unchanged(
                        terminal_manifest_relative,
                        terminal_manifest_snapshot,
                    )
                    terminal_manifest_sha256 = _safe_file_sha256(
                        project_fs,
                        terminal_manifest_relative,
                        expected=terminal_manifest_snapshot,
                    )
                    project_fs._assert_unchanged(
                        terminal_manifest_relative,
                        terminal_manifest_snapshot,
                    )
                except BaseException as continuity_exc:
                    continuity_error = _exception_text(continuity_exc)
                    manifest_payload["status"] = "rollback-incomplete"
                    manifest_payload["rollback_receipt_phase"] = (
                        rollback_receipt_phase
                    )
                    manifest_payload["rollback_receipt_error"] = (
                        continuity_error
                    )
                    if rollback_receipt_phase == "database":
                        manifest_payload["database_restore_status"] = "failed"
                        manifest_payload["database_restore_error"] = (
                            continuity_error
                        )
                    elif rollback_receipt_phase == "projection":
                        manifest_payload["projection_restore_status"] = "failed"
                        manifest_payload["projection_restore_error"] = (
                            continuity_error
                        )
                    elif rollback_receipt_phase == "recovery-bundle":
                        manifest_payload["recovery_bundle_status"] = "failed"
                        manifest_payload["recovery_bundle_error"] = (
                            continuity_error
                        )
                    else:
                        manifest_payload["manifest_receipt_error"] = (
                            continuity_error
                        )
                    manifest_payload["error"] = primary_error
                    write_failure_manifest()
                    migration_guard.require_recovery(
                        recovery_manifest_path,
                        snapshot=recovery_manifest_snapshot,
                    )
                else:
                    frozen_projection_receipts = dict(
                        projection_restore_receipts
                    )
                    frozen_database_receipts = dict(
                        rollback_database_receipts
                    )
                    frozen_recovery_bundle = dict(
                        recovery_bundle_receipts
                    )

                    def verify_rollback_authorities(
                        *,
                        expected_active: _PathSnapshot = rollback_active_snapshot,
                        expected_active_digest: str = rollback_active_sha256,
                        expected_database_family: dict[
                            Path,
                            _PathSnapshot,
                        ] = frozen_database_receipts,
                        expected_projections: dict[
                            Path,
                            _PathSnapshot,
                        ] = frozen_projection_receipts,
                        expected_recovery_bundle: dict[
                            Path,
                            _FileAuthorityReceipt,
                        ] = frozen_recovery_bundle,
                        expected_manifest_relative: Path = terminal_manifest_relative,
                        expected_manifest: _PathSnapshot = terminal_manifest_snapshot,
                        expected_manifest_digest: str = terminal_manifest_sha256,
                    ) -> None:
                        project_fs._assert_unchanged(
                            active_relative,
                            expected_active,
                        )
                        _assert_database_family_receipts(
                            project_fs,
                            active_relative,
                            expected_database_family,
                            expected_digest=expected_active_digest,
                        )
                        _assert_projection_receipts(
                            project_fs,
                            expected_projections,
                            projection_backup,
                        )
                        _assert_recovery_bundle_receipts(
                            project_fs,
                            expected_recovery_bundle,
                        )
                        project_fs._assert_unchanged(
                            expected_manifest_relative,
                            expected_manifest,
                        )
                        if (
                            _safe_file_sha256(
                                project_fs,
                                expected_manifest_relative,
                                expected=expected_manifest,
                            )
                            != expected_manifest_digest
                        ):
                            raise LocalCoreMigrationError(
                                "rollback manifest changed before sentinel cleanup"
                            )
                        project_fs._assert_unchanged(
                            expected_manifest_relative,
                            expected_manifest,
                        )

                    migration_guard.mark_safe(
                        verify_rollback_authorities,
                        publish_terminal_authority_failure,
                    )
            raise


def migrate_project_to_schema30(
    root: Path,
    *,
    fail_at: str | None = None,
    staging_validator: Callable[[Path], None] | None = None,
    active_validator: Callable[[Path], None] | None = None,
) -> LocalCoreMigrationResult:
    """Retained compatibility migration for the fixed schema-30 contract."""

    return _migrate_project_to_target(
        root,
        target_version=SCHEMA30_VERSION,
        supported_source_versions=frozenset({27, 28, 29}),
        stage_function=stage_supported_schema_to_schema30,
        doctor_function=_schema30_doctor,
        schema_validator=_validate_staging_database,
        target_label="schema30",
        fail_at=fail_at,
        staging_validator=staging_validator,
        active_validator=active_validator,
    )


def _preflight_active_source(
    active_path: Path,
    source_version: int,
    project_fs: ProjectFS,
) -> None:
    if source_version == SCHEMA30_VERSION:
        preflight_schema30_to_active(
            active_path,
            pinned_fs=project_fs,
        )


def migrate_project_to_active_schema(
    root: Path,
    *,
    fail_at: str | None = None,
    staging_validator: Callable[[Path], None] | None = None,
    active_validator: Callable[[Path], None] | None = None,
) -> LocalCoreMigrationResult:
    """Migrate a supported local authority to the one active schema contract."""

    return _migrate_project_to_target(
        root,
        target_version=ACTIVE_SCHEMA_VERSION,
        supported_source_versions=frozenset({27, 28, 29, 30}),
        stage_function=stage_supported_schema_to_active,
        doctor_function=_active_schema_doctor,
        schema_validator=_validate_active_staging_database,
        target_label=f"schema{ACTIVE_SCHEMA_VERSION}",
        source_preflight=_preflight_active_source,
        fail_at=fail_at,
        staging_validator=staging_validator,
        active_validator=active_validator,
    )


def dry_run_project_to_active_schema(
    root: Path,
    *,
    staging_validator: Callable[[Path], None] | None = None,
) -> LocalCoreStagingReport:
    """Stage and validate the active schema without backup or activation.

    The complete source conversion runs under the ordinary project operation
    lock. The temporary staging database is removed before return, while the
    active DB, projections, migration sentinel, and backup tree remain
    untouched.
    """

    active_relative = Path(".ai-team/state/harness.db")
    staging_relative = Path(
        ".ai-team/state/"
        f".harness.schema{ACTIVE_SCHEMA_VERSION}.dry-run-{uuid.uuid4().hex}.db"
    )
    operation_error: BaseException | None = None
    staging_snapshot: _PathSnapshot | None = None
    with project_db_operation(root) as project_fs:
        root = project_fs.root
        active_snapshot = project_fs._snapshot(
            active_relative,
            allow_missing=True,
        )
        if not active_snapshot.exists:
            raise LocalCoreMigrationError(
                f"runtime database is missing: {project_fs.absolute(active_relative)}"
            )
        project_fs.audit(
            (
                *_database_family(active_relative),
                *_database_family(staging_relative),
            ),
            allow_missing=True,
        )
        staging_path = project_fs.absolute(staging_relative)
        try:
            report = stage_supported_schema_to_active(
                project_fs.absolute(active_relative),
                staging_path,
                project_root=root,
                pinned_fs=project_fs,
            )
            staging_snapshot = project_fs._snapshot(
                staging_relative,
                allow_missing=False,
            )
            if staging_validator is not None:
                staging_validator(staging_path)
            project_fs._assert_unchanged(active_relative, active_snapshot)
            return report
        except BaseException as exc:
            operation_error = exc
            raise
        finally:
            if staging_snapshot is None:
                candidate = project_fs._snapshot(
                    staging_relative,
                    allow_missing=True,
                )
                staging_snapshot = candidate if candidate.exists else None
            if staging_snapshot is not None:
                _apply_sqlite_teardown_errors(
                    operation_error,
                    _temporary_sqlite_family_cleanup_errors(
                        project_fs,
                        staging_relative,
                        staging_snapshot,
                        published=False,
                    ),
                )

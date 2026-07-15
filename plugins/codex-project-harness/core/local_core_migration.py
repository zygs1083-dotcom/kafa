"""Side-by-side conversion from the schema 29 runtime to the local schema 30 Kernel."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import uuid
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from .execution import command_matches_template
from .errors import HarnessError
from .project_fs import ProjectFS, ProjectPathSafetyError
from .projections import PROJECTION_PATHS, PROJECTION_ROLLBACK_PATHS
from .schema_lifecycle import (
    SCHEMA30_RUNTIME_VERSION,
    SCHEMA30_CATALOG_TABLES,
    SCHEMA30_TABLES,
    SCHEMA30_VERSION,
    SQLiteBackupManifest,
    backup_sqlite_database,
    create_schema30,
)
from .store import project_db_operation


class LocalCoreMigrationError(HarnessError):
    """Raised when a local-core staging conversion is unsafe or incomplete."""


class InjectedLocalCoreMigrationFailure(LocalCoreMigrationError):
    """Deterministic failure used to prove migration rollback boundaries."""


@dataclass
class _MigrationGuard:
    lock_path: Path
    project_fs: ProjectFS
    recovery_required: bool = False
    manifest_path: Path | None = None
    clear_allowed: bool = False

    def record_manifest(self, manifest_path: Path) -> None:
        self.manifest_path = manifest_path

    def require_recovery(self, manifest_path: Path) -> None:
        payload: dict[str, object] = {
            "pid": os.getpid(),
            "created_at": _timestamp(),
            "target_schema": SCHEMA30_VERSION,
            "status": "recovery-required",
            "manifest_path": str(manifest_path),
        }
        _write_json_atomic(
            self.lock_path,
            payload,
            project_fs=self.project_fs,
        )
        self.recovery_required = True
        self.manifest_path = manifest_path
        self.clear_allowed = False

    def mark_safe(self) -> None:
        self.recovery_required = False
        self.clear_allowed = True


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


def _exception_text(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


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


def _safe_file_sha256(project_fs: ProjectFS, relative: Path) -> str:
    return hashlib.sha256(project_fs.read_bytes(relative)).hexdigest()


def _safe_file_mode(project_fs: ProjectFS, relative: Path) -> int:
    snapshot = project_fs._snapshot(relative, allow_missing=False)
    assert snapshot.identity is not None
    if os.name == "nt":
        return stat.S_IMODE(project_fs.absolute(relative).stat().st_mode)
    return stat.S_IMODE(snapshot.identity.mode_or_attributes)


def _database_family(relative: Path) -> tuple[Path, Path, Path, Path]:
    value = relative.as_posix()
    return (
        relative,
        Path(f"{value}-wal"),
        Path(f"{value}-shm"),
        Path(f"{value}-journal"),
    )


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


def _safe_database_digest(project_fs: ProjectFS, relative: Path) -> str:
    return hashlib.sha256(project_fs.read_bytes(relative)).hexdigest()


def _cleanup_database_family(
    project_fs: ProjectFS,
    relative: Path,
    *,
    include_database: bool,
) -> None:
    family = _database_family(relative)
    candidates = family if include_database else family[1:]
    for candidate in reversed(candidates):
        project_fs.unlink_regular(candidate, missing_ok=True)


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
                "candidate_sha": row.get("candidate_sha") or row.get("reviewed_commit") or "",
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

    def normalize_finding(values: dict[str, object]) -> dict[str, object]:
        values = normalize_cycle(values)
        for key in ("candidate_sha", "waived_by", "waiver_reason", "waiver_scope", "waiver_expires_at"):
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
        source_uri = f"{source_absolute.as_uri()}?mode=ro"
        staging_uri = f"{staging_absolute.as_uri()}?mode=rw"
        try:
            with closing(
                sqlite3.connect(source_uri, uri=True, timeout=5.0)
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
                with closing(
                    sqlite3.connect(staging_uri, uri=True, timeout=5.0)
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
        except BaseException:
            if created_staging:
                _cleanup_database_family(
                    active_destination_fs,
                    staging_relative,
                    include_database=True,
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
        uri = f"{project_fs.absolute(relative).as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=5.0)) as conn:
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
        source_uri = (
            f"{active_source_fs.absolute(source_relative).as_uri()}?mode=ro"
        )
        destination_uri = (
            f"{active_destination_fs.absolute(destination_relative).as_uri()}?mode=rw"
        )
        try:
            with closing(
                sqlite3.connect(source_uri, uri=True, timeout=5.0)
            ) as source:
                source.execute("pragma query_only = on")
                active_source_fs._assert_unchanged(
                    source_relative,
                    source_snapshot,
                )
                with closing(
                    sqlite3.connect(destination_uri, uri=True, timeout=5.0)
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
        except BaseException:
            _cleanup_database_family(
                active_destination_fs,
                destination_relative,
                include_database=True,
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
        source_uri = f"{project_fs.absolute(relative).as_uri()}?mode=ro"
        with closing(
            sqlite3.connect(source_uri, uri=True, timeout=5.0)
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
        source_uri = f"{source_fs.absolute(source_relative).as_uri()}?mode=ro"
        with closing(
            sqlite3.connect(source_uri, uri=True, timeout=5.0)
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
        if (
            _safe_database_fingerprint(source_fs, source_relative)
            != source_fingerprint
        ):
            _cleanup_database_family(
                destination_fs,
                staging_relative,
                include_database=True,
            )
            raise LocalCoreMigrationError(
                f"active schema {source_version} database changed during isolated legacy conversion"
            )
    return replace(
        report,
        source_version=source_version,
        source_path=str(source_absolute),
        source_sha256=source_sha256,
        source_row_counts=original_counts,
    )


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json_atomic(
    path: Path,
    payload: dict[str, object],
    *,
    project_fs: ProjectFS | None = None,
) -> None:
    root = _project_root_for_internal_path(path)
    with _project_fs_scope(root, project_fs) as active_project_fs:
        relative = active_project_fs.relative_to_root(path)
        legacy_temporary = relative.with_name(relative.name + ".tmp")
        active_project_fs.audit(
            (relative, legacy_temporary),
            allow_missing=True,
        )
        active_project_fs.atomic_write(
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

            mode = _safe_file_mode(project_fs, relative_path)
            content = project_fs.read_bytes(relative_path)
            digest = hashlib.sha256(content).hexdigest()
            projection_copy = (
                projection_dir / f"{index:02d}-{relative_path.name}.bin"
            )
            project_fs.create_exclusive(
                projection_copy,
                content,
                mode=0o600,
            )
            if _safe_file_sha256(project_fs, projection_copy) != digest:
                raise LocalCoreMigrationError(
                    f"projection backup digest mismatch: {relative_path}"
                )
            project_fs._assert_unchanged(relative_path, snapshot)
            if (
                _safe_file_sha256(project_fs, relative_path) != digest
                or _safe_file_mode(project_fs, relative_path) != mode
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
) -> None:
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
                    project_fs.unlink_regular(relative_path)
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
            backup_content = project_fs.read_bytes(backup_path)
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
                and _safe_file_sha256(project_fs, relative_path) == digest
                and _safe_file_mode(project_fs, relative_path) == mode
            ):
                continue
            project_fs.atomic_write(
                relative_path,
                backup_content,
                mode=mode,
            )
            if (
                _safe_file_sha256(project_fs, relative_path) != digest
                or _safe_file_mode(project_fs, relative_path) != mode
            ):
                raise LocalCoreMigrationError(
                    f"restored projection failed verification: {relative_path}"
                )

        for relative_path, entry in zip(
            PROJECTION_ROLLBACK_PATHS,
            entries,
            strict=True,
        ):
            snapshot = project_fs._snapshot(
                relative_path,
                allow_missing=True,
            )
            if entry["existed"] is False:
                if snapshot.exists:
                    raise LocalCoreMigrationError(
                        f"projection should be absent after rollback: {relative_path}"
                    )
                continue
            if (
                not snapshot.exists
                or _safe_file_sha256(project_fs, relative_path)
                != entry["sha256"]
                or _safe_file_mode(project_fs, relative_path) != entry["mode"]
            ):
                raise LocalCoreMigrationError(
                    "projection rollback bundle failed final verification: "
                    f"{relative_path}"
                )


@contextmanager
def _project_migration_lock(
    root: Path,
) -> Iterator[tuple[_MigrationGuard, ProjectFS]]:
    relative_lock = Path(".ai-team/state/local-core-migration.lock")
    with ProjectFS.open(root) as project_fs:
        root = project_fs.root
        lock_path = project_fs.absolute(relative_lock)
        payload = json.dumps(
            {
                "pid": os.getpid(),
                "created_at": _timestamp(),
                "target_schema": SCHEMA30_VERSION,
            },
            sort_keys=True,
        ).encode("utf-8")
        try:
            project_fs.create_exclusive(
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
        )
        completed = False
        try:
            with project_db_operation(
                root,
                purpose="migration",
                project_fs=project_fs,
            ) as locked_project_fs:
                yield guard, locked_project_fs
                completed = True
        finally:
            if completed or guard.clear_allowed:
                project_fs.unlink_regular(relative_lock, missing_ok=True)
            elif guard.recovery_required:
                try:
                    recovery_payload: dict[str, object] = {
                        "pid": os.getpid(),
                        "created_at": _timestamp(),
                        "target_schema": SCHEMA30_VERSION,
                        "status": "rollback-incomplete",
                    }
                    if guard.manifest_path is not None:
                        recovery_payload["manifest_path"] = str(
                            guard.manifest_path
                        )
                    _write_json_atomic(
                        lock_path,
                        recovery_payload,
                        project_fs=project_fs,
                    )
                except BaseException:
                    # The exclusive original sentinel remains fail-closed.
                    pass
            elif guard.manifest_path is not None:
                try:
                    _write_json_atomic(
                        lock_path,
                        {
                            "pid": os.getpid(),
                            "created_at": _timestamp(),
                            "target_schema": SCHEMA30_VERSION,
                            "status": "migration-failed",
                            "manifest_path": str(guard.manifest_path),
                        },
                        project_fs=project_fs,
                    )
                except BaseException:
                    # Preserve the original diagnostic sentinel.
                    pass


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
        uri = f"{project_fs.absolute(relative).as_uri()}?mode=rw"
        with closing(sqlite3.connect(uri, uri=True, timeout=5.0)) as conn:
            conn.execute("pragma busy_timeout = 5000")
            project_fs._assert_unchanged(relative, snapshot)
            result = conn.execute(
                "pragma wal_checkpoint(truncate)"
            ).fetchone()
            project_fs._assert_unchanged(relative, snapshot)
        project_fs.audit(_database_family(relative), allow_missing=True)
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
    pinned_fs: ProjectFS | None = None,
) -> None:
    with _project_fs_scope(
        _project_root_for_database(staging_path),
        pinned_fs,
    ) as project_fs:
        relative = project_fs.relative_to_root(staging_path)
        snapshot = project_fs._snapshot(relative, allow_missing=False)
        project_fs.audit(_database_family(relative), allow_missing=True)
        uri = f"{project_fs.absolute(relative).as_uri()}?mode=rw"
        with closing(sqlite3.connect(uri, uri=True, timeout=5.0)) as conn:
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
                    SCHEMA30_VERSION,
                ),
            )
            if updated.rowcount != 1:
                conn.rollback()
                raise LocalCoreMigrationError(
                    "schema 30 staging database is missing its migration record"
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
                    SCHEMA30_VERSION,
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
            _validate_staging_database(conn)
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
        uri = f"{project_fs.absolute(relative).as_uri()}?mode=ro&immutable=1"
        with closing(sqlite3.connect(uri, uri=True, timeout=5.0)) as conn:
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
        for source, destination, source_snapshot in zip(
            sources,
            destinations,
            source_snapshots,
            strict=True,
        ):
            if not source_snapshot.exists:
                continue
            project_fs._assert_unchanged(source, source_snapshot)
            project_fs.replace_file(source, destination)
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
    pinned_fs: ProjectFS | None = None,
) -> None:
    root = _project_root_for_database(active_path)
    with _project_fs_scope(root, pinned_fs) as project_fs:
        active_relative = project_fs.relative_to_root(active_path)
        backup_relative = project_fs.relative_to_root(Path(backup.backup_path))
        restore_relative = active_relative.with_name(
            active_relative.name + ".restore"
        )
        sidecars = _database_family(active_relative)[1:]
        project_fs._snapshot(backup_relative, allow_missing=False)
        project_fs._snapshot(active_relative, allow_missing=True)
        project_fs._snapshot(restore_relative, allow_missing=True)
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

        backup_payload = project_fs.read_bytes(backup_relative)
        if hashlib.sha256(backup_payload).hexdigest() != backup.sha256:
            raise LocalCoreMigrationError(
                "verified migration backup is missing or has a digest mismatch"
            )
        project_fs.atomic_write(
            restore_relative,
            backup_payload,
            mode=0o600,
        )
        if _safe_database_digest(project_fs, restore_relative) != backup.sha256:
            raise LocalCoreMigrationError(
                "temporary restored database does not match the verified backup digest"
            )
        project_fs.replace_file(restore_relative, active_relative)
        active_snapshot = project_fs._snapshot(
            active_relative,
            allow_missing=False,
        )
        if _safe_database_digest(project_fs, active_relative) != backup.sha256:
            raise LocalCoreMigrationError(
                "restored active database does not match the verified backup digest"
            )
        uri = f"{project_fs.absolute(active_relative).as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=5.0)) as conn:
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
        for sidecar in sidecars:
            project_fs.unlink_regular(sidecar, missing_ok=True)
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


def _diagnostic_database_digest(
    project_fs: ProjectFS,
    relative: Path,
) -> str:
    return _safe_database_digest(project_fs, relative)


def _move_failed_schema30(
    project_fs: ProjectFS,
    source: Path,
    destination: Path,
) -> None:
    project_fs.replace_file(source, destination)


def _copy_failed_schema30(
    project_fs: ProjectFS,
    source: Path,
    destination: Path,
) -> None:
    project_fs.atomic_write(
        destination,
        project_fs.read_bytes(source),
        mode=0o600,
    )


def _cleanup_failed_schema30(
    project_fs: ProjectFS,
    relative: Path,
) -> None:
    project_fs.unlink_regular(relative, missing_ok=True)


def _preserve_failed_schema30(
    active_path: Path,
    failed_path: Path,
    *,
    pinned_fs: ProjectFS | None = None,
) -> tuple[str, str]:
    """Best-effort diagnostic preservation that must never block authority restore."""
    root = _project_root_for_database(active_path)
    with _project_fs_scope(root, pinned_fs) as project_fs:
        active_relative = project_fs.relative_to_root(active_path)
        failed_relative = project_fs.relative_to_root(failed_path)
        active_snapshot = project_fs._snapshot(
            active_relative,
            allow_missing=False,
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
            )

        try:
            _move_failed_schema30(
                project_fs,
                active_relative,
                failed_relative,
            )
        except ProjectPathSafetyError:
            raise
        except BaseException as move_exc:
            try:
                _copy_failed_schema30(
                    project_fs,
                    active_relative,
                    failed_relative,
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
                )
            preservation_status = "copied-after-move-failure"
            preservation_error = _exception_text(move_exc)
        else:
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
        )
    return preservation_status, preservation_error


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
        for sidecar in sidecars:
            project_fs.unlink_regular(sidecar, missing_ok=True)


def _activate_staging_database(
    project_fs: ProjectFS,
    staging_relative: Path,
    active_relative: Path,
) -> None:
    project_fs.replace_file(staging_relative, active_relative)


def migrate_project_to_schema30(
    root: Path,
    *,
    fail_at: str | None = None,
    staging_validator: Callable[[Path], None] | None = None,
    active_validator: Callable[[Path], None] | None = None,
) -> LocalCoreMigrationResult:
    """Back up, stage, atomically activate, and automatically roll back schema 30."""

    if active_validator is None:
        raise LocalCoreMigrationError(
            "post-activation projection publication and validator callback is required"
        )
    if fail_at is not None and fail_at not in MIGRATION_FAILURE_POINTS:
        raise LocalCoreMigrationError(
            f"unknown migration failure point {fail_at!r}; expected one of {sorted(MIGRATION_FAILURE_POINTS)}"
        )
    active_relative = Path(".ai-team/state/harness.db")
    with _project_migration_lock(root) as (
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
        if source_version not in {27, 28, 29}:
            migration_guard.mark_safe()
            raise LocalCoreMigrationError(
                f"unsupported local-core migration source schema {source_version}"
            )
        source_fingerprint = _safe_database_fingerprint(
            project_fs,
            active_relative,
        )
        backup = backup_sqlite_database(
            root,
            source_path=active_path,
            expected_source_version=source_version,
            project_fs=project_fs,
        )
        backup_dir = Path(backup.backup_path).parent
        staging_path = backup_dir / "harness.schema30.new.db"
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
            "target_version": SCHEMA30_VERSION,
            "backup": backup.safe_payload(),
            "projection_backup": {"status": "pending"},
            "projection_restore_status": "not-needed",
            "failure_point": fail_at or "",
        }
        _write_json_atomic(
            migration_manifest_path,
            manifest_payload,
            project_fs=project_fs,
        )
        migration_guard.record_manifest(migration_manifest_path)
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
            _write_json_atomic(
                migration_manifest_path,
                manifest_payload,
                project_fs=project_fs,
            )
            raise
        manifest_payload["projection_backup"] = projection_backup
        _write_json_atomic(
            migration_manifest_path,
            manifest_payload,
            project_fs=project_fs,
        )
        activated = False
        activation_attempted = False
        expected_active_sha256 = ""
        report: LocalCoreStagingReport | None = None
        try:
            _inject_failure(fail_at, "before_copy")
            report = stage_supported_schema_to_schema30(
                active_path,
                staging_path,
                project_root=root,
                fail_at=fail_at,
                pinned_fs=project_fs,
            )
            manifest_payload["staging"] = asdict(report)
            manifest_payload["status"] = "staged"
            _write_json_atomic(
                migration_manifest_path,
                manifest_payload,
                project_fs=project_fs,
            )
            _finalize_staging_metadata(
                staging_path,
                report,
                backup,
                migration_manifest_path,
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
            migration_guard.require_recovery(migration_manifest_path)
            activation_attempted = True
            _activate_staging_database(
                project_fs,
                staging_relative,
                active_relative,
            )
            activated = True
            _inject_failure(fail_at, "after_atomic_replace")
            _schema30_doctor(active_path, pinned_fs=project_fs)
            active_snapshot = project_fs._snapshot(
                active_relative,
                allow_missing=False,
            )
            project_fs.audit(
                _database_family(active_relative),
                allow_missing=True,
            )
            active_uri = (
                f"{project_fs.absolute(active_relative).as_uri()}?mode=rw"
            )
            with closing(
                sqlite3.connect(active_uri, uri=True, timeout=5.0)
            ) as callback_prep:
                project_fs._assert_unchanged(
                    active_relative,
                    active_snapshot,
                )
                journal_mode = callback_prep.execute(
                    "pragma journal_mode=wal"
                ).fetchone()
                if journal_mode is None or str(journal_mode[0]).lower() != "wal":
                    raise LocalCoreMigrationError(
                        f"cannot stabilize callback database journal mode: {journal_mode}"
                    )
                checkpoint = callback_prep.execute(
                    "pragma wal_checkpoint(truncate)"
                ).fetchone()
                if checkpoint is None or int(checkpoint[0]) != 0:
                    raise LocalCoreMigrationError(
                        f"cannot stabilize callback database WAL: {checkpoint}"
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
            if active_validator:
                active_validator(active_path)
            post_callback_fingerprint = _safe_database_fingerprint(
                project_fs,
                active_relative,
            )
            if post_callback_fingerprint != pre_callback_fingerprint:
                raise LocalCoreMigrationError(
                    "projection callback mutated the active database authority"
                )
            _schema30_doctor(active_path, pinned_fs=project_fs)
            from core.projections import projection_content_issues

            projection_issues = projection_content_issues(root)
            if projection_issues:
                raise LocalCoreMigrationError(
                    "post-activation projection content verification failed: "
                    + "; ".join(projection_issues)
                )
            active_sha256 = _safe_database_digest(
                project_fs,
                active_relative,
            )
            manifest_payload["status"] = "activated"
            manifest_payload["active_sha256"] = active_sha256
            _write_json_atomic(
                migration_manifest_path,
                manifest_payload,
                project_fs=project_fs,
            )
            assert report is not None
            return LocalCoreMigrationResult(
                source_version=source_version,
                target_version=SCHEMA30_VERSION,
                active_path=str(active_path),
                active_sha256=active_sha256,
                backup=backup,
                staging=report,
                migration_manifest_path=str(migration_manifest_path),
            )
        except BaseException as exc:
            primary_error = _exception_text(exc)
            if not activated and activation_attempted:
                try:
                    active_exists = project_fs._snapshot(
                        active_relative,
                        allow_missing=True,
                    ).exists
                    staging_exists = project_fs._snapshot(
                        staging_relative,
                        allow_missing=True,
                    ).exists
                except BaseException as detection_exc:
                    activated = True
                    manifest_payload["activation_detection_status"] = (
                        "activation-state-unsafe-assume-replacement"
                    )
                    manifest_payload["activation_detection_error"] = _exception_text(
                        detection_exc
                    )
                else:
                    activated = active_exists and not staging_exists
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
                migration_guard.require_recovery(migration_manifest_path)
                failed_path = backup_dir / "harness.schema30.failed-after-activation.db"
                try:
                    failed_relative = project_fs.relative_to_root(failed_path)
                    while project_fs._snapshot(
                        failed_relative,
                        allow_missing=True,
                    ).exists:
                        failed_path = backup_dir / (
                            "harness.schema30.failed-after-activation-"
                            f"{uuid.uuid4().hex[:8]}.db"
                        )
                        failed_relative = project_fs.relative_to_root(
                            failed_path
                        )
                    (
                        preservation_status,
                        preservation_error,
                    ) = _preserve_failed_schema30(
                        active_path,
                        failed_path,
                        pinned_fs=project_fs,
                    )
                except Exception as preserve_exc:
                    preservation_status = "failed"
                    preservation_error = _exception_text(preserve_exc)
                manifest_payload["failed_schema30_preservation_status"] = preservation_status
                manifest_payload["failed_schema30_path"] = str(failed_path)
                if preservation_error:
                    manifest_payload["failed_schema30_preservation_error"] = preservation_error
                try:
                    _restore_verified_backup(
                        active_path,
                        backup,
                        pinned_fs=project_fs,
                    )
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
                    _write_json_atomic(
                        migration_manifest_path,
                        manifest_payload,
                        project_fs=project_fs,
                    )
                    raise LocalCoreMigrationError(
                        f"migration failed after activation and database rollback failed: {restore_error}"
                        f"; recovery manifest: {migration_manifest_path}"
                    ) from restore_exc
                manifest_payload["database_restore_status"] = "restored"
                try:
                    _restore_projection_backup(
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
                    _write_json_atomic(
                        migration_manifest_path,
                        manifest_payload,
                        project_fs=project_fs,
                    )
                    raise LocalCoreMigrationError(
                        f"{primary_error}; database restored but projection restore failed: {restore_error}"
                        f"; recovery manifest: {migration_manifest_path}"
                    ) from restore_exc
                manifest_payload["status"] = "rolled-back"
                manifest_payload["projection_restore_status"] = "restored"
                if preservation_status == "failed":
                    manifest_payload["status"] = "rollback-incomplete"
                    manifest_payload["error"] = primary_error
                    _write_json_atomic(
                        migration_manifest_path,
                        manifest_payload,
                        project_fs=project_fs,
                    )
                    raise LocalCoreMigrationError(
                        f"{primary_error}; database and projections restored but failed schema30 diagnostic "
                        f"preservation was incomplete: {preservation_error}; "
                        f"recovery manifest: {migration_manifest_path}"
                    ) from exc
            else:
                try:
                    staging_exists = project_fs._snapshot(
                        staging_relative,
                        allow_missing=True,
                    ).exists
                    if staging_exists:
                        failed_path = backup_dir / (
                            "harness.schema30.failed-before-activation.db"
                        )
                        failed_relative = project_fs.relative_to_root(
                            failed_path
                        )
                        if project_fs._snapshot(
                            failed_relative,
                            allow_missing=True,
                        ).exists:
                            failed_path = backup_dir / (
                                "harness.schema30.failed-before-activation-"
                                f"{uuid.uuid4().hex[:8]}.db"
                            )
                            failed_relative = project_fs.relative_to_root(
                                failed_path
                            )
                        project_fs.replace_file(
                            staging_relative,
                            failed_relative,
                        )
                        _quarantine_failed_database_sidecars(
                            staging_path,
                            failed_path,
                            pinned_fs=project_fs,
                        )
                        manifest_payload["failed_schema30_path"] = str(
                            failed_path
                        )
                except BaseException as preservation_exc:
                    manifest_payload[
                        "failed_schema30_preservation_status"
                    ] = "failed"
                    manifest_payload[
                        "failed_schema30_preservation_error"
                    ] = _exception_text(preservation_exc)
                if (
                    _safe_database_fingerprint(project_fs, active_relative)
                    != source_fingerprint
                ):
                    manifest_payload["status"] = "source-changed-before-activation"
                    manifest_payload["error"] = primary_error
                    _write_json_atomic(
                        migration_manifest_path,
                        manifest_payload,
                        project_fs=project_fs,
                    )
                    raise LocalCoreMigrationError(
                        "active source changed before activation; refusing to overwrite concurrent facts"
                    ) from exc
                manifest_payload["database_restore_status"] = "unchanged-verified"
                try:
                    _restore_projection_backup(
                        root,
                        projection_backup,
                        pinned_fs=project_fs,
                    )
                except BaseException as restore_exc:
                    restore_error = _exception_text(restore_exc)
                    migration_guard.require_recovery(migration_manifest_path)
                    manifest_payload["status"] = "rollback-incomplete"
                    manifest_payload["projection_restore_status"] = "failed"
                    manifest_payload["projection_restore_error"] = restore_error
                    manifest_payload["error"] = primary_error
                    _write_json_atomic(
                        migration_manifest_path,
                        manifest_payload,
                        project_fs=project_fs,
                    )
                    raise LocalCoreMigrationError(
                        f"{primary_error}; pre-activation projection rollback failed: "
                        f"{restore_error}; recovery manifest: {migration_manifest_path}"
                    ) from restore_exc
                manifest_payload["projection_restore_status"] = "restored"
                manifest_payload["status"] = "failed-before-activation"
            manifest_payload["error"] = primary_error
            _write_json_atomic(
                migration_manifest_path,
                manifest_payload,
                project_fs=project_fs,
            )
            if manifest_payload["status"] in {"rolled-back", "failed-before-activation"}:
                migration_guard.mark_safe()
            raise

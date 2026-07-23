#!/usr/bin/env python3
"""SQLite-backed runtime for Codex Project Harness."""

from __future__ import annotations

import json
import hashlib
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
import uuid
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Iterator

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from harness_lib import (
    DistributionManifestError,
    ensure_parent,
    git_dirty,
    load_distribution_manifest,
    markdown_row,
    now_iso,
)
from core import RUNTIME_VERSION, SCHEMA_VERSION
from core.errors import HarnessError
from core.delivery_plan import (
    DeliveryPlan,
    DeliveryPlanError,
    derive_plan_ids,
    logical_plan_digest,
    normalize_plan_id,
    parse_delivery_plan,
    plan_ids,
    planned_mutations,
)
from core.execution import (
    latest_acceptance_target_qualification,
    recorded_execution_provenance_issues,
    target_definition_digest,
)
from core.lock_manager import parse_time
from core.outcome_metrics import (
    OUTCOME_EVIDENCE_MODE,
    OUTCOME_METRICS_VERSION,
    build_outcome_metrics,
)
from core.operator_output import (
    OperatorEnvelope,
    build_operator_envelope,
)
from core.cycle_ledger import (
    DEFAULT_CYCLE_ID,
    LEGACY_CYCLE_ID,
    baseline_issues,
    baseline_snapshot,
    current_candidate_sha,
    current_cycle_id,
    current_cycle_row,
    ensure_delivery_cycles,
    latest_baseline,
    project_row,
    trace_rows,
    trace_snapshot,
    traceability_issues,
)
from core.schema_guard import (
    ACCEPTANCE_STATUSES,
    FAILURE_MODE_STATUSES,
    REQUIREMENT_STATUSES,
    RESULT_FORMATS,
    SANDBOX_STATUSES,
    STACK_PROFILES,
    TASK_STATUSES,
    TEST_TARGET_KINDS,
    normalize_outcome_timestamp,
)
from core.store import (
    DB_PATH,
    MIGRATION_SENTINEL_PATH,
    OPERATION_LOCK_PATH,
    InMemoryStore,
    SqliteStore,
    Store,
    _verified_sqlite_connection,
    project_db_operation,
    raise_if_project_migration_announced,
)
from core.project_fs import ProjectFS, pin_project_filesystem
from core.schema_lifecycle import (
    ACTIVE_SCHEMA_CATALOG_TABLES,
    ACTIVE_SCHEMA_TABLES,
    backup_sqlite_database,
    create_schema as create_schema29,
    create_active_schema,
)


REGISTERED_SCHEMA_SOURCES = frozenset({27, 28, 29, 30})
DEFAULT_CONTAINER_IMAGE = "python:3.12-slim"
STACK_PROFILE_IMAGES = {
    "python": DEFAULT_CONTAINER_IMAGE,
    "node": "node:22-bookworm-slim",
    "go": "golang:1.23-bookworm",
    "rust": "rust:1.83-bookworm",
    "java": "eclipse-temurin:21-jdk",
    "browser-e2e": "mcr.microsoft.com/playwright:v1.49.0-noble",
    "data-integration": DEFAULT_CONTAINER_IMAGE,
}


def _before_staging_validation_snapshot_read(
    _project_fs: ProjectFS,
    _relative: Path,
) -> None:
    """Deterministic test seam before copying the pinned staging authority."""


def _before_verified_patch_envelope(
    _root: Path,
    _execution_id: str,
    _validation_id: str,
) -> None:
    """Deterministic test seam after verification and before final candidate check."""


RUNTIME_GITIGNORE_PATTERNS = [
    ".ai-team/state/",
    ".ai-team/backups/",
    ".ai-team/runtime/",
    "__pycache__/",
    "*.pyc",
]
CODEX_AGENT_TEMPLATE_FIELDS = frozenset({"name", "description", "developer_instructions"})


def codex_agent_template_names() -> tuple[str, ...]:
    """Return the Native template inventory from this exact plugin root."""

    try:
        distribution = load_distribution_manifest(PLUGIN_ROOT)
    except DistributionManifestError as exc:
        raise HarnessError(str(exc)) from exc
    return tuple(distribution["templates"]["native_agents"])

PHASES = [
    "intake",
    "project_bootstrap",
    "requirement_baseline",
    "confirmation",
    "team_architecture",
    "planning",
    "implementation",
    "qa",
    "delivery_readiness",
    "retrospective",
    "archived",
]

PHASE_TRANSITIONS = {
    "intake": {"project_bootstrap"},
    "project_bootstrap": {"requirement_baseline"},
    "requirement_baseline": {"confirmation"},
    "confirmation": {"team_architecture", "planning"},
    "team_architecture": {"planning"},
    "planning": {"implementation"},
    "implementation": {"qa"},
    "qa": {"delivery_readiness", "implementation"},
    "delivery_readiness": {"retrospective"},
    "retrospective": {"archived"},
    "archived": set(),
}

DELIVERY_CYCLE_STATUSES = {"active", "delivered", "archived"}
VALIDATION_STATUSES = {"active", "superseded", "invalidated"}
GATEABLE_TEST_PREFIXES = [
    "python3 -m unittest",
    "python3 -B -m unittest",
    "python -m unittest",
    "python3 -m pytest",
    "python -m pytest",
    "pytest",
    "npm test",
    "npm run test",
    "pnpm test",
    "pnpm run test",
    "yarn test",
    "yarn run test",
    "jest",
    "npx jest",
    "go test",
    "cargo test",
    "dotnet test",
    "make test",
]
DUMB_COMMAND_PREFIXES = ["echo", "true", "false", "cat", "pwd", "ls", "printf"]

_store_factory: Callable[[Path], Store] = SqliteStore


_CLOSED_CYCLE_MUTATION_ALLOWLIST = frozenset(
    {
        "init_runtime",
        "cycle_start",
        "record_outcome_observation",
        "record_decision",
        "render_all",
        "render_affected",
        "_apply_delivery_plan_locked",
    }
)


def _require_active_cycle_for_mutation(root: Path, operation: str) -> None:
    """Prevent public delivery facts from changing after the cycle is closed."""

    if operation in _CLOSED_CYCLE_MUTATION_ALLOWLIST:
        return
    with connection(root) as conn:
        cycle = current_cycle_row(conn)
    if str(cycle["status"]) != "active":
        raise HarnessError(
            "current cycle is closed for mutation: "
            f"{cycle['id']} status={cycle['status']}; "
            f"start a new cycle before {operation}"
        )


def _project_mutation(function: Callable[..., Any]) -> Callable[..., Any]:
    """Keep each DB mutation and its synchronous projections in one operation lock."""

    @wraps(function)
    def locked(root: Path, *args: Any, **kwargs: Any) -> Any:
        if isinstance(get_store(root), InMemoryStore):
            _require_active_cycle_for_mutation(root, function.__name__)
            return function(root, *args, **kwargs)
        if function.__name__ == "init_runtime":
            _preflight_init_paths(root)
        with project_db_operation(root):
            from core.projections import preflight_projection_paths

            preflight_projection_paths(root)
            _require_active_cycle_for_mutation(root, function.__name__)
            return function(root, *args, **kwargs)

    return locked


def _preflight_init_paths(root: Path) -> None:
    """Audit every canonical init destination before the operation lock writes."""

    from core.projections import PROJECTION_ROLLBACK_PATHS

    database_family = SqliteStore._db_family()
    templates = tuple(
        Path(".codex/agents") / name
        for name in sorted(codex_agent_template_names())
    )
    with ProjectFS.open(root) as project_fs:
        project_fs.audit(
            (
                *database_family,
                OPERATION_LOCK_PATH,
                MIGRATION_SENTINEL_PATH,
                Path(".gitignore"),
                *PROJECTION_ROLLBACK_PATHS,
                *templates,
            ),
            allow_missing=True,
        )


def create_schema(conn: sqlite3.Connection) -> None:
    """Create or validate the generation-neutral active local-only Kernel."""

    tables = {
        str(row[0])
        for row in conn.execute(
            "select name from sqlite_master where type='table'"
        )
    }
    if not tables:
        create_active_schema(conn)
        return
    if tables == ACTIVE_SCHEMA_CATALOG_TABLES:
        return
    project_version = None
    if "project" in tables:
        row = conn.execute("select schema_version from project where id=1").fetchone()
        project_version = int(row[0]) if row else None
    if project_version in REGISTERED_SCHEMA_SOURCES:
        raise HarnessError(
            f"schema {project_version} requires side-by-side migration; run migrate "
            f"--from-version {project_version} --to-version {SCHEMA_VERSION}"
        )
    raise HarnessError(
        f"active schema {SCHEMA_VERSION} table inventory mismatch: "
        f"missing={sorted(ACTIVE_SCHEMA_CATALOG_TABLES - tables)} "
        f"extra={sorted(tables - ACTIVE_SCHEMA_CATALOG_TABLES)}"
    )


def set_store_factory(factory: Callable[[Path], Store]) -> None:
    """Test seam: override how stores are created."""
    global _store_factory
    _store_factory = factory


def get_store(root: Path) -> Store:
    return _store_factory(Path(root))


def db_file(root: Path) -> Path:
    return root / DB_PATH


def runtime_initialized(root: Path) -> bool:
    store = get_store(root)
    with runtime_path_audit(root, store=store) as project_fs:
        return _runtime_initialized_in_audit(
            root,
            store,
            project_fs,
        )


def _runtime_initialized_in_audit(
    root: Path,
    store: Store,
    project_fs: ProjectFS | None,
) -> bool:
    if isinstance(store, SqliteStore):
        if project_fs is None:
            return False
        if not project_fs._snapshot(
            DB_PATH,
            allow_missing=True,
        ).exists:
            return False
    try:
        with store.connection() as conn:
            exists = conn.execute(
                "select 1 from sqlite_master where type='table' and name = 'project'"
            ).fetchone()
            if not exists:
                return False
            return (
                conn.execute(
                    "select 1 from project where id = 1"
                ).fetchone()
                is not None
            )
    except sqlite3.Error:
        return False


def project_doctor_probe(root: Path) -> dict[str, object]:
    """Capture initialization and gitignore facts under one pinned audit."""

    store = get_store(root)
    with runtime_path_audit(root, store=store) as project_fs:
        database_exists = (
            not isinstance(store, SqliteStore)
            or (
                project_fs is not None
                and project_fs._snapshot(DB_PATH, allow_missing=True).exists
            )
        )
        initialized = _runtime_initialized_in_audit(
            root,
            store,
            project_fs,
        )
        gitignore_issues = (
            _gitignore_runtime_issues(project_fs)
            if project_fs is not None
            else [
                f"missing .gitignore runtime pattern: {pattern}"
                for pattern in RUNTIME_GITIGNORE_PATTERNS
            ]
        )
        return {
            "initialized": initialized,
            "database_exists": database_exists,
            "state_error": (
                ""
                if initialized or not database_exists
                else (
                    "existing harness database is unreadable or has no canonical "
                    "project state; inspect or recover it before initialization"
                )
            ),
            "gitignore_issues": gitignore_issues,
        }


def _runtime_audit_inventory() -> tuple[Path, ...]:
    from core.projections import PROJECTION_ROLLBACK_PATHS

    templates = tuple(
        Path(".codex/agents") / name
        for name in sorted(codex_agent_template_names())
    )
    return (
        *SqliteStore._db_family(),
        OPERATION_LOCK_PATH,
        MIGRATION_SENTINEL_PATH,
        Path(".gitignore"),
        *PROJECTION_ROLLBACK_PATHS,
        *templates,
    )


@contextmanager
def runtime_path_audit(
    root: Path,
    *,
    store: Store | None = None,
) -> Iterator[ProjectFS | None]:
    """Pin and audit bounded runtime paths for a complete SQLite lifecycle."""

    active_store = store or get_store(root)
    if isinstance(active_store, InMemoryStore):
        yield None
        return

    expanded_root = Path(root).expanduser()
    if not expanded_root.exists() and not expanded_root.is_symlink():
        # Read-only status and doctor probes must not materialize a root.
        yield None
        return

    inventory = _runtime_audit_inventory()
    with ProjectFS.open(root) as project_fs:
        with pin_project_filesystem(project_fs):
            project_fs.audit(inventory, allow_missing=True)
            database_exists = project_fs._snapshot(
                DB_PATH,
                allow_missing=True,
            ).exists
            if not database_exists:
                # Preserve migration guidance without creating an operation-lock
                # file in an otherwise uninitialized project.
                raise_if_project_migration_announced(root)
                yield project_fs
                return

            # A migration callback re-enters the operation already held by the
            # same thread. Normal callers acquire the lock and re-check the
            # sentinel before this context permits SQLite to open.
            with project_db_operation(
                root,
                project_fs=project_fs,
            ) as locked_project_fs:
                locked_project_fs.audit(inventory, allow_missing=True)
                yield locked_project_fs


@contextmanager
def _operator_runtime_snapshot(root: Path) -> Iterator[tuple[bool, bool]]:
    """Keep initialization detection and report reads in one pinned lifecycle."""

    store = get_store(root)
    with runtime_path_audit(root, store=store) as project_fs:
        initialized = _runtime_initialized_in_audit(root, store, project_fs)
        database_exists = (
            not isinstance(store, SqliteStore)
            or (
                project_fs is not None
                and project_fs._snapshot(DB_PATH, allow_missing=True).exists
            )
        )
        yield initialized, database_exists


def _unavailable_existing_state_report(root: Path) -> OperatorEnvelope:
    return operator_error_report(
        root,
        HarnessError(
            "existing harness database is unreadable or has no canonical project state; "
            "inspect or recover it before initialization"
        ),
        allow_init=False,
    )


def audit_runtime_paths(root: Path) -> None:
    """Fail closed on the bounded canonical inventory before SQLite opens."""

    with runtime_path_audit(root):
        pass


def uninitialized_lines(root: Path) -> list[str]:
    return [
        f"ERROR: harness is not initialized in this project: {root}",
        f"NEXT: {_render_harness_command(root, 'init')}",
        f"NEXT: {_render_harness_command(root, 'quickstart', 'status')}",
    ]


def _operator_error_text(exc: BaseException) -> str:
    primary = str(exc) or type(exc).__name__
    notes = [str(note) for note in getattr(exc, "__notes__", ()) if str(note)]
    return "\n".join((primary, *(f"NOTE: {note}" for note in notes)))


def _operator_blocker_message(text: str) -> str:
    """Keep the concise blocker on one line without discarding full details."""

    return "; ".join(part.strip() for part in text.splitlines() if part.strip())


def _operator_error_code(text: str) -> str:
    lowered = text.lower()
    for code in (
        "rollback-incomplete",
        "recovery-required",
        "migration-in-progress",
    ):
        if code in lowered:
            return code
    if "not initialized" in lowered:
        return "not-initialized"
    if "path" in lowered and any(
        marker in lowered
        for marker in ("symlink", "escape", "unsafe", "changed during")
    ):
        return "path-safety"
    return "runtime-error"


OPERATOR_STATE_READ_ERRORS = (
    HarnessError,
    sqlite3.Error,
    IndexError,
    KeyError,
    TypeError,
    ValueError,
)


def operator_error_report(
    root: Path,
    exc: BaseException,
    *,
    allow_init: bool = False,
) -> OperatorEnvelope:
    """Project a pre-SQLite runtime failure without weakening fail-closed truth."""

    text = _operator_error_text(exc)
    code = _operator_error_code(text)
    recovery = code in {
        "rollback-incomplete",
        "recovery-required",
        "migration-in-progress",
    }
    uninitialized = code == "not-initialized"
    actions: list[str] = []
    if uninitialized and allow_init and not recovery:
        actions = [
            _render_harness_command(root, "init"),
            _render_harness_command(root, "quickstart", "status"),
        ]
    state = (
        "recovery-required"
        if recovery
        else "not-initialized"
        if uninitialized
        else "error"
    )
    return build_operator_envelope(
        state=state,
        blockers=(
            {
                "code": code,
                "message": _operator_blocker_message(text),
            },
        ),
        actions=actions,
        details={
            "initialized": False,
            "root": str(root),
            "error": text,
        },
    )


def uninitialized_operator_report(root: Path) -> OperatorEnvelope:
    """Return the shared envelope for an ordinary, safely uninitialized root."""

    message = f"harness is not initialized in this project: {root}"
    return build_operator_envelope(
        state="not-initialized",
        blockers=(
            {
                "code": "not-initialized",
                "message": message,
            },
        ),
        actions=(
            _render_harness_command(root, "init"),
            _render_harness_command(root, "quickstart", "status"),
        ),
        details={
            "initialized": False,
            "root": str(root),
            "error": message,
        },
    )


@contextmanager
def connection(root: Path) -> Iterator[sqlite3.Connection]:
    with get_store(root).connection() as conn:
        yield conn


def transaction_invariant_issues(conn: sqlite3.Connection, root: Path, touched: list[tuple[str, str]] | None = None) -> list[object]:
    exists = conn.execute("select 1 from sqlite_master where type='table' and name = 'project'").fetchone()
    if not exists:
        return []
    from core.invariant_checker import check_runtime_invariants

    return check_runtime_invariants(conn, root, scope=touched or [], full=False)


def full_invariant_issues(conn: sqlite3.Connection, root: Path) -> list[object]:
    exists = conn.execute("select 1 from sqlite_master where type='table' and name = 'project'").fetchone()
    if not exists:
        return []
    from core.invariant_checker import check_runtime_invariants

    return check_runtime_invariants(conn, root)


def require_full_invariants(conn: sqlite3.Connection, root: Path, label: str) -> None:
    issues = full_invariant_issues(conn, root)
    if issues:
        raise HarnessError(f"{label} invariant failed: " + "; ".join(str(issue) for issue in issues))


@contextmanager
def transaction(
    root: Path,
    *,
    validate_invariants: bool = True,
    touched: list[tuple[str, str]] | None = None,
    before_commit_check: Callable[[sqlite3.Connection], None] | None = None,
) -> Iterator[sqlite3.Connection]:
    def before_commit(conn: sqlite3.Connection) -> None:
        if before_commit_check is not None:
            before_commit_check(conn)
        if validate_invariants:
            issues = transaction_invariant_issues(conn, root, touched)
            if issues:
                raise HarnessError("; ".join(str(issue) for issue in issues))

    with get_store(root).transaction(before_commit=before_commit) as conn:
        yield conn






def ensure_runtime_gitignore(root: Path) -> None:
    relative = Path(".gitignore")
    with ProjectFS.open(root) as project_fs:
        snapshot = project_fs._snapshot(relative, allow_missing=True)
        existing = (
            project_fs.read_bytes(relative).decode("utf-8").splitlines()
            if snapshot.exists
            else []
        )
        normalized = {line.strip() for line in existing}
        missing = [
            pattern
            for pattern in RUNTIME_GITIGNORE_PATTERNS
            if pattern not in normalized
        ]
        if not missing:
            return
        lines = existing[:]
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# Codex Project Harness runtime state")
        lines.extend(missing)
        project_fs.atomic_write(
            relative,
            ("\n".join(lines).rstrip() + "\n").encode("utf-8"),
            mode=0o644,
        )


def git_tracked_runtime_paths(root: Path) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "ls-files", ".ai-team/state", ".ai-team/backups", ".ai-team/runtime"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _gitignore_runtime_issues(project_fs: ProjectFS) -> list[str]:
    issues: list[str] = []
    snapshot = project_fs._snapshot(
        Path(".gitignore"),
        allow_missing=True,
    )
    lines = (
        {
            line.strip()
            for line in project_fs.read_bytes(Path(".gitignore"))
            .decode("utf-8")
            .splitlines()
        }
        if snapshot.exists
        else set()
    )
    tracked = git_tracked_runtime_paths(project_fs.root)
    for pattern in RUNTIME_GITIGNORE_PATTERNS:
        if pattern not in lines:
            issues.append(f"missing .gitignore runtime pattern: {pattern}")
    if tracked:
        issues.append(
            "runtime state is tracked by git: "
            + ", ".join(tracked)
            + " (fix with: git rm --cached "
            + " ".join(tracked)
            + ")"
        )
    return issues


def gitignore_runtime_issues(root: Path) -> list[str]:
    with ProjectFS.open(root) as project_fs:
        return _gitignore_runtime_issues(project_fs)










def initialize_project(conn: sqlite3.Connection) -> None:
    existing = conn.execute("select id from project where id = 1").fetchone()
    if existing:
        ensure_delivery_cycles(conn)
        return
    now = now_iso()
    conn.execute(
        """
        insert into project
        (id, project_id, schema_version, runtime_version, phase, current_cycle_id,
         status, scope_status, current_owner, revision, updated_at)
        values (1, ?, ?, ?, 'intake', ?, 'draft', 'unconfirmed', 'project-manager', 1, ?)
        """,
        (str(uuid.uuid4()), SCHEMA_VERSION, RUNTIME_VERSION, DEFAULT_CYCLE_ID, now),
    )
    ensure_delivery_cycles(conn)


def stable_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_digest(value: object) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()




def row_snapshot(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def table_rows(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    return [row_snapshot(row) or {} for row in conn.execute(f"select * from {table} order by 1")]


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row["name"] for row in conn.execute(f"pragma table_info({table})")]














def emit_audit_event(
    conn: sqlite3.Connection,
    event_type: str,
    *,
    entity_type: str,
    entity_id: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    actor: str = "",
    command: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    from core.event_bus import compact_summary, emit_audit

    project = conn.execute(
        "select schema_version from project where id = 1"
    ).fetchone()
    event_schema_version = (
        int(project["schema_version"])
        if project is not None
        else SCHEMA_VERSION
    )

    event_columns = {
        str(row[1]) for row in conn.execute("pragma table_info(events)")
    }
    if "event_type" in event_columns:
        emit_audit(
            conn,
            event_schema_version,
            event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            before=before,
            after=after,
            actor=actor,
            command=command,
            extra=extra,
        )
        return
    legacy_columns = {
        "id",
        "schema_version",
        "type",
        "source",
        "target",
        "correlation_id",
        "payload_json",
        "created_at",
    }
    if not legacy_columns.issubset(event_columns):
        raise HarnessError(
            "unsupported legacy audit event schema: "
            f"columns={sorted(event_columns)}"
        )
    after_summary = dict(after or {})
    if extra:
        after_summary.update(extra)
    conn.execute(
        """
        insert into events
        (id, schema_version, type, source, target, correlation_id,
         payload_json, created_at)
        values (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            event_schema_version,
            event_type,
            entity_type or "runtime",
            f"{entity_type or 'entity'}:{entity_id}",
            str(uuid.uuid4()),
            stable_json(
                {
                    "actor": actor or "root-controller",
                    "command": command or event_type,
                    "before": compact_summary(before),
                    "after": compact_summary(after_summary),
                }
            ),
            now_iso(),
        ),
    )


def is_expired(value: str | None) -> bool:
    from core.lock_manager import is_expired as core_is_expired

    return core_is_expired(value)


def guard_schema(callable_name: str, *args: object) -> None:
    from core import schema_guard

    try:
        getattr(schema_guard, callable_name)(*args)
    except schema_guard.SchemaGuardError as exc:
        raise HarnessError(str(exc)) from exc


def bool_int(value: bool) -> int:
    return 1 if value else 0


def command_has_prefix(command: str, prefixes: list[str]) -> bool:
    from core.execution import command_matches_prefix

    return any(command_matches_prefix(command, prefix) for prefix in prefixes)


def target_gateability(kind: str, command_template: str) -> tuple[int, str]:
    if command_has_prefix(command_template, DUMB_COMMAND_PREFIXES):
        return 0, "not a gateable test target: command is a shell utility or placeholder"
    if kind in {"unit", "integration"} and not command_has_prefix(command_template, GATEABLE_TEST_PREFIXES):
        return 0, "not a gateable test target: unit/integration command must use a known test runner"
    return 1, ""


def bump_project(conn: sqlite3.Connection, **updates: str) -> None:
    project = conn.execute("select revision from project where id = 1").fetchone()
    revision = int(project["revision"]) + 1
    assignments = ["revision = ?", "updated_at = ?"]
    values: list[object] = [revision, now_iso()]
    for key, value in updates.items():
        assignments.append(f"{key} = ?")
        values.append(value)
    values.append(1)
    conn.execute(f"update project set {', '.join(assignments)} where id = ?", values)


def invalidate_downstream(conn: sqlite3.Connection, source_type: str, source_id: str, reason: str) -> None:
    cycle_id = current_cycle_id(conn)
    targets: list[tuple[str, str]] = []
    if source_type == "acceptance":
        targets.extend(
            ("task", row["task_id"])
            for row in conn.execute(
                "select ta.task_id from task_acceptance ta join tasks t on t.cycle_id = ta.cycle_id and t.id = ta.task_id where ta.cycle_id = ? and ta.acceptance_id = ?",
                (cycle_id, source_id),
            )
        )
        targets.extend(("validation", row["id"]) for row in conn.execute("select id from validations where acceptance_id = ? and cycle_id = ?", (source_id, cycle_id)))
        targets.extend(("quality_gate", row["id"]) for row in conn.execute("select id from quality_gates where cycle_id = ?", (cycle_id,)))
    elif source_type == "failure_mode":
        targets.extend(
            ("task", row["task_id"])
            for row in conn.execute(
                "select tfm.task_id from task_failure_modes tfm join tasks t on t.cycle_id = tfm.cycle_id and t.id = tfm.task_id where tfm.cycle_id = ? and tfm.failure_mode_id = ?",
                (cycle_id, source_id),
            )
        )
        targets.extend(
            ("validation", row["validation_id"])
            for row in conn.execute(
                """
                select vfm.validation_id from validation_failure_modes vfm
                join validations v on v.id = vfm.validation_id
                where vfm.cycle_id = ? and vfm.failure_mode_id = ? and v.cycle_id = vfm.cycle_id
                """,
                (cycle_id, source_id),
            )
        )
        targets.extend(("quality_gate", row["id"]) for row in conn.execute("select id from quality_gates where cycle_id = ?", (cycle_id,)))
    elif source_type == "requirement":
        acceptance_ids = [
            row["acceptance_id"]
            for row in conn.execute(
                """
                select ra.acceptance_id from requirement_acceptance ra
                join acceptance a on a.cycle_id = ra.cycle_id and a.id = ra.acceptance_id
                where ra.cycle_id = ? and ra.requirement_id = ?
                """,
                (cycle_id, source_id),
            )
        ]
        for acceptance_id in acceptance_ids:
            targets.append(("acceptance", acceptance_id))
            targets.extend(
                ("task", row["task_id"])
                for row in conn.execute(
                    "select ta.task_id from task_acceptance ta join tasks t on t.cycle_id = ta.cycle_id and t.id = ta.task_id where ta.cycle_id = ? and ta.acceptance_id = ?",
                    (cycle_id, acceptance_id),
                )
            )
            targets.extend(("validation", row["id"]) for row in conn.execute("select id from validations where acceptance_id = ? and cycle_id = ?", (acceptance_id, cycle_id)))
        targets.extend(("quality_gate", row["id"]) for row in conn.execute("select id from quality_gates where cycle_id = ?", (cycle_id,)))
    for target_type, target_id in targets:
        exists = conn.execute(
            """
            select 1 from invalidations
            where cycle_id = ? and source_type = ? and source_id = ? and target_type = ? and target_id = ? and resolved_at is null
            """,
            (cycle_id, source_type, source_id, target_type, target_id),
        ).fetchone()
        if exists:
            continue
        conn.execute(
            """
            insert into invalidations (id, cycle_id, source_type, source_id, target_type, target_id, reason, created_at)
            values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (str(uuid.uuid4()), cycle_id, source_type, source_id, target_type, target_id, reason, now_iso()),
        )


def resolve_invalidations(conn: sqlite3.Connection, *, source_type: str | None = None, source_id: str | None = None, target_type: str | None = None) -> None:
    clauses = ["resolved_at is null"]
    values: list[object] = []
    try:
        clauses.append("cycle_id = ?")
        values.append(current_cycle_id(conn))
    except HarnessError:
        pass
    if source_type:
        clauses.append("source_type = ?")
        values.append(source_type)
    if source_id:
        clauses.append("source_id = ?")
        values.append(source_id)
    if target_type:
        clauses.append("target_type = ?")
        values.append(target_type)
    values.append(now_iso())
    conn.execute(f"update invalidations set resolved_at = ? where {' and '.join(clauses)}", [values[-1], *values[:-1]])


@_project_mutation
def init_runtime(root: Path) -> None:
    ensure_runtime_gitignore(root)
    with transaction(root, validate_invariants=False) as conn:
        create_schema(conn)
        initialize_project(conn)
        project = project_row(conn)
        emit_audit_event(
            conn,
            "runtime_initialized",
            entity_type="project",
            entity_id=str(project["project_id"]),
            before=None,
            after=row_snapshot(project),
            actor="root-controller",
            command="init",
        )
        require_full_invariants(conn, root, "init")
    render_all(root)
    install_project_agent_templates(root)


def validate_codex_agent_template(path: Path) -> dict[str, Any]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise HarnessError(f"invalid agent template {path.name}: {exc}") from exc
    missing = sorted(
        field for field in CODEX_AGENT_TEMPLATE_FIELDS if not str(data.get(field, "")).strip()
    )
    if missing:
        raise HarnessError(f"invalid agent template {path.name}: missing {', '.join(missing)}")
    extra = sorted(set(data) - CODEX_AGENT_TEMPLATE_FIELDS)
    if extra:
        raise HarnessError(f"invalid agent template {path.name}: unsupported fields {', '.join(extra)}")
    if data["name"] != path.stem:
        raise HarnessError(f"invalid agent template {path.name}: name must be {path.stem}")
    return data


def install_project_agent_templates(root: Path) -> int:
    """Install the three static Native Codex templates without owning agent lifecycle."""

    template_dir = PLUGIN_ROOT / "templates" / "agents"
    expected = frozenset(codex_agent_template_names())
    actual = {path.name for path in template_dir.glob("*.toml") if path.is_file()}
    if actual != expected:
        raise HarnessError(
            "agent template inventory mismatch: "
            f"actual={sorted(actual)} expected={sorted(expected)}"
        )
    installed = 0
    with ProjectFS.open(root) as project_fs:
        destinations = tuple(
            Path(".codex/agents") / name
            for name in sorted(expected)
        )
        project_fs.audit(destinations, allow_missing=True)
        for name, destination in zip(
            sorted(expected),
            destinations,
            strict=True,
        ):
            source = template_dir / name
            validate_codex_agent_template(source)
            if project_fs._snapshot(destination, allow_missing=True).exists:
                continue
            project_fs.copy_from_external(
                source,
                destination,
                mode=0o644,
            )
            installed += 1
    return installed


def cycle_status(root: Path, cycle_id: str = "") -> dict[str, Any]:
    with connection(root) as conn:
        cycle = (
            conn.execute(
                "select * from delivery_cycles where id = ?",
                (cycle_id,),
            ).fetchone()
            if cycle_id
            else current_cycle_row(conn)
        )
        if cycle is None:
            raise HarnessError(f"missing delivery cycle: {cycle_id}")
        return row_snapshot(cycle) or {}


def cycle_audit(root: Path, cycle_id: str) -> dict[str, Any]:
    """Return a read-only, cycle-selected consistency and fact snapshot."""

    normalized_id = cycle_id.strip()
    if not normalized_id:
        raise HarnessError("cycle audit requires a non-empty cycle id")
    from core.delivery import (
        evaluate_historical_cycle_prerequisites,
        historical_cycle_event_facts,
    )

    with connection(root) as conn:
        cycle = conn.execute(
            "select * from delivery_cycles where id = ?",
            (normalized_id,),
        ).fetchone()
        if cycle is None:
            raise HarnessError(f"missing delivery cycle: {normalized_id}")
        direct_tables = (
            "requirements",
            "acceptance",
            "failure_modes",
            "baselines",
            "tasks",
            "acceptance_target_qualifications",
            "executions",
            "validations",
            "findings",
            "quality_gates",
            "deliveries",
            "invalidations",
            "outcome_observations",
            "decisions",
        )
        relation_tables = (
            "requirement_acceptance",
            "failure_mode_acceptance",
            "task_acceptance",
            "task_failure_modes",
            "task_dependencies",
            "task_test_targets",
            "validation_executions",
            "validation_failure_modes",
            "quality_gate_qualifications",
            "delivery_acceptance",
        )
        facts: dict[str, list[dict[str, Any]]] = {}
        for table in (*direct_tables, *relation_tables):
            facts[table] = [
                row_snapshot(row) or {}
                for row in conn.execute(
                    f"select * from {table} where cycle_id = ? order by rowid",
                    (normalized_id,),
                ).fetchall()
            ]
        facts["quality_gate_findings"] = [
            row_snapshot(row) or {}
            for row in conn.execute(
                """
                select link.* from quality_gate_findings link
                join quality_gates g on g.id = link.gate_id
                where g.cycle_id = ?
                order by link.gate_id, link.finding_id
                """,
                (normalized_id,),
            ).fetchall()
        ]
        target_ids = {
            str(row["target_id"])
            for table in (
                "task_test_targets",
                "acceptance_target_qualifications",
                "executions",
            )
            for row in facts[table]
            if str(row.get("target_id") or "")
        }
        facts["test_targets"] = [
            row_snapshot(row) or {}
            for target_id in sorted(target_ids)
            for row in conn.execute(
                "select * from test_targets where id = ?",
                (target_id,),
            ).fetchall()
        ]
        facts["events"] = list(
            historical_cycle_event_facts(conn, normalized_id)
        )
        blockers = evaluate_historical_cycle_prerequisites(
            conn,
            root,
            normalized_id,
        )
        cycle_snapshot = row_snapshot(cycle) or {}
        fact_snapshot = {"cycle": cycle_snapshot, "facts": facts}
        return {
            "cycle": cycle_snapshot,
            "consistent": not blockers,
            "blockers": [blocker.as_dict() for blocker in blockers],
            "counts": {table: len(rows) for table, rows in facts.items()},
            "facts_sha256": stable_digest(fact_snapshot),
        }


@_project_mutation
def cycle_start(root: Path, cycle_id: str, name: str, goal: str, *, base_ref: str = "") -> None:
    if not cycle_id or not name or not goal:
        raise HarnessError("cycle start requires id, name, and goal")
    with transaction(root, touched=[("project", "1"), ("delivery_cycle", cycle_id)]) as conn:
        current = current_cycle_row(conn)
        if current["status"] not in {"delivered", "archived"}:
            raise HarnessError(f"current cycle is not closed: {current['id']} status={current['status']}")
        if conn.execute("select 1 from delivery_cycles where id = ?", (cycle_id,)).fetchone():
            raise HarnessError(f"duplicate cycle id: {cycle_id}")
        now = now_iso()
        conn.execute(
            """
            insert into delivery_cycles
            (id, name, goal, status, phase, base_ref, candidate_sha, started_at, closed_at, created_at, updated_at)
            values (?, ?, ?, 'active', 'intake', ?, '', ?, '', ?, ?)
            """,
            (cycle_id, name, goal, base_ref, now, now, now),
        )
        bump_project(
            conn,
            current_cycle_id=cycle_id,
            phase="intake",
            status="draft",
            scope_status="unconfirmed",
            current_owner="project-manager",
        )
        created = conn.execute("select * from delivery_cycles where id = ?", (cycle_id,)).fetchone()
        emit_audit_event(
            conn,
            "delivery_cycle_started",
            entity_type="delivery_cycle",
            entity_id=cycle_id,
            before=None,
            after=row_snapshot(created),
            command="cycle start",
        )
    render_affected(
        root,
        "project-state",
        "requirements",
        "traceability",
        "acceptance",
        "failure-modes",
        "tasks",
        "validation",
        "gates",
        "deliveries",
    )


@_project_mutation
def cycle_close(root: Path, status: str) -> None:
    if status == "delivered":
        raise HarnessError(
            "cycle close cannot mark a cycle delivered; use delivery record so "
            "the canonical delivery prerequisites and delivery row are applied"
        )
    if status != "archived":
        raise HarnessError("cycle close status must be archived")
    with transaction(root, touched=[("project", "1")]) as conn:
        cycle = current_cycle_row(conn)
        if cycle["status"] != "active":
            raise HarnessError(f"current cycle is already closed: {cycle['id']} status={cycle['status']}")
        now = now_iso()
        candidate_sha = cycle["candidate_sha"] or current_candidate_sha(root)
        conn.execute(
            """
            update delivery_cycles
            set status = ?, phase = case when ? = 'archived' then 'archived' else phase end,
                candidate_sha = ?, closed_at = ?, updated_at = ?
            where id = ?
            """,
            (status, status, candidate_sha, now, now, cycle["id"]),
        )
        if status == "archived":
            bump_project(conn, phase="archived", status="archived")
        closed = conn.execute("select * from delivery_cycles where id = ?", (cycle["id"],)).fetchone()
        emit_audit_event(
            conn,
            "delivery_cycle_closed",
            entity_type="delivery_cycle",
            entity_id=str(cycle["id"]),
            before=row_snapshot(cycle),
            after=row_snapshot(closed),
            command="cycle close",
        )


@_project_mutation
def record_outcome_observation(
    root: Path,
    observation_id: str,
    kind: str,
    value: int,
    details: str,
    recorded_by: str,
    observed_at: str,
    *,
    cycle_id: str = "",
) -> dict[str, Any]:
    guard_schema(
        "validate_outcome_observation",
        observation_id,
        kind,
        value,
        details,
        recorded_by,
        observed_at,
    )
    normalized_id = observation_id.strip()
    normalized_details = details.strip()
    normalized_actor = recorded_by.strip()
    try:
        normalized_observed_at = normalize_outcome_timestamp(
            observed_at,
            label="outcome observation observed_at",
        )
        created_at = normalize_outcome_timestamp(
            now_iso(),
            label="outcome observation created_at",
        )
    except ValueError as exc:
        raise HarnessError(str(exc)) from exc
    with transaction(
        root,
        touched=[("outcome_observation", normalized_id)],
    ) as conn:
        selected_cycle = cycle_id.strip() or current_cycle_id(conn)
        if not conn.execute(
            "select 1 from delivery_cycles where id = ?",
            (selected_cycle,),
        ).fetchone():
            raise HarnessError(f"missing delivery cycle: {selected_cycle}")
        if conn.execute(
            "select 1 from outcome_observations where id = ?",
            (normalized_id,),
        ).fetchone():
            raise HarnessError(f"duplicate outcome observation id: {normalized_id}")
        conn.execute(
            """
            insert into outcome_observations
            (id, cycle_id, kind, value, details, recorded_by, observed_at, created_at)
            values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized_id,
                selected_cycle,
                kind,
                value,
                normalized_details,
                normalized_actor,
                normalized_observed_at,
                created_at,
            ),
        )
        row = conn.execute(
            "select * from outcome_observations where id = ?",
            (normalized_id,),
        ).fetchone()
        emit_audit_event(
            conn,
            "outcome_observation_recorded",
            entity_type="outcome_observation",
            entity_id=normalized_id,
            before=None,
            after=row_snapshot(row),
            actor=normalized_actor,
            command="cycle outcome-record",
            extra={"kind": kind, "value": value},
        )
    return row_snapshot(row) or {}


def outcome_report(root: Path) -> dict[str, Any]:
    with connection(root) as conn:
        cycle = current_cycle_row(conn)
        generated_at = normalize_outcome_timestamp(
            now_iso(),
            label="outcome report generated_at",
        )
        rows = [
            row_snapshot(row) or {}
            for row in conn.execute(
                """
                select * from outcome_observations
                where cycle_id = ?
                order by observed_at, created_at, id
                """,
                (cycle["id"],),
            )
        ]
        cycle_snapshot = row_snapshot(cycle) or {}
        metrics = build_outcome_metrics(
            conn,
            cycle=cycle_snapshot,
            observations=rows,
            generated_at=generated_at,
        )
    return {
        "report_version": "kafa-outcome-v1",
        "metrics_version": OUTCOME_METRICS_VERSION,
        "evidence_scope": "local-only",
        "evidence_mode": OUTCOME_EVIDENCE_MODE,
        "generated_at": generated_at,
        "cycle_id": str(cycle["id"]),
        "observation_count": len(rows),
        "observations": rows,
        "metrics": metrics,
    }


@_project_mutation
def transition_phase(root: Path, phase: str, *, status: str | None = None, owner: str | None = None) -> None:
    if phase == "delivery_readiness":
        enter_delivery_readiness(root)
        return
    with transaction(root, touched=[("project", "1")]) as conn:
        row = project_row(conn)
        cycle = current_cycle_row(conn)
        if cycle["status"] != "active" and phase != row["phase"]:
            raise HarnessError(f"current cycle is not active: {cycle['id']} status={cycle['status']}")
        current = row["phase"]
        if phase not in PHASES:
            raise HarnessError(f"unknown phase: {phase}")
        if phase != current and phase not in PHASE_TRANSITIONS[current]:
            allowed = ", ".join(sorted(PHASE_TRANSITIONS[current])) or "none"
            order = " -> ".join(PHASES)
            raise HarnessError(f"illegal phase transition: {current} -> {phase}; allowed next: {allowed}; phase order: {order}")
        issues = phase_prerequisite_issues(conn, phase)
        if issues:
            raise HarnessError(f"phase prerequisites blocked: {'; '.join(issues)}")
        assignments = ["phase = ?", "updated_at = ?"]
        values: list[object] = [phase, now_iso()]
        if status:
            assignments.append("status = ?")
            values.append(status)
        if owner:
            assignments.append("current_owner = ?")
            values.append(owner)
        values.append(1)
        conn.execute(
            f"update project set {', '.join(assignments)} where id = ?",
            values,
        )
        conn.execute("update delivery_cycles set phase = ?, updated_at = ? where id = ?", (phase, now_iso(), cycle["id"]))
        after = project_row(conn)
        emit_audit_event(
            conn,
            "phase_updated",
            entity_type="project",
            entity_id=str(row["project_id"]),
            before=row_snapshot(row),
            after=row_snapshot(after),
            actor=owner or "",
            command="phase",
            extra={"from": current, "to": phase},
        )
    render_affected(root, "project-state")


@_project_mutation
def enter_delivery_readiness(root: Path) -> None:
    """Atomically enter readiness through the canonical prerequisite evaluator."""

    from core.delivery import evaluate_delivery_prerequisites

    with transaction(root, touched=[("project", "1")]) as conn:
        project = project_row(conn)
        cycle = current_cycle_row(conn)
        blockers = evaluate_delivery_prerequisites(
            conn,
            root,
            mode="enter-readiness",
            is_expired=is_expired,
        )
        if blockers:
            raise HarnessError(
                "delivery readiness blocked: "
                + "; ".join(blocker.render() for blocker in blockers)
            )
        if str(cycle["status"]) != "active":
            raise HarnessError(
                f"delivery readiness requires active cycle: {cycle['id']} status={cycle['status']}"
            )
        now = now_iso()
        conn.execute(
            """
            update project
            set phase = 'delivery_readiness', status = 'ready-for-delivery',
                updated_at = ?
            where id = 1
            """,
            (now,),
        )
        conn.execute(
            """
            update delivery_cycles
            set phase = 'delivery_readiness', updated_at = ?
            where id = ?
            """,
            (now, cycle["id"]),
        )
        after = project_row(conn)
        emit_audit_event(
            conn,
            "delivery_readiness_entered",
            entity_type="project",
            entity_id=str(project["project_id"]),
            before=row_snapshot(project),
            after=row_snapshot(after),
            actor="root-controller",
            command="delivery ready",
            extra={"from": project["phase"], "to": "delivery_readiness"},
        )
    render_affected(root, "project-state")


def phase_prerequisite_issues(conn: sqlite3.Connection, phase: str) -> list[str]:
    issues: list[str] = []
    project = project_row(conn)
    cycle_id = project["current_cycle_id"]
    requirement_count = conn.execute("select count(*) from requirements where cycle_id = ? and status != 'cancelled'", (cycle_id,)).fetchone()[0]
    acceptance_count = conn.execute("select count(*) from acceptance where cycle_id = ?", (cycle_id,)).fetchone()[0]
    task_count = conn.execute("select count(*) from tasks where cycle_id = ?", (cycle_id,)).fetchone()[0]
    if phase in {"confirmation", "team_architecture", "planning"} and requirement_count == 0:
        issues.append(f"{phase} requires at least one requirement baseline record")
    if phase in {"confirmation", "team_architecture", "planning"} and acceptance_count == 0:
        issues.append(f"{phase} requires at least one acceptance criterion")
    if phase in {"implementation", "qa"} and task_count == 0:
        issues.append(f"{phase} requires at least one task")
    if phase in {"planning", "implementation", "qa", "delivery_readiness"}:
        if project["scope_status"] != "confirmed":
            issues.append(f"{phase} requires confirmed scope")
        if baseline_issues(conn):
            issues.extend(f"{phase} requires current frozen baseline: {issue}" for issue in baseline_issues(conn))
    if phase == "qa":
        active = conn.execute(
            "select id, status from tasks where status in ('planned', 'active', 'blocked') order by id"
        ).fetchall()
        for task in active:
            issues.append(f"qa requires implementation task submitted or accepted: {task['id']} status={task['status']}")
    return issues


@_project_mutation
def confirm_scope(root: Path, by: str, summary: str) -> None:
    normalized_actor = by.strip()
    normalized_summary = summary.strip()
    if not normalized_actor or not normalized_summary:
        raise HarnessError("scope confirmation requires non-empty actor and summary")
    with transaction(root, touched=[("project", "1")]) as conn:
        cycle_id = current_cycle_id(conn)
        baseline = latest_baseline(conn)
        if baseline is None:
            raise HarnessError("scope confirmation requires a current frozen baseline")
        if str(baseline["digest"]) != stable_digest(baseline_snapshot(conn)):
            raise HarnessError(f"scope confirmation requires a current baseline: {baseline['id']}")
        before = project_row(conn)
        bump_project(
            conn,
            scope_status="confirmed",
            current_owner=normalized_actor,
            status="scope-confirmed",
        )
        after = project_row(conn)
        emit_audit_event(
            conn,
            "baseline_confirmed",
            entity_type="baseline",
            entity_id=str(baseline["id"]),
            before=None,
            after={
                "id": baseline["id"],
                "cycle_id": cycle_id,
                "digest": baseline["digest"],
                "summary": normalized_summary,
                "project_revision": after["revision"],
            },
            actor=normalized_actor,
            command="baseline confirm",
        )
        emit_audit_event(
            conn,
            "scope_confirmed",
            entity_type="project",
            entity_id=str(before["project_id"]),
            before=row_snapshot(before),
            after=row_snapshot(after),
            actor=normalized_actor,
            command="scope confirm",
            extra={"summary": normalized_summary},
        )
    render_affected(root, "project-state")


def _write_baseline(
    conn: sqlite3.Connection,
    baseline_id: str,
    summary: str,
    *,
    by: str,
) -> sqlite3.Row:
    snapshot = baseline_snapshot(conn)
    digest = stable_digest(snapshot)
    cycle_id = current_cycle_id(conn)
    existing = conn.execute(
        "select id, cycle_id from baselines where id = ?",
        (baseline_id,),
    ).fetchone()
    if existing is not None and str(existing["cycle_id"]) != cycle_id:
        raise HarnessError(
            f"baseline ID {baseline_id} belongs to closed/history cycle "
            f"{existing['cycle_id']}; use a new baseline ID for cycle {cycle_id}"
        )
    if existing is not None:
        # Baseline IDs may be intentionally rewritten within one active cycle.
        # Reinsert so SQLite rowid remains the deterministic write-order tie
        # breaker when timestamps share one-second precision.
        conn.execute("delete from baselines where id = ?", (baseline_id,))
    conn.execute(
        """
        insert into baselines
        (id, cycle_id, summary, snapshot_json, digest, project_revision,
         created_by, created_at)
        values (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            baseline_id,
            cycle_id,
            summary,
            stable_json(snapshot),
            digest,
            int(project_row(conn)["revision"]),
            by,
            now_iso(),
        ),
    )
    row = conn.execute(
        "select * from baselines where id = ?",
        (baseline_id,),
    ).fetchone()
    if row is None:  # pragma: no cover - SQLite insert contract
        raise HarnessError(f"baseline was not persisted: {baseline_id}")
    return row


@_project_mutation
def freeze_baseline(root: Path, baseline_id: str, summary: str, *, by: str = "") -> None:
    normalized_id = baseline_id.strip()
    normalized_summary = summary.strip()
    normalized_actor = by.strip()
    if not normalized_id or not normalized_summary:
        raise HarnessError("baseline freeze requires non-empty id and summary")
    with transaction(
        root,
        touched=[("baseline", normalized_id), ("project", "1")],
    ) as conn:
        before = project_row(conn)
        bump_project(
            conn,
            scope_status="unconfirmed",
            status="baseline-frozen",
            **({"current_owner": normalized_actor} if normalized_actor else {}),
        )
        baseline = _write_baseline(
            conn,
            normalized_id,
            normalized_summary,
            by=normalized_actor,
        )
        emit_audit_event(
            conn,
            "baseline_frozen",
            entity_type="baseline",
            entity_id=normalized_id,
            before=None,
            after={
                "id": normalized_id,
                "cycle_id": baseline["cycle_id"],
                "summary": normalized_summary,
                "digest": baseline["digest"],
            },
            actor=normalized_actor,
            command="baseline freeze",
        )
        emit_audit_event(
            conn,
            "scope_unconfirmed",
            entity_type="project",
            entity_id=str(before["project_id"]),
            before=row_snapshot(before),
            after=row_snapshot(project_row(conn)),
            actor=normalized_actor,
            command="baseline freeze",
            extra={"reason": "new baseline requires explicit confirmation"},
        )
    render_affected(root, "project-state")


@_project_mutation
def confirm_baseline(
    root: Path,
    baseline_id: str,
    summary: str,
    *,
    by: str,
) -> None:
    normalized_id = baseline_id.strip()
    normalized_summary = summary.strip()
    normalized_actor = by.strip()
    if not normalized_id or not normalized_summary or not normalized_actor:
        raise HarnessError(
            "baseline confirm requires non-empty id, summary, and actor"
        )
    with transaction(
        root,
        touched=[("baseline", normalized_id), ("project", "1")],
    ) as conn:
        accepted_risks = conn.execute(
            """
            select id, accepted_by, acceptance_reason, acceptance_scope,
                   accepted_revision, expires_at
            from failure_modes
            where cycle_id = ? and status in ('accepted', 'exempt')
            order by id
            """,
            (current_cycle_id(conn),),
        ).fetchall()
        for risk in accepted_risks:
            missing = [
                field
                for field in (
                    "accepted_by",
                    "acceptance_reason",
                    "acceptance_scope",
                    "expires_at",
                )
                if not str(risk[field] or "").strip()
            ]
            if missing:
                raise HarnessError(
                    "baseline confirm cannot bind incomplete accepted/exempt "
                    f"failure mode {risk['id']}: missing={','.join(missing)}"
                )
            if parse_time(str(risk["expires_at"])) is None:
                raise HarnessError(
                    "baseline confirm cannot bind accepted/exempt failure mode "
                    f"with invalid expiry: {risk['id']}"
                )
        before = project_row(conn)
        bump_project(
            conn,
            scope_status="confirmed",
            current_owner=normalized_actor,
            status="scope-confirmed",
        )
        after = project_row(conn)
        if accepted_risks:
            conn.execute(
                """
                update failure_modes
                set accepted_revision = ?
                where cycle_id = ? and status in ('accepted', 'exempt')
                """,
                (int(after["revision"]), current_cycle_id(conn)),
            )
            for risk in accepted_risks:
                rebound = conn.execute(
                    "select * from failure_modes where cycle_id = ? and id = ?",
                    (current_cycle_id(conn), risk["id"]),
                ).fetchone()
                emit_audit_event(
                    conn,
                    "risk_acceptance_rebound",
                    entity_type="failure_mode",
                    entity_id=str(risk["id"]),
                    before={"accepted_revision": risk["accepted_revision"]},
                    after={
                        "accepted_revision": rebound["accepted_revision"],
                        "baseline_id": normalized_id,
                    },
                    actor=normalized_actor,
                    command="baseline confirm",
                )
        baseline = _write_baseline(
            conn,
            normalized_id,
            normalized_summary,
            by=normalized_actor,
        )
        emit_audit_event(
            conn,
            "baseline_confirmed",
            entity_type="baseline",
            entity_id=normalized_id,
            before=None,
            after={
                "id": normalized_id,
                "cycle_id": baseline["cycle_id"],
                "summary": normalized_summary,
                "digest": baseline["digest"],
                "project_revision": after["revision"],
            },
            actor=normalized_actor,
            command="baseline confirm",
        )
        emit_audit_event(
            conn,
            "scope_confirmed",
            entity_type="project",
            entity_id=str(before["project_id"]),
            before=row_snapshot(before),
            after=row_snapshot(after),
            actor=normalized_actor,
            command="baseline confirm",
            extra={"summary": normalized_summary},
        )
    render_affected(root, "project-state", "failure-modes")


def baseline_validate(root: Path) -> list[str]:
    with connection(root) as conn:
        return baseline_issues(conn)


def baseline_diff(root: Path, from_id: str, to: str = "current") -> list[str]:
    with connection(root) as conn:
        baseline = conn.execute("select * from baselines where id = ?", (from_id,)).fetchone()
        if not baseline:
            raise HarnessError(f"missing baseline: {from_id}")
        before = json.loads(baseline["snapshot_json"])
        after = baseline_snapshot(conn) if to == "current" else json.loads(conn.execute("select snapshot_json from baselines where id = ?", (to,)).fetchone()["snapshot_json"])
    lines = [f"# Baseline Diff {from_id} -> {to}", ""]
    for table in ["requirements", "acceptance", "requirement_acceptance", "failure_modes", "failure_mode_acceptance"]:
        before_rows = stable_digest(before.get(table, []))
        after_rows = stable_digest(after.get(table, []))
        status = "same" if before_rows == after_rows else "changed"
        lines.append(f"- {table}: {status}")
    return lines


def _record_requirement_conn(
    conn: sqlite3.Connection,
    requirement_id: str,
    kind: str,
    body: str,
    priority: str = "",
    status: str = "active",
    *,
    create_only: bool = False,
) -> sqlite3.Row:
    guard_schema("validate_requirement", requirement_id, kind, body, status)
    cycle_id = current_cycle_id(conn)
    existing = conn.execute(
        "select * from requirements where cycle_id = ? and id = ?",
        (cycle_id, requirement_id),
    ).fetchone()
    if create_only and existing is not None:
        raise HarnessError(f"duplicate requirement id: {requirement_id}")
    if create_only:
        conn.execute(
            """
            insert into requirements
            (id, cycle_id, kind, body, priority, status, updated_at)
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                requirement_id,
                cycle_id,
                kind,
                body,
                priority,
                status,
                now_iso(),
            ),
        )
    else:
        conn.execute(
            """
            insert into requirements (id, cycle_id, kind, body, priority, status, updated_at)
            values (?, ?, ?, ?, ?, ?, ?)
            on conflict(cycle_id, id) do update set kind=excluded.kind, body=excluded.body, priority=excluded.priority,
              status=excluded.status, revision=requirements.revision+1, updated_at=excluded.updated_at
            """,
            (requirement_id, cycle_id, kind, body, priority, status, now_iso()),
        )
    if existing and (
        existing["kind"],
        existing["body"],
        existing["priority"],
        existing["status"],
    ) != (kind, body, priority, status):
        invalidate_downstream(conn, "requirement", requirement_id, "requirement changed")
    after = conn.execute(
        "select * from requirements where cycle_id = ? and id = ?",
        (cycle_id, requirement_id),
    ).fetchone()
    emit_audit_event(
        conn,
        "requirement_recorded",
        entity_type="requirement",
        entity_id=requirement_id,
        before=row_snapshot(existing),
        after=row_snapshot(after),
        command="requirement add",
        extra={"kind": kind},
    )
    if after is None:  # pragma: no cover - SQLite insert contract
        raise HarnessError(f"requirement was not persisted: {requirement_id}")
    return after


@_project_mutation
def add_requirement(root: Path, requirement_id: str, kind: str, body: str, priority: str = "", status: str = "active") -> None:
    with transaction(root, touched=[("requirement", requirement_id)]) as conn:
        _record_requirement_conn(
            conn,
            requirement_id,
            kind,
            body,
            priority,
            status,
        )
    render_affected(root, "project-state", "requirements", "traceability")


def _record_acceptance_conn(
    conn: sqlite3.Connection,
    acceptance_id: str,
    criterion: str,
    priority: str = "",
    *,
    create_only: bool = False,
) -> sqlite3.Row:
    guard_schema("validate_acceptance", acceptance_id, criterion)
    cycle_id = current_cycle_id(conn)
    existing = conn.execute(
        "select * from acceptance where cycle_id = ? and id = ?",
        (cycle_id, acceptance_id),
    ).fetchone()
    if create_only and existing is not None:
        raise HarnessError(f"duplicate acceptance id: {acceptance_id}")
    if create_only:
        conn.execute(
            """
            insert into acceptance (id, cycle_id, criterion, priority)
            values (?, ?, ?, ?)
            """,
            (acceptance_id, cycle_id, criterion, priority),
        )
    else:
        conn.execute(
            """
            insert into acceptance (id, cycle_id, criterion, priority)
            values (?, ?, ?, ?)
            on conflict(cycle_id, id) do update set criterion=excluded.criterion,
                priority=excluded.priority, revision=acceptance.revision+1
            """,
            (acceptance_id, cycle_id, criterion, priority),
        )
    if existing and (existing["criterion"], existing["priority"]) != (
        criterion,
        priority,
    ):
        invalidate_downstream(conn, "acceptance", acceptance_id, "acceptance criterion changed")
    after = conn.execute(
        "select * from acceptance where cycle_id = ? and id = ?",
        (cycle_id, acceptance_id),
    ).fetchone()
    emit_audit_event(
        conn,
        "acceptance_recorded",
        entity_type="acceptance",
        entity_id=acceptance_id,
        before=row_snapshot(existing),
        after=row_snapshot(after),
        command="acceptance add",
    )
    if after is None:  # pragma: no cover - SQLite insert contract
        raise HarnessError(f"acceptance was not persisted: {acceptance_id}")
    return after


@_project_mutation
def add_acceptance(root: Path, acceptance_id: str, criterion: str, priority: str = "") -> None:
    with transaction(root, touched=[("acceptance", acceptance_id)]) as conn:
        _record_acceptance_conn(conn, acceptance_id, criterion, priority)
    render_affected(root, "acceptance", "traceability")


def _record_failure_mode_conn(
    conn: sqlite3.Connection,
    fm_id: str,
    feature: str,
    scenario: str,
    trigger: str,
    expected: str,
    *,
    risk: str = "medium",
    status: str = "identified",
    acceptance: str = "",
    recovery: str = "",
    data_safety: str = "",
    accepted_by: str = "",
    acceptance_reason: str = "",
    acceptance_scope: str = "",
    expires_at: str = "",
    create_only: bool = False,
) -> sqlite3.Row:
    guard_schema("validate_failure_mode", fm_id, risk, status)
    cycle_id = current_cycle_id(conn)
    existing = conn.execute(
        "select * from failure_modes where cycle_id = ? and id = ?",
        (cycle_id, fm_id),
    ).fetchone()
    if create_only and existing is not None:
        raise HarnessError(f"duplicate failure mode id: {fm_id}")
    accepted_revision = None
    if status not in FAILURE_MODE_STATUSES:
        raise HarnessError("failure mode status must be identified, accepted, or exempt; coverage is derived from passing validation")
    if status in {"accepted", "exempt"}:
        if not accepted_by or not acceptance_reason or not acceptance_scope or not expires_at:
            raise HarnessError("accepted or exempt failure modes require accepted-by, acceptance-reason, acceptance-scope, and expires-at")
        accepted_revision = int(project_row(conn)["revision"])
    if create_only:
        conn.execute(
            """
            insert into failure_modes
            (id, cycle_id, feature, scenario, trigger, expected_behavior, recovery, data_safety, risk, status,
             accepted_by, acceptance_reason, acceptance_scope, accepted_revision, expires_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fm_id,
                cycle_id,
                feature,
                scenario,
                trigger,
                expected,
                recovery,
                data_safety,
                risk,
                status,
                accepted_by,
                acceptance_reason,
                acceptance_scope,
                accepted_revision,
                expires_at,
            ),
        )
    else:
        conn.execute(
            """
            insert into failure_modes
            (id, cycle_id, feature, scenario, trigger, expected_behavior, recovery, data_safety, risk, status,
             accepted_by, acceptance_reason, acceptance_scope, accepted_revision, expires_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(cycle_id, id) do update set feature=excluded.feature, scenario=excluded.scenario, trigger=excluded.trigger,
              expected_behavior=excluded.expected_behavior, recovery=excluded.recovery, data_safety=excluded.data_safety,
              risk=excluded.risk, status=excluded.status, accepted_by=excluded.accepted_by,
              acceptance_reason=excluded.acceptance_reason, acceptance_scope=excluded.acceptance_scope,
              accepted_revision=excluded.accepted_revision, expires_at=excluded.expires_at, revision=failure_modes.revision+1
            """,
            (
                fm_id,
                cycle_id,
                feature,
                scenario,
                trigger,
                expected,
                recovery,
                data_safety,
                risk,
                status,
                accepted_by,
                acceptance_reason,
                acceptance_scope,
                accepted_revision,
                expires_at,
            ),
        )
    if acceptance:
        require_acceptance(conn, acceptance)
        conn.execute(
            "insert or ignore into failure_mode_acceptance (cycle_id, failure_mode_id, acceptance_id) values (?, ?, ?)",
            (cycle_id, fm_id, acceptance),
        )
    if existing and (
        existing["feature"],
        existing["scenario"],
        existing["trigger"],
        existing["expected_behavior"],
        existing["risk"],
        existing["status"],
    ) != (feature, scenario, trigger, expected, risk, status):
        invalidate_downstream(conn, "failure_mode", fm_id, "failure mode changed")
    after = conn.execute(
        "select * from failure_modes where cycle_id = ? and id = ?",
        (cycle_id, fm_id),
    ).fetchone()
    emit_audit_event(
        conn,
        "failure_mode_recorded",
        entity_type="failure_mode",
        entity_id=fm_id,
        before=row_snapshot(existing),
        after=row_snapshot(after),
        command="failure-mode add",
        extra={"risk": risk},
    )
    if after is None:  # pragma: no cover - SQLite insert contract
        raise HarnessError(f"failure mode was not persisted: {fm_id}")
    return after


@_project_mutation
def add_failure_mode(
    root: Path,
    fm_id: str,
    feature: str,
    scenario: str,
    trigger: str,
    expected: str,
    *,
    risk: str = "medium",
    status: str = "identified",
    acceptance: str = "",
    recovery: str = "",
    data_safety: str = "",
    accepted_by: str = "",
    acceptance_reason: str = "",
    acceptance_scope: str = "",
    expires_at: str = "",
) -> None:
    with transaction(root, touched=[("failure_mode", fm_id)]) as conn:
        _record_failure_mode_conn(
            conn,
            fm_id,
            feature,
            scenario,
            trigger,
            expected,
            risk=risk,
            status=status,
            acceptance=acceptance,
            recovery=recovery,
            data_safety=data_safety,
            accepted_by=accepted_by,
            acceptance_reason=acceptance_reason,
            acceptance_scope=acceptance_scope,
            expires_at=expires_at,
        )
    render_affected(root, "failure-modes")


def require_acceptance(conn: sqlite3.Connection, acceptance_id: str) -> None:
    cycle_id = current_cycle_id(conn)
    if not conn.execute("select id from acceptance where cycle_id = ? and id = ?", (cycle_id, acceptance_id)).fetchone():
        raise HarnessError(f"missing acceptance: {acceptance_id}")


def require_requirement(conn: sqlite3.Connection, requirement_id: str) -> None:
    cycle_id = current_cycle_id(conn)
    if not conn.execute("select id from requirements where cycle_id = ? and id = ?", (cycle_id, requirement_id)).fetchone():
        raise HarnessError(f"missing requirement: {requirement_id}")


def _link_requirement_acceptance_conn(
    conn: sqlite3.Connection,
    requirement_id: str,
    acceptance_id: str,
) -> None:
    cycle_id = current_cycle_id(conn)
    require_requirement(conn, requirement_id)
    require_acceptance(conn, acceptance_id)
    before = trace_snapshot(conn, requirement_id)
    conn.execute(
        "insert or ignore into requirement_acceptance (cycle_id, requirement_id, acceptance_id) values (?, ?, ?)",
        (cycle_id, requirement_id, acceptance_id),
    )
    after = trace_snapshot(conn, requirement_id)
    emit_audit_event(
        conn,
        "requirement_acceptance_linked",
        entity_type="requirement",
        entity_id=requirement_id,
        before=before,
        after=after,
        command="requirement link",
    )


@_project_mutation
def link_requirement_acceptance(root: Path, requirement_id: str, acceptance_id: str) -> None:
    with transaction(root, touched=[("requirement", requirement_id), ("acceptance", acceptance_id)]) as conn:
        _link_requirement_acceptance_conn(conn, requirement_id, acceptance_id)
    render_affected(root, "traceability")


def require_task(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    cycle_id = current_cycle_id(conn)
    row = conn.execute("select * from tasks where cycle_id = ? and id = ?", (cycle_id, task_id)).fetchone()
    if not row:
        raise HarnessError(f"missing task: {task_id}")
    return row


def parse_ids(value: str) -> list[str]:
    return [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]




def assert_no_dependency_cycle(conn: sqlite3.Connection, task_id: str, depends_on: str) -> None:
    from core.scheduler import assert_no_dependency_cycle as core_assert_no_dependency_cycle

    core_assert_no_dependency_cycle(
        conn,
        task_id,
        depends_on,
        cycle_id=current_cycle_id(conn),
        error_factory=HarnessError,
    )


def _create_task_conn(
    conn: sqlite3.Connection,
    task_id: str,
    task: str,
    *,
    owner: str = "unassigned",
    acceptance: str = "",
    failure_modes: str = "",
    depends_on: str = "",
) -> sqlite3.Row:
    guard_schema("validate_task", task_id, task, "planned")
    cycle_id = current_cycle_id(conn)
    if conn.execute(
        "select id from tasks where cycle_id = ? and id = ?",
        (cycle_id, task_id),
    ).fetchone():
        raise HarnessError(f"duplicate task id: {task_id}")
    conn.execute(
        """
        insert into tasks (id, cycle_id, task, owner, status, updated_at)
        values (?, ?, ?, ?, 'planned', ?)
        """,
        (task_id, cycle_id, task, owner, now_iso()),
    )
    for acceptance_id in parse_ids(acceptance):
        require_acceptance(conn, acceptance_id)
        conn.execute(
            "insert into task_acceptance (cycle_id, task_id, acceptance_id) values (?, ?, ?)",
            (cycle_id, task_id, acceptance_id),
        )
    for fm_id in parse_ids(failure_modes):
        if not conn.execute(
            "select id from failure_modes where cycle_id = ? and id = ?",
            (cycle_id, fm_id),
        ).fetchone():
            raise HarnessError(f"missing failure mode: {fm_id}")
        conn.execute(
            "insert into task_failure_modes (cycle_id, task_id, failure_mode_id) values (?, ?, ?)",
            (cycle_id, task_id, fm_id),
        )
    for dep in parse_ids(depends_on):
        require_task(conn, dep)
        assert_no_dependency_cycle(conn, task_id, dep)
        conn.execute(
            "insert into task_dependencies (cycle_id, task_id, depends_on) values (?, ?, ?)",
            (cycle_id, task_id, dep),
        )
    after = conn.execute(
        "select * from tasks where cycle_id = ? and id = ?",
        (cycle_id, task_id),
    ).fetchone()
    emit_audit_event(
        conn,
        "task_created",
        entity_type="task",
        entity_id=task_id,
        before=None,
        after=row_snapshot(after),
        actor=owner,
        command="task add",
    )
    if after is None:  # pragma: no cover - SQLite insert contract
        raise HarnessError(f"task was not persisted: {task_id}")
    return after


@_project_mutation
def add_task(
    root: Path,
    task_id: str,
    task: str,
    *,
    owner: str = "unassigned",
    acceptance: str = "",
    failure_modes: str = "",
    depends_on: str = "",
) -> None:
    with transaction(root, touched=[("task", task_id)]) as conn:
        _create_task_conn(
            conn,
            task_id,
            task,
            owner=owner,
            acceptance=acceptance,
            failure_modes=failure_modes,
            depends_on=depends_on,
        )
    render_affected(root, "tasks", *(["traceability"] if acceptance else []))


def list_tasks(root: Path) -> list[str]:
    with connection(root) as conn:
        rows = conn.execute(
            "select id, status, revision, owner, task from tasks where cycle_id = ? order by id",
            (current_cycle_id(conn),),
        ).fetchall()
    return [
        f"id={row['id']} status={row['status']} revision={row['revision']} "
        f"owner={row['owner']} task={row['task']}"
        for row in rows
    ]


def require_task_runnable(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    from core.scheduler import require_task_runnable as core_require_task_runnable

    core_require_task_runnable(conn, row, error_factory=HarnessError)


def require_task_transition(row: sqlite3.Row, allowed_from: set[str], target: str) -> None:
    if row["status"] not in allowed_from:
        expected = ", ".join(sorted(allowed_from))
        raise HarnessError(
            f"cannot transition task {row['id']} from {row['status']} to {target}; expected {expected}"
        )


@_project_mutation
def start_task(root: Path, task_id: str) -> None:
    with transaction(root, touched=[("task", task_id)]) as conn:
        row = require_task(conn, task_id)
        require_task_transition(row, {"planned"}, "active")
        require_task_runnable(conn, row)
        conn.execute(
            "update tasks set status = 'active', revision = revision + 1, updated_at = ? where uid = ?",
            (now_iso(), row["uid"]),
        )
        after = conn.execute("select * from tasks where uid = ?", (row["uid"],)).fetchone()
        emit_audit_event(
            conn,
            "task_started",
            entity_type="task",
            entity_id=task_id,
            before=row_snapshot(row),
            after=row_snapshot(after),
            actor="root-controller",
            command="task start",
        )
    render_affected(root, "tasks")


@_project_mutation
def submit_task(root: Path, task_id: str, evidence: str, *, context_id: str = "") -> None:
    if not evidence.strip():
        raise HarnessError("task submit evidence is required")
    with transaction(root, touched=[("task", task_id)]) as conn:
        row = require_task(conn, task_id)
        require_task_transition(row, {"active"}, "submitted")
        conn.execute(
            """
            update tasks
            set status = 'submitted', evidence = ?, submitted_context_id = ?,
                revision = revision + 1, updated_at = ?
            where uid = ?
            """,
            (evidence.strip(), context_id.strip(), now_iso(), row["uid"]),
        )
        after = conn.execute("select * from tasks where uid = ?", (row["uid"],)).fetchone()
        emit_audit_event(
            conn,
            "task_submitted",
            entity_type="task",
            entity_id=task_id,
            before=row_snapshot(row),
            after=row_snapshot(after),
            actor="root-controller",
            command="task submit",
            extra={"submitted_context_id": context_id.strip()},
        )
    render_affected(root, "tasks")


@_project_mutation
def accept_task(root: Path, task_id: str, evidence: str) -> None:
    if not evidence.strip():
        raise HarnessError("task accept evidence is required")
    with transaction(root, touched=[("task", task_id)]) as conn:
        row = require_task(conn, task_id)
        require_task_transition(row, {"submitted"}, "accepted")
        conn.execute(
            """
            update tasks
            set status = 'accepted', evidence = ?, accepted_by = 'root-controller',
                revision = revision + 1, updated_at = ?
            where uid = ?
            """,
            (evidence.strip(), now_iso(), row["uid"]),
        )
        after = conn.execute("select * from tasks where uid = ?", (row["uid"],)).fetchone()
        emit_audit_event(
            conn,
            "task_accepted",
            entity_type="task",
            entity_id=task_id,
            before=row_snapshot(row),
            after=row_snapshot(after),
            actor="root-controller",
            command="task accept",
        )
    render_affected(root, "tasks", "traceability")


@_project_mutation
def block_task(root: Path, task_id: str, reason: str) -> None:
    if not reason.strip():
        raise HarnessError("task block reason is required")
    with transaction(root, touched=[("task", task_id)]) as conn:
        row = require_task(conn, task_id)
        require_task_transition(row, {"active", "submitted"}, "blocked")
        conn.execute(
            """
            update tasks
            set status = 'blocked', evidence = ?, revision = revision + 1, updated_at = ?
            where uid = ?
            """,
            (reason.strip(), now_iso(), row["uid"]),
        )
        after = conn.execute("select * from tasks where uid = ?", (row["uid"],)).fetchone()
        emit_audit_event(
            conn,
            "task_blocked",
            entity_type="task",
            entity_id=task_id,
            before=row_snapshot(row),
            after=row_snapshot(after),
            actor="root-controller",
            command="task block",
        )
    render_affected(root, "tasks")


@_project_mutation
def cancel_task(root: Path, task_id: str, reason: str = "") -> None:
    with transaction(root, touched=[("task", task_id)]) as conn:
        row = require_task(conn, task_id)
        require_task_transition(row, {"planned", "active", "submitted"}, "cancelled")
        conn.execute(
            """
            update tasks
            set status = 'cancelled',
                evidence = case when ? != '' then ? else evidence end,
                revision = revision + 1,
                updated_at = ?
            where uid = ?
            """,
            (reason.strip(), reason.strip(), now_iso(), row["uid"]),
        )
        after = conn.execute("select * from tasks where uid = ?", (row["uid"],)).fetchone()
        emit_audit_event(
            conn,
            "task_cancelled",
            entity_type="task",
            entity_id=task_id,
            before=row_snapshot(row),
            after=row_snapshot(after),
            actor="root-controller",
            command="task cancel",
            extra={"reason": reason.strip()},
        )
    render_affected(root, "tasks", "traceability")


@_project_mutation
def record_decision(root: Path, decision: str, reason: str) -> None:
    with transaction(root) as conn:
        decision_id = str(uuid.uuid4())
        decision_columns = {
            str(row[1]) for row in conn.execute("pragma table_info(decisions)")
        }
        if {"cycle_id", "candidate_sha"}.issubset(decision_columns):
            conn.execute(
                "insert into decisions "
                "(id, cycle_id, candidate_sha, decision, reason, created_at) "
                "values (?, ?, ?, ?, ?, ?)",
                (
                    decision_id,
                    current_cycle_id(conn),
                    current_candidate_sha(root),
                    decision,
                    reason,
                    now_iso(),
                ),
            )
        else:
            # Schema 27-30 projects remain writable until the side-by-side
            # migration obtains the operation lock and snapshots their final
            # committed state.
            conn.execute(
                "insert into decisions (id, decision, reason, created_at) "
                "values (?, ?, ?, ?)",
                (decision_id, decision, reason, now_iso()),
            )
        created = conn.execute("select * from decisions where id = ?", (decision_id,)).fetchone()
        emit_audit_event(
            conn,
            "decision_recorded",
            entity_type="decision",
            entity_id=decision_id,
            before=None,
            after=row_snapshot(created),
            command="decision record",
        )
    render_affected(root, "decisions")


def _record_test_target_conn(
    conn: sqlite3.Connection,
    target_id: str,
    kind: str,
    command_template: str,
    description: str = "",
    *,
    stack_profile: str = "python",
    container_image: str = "",
    requires_sandbox: bool = False,
    requires_no_network: bool = False,
    result_format: str = "regex",
    result_path: str = "",
    create_only: bool = False,
) -> tuple[sqlite3.Row, bool]:
    guard_schema("validate_test_target", target_id, kind, command_template, stack_profile, result_format)
    gateable, gate_block_reason = target_gateability(kind, command_template)
    before = conn.execute(
        "select * from test_targets where id = ?", (target_id,)
    ).fetchone()
    requested = {
        "kind": kind,
        "command_template": command_template,
        "description": description,
        "gateable": gateable,
        "gate_block_reason": gate_block_reason,
        "stack_profile": stack_profile,
        "container_image": container_image,
        "requires_sandbox": bool_int(requires_sandbox),
        "requires_no_network": bool_int(requires_no_network),
        "result_format": result_format,
        "result_path": result_path,
    }
    if before is not None and all(
        before[field] == value for field, value in requested.items()
    ):
        return before, False
    if create_only and before is not None:
        raise HarnessError(f"duplicate test target id: {target_id}")
    if before is not None:
        closed_cycles = [
            str(row["cycle_id"])
            for row in conn.execute(
                """
                select distinct refs.cycle_id
                from (
                    select cycle_id from acceptance_target_qualifications
                    where target_id = ?
                    union
                    select cycle_id from task_test_targets where target_id = ?
                    union
                    select cycle_id from executions where target_id = ?
                ) refs
                join delivery_cycles c on c.id = refs.cycle_id
                where c.status in ('delivered', 'archived')
                order by refs.cycle_id
                """,
                (target_id, target_id, target_id),
            ).fetchall()
        ]
        if closed_cycles:
            changed = sorted(
                field
                for field, value in requested.items()
                if before[field] != value
            )
            raise HarnessError(
                f"test target {target_id} is referenced by closed cycle "
                f"{','.join(closed_cycles)}; changed={','.join(changed)}; "
                "use a new target ID"
            )
    if create_only:
        conn.execute(
            """
            insert into test_targets
            (id, kind, command_template, description, gateable, gate_block_reason, stack_profile, container_image,
             requires_sandbox, requires_no_network, result_format, result_path, created_at, updated_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                target_id,
                kind,
                command_template,
                description,
                gateable,
                gate_block_reason,
                stack_profile,
                container_image,
                bool_int(requires_sandbox),
                bool_int(requires_no_network),
                result_format,
                result_path,
                now_iso(),
                now_iso(),
            ),
        )
    else:
        conn.execute(
            """
            insert into test_targets
            (id, kind, command_template, description, gateable, gate_block_reason, stack_profile, container_image,
             requires_sandbox, requires_no_network, result_format, result_path, created_at, updated_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(id) do update set kind=excluded.kind, command_template=excluded.command_template,
              description=excluded.description, gateable=excluded.gateable, gate_block_reason=excluded.gate_block_reason,
              stack_profile=excluded.stack_profile, container_image=excluded.container_image,
              requires_sandbox=excluded.requires_sandbox, requires_no_network=excluded.requires_no_network,
              result_format=excluded.result_format, result_path=excluded.result_path,
              updated_at=excluded.updated_at
            """,
            (
                target_id,
                kind,
                command_template,
                description,
                gateable,
                gate_block_reason,
                stack_profile,
                container_image,
                bool_int(requires_sandbox),
                bool_int(requires_no_network),
                result_format,
                result_path,
                now_iso(),
                now_iso(),
            ),
        )
    after = conn.execute(
        "select * from test_targets where id = ?", (target_id,)
    ).fetchone()
    emit_audit_event(
        conn,
        "test_target_recorded",
        entity_type="test_target",
        entity_id=target_id,
        before=row_snapshot(before),
        after=row_snapshot(after),
        command="test-target add",
    )
    if after is None:  # pragma: no cover - SQLite insert contract
        raise HarnessError(f"test target was not persisted: {target_id}")
    return after, True


@_project_mutation
def add_test_target(
    root: Path,
    target_id: str,
    kind: str,
    command_template: str,
    description: str = "",
    *,
    stack_profile: str = "python",
    container_image: str = "",
    requires_sandbox: bool = False,
    requires_no_network: bool = False,
    result_format: str = "regex",
    result_path: str = "",
) -> None:
    changed = False
    with transaction(root, touched=[("test_target", target_id)]) as conn:
        _, changed = _record_test_target_conn(
            conn,
            target_id,
            kind,
            command_template,
            description,
            stack_profile=stack_profile,
            container_image=container_image,
            requires_sandbox=requires_sandbox,
            requires_no_network=requires_no_network,
            result_format=result_format,
            result_path=result_path,
        )
    if not changed:
        return
    render_affected(root, "test-targets")


def _link_task_test_target_conn(
    conn: sqlite3.Connection,
    task_id: str,
    target_id: str,
) -> None:
    cycle_id = current_cycle_id(conn)
    if not conn.execute(
        "select id from tasks where cycle_id = ? and id = ?",
        (cycle_id, task_id),
    ).fetchone():
        raise HarnessError(f"missing task: {task_id}")
    if not conn.execute(
        "select id from test_targets where id = ?", (target_id,)
    ).fetchone():
        raise HarnessError(f"missing test target: {target_id}")
    conn.execute(
        "insert or ignore into task_test_targets (cycle_id, task_id, target_id) values (?, ?, ?)",
        (cycle_id, task_id, target_id),
    )
    emit_audit_event(
        conn,
        "task_test_target_linked",
        entity_type="task_test_target",
        entity_id=f"{cycle_id}:{task_id}:{target_id}",
        before=None,
        after={"cycle_id": cycle_id, "task_id": task_id, "target_id": target_id},
        command="test-target link",
    )


@_project_mutation
def link_task_test_target(root: Path, task_id: str, target_id: str) -> None:
    with transaction(root, touched=[("task", task_id)]) as conn:
        _link_task_test_target_conn(conn, task_id, target_id)


def list_test_targets(root: Path) -> list[str]:
    with connection(root) as conn:
        rows = conn.execute(
            """
            select id, kind, command_template, description, gateable, gate_block_reason, stack_profile,
                   container_image, requires_sandbox, requires_no_network, result_format, result_path
            from test_targets order by id
            """
        ).fetchall()
    return [
        markdown_row(
            [
                row["id"],
                row["kind"],
                row["command_template"],
                row["description"],
                str(row["gateable"]),
                row["gate_block_reason"],
                row["stack_profile"],
                row["container_image"],
                str(row["requires_sandbox"]),
                str(row["requires_no_network"]),
                row["result_format"],
                row["result_path"],
            ]
        )
        for row in rows
    ]


def _require_current_qualification(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    acceptance_id: str,
    target_id: str,
    target_digest: str,
) -> sqlite3.Row:
    acceptance = conn.execute(
        "select * from acceptance where cycle_id = ? and id = ?",
        (cycle_id, acceptance_id),
    ).fetchone()
    if acceptance is None:
        raise HarnessError(f"missing acceptance: {acceptance_id}")
    if str(acceptance["status"]) != "active":
        raise HarnessError(
            f"acceptance is not active: {acceptance_id} status={acceptance['status']}"
        )
    latest = latest_acceptance_target_qualification(
        conn,
        cycle_id=cycle_id,
        acceptance_id=acceptance_id,
        target_id=target_id,
    )
    if latest is None:
        raise HarnessError(
            "[qualification-missing] "
            f"acceptance {acceptance_id} has no qualification for target {target_id}"
        )
    if (
        int(latest["acceptance_revision"]) == int(acceptance["revision"])
        and str(latest["target_definition_sha256"]) == target_digest
    ):
        return latest
    if int(latest["acceptance_revision"]) != int(acceptance["revision"]):
        detail = (
            f"acceptance {acceptance_id} revision changed from "
            f"{latest['acceptance_revision']} to {acceptance['revision']}"
        )
    else:
        detail = f"target {target_id} definition digest changed"
    raise HarnessError(
        "[qualification-stale] "
        f"qualification {latest['id']} is stale: {detail}"
    )


def _qualify_test_target_conn(
    conn: sqlite3.Connection,
    qualification_id: str,
    target_id: str,
    acceptance_id: str,
    rationale: str,
    qualified_by: str,
) -> tuple[str, bool]:
    """Record one immutable qualification on an existing transaction."""

    normalized_id = qualification_id.strip()
    normalized_target = target_id.strip()
    normalized_acceptance = acceptance_id.strip()
    normalized_rationale = rationale.strip()
    normalized_actor = qualified_by.strip()
    missing = [
        name
        for name, value in (
            ("id", normalized_id),
            ("target", normalized_target),
            ("acceptance", normalized_acceptance),
            ("rationale", normalized_rationale),
            ("by", normalized_actor),
        )
        if not value
    ]
    if missing:
        raise HarnessError(
            "test-target qualification requires non-empty " + ", ".join(missing)
        )

    cycle_id = current_cycle_id(conn)
    acceptance = conn.execute(
        "select * from acceptance where cycle_id = ? and id = ?",
        (cycle_id, normalized_acceptance),
    ).fetchone()
    if acceptance is None:
        another_cycle = conn.execute(
            "select cycle_id from acceptance where id = ? order by cycle_id limit 1",
            (normalized_acceptance,),
        ).fetchone()
        if another_cycle:
            raise HarnessError(
                "cross-cycle qualification is not allowed: "
                f"acceptance {normalized_acceptance} belongs to "
                f"{another_cycle['cycle_id']}, current={cycle_id}"
            )
        raise HarnessError(f"missing acceptance: {normalized_acceptance}")
    if str(acceptance["status"]) != "active":
        raise HarnessError(
            "qualification requires an active acceptance: "
            f"{normalized_acceptance} status={acceptance['status']}"
        )
    target = conn.execute(
        "select * from test_targets where id = ?",
        (normalized_target,),
    ).fetchone()
    if target is None:
        raise HarnessError(f"missing test target: {normalized_target}")
    digest = target_definition_digest(dict(target))
    values = (
        normalized_id,
        cycle_id,
        normalized_acceptance,
        int(acceptance["revision"]),
        normalized_target,
        digest,
        normalized_rationale,
        normalized_actor,
    )
    existing = conn.execute(
        "select * from acceptance_target_qualifications where id = ?",
        (normalized_id,),
    ).fetchone()
    if existing is not None:
        if str(existing["cycle_id"]) != cycle_id:
            raise HarnessError(
                f"qualification ID {normalized_id} belongs to cycle "
                f"{existing['cycle_id']}; use a new qualification ID for "
                f"cycle {cycle_id}"
            )
        existing_values = tuple(
            existing[field]
            for field in (
                "id",
                "cycle_id",
                "acceptance_id",
                "acceptance_revision",
                "target_id",
                "target_definition_sha256",
                "rationale",
                "qualified_by",
            )
        )
        if existing_values != values:
            raise HarnessError(
                "conflicting immutable qualification already exists: "
                f"{normalized_id}"
            )
        return normalized_id, False

    conn.execute(
        """
        insert into acceptance_target_qualifications
        (id, cycle_id, acceptance_id, acceptance_revision, target_id,
         target_definition_sha256, rationale, qualified_by, created_at)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (*values, now_iso()),
    )
    created_row = conn.execute(
        "select * from acceptance_target_qualifications where id = ?",
        (normalized_id,),
    ).fetchone()
    emit_audit_event(
        conn,
        "acceptance_target_qualified",
        entity_type="acceptance_target_qualification",
        entity_id=normalized_id,
        before=None,
        after=row_snapshot(created_row),
        actor=normalized_actor,
        command="test-target qualify",
        extra={
            "acceptance_id": normalized_acceptance,
            "target_id": normalized_target,
            "digest": digest,
        },
    )
    return normalized_id, True


@_project_mutation
def qualify_test_target(
    root: Path,
    qualification_id: str,
    target_id: str,
    acceptance_id: str,
    rationale: str,
    qualified_by: str,
) -> str:
    """Record an immutable procedural acceptance-to-target qualification."""

    created = False
    normalized_id = qualification_id.strip()
    with transaction(
        root,
        touched=[("acceptance_target_qualification", normalized_id)],
    ) as conn:
        normalized_id, created = _qualify_test_target_conn(
            conn,
            qualification_id,
            target_id,
            acceptance_id,
            rationale,
            qualified_by,
        )
    if created:
        render_affected(root, "test-targets")
    return normalized_id


@_project_mutation
def verify_run(
    root: Path,
    target_id: str,
    *,
    acceptance: str = "",
    failure_modes: list[str] | None = None,
    runner: str = "local",
    container_image: str = "",
) -> tuple[str, str]:
    """Execute one registered target and atomically record normalized facts."""

    from core.execution import (
        ContainerExecutor,
        ExecutionPolicyError,
        LocalExecutor,
        target_policy_from_row,
        validate_execution_result,
    )

    if runner not in {"local", "container"}:
        raise HarnessError(f"unknown verification runner: {runner}")
    requested_failure_modes = sorted(
        {value.strip() for value in (failure_modes or []) if value.strip()}
    )
    if requested_failure_modes and not acceptance.strip():
        raise HarnessError(
            "failure-mode coverage requires an acceptance-bound qualified target"
        )
    with connection(root) as conn:
        cycle_id = current_cycle_id(conn)
        candidate_sha = current_candidate_sha(root)
        target_row = conn.execute(
            "select * from test_targets where id = ?", (target_id,)
        ).fetchone()
        if not target_row:
            raise HarnessError(f"missing test target: {target_id}")
        target_data = dict(target_row)
        target_digest = target_definition_digest(target_data)
        if int(target_data.get("gateable") or 0) != 1:
            reason = str(target_data.get("gate_block_reason") or "not gateable")
            raise HarnessError(f"test target is not gateable: {target_id}: {reason}")
        qualification_data: dict[str, object] | None = None
        if acceptance:
            qualification_data = dict(
                _require_current_qualification(
                    conn,
                    cycle_id=cycle_id,
                    acceptance_id=acceptance,
                    target_id=target_id,
                    target_digest=target_digest,
                )
            )
        for failure_mode_id in requested_failure_modes:
            if not conn.execute(
                "select 1 from failure_modes where cycle_id = ? and id = ?",
                (cycle_id, failure_mode_id),
            ).fetchone():
                raise HarnessError(f"missing failure mode: {failure_mode_id}")
            if not conn.execute(
                """
                select 1 from failure_mode_acceptance
                where cycle_id = ? and failure_mode_id = ? and acceptance_id = ?
                """,
                (cycle_id, failure_mode_id, acceptance),
            ).fetchone():
                raise HarnessError(
                    "failure-mode coverage acceptance is not linked: "
                    f"{failure_mode_id}->{acceptance}"
                )

    policy = target_policy_from_row(target_data)
    if (
        acceptance
        and container_image.strip()
        and container_image.strip() != policy.container_image.strip()
    ):
        raise HarnessError(
            "acceptance-bound verification cannot override the qualified "
            f"container image for target {target_id}"
        )
    if runner == "local" and (policy.requires_sandbox or policy.requires_no_network):
        requirements = []
        if policy.requires_sandbox:
            requirements.append("sandbox")
        if policy.requires_no_network:
            requirements.append("no-network")
        raise HarnessError(
            f"target {target_id} requires {' and '.join(requirements)} container verification"
        )
    try:
        if runner == "container":
            image = (
                container_image.strip()
                or policy.container_image.strip()
                or STACK_PROFILE_IMAGES.get(
                    str(target_data.get("stack_profile") or "python"),
                    DEFAULT_CONTAINER_IMAGE,
                )
            )
            result = ContainerExecutor(root).run(
                policy.command_template,
                target_id=policy.id,
                target_command_template=policy.command_template,
                container_image=image,
                result_format=policy.result_format,
                result_path=policy.result_path,
                target_definition_sha256=target_digest,
            )
        else:
            result = LocalExecutor(root).run(
                policy.command_template,
                target_id=policy.id,
                target_command_template=policy.command_template,
                result_format=policy.result_format,
                result_path=policy.result_path,
                target_definition_sha256=target_digest,
            )
        validate_execution_result(root, policy, result, runner=runner)
    except ExecutionPolicyError as exc:
        raise HarnessError(str(exc)) from exc

    execution_id = f"EX-{uuid.uuid4().hex}"
    validation_id = f"VAL-{uuid.uuid4().hex}"
    surface = f"test-target:{target_id}"

    def revalidate_execution_before_commit(commit_conn: sqlite3.Connection) -> None:
        try:
            validate_execution_result(root, policy, result, runner=runner)
        except ExecutionPolicyError as exc:
            raise HarnessError(str(exc)) from exc
        if current_candidate_sha(root) != candidate_sha:
            raise HarnessError(
                "stale candidate: project source changed before verification commit"
            )
        commit_target = commit_conn.execute(
            "select * from test_targets where id = ?",
            (target_id,),
        ).fetchone()
        if (
            commit_target is None
            or target_definition_digest(dict(commit_target)) != target_digest
        ):
            raise HarnessError(
                f"stale target: registered target changed before commit: {target_id}"
            )
        if acceptance:
            commit_qualification = _require_current_qualification(
                commit_conn,
                cycle_id=cycle_id,
                acceptance_id=acceptance,
                target_id=target_id,
                target_digest=target_digest,
            )
            if (
                qualification_data is None
                or dict(commit_qualification) != qualification_data
            ):
                raise HarnessError(
                    "stale qualification: acceptance-target mapping changed before commit: "
                    f"{acceptance}->{target_id}"
                )

    with transaction(
        root,
        touched=[("execution", execution_id), ("validation", validation_id)],
        before_commit_check=revalidate_execution_before_commit,
    ) as conn:
        if current_cycle_id(conn) != cycle_id:
            raise HarnessError(
                f"stale candidate: current cycle changed during verification from {cycle_id}"
            )
        current_candidate = current_candidate_sha(root)
        if current_candidate != candidate_sha:
            raise HarnessError(
                "stale candidate: project source changed during verification; "
                "discarding the completed command result"
            )
        live_target = conn.execute(
            "select * from test_targets where id = ?", (target_id,)
        ).fetchone()
        if (
            not live_target
            or target_definition_digest(dict(live_target)) != target_digest
        ):
            raise HarnessError(
                f"stale target: registered target changed during verification: {target_id}"
            )
        if acceptance:
            live_qualification = _require_current_qualification(
                conn,
                cycle_id=cycle_id,
                acceptance_id=acceptance,
                target_id=target_id,
                target_digest=target_digest,
            )
            if (
                qualification_data is None
                or dict(live_qualification) != qualification_data
            ):
                raise HarnessError(
                    "stale qualification: acceptance-target mapping changed "
                    f"during verification: {acceptance}->{target_id}"
                )
        for failure_mode_id in requested_failure_modes:
            if not conn.execute(
                "select 1 from failure_modes where cycle_id = ? and id = ?",
                (cycle_id, failure_mode_id),
            ).fetchone():
                raise HarnessError(f"stale failure mode: {failure_mode_id}")
            if not conn.execute(
                """
                select 1 from failure_mode_acceptance
                where cycle_id = ? and failure_mode_id = ? and acceptance_id = ?
                """,
                (cycle_id, failure_mode_id, acceptance),
            ).fetchone():
                raise HarnessError(
                    "stale failure-mode coverage acceptance link: "
                    f"{failure_mode_id}->{acceptance}"
                )
        try:
            validate_execution_result(root, policy, result, runner=runner)
        except ExecutionPolicyError as exc:
            raise HarnessError(str(exc)) from exc
        created_at = now_iso()
        conn.execute(
            """
            insert into executions
            (id, cycle_id, candidate_sha, target_id, target_definition_sha256,
             command, exit_code,
             stdout_sha256, artifact_path, executed_count, result_format,
             semantic_status, runner, sandbox_status, no_network, policy_status,
             platform, runtime_executable, runtime_version,
             runtime_executable_sha256, policy_version, container_engine,
             container_engine_version, container_engine_endpoint,
             container_image_requested,
             container_image_digest, provenance_status,
             created_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                execution_id,
                cycle_id,
                candidate_sha,
                target_id,
                target_digest,
                result.command,
                result.exit_code,
                result.stdout_sha256,
                result.artifact_path,
                result.executed_count,
                result.result_format,
                result.semantic_status,
                runner,
                result.sandbox_status,
                bool_int(result.no_network),
                result.policy_status,
                result.platform,
                result.runtime_executable,
                result.runtime_version,
                result.runtime_executable_sha256,
                result.policy_version,
                result.container_engine,
                result.container_engine_version,
                result.container_engine_endpoint,
                result.container_image_requested,
                result.container_image_digest,
                result.provenance_status,
                created_at,
            ),
        )
        conn.execute(
            """
            insert into validations
            (id, cycle_id, candidate_sha, acceptance_id, qualification_id,
             surface, result,
             validation_status, superseded_by, findings, residual_risk, created_at)
            values (?, ?, ?, ?, ?, ?, 'pass', 'active', null,
                    'controller execution passed', '', ?)
            """,
            (
                validation_id,
                cycle_id,
                candidate_sha,
                acceptance or None,
                (
                    str(qualification_data["id"])
                    if qualification_data is not None
                    else None
                ),
                surface,
                created_at,
            ),
        )
        conn.execute(
            """
            update validations
            set validation_status = 'superseded', superseded_by = ?
            where id != ? and cycle_id = ? and surface = ?
              and coalesce(acceptance_id, '') = ? and validation_status = 'active'
            """,
            (validation_id, validation_id, cycle_id, surface, acceptance),
        )
        conn.execute(
            """insert into validation_executions
            (validation_id, execution_id, cycle_id, candidate_sha) values (?, ?, ?, ?)""",
            (validation_id, execution_id, cycle_id, candidate_sha),
        )
        for failure_mode_id in requested_failure_modes:
            conn.execute(
                """
                insert into validation_failure_modes
                (validation_id, cycle_id, failure_mode_id) values (?, ?, ?)
                """,
                (validation_id, cycle_id, failure_mode_id),
            )
            resolve_invalidations(
                conn,
                source_type="failure_mode",
                source_id=failure_mode_id,
            )
        if acceptance:
            resolve_invalidations(
                conn,
                source_type="acceptance",
                source_id=acceptance,
            )
        after = conn.execute(
            "select * from validations where id = ?", (validation_id,)
        ).fetchone()
        emit_audit_event(
            conn,
            "verification_recorded",
            entity_type="validation",
            entity_id=validation_id,
            before=None,
            after=row_snapshot(after),
            actor="root-controller",
            command=f"verify run --target {target_id}",
            extra={
                "execution_id": execution_id,
                "target_id": target_id,
                "candidate_sha": candidate_sha,
            },
        )
    render_affected(
        root,
        "executions",
        "validation",
        *(["failure-modes"] if requested_failure_modes else []),
        *(["traceability"] if acceptance or requested_failure_modes else []),
    )
    return execution_id, validation_id


@_project_mutation
def record_validation(
    root: Path,
    surface: str,
    findings: str,
    result: str,
    *,
    acceptance: str = "",
    failure_modes: str = "",
    residual_risk: str = "",
) -> None:
    """Record a judgment-only validation that is never execution evidence."""

    guard_schema("validate_validation", surface, findings, result)
    with transaction(root, touched=[("validation", "")]) as conn:
        cycle_id = current_cycle_id(conn)
        candidate_sha = current_candidate_sha(root)
        if acceptance and not conn.execute(
            "select 1 from acceptance where cycle_id = ? and id = ?",
            (cycle_id, acceptance),
        ).fetchone():
            raise HarnessError(f"missing acceptance: {acceptance}")
        validation_id = f"VAL-{uuid.uuid4().hex}"
        conn.execute(
            """
            insert into validations
            (id, cycle_id, candidate_sha, acceptance_id, surface, result,
             validation_status, superseded_by, findings, residual_risk, created_at)
            values (?, ?, ?, ?, ?, ?, 'active', null, ?, ?, ?)
            """,
            (
                validation_id,
                cycle_id,
                candidate_sha,
                acceptance or None,
                surface,
                result,
                findings,
                residual_risk,
                now_iso(),
            ),
        )
        conn.execute(
            """
            update validations
            set validation_status = 'superseded', superseded_by = ?
            where id != ? and cycle_id = ? and surface = ?
              and coalesce(acceptance_id, '') = ? and validation_status = 'active'
            """,
            (validation_id, validation_id, cycle_id, surface, acceptance),
        )
        if acceptance:
            resolve_invalidations(conn, source_type="acceptance", source_id=acceptance)
        for fm_id in parse_ids(failure_modes):
            if not conn.execute("select id from failure_modes where cycle_id = ? and id = ?", (cycle_id, fm_id)).fetchone():
                raise HarnessError(f"missing failure mode: {fm_id}")
            conn.execute(
                "insert into validation_failure_modes (validation_id, cycle_id, failure_mode_id) values (?, ?, ?)",
                (validation_id, cycle_id, fm_id),
            )
            resolve_invalidations(conn, source_type="failure_mode", source_id=fm_id)
        after = conn.execute("select * from validations where id = ?", (validation_id,)).fetchone()
        emit_audit_event(
            conn,
            "validation_judgment_recorded",
            entity_type="validation",
            entity_id=validation_id,
            before=None,
            after=row_snapshot(after),
            actor="root-controller",
            command="validation record",
            extra={
                "surface": surface,
                "result": result,
                "gate_eligible": False,
            },
        )
    render_affected(
        root,
        "validation",
        *(["failure-modes"] if failure_modes else []),
        *(["traceability"] if acceptance or failure_modes else []),
    )


@_project_mutation
def record_finding(
    root: Path,
    finding_id: str,
    surface: str,
    severity: str,
    status: str,
    summary: str,
    *,
    waived_by: str = "",
    waiver_reason: str = "",
    waiver_scope: str = "",
    waived_revision: int | None = None,
    waiver_expires_at: str = "",
) -> None:
    if status == "accepted":
        complete_text = all(
            value.strip()
            for value in (waived_by, waiver_reason, waiver_scope, waiver_expires_at)
        )
        valid_revision = (
            isinstance(waived_revision, int)
            and not isinstance(waived_revision, bool)
            and waived_revision > 0
        )
        if not complete_text or not valid_revision:
            raise HarnessError(
                "accepted finding requires actor, reason, scope, positive revision, and expiry"
            )
        if parse_time(waiver_expires_at) is None:
            raise HarnessError("accepted finding expiry must be a valid ISO-8601 timestamp")
    with transaction(root) as conn:
        cycle_id = current_cycle_id(conn)
        candidate_sha = current_candidate_sha(root)
        before = conn.execute("select * from findings where id = ?", (finding_id,)).fetchone()
        if before is not None and str(before["cycle_id"]) != cycle_id:
            raise HarnessError(
                f"finding ID {finding_id} belongs to cycle {before['cycle_id']}; "
                f"use a new finding ID for cycle {cycle_id}"
            )
        conn.execute(
            """
            insert into findings
            (id, cycle_id, candidate_sha, surface, severity, status, summary,
             waived_by, waiver_reason, waiver_scope, waived_revision, waiver_expires_at, created_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(id) do update set surface=excluded.surface, severity=excluded.severity, status=excluded.status,
              cycle_id=excluded.cycle_id, candidate_sha=excluded.candidate_sha,
              summary=excluded.summary,
              waived_by=excluded.waived_by, waiver_reason=excluded.waiver_reason,
              waiver_scope=excluded.waiver_scope, waived_revision=excluded.waived_revision,
              waiver_expires_at=excluded.waiver_expires_at, created_at=excluded.created_at
            """,
            (finding_id, cycle_id, candidate_sha, surface, severity, status, summary,
             waived_by, waiver_reason, waiver_scope, waived_revision, waiver_expires_at, now_iso()),
        )
        after = conn.execute("select * from findings where id = ?", (finding_id,)).fetchone()
        emit_audit_event(
            conn,
            "finding_recorded",
            entity_type="finding",
            entity_id=finding_id,
            before=row_snapshot(before),
            after=row_snapshot(after),
            actor=waived_by or "root-controller",
            command="finding record",
        )
    render_affected(root, "findings")


@_project_mutation
def record_gate(
    root: Path,
    reviewer_context: str,
    result: str,
    *,
    gate: str = "independent_qa",
    blocking_findings: str = "",
    residual_risk: str = "",
    findings: str = "",
    reviewer_context_id: str = "",
    qualifications: list[str] | None = None,
) -> None:
    guard_schema("validate_gate", reviewer_context, result, gate)
    if reviewer_context == "fresh" and not reviewer_context_id:
        raise HarnessError("fresh reviewer context requires reviewer context metadata")
    if (
        reviewer_context == "same-context-degraded"
        and result == "pass"
        and not residual_risk.strip()
    ):
        raise HarnessError(
            "same-context-degraded passing gate requires non-empty residual-risk text"
        )
    if result == "pass" and git_dirty(root):
        raise HarnessError("cannot record a passing quality gate with a dirty git worktree")
    if qualifications is not None and any(
        not isinstance(value, str) or not value.strip()
        for value in qualifications
    ):
        raise HarnessError("gate qualification IDs must be non-empty")
    qualification_ids = sorted(
        {value.strip() for value in (qualifications or []) if value.strip()}
    )
    captured_candidate = ""

    def revalidate_gate_before_commit(commit_conn: sqlite3.Connection) -> None:
        if not captured_candidate:
            return
        if current_candidate_sha(root) != captured_candidate:
            raise HarnessError(
                "stale candidate: project source changed before quality gate commit"
            )
        from core.delivery import qualified_validation_execution_issues

        for qualification_id in qualification_ids:
            qualification = commit_conn.execute(
                "select * from acceptance_target_qualifications where id = ?",
                (qualification_id,),
            ).fetchone()
            validation = commit_conn.execute(
                """
                select * from validations
                where qualification_id = ? and candidate_sha = ?
                  and validation_status = 'active' and result = 'pass'
                order by created_at desc, id desc limit 1
                """,
                (qualification_id, captured_candidate),
            ).fetchone()
            if qualification is None or validation is None:
                raise HarnessError(
                    "qualification evidence changed before quality gate commit: "
                    f"{qualification_id}"
                )
            issues = qualified_validation_execution_issues(
                commit_conn,
                root,
                validation,
                qualification,
                captured_candidate,
            )
            if issues:
                raise HarnessError(
                    "qualification evidence became ineligible before quality gate commit: "
                    f"{qualification_id}: {'; '.join(issues)}"
                )

    with transaction(
        root,
        before_commit_check=revalidate_gate_before_commit,
    ) as conn:
        cycle_id = current_cycle_id(conn)
        candidate_sha = current_candidate_sha(root)
        captured_candidate = candidate_sha
        project_revision = int(project_row(conn)["revision"])
        producer_contexts = [
            str(row["submitted_context_id"])
            for row in conn.execute(
                """
                select distinct submitted_context_id from tasks
                where cycle_id = ? and status in ('submitted', 'accepted')
                  and submitted_context_id != ''
                order by submitted_context_id
                """,
                (cycle_id,),
            )
        ]
        if reviewer_context == "fresh":
            if reviewer_context_id in producer_contexts:
                raise HarnessError(
                    f"fresh reviewer context matches producer context: {reviewer_context_id}"
                )
            review_status = "reviewed-local" if producer_contexts else "same-context-degraded"
        else:
            review_status = "same-context-degraded"
        if (
            review_status == "same-context-degraded"
            and result == "pass"
            and not residual_risk.strip()
        ):
            raise HarnessError(
                "same-context-degraded passing gate requires non-empty residual-risk text"
            )
        producer_context_id = ",".join(producer_contexts)
        finding_rows: list[sqlite3.Row] = []
        for finding_id in parse_ids(findings):
            finding = conn.execute(
                "select * from findings where id = ?",
                (finding_id,),
            ).fetchone()
            if finding is None:
                raise HarnessError(f"missing finding: {finding_id}")
            if str(finding["cycle_id"]) != cycle_id:
                raise HarnessError(
                    "cross-cycle gate finding is not allowed: "
                    f"{finding_id} cycle={finding['cycle_id']} current={cycle_id}"
                )
            if str(finding["candidate_sha"] or "") != candidate_sha:
                raise HarnessError(
                    "stale-candidate gate finding is not allowed: "
                    f"{finding_id} candidate={finding['candidate_sha'] or 'empty'} "
                    f"current={candidate_sha}"
                )
            finding_rows.append(finding)
        qualification_rows: list[sqlite3.Row] = []
        for qualification_id in qualification_ids:
            qualification = conn.execute(
                """
                select q.*, a.revision as current_acceptance_revision,
                       a.status as acceptance_status,
                       t.kind, t.command_template, t.stack_profile,
                       t.container_image, t.requires_sandbox,
                       t.requires_no_network, t.result_format, t.result_path
                from acceptance_target_qualifications q
                join acceptance a
                  on a.cycle_id = q.cycle_id and a.id = q.acceptance_id
                join test_targets t on t.id = q.target_id
                where q.id = ?
                """,
                (qualification_id,),
            ).fetchone()
            if qualification is None:
                raise HarnessError(f"missing qualification: {qualification_id}")
            if str(qualification["cycle_id"]) != cycle_id:
                raise HarnessError(
                    "cross-cycle gate qualification is not allowed: "
                    f"{qualification_id} cycle={qualification['cycle_id']} "
                    f"current={cycle_id}"
                )
            latest_qualification = latest_acceptance_target_qualification(
                conn,
                cycle_id=cycle_id,
                acceptance_id=str(qualification["acceptance_id"]),
                target_id=str(qualification["target_id"]),
            )
            if (
                latest_qualification is None
                or str(latest_qualification["id"]) != qualification_id
            ):
                raise HarnessError(
                    "[qualification-stale] qualification is superseded: "
                    f"{qualification_id} latest="
                    f"{latest_qualification['id'] if latest_qualification else 'missing'}"
                )
            if str(qualification["acceptance_status"]) != "active":
                raise HarnessError(
                    f"qualification acceptance is not active: {qualification_id}"
                )
            if int(qualification["acceptance_revision"]) != int(
                qualification["current_acceptance_revision"]
            ):
                raise HarnessError(
                    "[qualification-stale] "
                    f"qualification {qualification_id} acceptance revision is stale"
                )
            live_target = {
                field: qualification[field]
                for field in (
                    "kind",
                    "command_template",
                    "stack_profile",
                    "container_image",
                    "requires_sandbox",
                    "requires_no_network",
                    "result_format",
                    "result_path",
                )
            }
            if target_definition_digest(live_target) != str(
                qualification["target_definition_sha256"]
            ):
                raise HarnessError(
                    "[qualification-stale] "
                    f"qualification {qualification_id} target definition is stale"
                )
            evidence = conn.execute(
                """
                select v.*
                from validations v
                join validation_executions ve
                  on ve.validation_id = v.id
                 and ve.cycle_id = v.cycle_id
                 and ve.candidate_sha = v.candidate_sha
                join executions e
                  on e.id = ve.execution_id
                 and e.cycle_id = ve.cycle_id
                 and e.candidate_sha = ve.candidate_sha
                where v.qualification_id = ?
                  and v.cycle_id = ? and v.candidate_sha = ?
                  and v.acceptance_id = ?
                  and v.validation_status = 'active' and v.result = 'pass'
                  and e.target_id = ?
                  and e.target_definition_sha256 = ?
                  and e.exit_code = 0 and e.executed_count > 0
                  and e.semantic_status = 'pass'
                limit 1
                """,
                (
                    qualification_id,
                    cycle_id,
                    candidate_sha,
                    qualification["acceptance_id"],
                    qualification["target_id"],
                    qualification["target_definition_sha256"],
                ),
            ).fetchone()
            if evidence is None:
                raise HarnessError(
                    "qualification has no passing current-candidate execution evidence: "
                    f"{qualification_id}"
                )
            from core.delivery import qualified_validation_execution_issues

            evidence_issues = qualified_validation_execution_issues(
                conn,
                root,
                evidence,
                qualification,
                candidate_sha,
            )
            if evidence_issues:
                raise HarnessError(
                    "qualification has ineligible current-candidate execution evidence: "
                    f"{qualification_id}: {'; '.join(evidence_issues)}"
                )
            qualification_rows.append(qualification)
        gate_id = f"GATE-{uuid.uuid4().hex}"
        sequence = int(conn.execute("select coalesce(max(sequence), 0) + 1 from quality_gates").fetchone()[0])
        previous_gate = conn.execute(
            """
            select id from quality_gates
            where cycle_id = ? and candidate_sha = ? and gate_status = 'active'
            order by sequence desc limit 1
            """,
            (cycle_id, candidate_sha),
        ).fetchone()
        conn.execute(
            """
            insert into quality_gates
            (id, sequence, cycle_id, candidate_sha, gate_status, superseded_by,
             gate, producer_context_id, reviewer_context_id, review_status, result,
             blocking_findings, residual_risk, reviewed_revision, created_at)
            values (?, ?, ?, ?, 'active', null, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                gate_id,
                sequence,
                cycle_id,
                candidate_sha,
                gate,
                producer_context_id,
                reviewer_context_id,
                review_status,
                result,
                blocking_findings,
                residual_risk,
                project_revision,
                now_iso(),
            ),
        )
        if previous_gate:
            conn.execute(
                "update quality_gates set gate_status = 'superseded', superseded_by = ? where id = ? and gate_status = 'active'",
                (gate_id, previous_gate["id"]),
            )
        for finding in finding_rows:
            conn.execute(
                "insert or ignore into quality_gate_findings (gate_id, finding_id) "
                "values (?, ?)",
                (gate_id, finding["id"]),
            )
        for qualification in qualification_rows:
            conn.execute(
                """
                insert into quality_gate_qualifications
                (gate_id, qualification_id, cycle_id, candidate_sha)
                values (?, ?, ?, ?)
                """,
                (
                    gate_id,
                    qualification["id"],
                    cycle_id,
                    candidate_sha,
                ),
            )
        if result == "pass":
            resolve_invalidations(conn, target_type="quality_gate")
        after = conn.execute("select * from quality_gates where id = ?", (gate_id,)).fetchone()
        emit_audit_event(
            conn,
            "quality_gate_recorded",
            entity_type="quality_gate",
            entity_id=gate_id,
            before=None,
            after=row_snapshot(after),
            actor="root-controller",
            command="gate record",
            extra={
                "gate": gate,
                "result": result,
                "review_status": review_status,
                "qualification_id": ",".join(qualification_ids),
            },
        )
    render_affected(root, "gates", "test-targets")


@_project_mutation
def record_delivery(
    root: Path,
    scope: str,
    *,
    acceptance: str = "",
    changed_files: str = "",
    validation: str = "",
    qa: str = "",
    failure_mode_coverage: str = "",
    quality_gate: str = "",
    data_config_notes: str = "",
    known_gaps: str = "",
    handoff: str = "",
) -> None:
    guard_schema("validate_delivery", scope)
    captured_candidate = ""

    def revalidate_delivery_before_commit(
        commit_conn: sqlite3.Connection,
    ) -> None:
        if not captured_candidate:
            return
        if current_candidate_sha(root) != captured_candidate:
            raise HarnessError(
                "delivery record blocked: stale candidate: project source changed "
                "before delivery commit"
            )
        from core.delivery import evaluate_delivery_prerequisites

        consistency = evaluate_delivery_prerequisites(
            commit_conn,
            root,
            mode="delivered-consistency",
            is_expired=is_expired,
        )
        if consistency:
            raise HarnessError(
                "delivery record consistency blocked before commit: "
                + "; ".join(blocker.render() for blocker in consistency)
            )

    with transaction(
        root,
        before_commit_check=revalidate_delivery_before_commit,
    ) as conn:
        active_project = project_row(conn)
        if int(active_project["schema_version"]) != SCHEMA_VERSION:
            raise HarnessError(
                "delivery record requires active schema "
                f"{SCHEMA_VERSION}; current={active_project['schema_version']}; "
                "run the supported side-by-side migration first"
            )
        from core.delivery import (
            evaluate_delivery_prerequisites,
            evaluate_delivery_report,
        )

        cycle = current_cycle_row(conn)
        report = evaluate_delivery_report(
            conn,
            root,
            mode="record-delivery",
            is_expired=is_expired,
        )
        if report.blockers:
            raise HarnessError(
                "delivery record blocked: "
                + "; ".join(blocker.render() for blocker in report.blockers)
            )
        if str(cycle["status"]) != "active":
            raise HarnessError(
                f"delivery record requires active current cycle, current={cycle['status']}"
            )
        candidate_sha = report.candidate_sha
        captured_candidate = candidate_sha
        trust = report.trust
        if current_candidate_sha(root) != candidate_sha:
            raise HarnessError(
                "delivery record blocked: stale candidate: project source changed during delivery validation"
            )
        delivery_id = str(uuid.uuid4())
        decision_status = (
            trust.status
            if trust.status in {"accepted-risk", "same-context-degraded"}
            else "delivered"
        )
        conn.execute(
            """
            insert into deliveries
            (id, cycle_id, candidate_sha, scope, acceptance, changed_files, validation, qa, failure_mode_coverage, quality_gate,
             data_config_notes, known_gaps, handoff, decision_status, created_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                delivery_id,
                cycle["id"],
                candidate_sha,
                scope,
                acceptance,
                changed_files,
                validation,
                qa,
                failure_mode_coverage,
                quality_gate,
                data_config_notes,
                known_gaps,
                handoff,
                decision_status,
                now_iso(),
            ),
        )
        for acceptance_id in report.proven_acceptance_ids:
            conn.execute(
                "insert into delivery_acceptance (delivery_id, cycle_id, acceptance_id) values (?, ?, ?)",
                (delivery_id, cycle["id"], acceptance_id),
            )
        after = conn.execute("select * from deliveries where id = ?", (delivery_id,)).fetchone()
        now = now_iso()
        conn.execute(
            "update delivery_cycles set status = 'delivered', candidate_sha = ?, closed_at = coalesce(nullif(closed_at, ''), ?), updated_at = ? where id = ?",
            (candidate_sha, now, now, cycle["id"]),
        )
        emit_audit_event(
            conn,
            "delivery_recorded",
            entity_type="delivery",
            entity_id=delivery_id,
            before=None,
            after=row_snapshot(after),
            command="delivery record",
            extra={"scope": scope, "decision_status": decision_status},
        )
        if current_candidate_sha(root) != candidate_sha:
            raise HarnessError(
                "delivery record blocked: stale candidate: project source changed while recording delivery"
            )
        consistency_blockers = evaluate_delivery_prerequisites(
            conn,
            root,
            mode="delivered-consistency",
            is_expired=is_expired,
        )
        if consistency_blockers:
            raise HarnessError(
                "delivery record consistency blocked: "
                + "; ".join(
                    blocker.render() for blocker in consistency_blockers
                )
            )
    render_affected(root, "deliveries", "traceability")


# Legacy schema 27/28 -> 29 staging inventory. These names are intentionally
# confined to the isolated migration path; schema 30 filters task-attempt and
# dispatch facts instead of copying them into the active local Kernel.
SCHEMA29_FACT_TABLES = ("requirements", "acceptance", "failure_modes", "tasks")
SCHEMA29_RELATION_TABLES = (
    "requirement_acceptance",
    "failure_mode_acceptance",
    "task_acceptance",
    "task_failure_modes",
    "task_dependencies",
    "task_test_targets",
    "validation_failure_modes",
    "delivery_acceptance",
    "task_attempts",
    "dispatch_assignments",
)
LEGACY_TASK_STATUS_MAP = {
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


def schema29_fact_uid(table: str, cycle_id: str, local_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"codex-project-harness:{table}:{cycle_id}:{local_id}"))


def insert_snapshot_rows(conn: sqlite3.Connection, table: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    columns = table_columns(conn, table)
    for row in rows:
        writable = [column for column in columns if column in row]
        conn.execute(
            f"insert into {table} ({','.join(writable)}) values ({','.join('?' for _ in writable)})",
            [row[column] for column in writable],
        )


def repair_schema29_relation_cycles(conn: sqlite3.Connection) -> None:
    relation_specs = {
        "requirement_acceptance": (("requirements", "requirement_id"), ("acceptance", "acceptance_id")),
        "failure_mode_acceptance": (("failure_modes", "failure_mode_id"), ("acceptance", "acceptance_id")),
        "task_acceptance": (("tasks", "task_id"), ("acceptance", "acceptance_id")),
        "task_failure_modes": (("tasks", "task_id"), ("failure_modes", "failure_mode_id")),
        "task_dependencies": (("tasks", "task_id"), ("tasks", "depends_on")),
        "task_test_targets": (("tasks", "task_id"),),
        "task_attempts": (("tasks", "task_id"),),
        "dispatch_assignments": (("tasks", "task_id"),),
    }
    for relation, parents in relation_specs.items():
        rows = conn.execute(f"select rowid as migration_rowid, * from {relation}").fetchall()
        for row in rows:
            candidate_cycles: set[str] | None = None
            for parent_table, relation_column in parents:
                parent_cycles = {
                    str(parent["cycle_id"])
                    for parent in conn.execute(
                        f"select cycle_id from {parent_table} where id = ?",
                        (row[relation_column],),
                    )
                    if parent["cycle_id"]
                }
                candidate_cycles = parent_cycles if candidate_cycles is None else candidate_cycles & parent_cycles
            candidates = candidate_cycles or set()
            current_cycle = str(row["cycle_id"] or "")
            if current_cycle in candidates:
                continue
            if len(candidates) != 1:
                raise HarnessError(
                    f"schema29 migration cannot infer relation cycle: {relation}:rowid={row['migration_rowid']}"
                )
            conn.execute(
                f"update {relation} set cycle_id = ? where rowid = ?",
                (next(iter(candidates)), row["migration_rowid"]),
            )

    scoped_specs = {
        "validation_failure_modes": ("validations", "validation_id", "failure_modes", "failure_mode_id"),
        "delivery_acceptance": ("deliveries", "delivery_id", "acceptance", "acceptance_id"),
    }
    for relation, (owner_table, owner_column, parent_table, parent_column) in scoped_specs.items():
        rows = conn.execute(f"select rowid as migration_rowid, * from {relation}").fetchall()
        for row in rows:
            owner_cycles = {
                str(owner["cycle_id"])
                for owner in conn.execute(
                    f"select cycle_id from {owner_table} where id = ?",
                    (row[owner_column],),
                )
                if owner["cycle_id"]
            }
            valid_cycles = {
                cycle_id
                for cycle_id in owner_cycles
                if conn.execute(
                    f"select 1 from {parent_table} where cycle_id = ? and id = ?",
                    (cycle_id, row[parent_column]),
                ).fetchone()
            }
            current_cycle = str(row["cycle_id"] or "")
            if current_cycle in valid_cycles:
                continue
            if len(valid_cycles) != 1:
                raise HarnessError(
                    f"schema29 migration cannot infer relation cycle: {relation}:rowid={row['migration_rowid']}"
                )
            conn.execute(
                f"update {relation} set cycle_id = ? where rowid = ?",
                (next(iter(valid_cycles)), row["migration_rowid"]),
            )


def migrate_cycle_identity_schema29(conn: sqlite3.Connection, *, fallback_cycle_override: str = "") -> None:
    if "uid" in table_columns(conn, "requirements"):
        blank_fact_cycle = any(
            conn.execute(f"select 1 from {table} where cycle_id = '' limit 1").fetchone()
            for table in SCHEMA29_FACT_TABLES
        )
        if not blank_fact_cycle:
            repair_schema29_relation_cycles(conn)
        return
    project = conn.execute("select current_cycle_id from project where id = 1").fetchone()
    fallback_cycle = fallback_cycle_override or (
        str(project["current_cycle_id"] or DEFAULT_CYCLE_ID) if project else DEFAULT_CYCLE_ID
    )
    now = now_iso()
    fallback_status = "archived" if fallback_cycle == LEGACY_CYCLE_ID else "active"
    fallback_phase = "archived" if fallback_cycle == LEGACY_CYCLE_ID else "intake"
    conn.execute(
        """
        insert into delivery_cycles
        (id, name, goal, status, phase, base_ref, candidate_sha, started_at, closed_at, created_at, updated_at)
        values (?, 'Migrated Delivery Cycle', 'Schema 29 identity migration.', ?, ?, '', '', ?, ?, ?, ?)
        on conflict(id) do nothing
        """,
        (fallback_cycle, fallback_status, fallback_phase, now, now if fallback_status == "archived" else "", now, now),
    )
    if fallback_cycle == LEGACY_CYCLE_ID:
        conn.execute(
            """
            insert into delivery_cycles
            (id, name, goal, status, phase, base_ref, candidate_sha, started_at, closed_at, created_at, updated_at)
            values (?, 'Current Delivery Cycle', 'Current active delivery candidate.', 'active', 'intake', '', '', ?, '', ?, ?)
            on conflict(id) do nothing
            """,
            (DEFAULT_CYCLE_ID, now, now, now),
        )
    session_contexts: dict[str, str] = {}
    if conn.execute(
        "select 1 from sqlite_master where type='table' and name='agent_sessions'"
    ).fetchone() and {"session_id", "context_id"}.issubset(
        table_columns(conn, "agent_sessions")
    ):
        for session in conn.execute(
            "select session_id, context_id from agent_sessions"
        ).fetchall():
            session_id = str(session["session_id"] or "").strip()
            context_id = str(session["context_id"] or "").strip()
            if session_id and context_id:
                session_contexts[session_id] = context_id

    fact_rows = {table: table_rows(conn, table) for table in SCHEMA29_FACT_TABLES}
    for rows in fact_rows.values():
        for row in rows:
            if not row.get("cycle_id"):
                row["cycle_id"] = fallback_cycle
    for row in fact_rows["tasks"]:
        source_status = str(row.get("status") or "")
        if source_status not in LEGACY_TASK_STATUS_MAP:
            raise HarnessError(
                f"schema29 migration cannot normalize task status: {row.get('id')}={source_status}"
            )
        row["status"] = LEGACY_TASK_STATUS_MAP[source_status]
        submitted_context_id = str(row.get("submitted_context_id") or "").strip()
        submitted_session_id = str(row.get("submitted_session_id") or "").strip()
        row["submitted_context_id"] = submitted_context_id or session_contexts.get(
            submitted_session_id,
            "",
        )
    relation_rows = {table: table_rows(conn, table) for table in SCHEMA29_RELATION_TABLES}
    cycle_maps = {
        table: {str(row["id"]): str(row["cycle_id"]) for row in rows}
        for table, rows in fact_rows.items()
    }

    def require_cycle(table: str, local_id: str) -> str:
        cycle_id = cycle_maps[table].get(str(local_id), "")
        if not cycle_id:
            raise HarnessError(f"schema29 migration missing cycle identity: {table}:{local_id}")
        return cycle_id

    for row in relation_rows["requirement_acceptance"]:
        req_cycle = require_cycle("requirements", row["requirement_id"])
        acceptance_cycle = require_cycle("acceptance", row["acceptance_id"])
        if req_cycle != acceptance_cycle:
            raise HarnessError("schema29 migration found cross-cycle requirement acceptance link")
        row["cycle_id"] = req_cycle
    for row in relation_rows["failure_mode_acceptance"]:
        fm_cycle = require_cycle("failure_modes", row["failure_mode_id"])
        acceptance_cycle = require_cycle("acceptance", row["acceptance_id"])
        if fm_cycle != acceptance_cycle:
            raise HarnessError("schema29 migration found cross-cycle failure-mode acceptance link")
        row["cycle_id"] = fm_cycle
    for table in ["task_acceptance", "task_failure_modes"]:
        other_table = "acceptance" if table == "task_acceptance" else "failure_modes"
        other_column = "acceptance_id" if table == "task_acceptance" else "failure_mode_id"
        for row in relation_rows[table]:
            task_cycle = require_cycle("tasks", row["task_id"])
            other_cycle = require_cycle(other_table, row[other_column])
            if task_cycle != other_cycle:
                raise HarnessError(f"schema29 migration found cross-cycle {table} link")
            row["cycle_id"] = task_cycle
    for row in relation_rows["task_dependencies"]:
        task_cycle = require_cycle("tasks", row["task_id"])
        dependency_cycle = require_cycle("tasks", row["depends_on"])
        if task_cycle != dependency_cycle:
            raise HarnessError("schema29 migration found cross-cycle task dependency")
        row["cycle_id"] = task_cycle
    for row in relation_rows["task_test_targets"]:
        row["cycle_id"] = require_cycle("tasks", row["task_id"])
    validation_cycles = {
        str(row["id"]): str(row["cycle_id"])
        for row in conn.execute("select id, cycle_id from validations")
    }
    for row in relation_rows["validation_failure_modes"]:
        fm_cycle = require_cycle("failure_modes", row["failure_mode_id"])
        validation_cycle = validation_cycles.get(str(row["validation_id"]), "")
        if not validation_cycle or fm_cycle != validation_cycle:
            raise HarnessError("schema29 migration found cross-cycle validation failure-mode link")
        row["cycle_id"] = fm_cycle
    delivery_cycles = {
        str(row["id"]): str(row["cycle_id"])
        for row in conn.execute("select id, cycle_id from deliveries")
    }
    for row in relation_rows["delivery_acceptance"]:
        acceptance_cycle = require_cycle("acceptance", row["acceptance_id"])
        delivery_cycle = delivery_cycles.get(str(row["delivery_id"]), "")
        if not delivery_cycle or acceptance_cycle != delivery_cycle:
            raise HarnessError("schema29 migration found cross-cycle delivery acceptance link")
        row["cycle_id"] = acceptance_cycle
    for table in ["task_attempts", "dispatch_assignments"]:
        for row in relation_rows[table]:
            row["cycle_id"] = require_cycle("tasks", row["task_id"])

    conn.execute("pragma defer_foreign_keys = on")
    for table in SCHEMA29_RELATION_TABLES:
        conn.execute(f"drop table {table}")
    for table in reversed(SCHEMA29_FACT_TABLES):
        conn.execute(f"drop table {table}")
    create_schema29(conn)

    for table, rows in fact_rows.items():
        for row in rows:
            row["uid"] = schema29_fact_uid(table, str(row["cycle_id"]), str(row["id"]))
        insert_snapshot_rows(conn, table, rows)
    for table, rows in relation_rows.items():
        insert_snapshot_rows(conn, table, rows)

    for table, rows in fact_rows.items():
        restored = int(conn.execute(f"select count(*) from {table}").fetchone()[0])
        if restored != len(rows):
            raise HarnessError(f"schema29 migration row count mismatch: {table} expected={len(rows)} actual={restored}")
    for table, rows in relation_rows.items():
        restored = int(conn.execute(f"select count(*) from {table}").fetchone()[0])
        if restored != len(rows):
            raise HarnessError(f"schema29 migration link count mismatch: {table} expected={len(rows)} actual={restored}")


def migrate_quality_gate_schema29(conn: sqlite3.Connection) -> None:
    rows = [
        row_snapshot(row) or {}
        for row in conn.execute("select rowid as legacy_rowid, * from quality_gates order by created_at, rowid")
    ]
    timestamp_counts: dict[tuple[str, str, str], int] = {}
    by_candidate: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        timestamp_key = (str(row["cycle_id"]), str(row["candidate_sha"]), str(row["created_at"]))
        timestamp_counts[timestamp_key] = timestamp_counts.get(timestamp_key, 0) + 1
        by_candidate.setdefault((str(row["cycle_id"]), str(row["candidate_sha"])), []).append(row)
    for sequence, row in enumerate(rows, start=1):
        conn.execute("update quality_gates set sequence = ? where id = ?", (sequence, row["id"]))
    for candidate_rows in by_candidate.values():
        for index, row in enumerate(candidate_rows):
            timestamp_key = (str(row["cycle_id"]), str(row["candidate_sha"]), str(row["created_at"]))
            ambiguous = timestamp_counts[timestamp_key] > 1
            later = candidate_rows[index + 1 :]
            status = "legacy-ambiguous" if ambiguous else ("superseded" if later else "active")
            superseded_by = str(later[0]["id"]) if status == "superseded" else ""
            conn.execute(
                "update quality_gates set gate_status = ?, superseded_by = ? where id = ?",
                (status, superseded_by, row["id"]),
            )
    conn.execute("create unique index if not exists quality_gates_sequence_unique on quality_gates(sequence)")


def downgrade_legacy_trust_schema29(conn: sqlite3.Connection) -> None:
    for table in ["agent_sessions", "session_attestations"]:
        if not conn.execute(
            "select 1 from sqlite_master where type = 'table' and name = ?",
            (table,),
        ).fetchone():
            continue
        columns = set(table_columns(conn, table))
        if not {"origin", "trust_level"} <= columns:
            continue
        if {"effective_trust", "receipt_provenance"} <= columns:
            conn.execute(
                f"""
                update {table}
                set effective_trust = case when origin = 'connector' or trust_level = 'connector'
                      then 'legacy-untrusted' else trust_level end,
                    receipt_provenance = case when origin = 'connector' or trust_level = 'connector'
                      then 'schema28-unprovable' else 'schema28-local' end,
                    origin = case when origin = 'connector' then 'manual' else origin end,
                    trust_level = case when trust_level = 'connector' then 'local-only' else trust_level end
                """
            )
        else:
            conn.execute(
                f"""
                update {table}
                set origin = case when origin = 'connector' then 'manual' else origin end,
                    trust_level = case when trust_level = 'connector' then 'local-only' else trust_level end
                """
            )
    for table in ["ci_verifications", "external_session_verifications"]:
        if not conn.execute(
            "select 1 from sqlite_master where type = 'table' and name = ?",
            (table,),
        ).fetchone():
            continue
        columns = set(table_columns(conn, table))
        if not {"origin", "token_status", "effective_trust", "receipt_provenance"} <= columns:
            continue
        conn.execute(
            f"""
            update {table}
            set effective_trust = case when origin = 'connector' or token_status = 'hmac-valid'
                  then 'legacy-untrusted' else 'local-only' end,
                receipt_provenance = case when origin = 'connector' or token_status = 'hmac-valid'
                  then 'schema28-unprovable' else 'schema28-local' end
            """
        )
    if "review_trust_level" in table_columns(conn, "quality_gates"):
        conn.execute(
            "update quality_gates set review_trust_level = 'legacy-untrusted' where review_trust_level = 'connector'"
        )


def migrate_schema29(conn: sqlite3.Connection, source_version: int) -> None:
    if source_version >= 29:
        return
    migrate_cycle_identity_schema29(conn)
    migrate_quality_gate_schema29(conn)
    downgrade_legacy_trust_schema29(conn)
    foreign_key_errors = conn.execute("pragma foreign_key_check").fetchall()
    if foreign_key_errors:
        details = "; ".join(":".join(str(value) for value in row) for row in foreign_key_errors[:5])
        raise HarnessError(f"schema29 migration foreign key check failed: {len(foreign_key_errors)} issue(s): {details}")


def migrate_legacy_to_schema29(root: Path, source_version: int) -> None:
    """Isolated v1 staging helper used only by the schema 30 converter."""

    if source_version not in {27, 28}:
        raise HarnessError(f"unsupported isolated legacy migration source: {source_version}")
    with get_store(root).transaction() as conn:
        project = conn.execute(
            "select schema_version from project where id=1"
        ).fetchone()
        if not project or int(project["schema_version"]) != source_version:
            observed = "missing" if not project else str(project["schema_version"])
            raise HarnessError(
                f"isolated legacy migration source mismatch: expected={source_version} actual={observed}"
            )
        create_schema29(conn)
        if "uid" not in table_columns(conn, "requirements"):
            migrate_cycle_identity_schema29(
                conn,
                fallback_cycle_override=LEGACY_CYCLE_ID if source_version < 25 else "",
            )
        migrate_schema29(conn, source_version)
        conn.execute(
            "insert into migrations (from_version, to_version, applied_at) values (?, 29, ?)",
            (source_version, now_iso()),
        )
        updated = conn.execute(
            """
            update project
            set schema_version=29, runtime_version='4.18.0',
                revision=revision+1, updated_at=?
            where id=1 and schema_version=?
            """,
            (now_iso(), source_version),
        )
        if updated.rowcount != 1:
            raise HarnessError(
                f"isolated legacy migration source changed: {source_version}"
            )
        foreign_key_errors = conn.execute("pragma foreign_key_check").fetchall()
        if foreign_key_errors:
            raise HarnessError(
                f"isolated schema29 foreign key validation failed: {len(foreign_key_errors)} issue(s)"
            )


def validated_migration_path(root: Path, from_version: str, to_version: int) -> tuple[int, list[tuple[int, int]]]:
    try:
        requested_from = int(from_version)
    except ValueError as exc:
        raise HarnessError(f"invalid migration source version: {from_version}") from exc
    current_schema_issues: list[str] = []
    store = get_store(root)
    if isinstance(store, InMemoryStore):
        connection_scope = store.connection()
        with connection_scope as conn:
            project_exists = conn.execute("select 1 from sqlite_master where type = 'table' and name = 'project'").fetchone()
            if not project_exists:
                raise HarnessError("migration requires an initialized runtime")
            row = conn.execute("select schema_version from project where id = 1").fetchone()
            if not row:
                raise HarnessError("migration requires project state")
            actual = int(row["schema_version"])
            if actual == SCHEMA_VERSION:
                try:
                    current_schema_issues = runtime_schema_issues(conn)
                except sqlite3.Error as exc:
                    current_schema_issues = [f"schema inspection failed: {exc}"]
    else:
        raise_if_project_migration_announced(root)
        with ProjectFS.open(root) as project_fs:
            project_fs.audit(
                (*SqliteStore._db_family(), MIGRATION_SENTINEL_PATH),
                allow_missing=True,
            )
            snapshot = project_fs._snapshot(DB_PATH, allow_missing=True)
            if not snapshot.exists:
                raise HarnessError("migration requires an initialized runtime")
            with _verified_sqlite_connection(
                project_fs,
                DB_PATH,
                access="ro",
                immutable=True,
            ) as conn:
                conn.row_factory = sqlite3.Row
                project_exists = conn.execute(
                    "select 1 from sqlite_master where type='table' and name='project'"
                ).fetchone()
                if not project_exists:
                    raise HarnessError("migration requires an initialized runtime")
                row = conn.execute(
                    "select schema_version from project where id=1"
                ).fetchone()
                if not row:
                    raise HarnessError("migration requires project state")
                actual = int(row["schema_version"])
                if actual == SCHEMA_VERSION:
                    try:
                        current_schema_issues = runtime_schema_issues(conn)
                    except sqlite3.Error as exc:
                        current_schema_issues = [
                            f"schema inspection failed: {exc}"
                        ]
                project_fs._assert_unchanged(DB_PATH, snapshot)
    if requested_from != actual:
        raise HarnessError(f"migration source mismatch: expected database schema {actual}, received {requested_from}")
    if to_version < actual:
        raise HarnessError(f"schema downgrade is not supported: {actual}->{to_version}")
    if to_version != SCHEMA_VERSION:
        raise HarnessError(f"unregistered migration target: {actual}->{to_version}; supported target is {SCHEMA_VERSION}")
    if actual == SCHEMA_VERSION:
        if current_schema_issues:
            raise HarnessError("current schema is incomplete: " + "; ".join(current_schema_issues[:5]))
        return actual, []
    if actual not in REGISTERED_SCHEMA_SOURCES:
        raise HarnessError(f"unregistered migration source: {actual}")
    return actual, [(actual, SCHEMA_VERSION)]


def migrate(root: Path, from_version: str, to_version: int, *, dry_run: bool = False) -> dict[str, Any] | None:
    _, path = validated_migration_path(root, from_version, to_version)
    if not path:
        if dry_run:
            return {
                "dry_run": True,
                "imported": {"schema_migration": 0},
                "skipped": {},
                "unrecognized": [],
            }
        render_all(root)
        return None

    from core.local_core_migration import (
        dry_run_project_to_active_schema,
        migrate_project_to_active_schema,
    )

    def validate_staging(staging_path: Path) -> None:
        with ProjectFS.open(root) as active_project_fs:
            staging_relative = active_project_fs.relative_to_root(
                staging_path
            )
            staging_snapshot = active_project_fs._snapshot(
                staging_relative,
                allow_missing=False,
            )
            _before_staging_validation_snapshot_read(
                active_project_fs,
                staging_relative,
            )
            staging_payload = active_project_fs.read_bytes(
                staging_relative
            )
            active_project_fs._assert_unchanged(
                staging_relative,
                staging_snapshot,
            )
        with tempfile.TemporaryDirectory(
            prefix="kafa-projection-dry-run-"
        ) as temp:
            staging_root = Path(temp)
            staging_db = staging_root / DB_PATH
            ensure_parent(staging_db)
            staging_db.write_bytes(staging_payload)
            with connection(staging_root) as conn:
                issues = runtime_schema_issues(conn) + [
                    str(issue) for issue in full_invariant_issues(conn, staging_root)
                ]
            if issues:
                raise HarnessError("staging migration invariant failed: " + "; ".join(issues))
            render_all(staging_root)

    def validate_active(_active_path: Path) -> None:
        issues = doctor(root, require_views=False, require_project_files=False)
        if issues:
            raise HarnessError("post-activation database doctor failed: " + "; ".join(issues))
        render_all(root)
        issues = doctor(root)
        if issues:
            raise HarnessError("post-activation projection verification failed: " + "; ".join(issues))

    if dry_run:
        report = dry_run_project_to_active_schema(
            root,
            staging_validator=validate_staging,
        )
        imported = {
            "schema_migration": 1,
            **{
                f"table:{table}": int(count)
                for table, count in sorted(report.staging_row_counts.items())
            },
            "normalized_failure_modes": int(
                report.normalized_failure_mode_count
            ),
        }
        return {
            "dry_run": True,
            "source_version": report.source_version,
            "target_version": report.target_version,
            "source_sha256": report.source_sha256,
            "staging_sha256": report.staging_sha256,
            "imported": imported,
            "skipped": dict(report.retired_row_counts),
            "unrecognized": [],
        }

    migrate_project_to_active_schema(
        root,
        staging_validator=validate_staging,
        active_validator=validate_active,
    )
    return None

def doctor(
    root: Path,
    *,
    require_views: bool = True,
    require_project_files: bool = True,
) -> list[str]:
    store = get_store(root)
    with runtime_path_audit(root, store=store) as project_fs:
        config_issues: list[str] = []
        integrity_issues: list[str] = []
        schema_invariant_issues: list[str] = []
        projection_issues: list[str] = []
        if isinstance(store, SqliteStore):
            if project_fs is None:
                return ["missing sqlite state: .ai-team/state/harness.db"]
            if not project_fs._snapshot(
                DB_PATH,
                allow_missing=True,
            ).exists:
                return ["missing sqlite state: .ai-team/state/harness.db"]
        if require_project_files:
            config_issues.extend(
                _gitignore_runtime_issues(project_fs)
                if project_fs is not None
                else gitignore_runtime_issues(root)
            )
        with store.connection() as conn:
            try:
                project = project_row(conn)
            except HarnessError as exc:
                schema_invariant_issues.append(str(exc))
            else:
                if int(project["schema_version"]) != SCHEMA_VERSION:
                    schema_invariant_issues.append(f"schema version mismatch: expected {SCHEMA_VERSION}, actual {project['schema_version']}")
                if project["runtime_version"] != RUNTIME_VERSION:
                    schema_invariant_issues.append(f"runtime version mismatch: expected {RUNTIME_VERSION}, actual {project['runtime_version']}")
            integrity = conn.execute("pragma integrity_check").fetchone()[0]
            if integrity != "ok":
                integrity_issues.append(f"sqlite integrity check failed: {integrity}")
            foreign_key_errors = conn.execute("pragma foreign_key_check").fetchall()
            if foreign_key_errors:
                integrity_issues.append(f"sqlite foreign key check failed: {len(foreign_key_errors)} issue(s)")
            schema_invariant_issues.extend(runtime_schema_issues(conn))
            from core.invariant_checker import check_runtime_invariants

            schema_invariant_issues.extend(check_runtime_invariants(conn, root))
            if require_views:
                from core.projections import projection_content_issues

                projection_issues.extend(projection_content_issues(root))
        return [
            *integrity_issues,
            *schema_invariant_issues,
            *projection_issues,
            *config_issues,
        ]


def _doctor_issue_code(issue: str) -> str:
    lowered = issue.lower()
    if "gitignore" in lowered or "runtime state is tracked by git" in lowered:
        return "gitignore-missing"
    if "schema version mismatch" in lowered:
        return "schema-version-mismatch"
    if "runtime version mismatch" in lowered:
        return "runtime-version-mismatch"
    if "sqlite integrity" in lowered:
        return "sqlite-integrity"
    if "foreign key" in lowered:
        return "foreign-key-integrity"
    if "projection" in lowered:
        return "projection-invalid"
    if "missing sqlite state" in lowered:
        return "state-missing"
    return "doctor-issue"


def doctor_operator_report(root: Path) -> OperatorEnvelope:
    """Run the complete doctor once and project ordered operator output."""

    try:
        with _operator_runtime_snapshot(root) as (initialized, database_exists):
            if not initialized:
                return (
                    _unavailable_existing_state_report(root)
                    if database_exists
                    else uninitialized_operator_report(root)
                )
            issues = [str(issue) for issue in doctor(root)]
    except OPERATOR_STATE_READ_ERRORS as exc:
        return operator_error_report(root, exc, allow_init=False)

    return build_operator_envelope(
        state="unhealthy" if issues else "healthy",
        blockers=tuple(
            {"code": _doctor_issue_code(issue), "message": issue}
            for issue in issues
        ),
        actions=(
            (_render_harness_command(root, "repair", "--dry-run"),)
            if issues
            else ()
        ),
        details={
            "initialized": True,
            "issues": issues,
        },
    )


def runtime_schema_issues(conn: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    tables = {
        str(row[0])
        for row in conn.execute(
            "select name from sqlite_master where type='table'"
        )
    }
    if tables != ACTIVE_SCHEMA_CATALOG_TABLES:
        issues.append(
            f"schema {SCHEMA_VERSION} table inventory mismatch: "
            f"missing={sorted(ACTIVE_SCHEMA_CATALOG_TABLES - tables)} "
            f"extra={sorted(tables - ACTIVE_SCHEMA_CATALOG_TABLES)}"
        )
        return issues
    enum_checks = [
        ("delivery_cycles", "status", DELIVERY_CYCLE_STATUSES, "delivery cycle status"),
        ("delivery_cycles", "phase", set(PHASES), "delivery cycle phase"),
        ("requirements", "status", REQUIREMENT_STATUSES, "requirement status"),
        ("acceptance", "status", ACCEPTANCE_STATUSES, "acceptance status"),
        ("tasks", "status", TASK_STATUSES, "task status"),
        ("failure_modes", "risk", {"low", "medium", "high", "critical"}, "failure mode risk"),
        ("failure_modes", "status", FAILURE_MODE_STATUSES, "failure mode status"),
        ("validations", "result", {"pass", "fail", "blocked", "partial"}, "validation result"),
        ("validations", "validation_status", VALIDATION_STATUSES, "validation status"),
        ("findings", "severity", {"low", "medium", "high", "critical"}, "finding severity"),
        ("findings", "status", {"open", "resolved", "accepted", "false-positive"}, "finding status"),
        ("quality_gates", "gate_status", {"active", "superseded", "legacy-ambiguous"}, "quality gate status"),
        ("quality_gates", "review_status", {"reviewed-local", "same-context-degraded"}, "quality gate review status"),
        ("quality_gates", "result", {"pass", "fail", "conditional", "blocked"}, "quality gate result"),
        ("deliveries", "decision_status", {"delivered", "accepted-risk", "same-context-degraded", "historical-migrated"}, "delivery decision status"),
        ("migrations", "status", {"legacy-history", "staged", "activated", "rolled-back", "failed", "rollback-incomplete", "recovery-required"}, "migration status"),
        ("outcome_observations", "kind", {"false-green-prevented", "escaped-defect", "rework"}, "outcome kind"),
        ("test_targets", "kind", TEST_TARGET_KINDS, "test target kind"),
        ("test_targets", "stack_profile", STACK_PROFILES, "test target stack profile"),
        ("test_targets", "result_format", RESULT_FORMATS, "test target result format"),
        ("executions", "result_format", RESULT_FORMATS, "execution result format"),
        ("executions", "semantic_status", {"pass", "fail"}, "execution semantic status"),
        ("executions", "runner", {"local", "container"}, "execution runner"),
        ("executions", "sandbox_status", SANDBOX_STATUSES, "execution sandbox status"),
        ("executions", "policy_status", {"allowed", "rejected"}, "execution policy status"),
        ("executions", "provenance_status", {"complete", "legacy-incomplete"}, "execution provenance status"),
    ]
    for table, column, allowed, label in enum_checks:
        placeholders = ",".join("?" for _ in allowed)
        for row in conn.execute(
            f"select id, {column} as value from {table} where {column} not in ({placeholders})",
            tuple(allowed),
        ):
            issues.append(f"invalid {label}: {table}.{row['id']}={row['value']}")
    required_columns = {
        "acceptance_target_qualifications": {
            "id", "cycle_id", "acceptance_id", "acceptance_revision",
            "target_id", "target_definition_sha256", "rationale",
            "qualified_by", "created_at",
        },
        "quality_gate_qualifications": {
            "gate_id", "qualification_id", "cycle_id", "candidate_sha",
        },
        "outcome_observations": {
            "id", "cycle_id", "kind", "value", "details", "recorded_by",
            "observed_at", "created_at",
        },
        "executions": {
            "target_definition_sha256", "platform", "runtime_executable",
            "runtime_version", "runtime_executable_sha256", "policy_version",
            "container_engine", "container_engine_version",
            "container_engine_endpoint",
            "container_image_requested", "container_image_digest",
            "provenance_status",
        },
        "validations": {"qualification_id"},
    }
    for table, expected in required_columns.items():
        actual = {
            str(row[1])
            for row in conn.execute(f"pragma table_info({table})")
        }
        missing = expected - actual
        if missing:
            issues.append(
                f"schema {SCHEMA_VERSION} column contract incomplete: "
                f"{table} missing={sorted(missing)}"
            )
    required_triggers = {
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
    actual_triggers = {
        str(row[0])
        for row in conn.execute(
            "select name from sqlite_master where type='trigger'"
        )
    }
    if missing_triggers := required_triggers - actual_triggers:
        issues.append(
            f"schema {SCHEMA_VERSION} immutable trigger contract incomplete: "
            f"missing={sorted(missing_triggers)}"
        )
    for row in conn.execute("select id, before_json, after_json from events"):
        for field in ("before_json", "after_json"):
            try:
                value = json.loads(row[field])
            except json.JSONDecodeError as exc:
                issues.append(f"invalid event {field}: {row['id']} {exc.msg}")
                continue
            if not isinstance(value, dict):
                issues.append(f"invalid event {field}: {row['id']} expected object")
    for row in conn.execute(
        "select * from executions where provenance_status = 'complete'"
    ):
        issues.extend(
            f"invalid execution provenance: executions.{row['id']}: {issue}"
            for issue in recorded_execution_provenance_issues(row)
        )
    issues.extend(schema_contract_issues(conn))
    return issues


def schema_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "schemas"


def load_schema(name: str) -> dict[str, Any]:
    return json.loads((schema_dir() / name).read_text(encoding="utf-8"))


def json_type_matches(value: Any, expected: str | list[str]) -> bool:
    from core.json_schema_contract import json_type_matches as matches

    return matches(value, expected)


def validate_object_against_schema(label: str, data: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    from core.json_schema_contract import validate_instance

    return validate_instance(label, data, schema)


def schema_entity_rows(conn: sqlite3.Connection) -> list[tuple[str, str, list[dict[str, Any]]]]:
    tasks = []
    for row in conn.execute("select * from tasks order by id"):
        data = row_snapshot(row) or {}
        data["acceptance_ids"] = [
            item["acceptance_id"]
            for item in conn.execute(
                "select acceptance_id from task_acceptance where cycle_id = ? and task_id = ? order by acceptance_id",
                (row["cycle_id"], row["id"]),
            )
        ]
        data["failure_mode_ids"] = [
            item["failure_mode_id"]
            for item in conn.execute(
                "select failure_mode_id from task_failure_modes where cycle_id = ? and task_id = ? order by failure_mode_id",
                (row["cycle_id"], row["id"]),
            )
        ]
        data["dependencies"] = [
            item["depends_on"]
            for item in conn.execute(
                "select depends_on from task_dependencies where cycle_id = ? and task_id = ? order by depends_on",
                (row["cycle_id"], row["id"]),
            )
        ]
        tasks.append(data)

    validations = []
    for row in conn.execute("select * from validations order by id"):
        data = row_snapshot(row) or {}
        data["failure_mode_ids"] = [
            item["failure_mode_id"]
            for item in conn.execute(
                "select failure_mode_id from validation_failure_modes where validation_id = ? and cycle_id = ? order by failure_mode_id",
                (row["id"], row["cycle_id"]),
            )
        ]
        validations.append(data)

    project = conn.execute(
        """
        select id, status, phase, current_cycle_id, scope_status, current_owner,
               schema_version, runtime_version, project_id, revision, updated_at
        from project where id = 1
        """
    ).fetchall()

    return [
        ("project-state.schema.json", "project", [row_snapshot(row) or {} for row in project]),
        ("delivery-cycle.schema.json", "delivery_cycles", [row_snapshot(row) or {} for row in conn.execute("select * from delivery_cycles")]),
        ("acceptance.schema.json", "acceptance", [row_snapshot(row) or {} for row in conn.execute("select * from acceptance")]),
        ("requirement.schema.json", "requirements", [row_snapshot(row) or {} for row in conn.execute("select * from requirements")]),
        ("failure-mode.schema.json", "failure_modes", [row_snapshot(row) or {} for row in conn.execute("select * from failure_modes")]),
        ("task.schema.json", "tasks", tasks),
        ("task-test-target.schema.json", "task_test_targets", [row_snapshot(row) or {} for row in conn.execute("select * from task_test_targets")]),
        ("test-target.schema.json", "test_targets", [row_snapshot(row) or {} for row in conn.execute("select * from test_targets")]),
        ("acceptance-target-qualification.schema.json", "acceptance_target_qualifications", [row_snapshot(row) or {} for row in conn.execute("select * from acceptance_target_qualifications")]),
        ("execution.schema.json", "executions", [row_snapshot(row) or {} for row in conn.execute("select * from executions")]),
        ("validation.schema.json", "validations", validations),
        ("finding.schema.json", "findings", [row_snapshot(row) or {} for row in conn.execute("select * from findings")]),
        ("quality-gate.schema.json", "quality_gates", [row_snapshot(row) or {} for row in conn.execute("select * from quality_gates")]),
        ("delivery.schema.json", "deliveries", [row_snapshot(row) or {} for row in conn.execute("select * from deliveries")]),
        ("baseline.schema.json", "baselines", [row_snapshot(row) or {} for row in conn.execute("select * from baselines")]),
        ("invalidation.schema.json", "invalidations", [row_snapshot(row) or {} for row in conn.execute("select * from invalidations")]),
        ("event.schema.json", "events", [row_snapshot(row) or {} for row in conn.execute("select * from events")]),
        ("outcome-observation.schema.json", "outcome_observations", [row_snapshot(row) or {} for row in conn.execute("select * from outcome_observations")]),
    ]


def schema_contract_issues(conn: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    for schema_name, entity, rows in schema_entity_rows(conn):
        schema = load_schema(schema_name)
        for row in rows:
            label = f"{entity}.{row.get('id', row.get('sequence', 'row'))}"
            issues.extend(validate_object_against_schema(label, row, schema))
    return issues






def _trace_show_conn(
    conn: sqlite3.Connection,
    root: Path,
    requirement_id: str | None = None,
    *,
    evidence_root: Path | None = None,
    candidate_override: str | None = None,
) -> list[str]:
    eligibility_root = evidence_root or root
    rows = trace_rows(
        conn,
        requirement_id,
        root=eligibility_root,
        candidate_override=candidate_override,
    )
    issues = traceability_issues(
        conn,
        requirement_id,
        root=eligibility_root,
        candidate_override=candidate_override,
    )
    lines = ["# Traceability", "", "| Requirement | Kind | Acceptance | Tasks | Validations | Failure Modes | Deliveries |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for row in rows:
        lines.append(
            markdown_row(
                [
                    row["requirement_id"],
                    row["requirement_kind"],
                    row["acceptance_id"],
                    row["task_ids"],
                    row["validation_ids"],
                    row["failure_mode_ids"],
                    row["delivery_ids"],
                ]
            )
        )
    lines.extend(["", "## Issues", ""])
    lines.extend(f"- {issue}" for issue in issues) if issues else lines.append("- none")
    return lines


def trace_show(
    root: Path,
    requirement_id: str | None = None,
    *,
    evidence_root: Path | None = None,
    candidate_override: str | None = None,
) -> list[str]:
    with connection(root) as conn:
        return _trace_show_conn(
            conn,
            root,
            requirement_id,
            evidence_root=evidence_root,
            candidate_override=candidate_override,
        )


def trace_validate(root: Path) -> list[str]:
    with connection(root) as conn:
        return traceability_issues(conn, root=root)


def validate_delivery(
    conn: sqlite3.Connection,
    root: Path,
    *,
    mode: str | None = None,
) -> list[str]:
    from core.delivery import evaluate_delivery_prerequisites

    if mode is None:
        cycle = current_cycle_row(conn)
        mode = (
            "delivered-consistency"
            if str(cycle["status"]) == "delivered"
            else "record-delivery"
        )
    blockers = evaluate_delivery_prerequisites(
        conn,
        root,
        mode=mode,  # type: ignore[arg-type]
        is_expired=is_expired,
    )
    return [blocker.render() for blocker in blockers]


def validate_runtime(root: Path, *, delivery: bool = False) -> list[str]:
    issues = doctor(root)
    if issues:
        return issues
    with connection(root) as conn:
        project = project_row(conn)
        if delivery or project["phase"] in {"delivery_readiness", "retrospective"}:
            issues.extend(validate_delivery(conn, root))
    return issues


def _render_harness_command(root: Path, *args: str) -> str:
    if os.environ.get("KAFA_PROJECT_ENTRYPOINT") == "1" and args:
        command = ["kafa", "project", args[0], "--repo", str(root), *args[1:]]
    else:
        command = [
            sys.executable,
            str(Path(__file__).resolve().with_name("harness.py")),
            "--root",
            str(root),
            *args,
        ]
    return subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)


def _quickstart_status_active(root: Path) -> dict[str, Any]:
    missing: list[str] = []
    next_commands: list[str] = []
    with connection(root) as conn:
        project = project_row(conn)
        cycle = current_cycle_row(conn)
        cycle_id = str(cycle["id"])
        cycle_token = re.sub(r"[^A-Za-z0-9._-]+", "-", cycle_id).strip("-._")
        cycle_token = cycle_token or "cycle"
        suggested_baseline_id = (
            "BL1" if cycle_id == DEFAULT_CYCLE_ID else f"BL-{cycle_token}-1"
        )
        candidate_sha = current_candidate_sha(root)
        requirement_count = int(
            conn.execute(
                "select count(*) from requirements where cycle_id=? and status!='cancelled'",
                (cycle_id,),
            ).fetchone()[0]
        )
        acceptance_rows = conn.execute(
            "select id from acceptance where cycle_id=? and status!='cancelled' order by id",
            (cycle_id,),
        ).fetchall()
        trace_count = int(
            conn.execute(
                "select count(*) from requirement_acceptance where cycle_id=?",
                (cycle_id,),
            ).fetchone()[0]
        )
        task_rows = conn.execute(
            "select id, status from tasks where cycle_id=? order by id", (cycle_id,)
        ).fetchall()
        task_acceptance_rows = conn.execute(
            """
            select ta.acceptance_id, t.id, t.status
            from task_acceptance ta
            join tasks t
              on t.cycle_id = ta.cycle_id and t.id = ta.task_id
            where ta.cycle_id = ?
            order by ta.acceptance_id, t.id
            """,
            (cycle_id,),
        ).fetchall()
        target_rows = conn.execute(
            "select id from test_targets order by id"
        ).fetchall()
        linked_target_count = int(
            conn.execute(
                "select count(*) from task_test_targets where cycle_id=?",
                (cycle_id,),
            ).fetchone()[0]
        )
        qualification_pairs = conn.execute(
            """
            select distinct q.acceptance_id, q.target_id
            from acceptance_target_qualifications q
            join acceptance a
              on a.cycle_id = q.cycle_id and a.id = q.acceptance_id
            join test_targets t on t.id = q.target_id
            where q.cycle_id = ? and a.status = 'active'
            order by q.acceptance_id, q.target_id
            """,
            (cycle_id,),
        ).fetchall()
        active_acceptance_ids = [str(row["id"]) for row in acceptance_rows]
        current_qualifications_by_acceptance: dict[str, list[str]] = {
            acceptance_id: [] for acceptance_id in active_acceptance_ids
        }
        for pair_row in qualification_pairs:
            pair = (str(pair_row["acceptance_id"]), str(pair_row["target_id"]))
            qualification = latest_acceptance_target_qualification(
                conn,
                cycle_id=cycle_id,
                acceptance_id=pair[0],
                target_id=pair[1],
            )
            if qualification is None:
                continue
            acceptance_row = conn.execute(
                "select revision from acceptance where cycle_id=? and id=?",
                (cycle_id, qualification["acceptance_id"]),
            ).fetchone()
            target_row = conn.execute(
                "select * from test_targets where id=?",
                (qualification["target_id"],),
            ).fetchone()
            if (
                acceptance_row is not None
                and target_row is not None
                and int(qualification["acceptance_revision"])
                == int(acceptance_row["revision"])
                and str(qualification["target_definition_sha256"])
                == target_definition_digest(dict(target_row))
            ):
                current_qualifications_by_acceptance.setdefault(pair[0], []).append(
                    str(qualification["id"])
                )
        current_qualification_ids = [
            qualification_id
            for acceptance_id in active_acceptance_ids
            for qualification_id in current_qualifications_by_acceptance.get(
                acceptance_id, []
            )
        ]
        missing_qualification_acceptance_ids = [
            acceptance_id
            for acceptance_id in active_acceptance_ids
            if not current_qualifications_by_acceptance.get(acceptance_id)
        ]
        execution_count = int(
            conn.execute(
                """
                select count(distinct e.id) from executions e
                join validation_executions ve on ve.execution_id=e.id
                join validations v on v.id=ve.validation_id
                join acceptance_target_qualifications q
                  on q.id=v.qualification_id and q.cycle_id=v.cycle_id
                where e.cycle_id=? and e.candidate_sha=? and e.exit_code=0
                  and e.executed_count>0 and e.semantic_status='pass'
                  and e.policy_status='allowed' and v.validation_status='active'
                  and v.result='pass'
                  and e.target_id=q.target_id
                  and e.target_definition_sha256=q.target_definition_sha256
                """,
                (cycle_id, candidate_sha),
            ).fetchone()[0]
        )
        quality_gate_count = int(
            conn.execute(
                """
                select count(*) from quality_gates
                join quality_gate_qualifications qg
                  on qg.gate_id=quality_gates.id
                where quality_gates.cycle_id=?
                  and quality_gates.candidate_sha=?
                  and quality_gates.gate_status='active'
                  and quality_gates.result='pass'
                """,
                (cycle_id, candidate_sha),
            ).fetchone()[0]
        )
        delivery_count = int(
            conn.execute(
                "select count(*) from deliveries where cycle_id=? and candidate_sha=?",
                (cycle_id, candidate_sha),
            ).fetchone()[0]
        )
        baseline_missing = baseline_issues(conn)
        if str(cycle["status"]) == "delivered":
            evaluation_mode = "delivered-consistency"
        elif str(project["phase"]) == "delivery_readiness":
            evaluation_mode = "record-delivery"
        else:
            evaluation_mode = "enter-readiness"
        from core.delivery import evaluate_delivery_prerequisites

        delivery_blockers = evaluate_delivery_prerequisites(
            conn,
            root,
            mode=evaluation_mode,  # type: ignore[arg-type]
            is_expired=is_expired,
            candidate_override=candidate_sha,
        )
        delivery_issues = [blocker.render() for blocker in delivery_blockers]
        accepted_task_blocker_ids = [
            blocker.entity_id
            for blocker in delivery_blockers
            if blocker.code == "accepted-task-missing"
            and blocker.entity_type == "acceptance"
        ]

    if requirement_count == 0:
        missing.append("requirement")
        next_commands.append(
            _render_harness_command(
                root,
                "requirement",
                "add",
                "--id",
                "REQ1",
                "--kind",
                "functional",
                "--body",
                "...",
            )
        )
    if not acceptance_rows:
        missing.append("acceptance")
        next_commands.append(
            _render_harness_command(
                root, "acceptance", "add", "--id", "AC1", "--criterion", "..."
            )
        )
    if trace_count == 0:
        missing.append("requirement_acceptance_link")
        next_commands.append(
            _render_harness_command(
                root,
                "requirement",
                "link",
                "--requirement",
                "REQ1",
                "--acceptance",
                "AC1",
            )
        )
    task_id = str(task_rows[0]["id"]) if task_rows else "T1"
    if not task_rows:
        missing.append("task")
        next_commands.append(
            _render_harness_command(
                root,
                "task",
                "add",
                "--id",
                task_id,
                "--task",
                "...",
                "--acceptance",
                "AC1",
            )
        )
    target_id = str(target_rows[0]["id"]) if target_rows else "UNIT"
    if not target_rows or linked_target_count == 0:
        missing.append("test_target")
        if not target_rows:
            next_commands.append(
                _render_harness_command(
                    root,
                    "test-target",
                    "add",
                    "--id",
                    target_id,
                    "--kind",
                    "unit",
                    "--command-template",
                    "python3 -m unittest",
                )
            )
        if linked_target_count == 0:
            next_commands.append(
                _render_harness_command(
                    root,
                    "test-target",
                    "link",
                    "--task",
                    task_id,
                    "--target",
                    target_id,
                )
            )
    if acceptance_rows and target_rows and missing_qualification_acceptance_ids:
        missing.append("qualification")
        for acceptance_id in missing_qualification_acceptance_ids:
            next_commands.append(
                _render_harness_command(
                    root,
                    "test-target",
                    "qualify",
                    "--id",
                    (
                        f"Q-{acceptance_id}-{target_id}"
                        if cycle_id == DEFAULT_CYCLE_ID
                        else f"Q-{cycle_token}-{acceptance_id}-{target_id}"
                    ),
                    "--target",
                    target_id,
                    "--acceptance",
                    acceptance_id,
                    "--rationale",
                    "explicit acceptance-to-target mapping",
                    "--by",
                    "root-controller",
                )
            )
    if baseline_missing or any(
        blocker.code == "scope-unconfirmed" for blocker in delivery_blockers
    ):
        missing.append("baseline")
        next_commands.append(
            _render_harness_command(
                root,
                "baseline",
                "confirm",
                "--id",
                suggested_baseline_id,
                "--summary",
                "current confirmed scope",
                "--by",
                "root-controller",
            )
        )
    if execution_count == 0:
        missing.append("controller_execution")
        verify_args = ["verify", "run", "--target", target_id]
        if acceptance_rows:
            verify_args.extend(["--acceptance", str(acceptance_rows[0]["id"])])
        if not acceptance_rows or current_qualification_ids:
            next_commands.append(_render_harness_command(root, *verify_args))
    unresolved_tasks = [
        row for row in task_rows if row["status"] not in {"accepted", "cancelled"}
    ]
    if accepted_task_blocker_ids:
        missing.append("accepted_task")
        existing_task_ids = {str(row["id"]) for row in task_rows}
        for acceptance_id in accepted_task_blocker_ids:
            linked = [
                row
                for row in task_acceptance_rows
                if str(row["acceptance_id"]) == acceptance_id
                and str(row["status"]) not in {"accepted", "cancelled"}
            ]
            if not linked:
                stem = re.sub(r"[^A-Za-z0-9._-]+", "-", acceptance_id).strip("-._")
                stem = stem or "AC"
                replacement_id = f"{stem}-T2"
                suffix = 2
                while replacement_id in existing_task_ids:
                    suffix += 1
                    replacement_id = f"{stem}-T{suffix}"
                existing_task_ids.add(replacement_id)
                next_commands.append(
                    _render_harness_command(
                        root,
                        "task",
                        "add",
                        "--id",
                        replacement_id,
                        "--task",
                        f"replace cancelled or missing work for {acceptance_id}",
                        "--acceptance",
                        acceptance_id,
                    )
                )
                continue
            for row in linked:
                if row["status"] == "planned":
                    next_commands.append(
                        _render_harness_command(root, "task", "start", str(row["id"]))
                    )
                elif row["status"] == "active" and execution_count:
                    next_commands.append(
                        _render_harness_command(
                            root,
                            "task",
                            "submit",
                            str(row["id"]),
                            "--context-id",
                            "producer-context",
                            "--evidence",
                            "verified immutable execution",
                        )
                    )
    if quality_gate_count == 0:
        missing.append("quality_gate")
        if (
            execution_count
            and not accepted_task_blocker_ids
            and not unresolved_tasks
            and not missing_qualification_acceptance_ids
        ):
            next_commands.append(
                _render_harness_command(
                    root,
                    "gate",
                    "record",
                "--reviewer-context",
                "same-context-degraded",
                "--result",
                "pass",
                "--residual-risk",
                "same-context local review; independent review not claimed",
                *sum(
                        (
                            ["--qualification", qualification]
                            for qualification in current_qualification_ids
                        ),
                        [],
                    ),
                )
            )
    if delivery_count == 0:
        missing.append("delivery")
        blocker_codes = {blocker.code for blocker in delivery_blockers}
        if evaluation_mode == "enter-readiness" and not delivery_blockers:
            next_commands.append(
                _render_harness_command(root, "delivery", "ready")
            )
        elif "phase-not-ready" in blocker_codes and not (
            blocker_codes - {"phase-not-ready"}
        ):
            next_commands.append(
                _render_harness_command(root, "delivery", "ready")
            )
        elif evaluation_mode == "record-delivery" and not delivery_issues:
            next_commands.append(
                _render_harness_command(
                    root,
                    "delivery",
                    "record",
                    "--scope",
                    "verified local handoff",
                )
            )
    if evaluation_mode == "delivered-consistency":
        # A closed cycle cannot legally execute graph mutation commands. Its
        # structured blockers are the actionable corruption diagnosis; repair
        # or a separately started cycle must be an explicit operator decision.
        next_commands = []
    final_candidate_sha = current_candidate_sha(root)
    if final_candidate_sha != candidate_sha:
        from core.delivery import DeliveryBlocker

        candidate_blocker = DeliveryBlocker(
            code="candidate-snapshot-changed",
            message=(
                "current candidate changed while quickstart status was assembled: "
                f"started={candidate_sha} finished={final_candidate_sha}"
            ),
            entity_type="delivery_cycle",
            entity_id=cycle_id,
        )
        delivery_blockers = (candidate_blocker, *delivery_blockers)
        delivery_issues = [blocker.render() for blocker in delivery_blockers]
    if any(
        blocker.code == "candidate-snapshot-changed"
        for blocker in delivery_blockers
    ):
        if "current_candidate" not in missing:
            missing.insert(0, "current_candidate")
        next_commands = []
    return {
        "initialized": True,
        "ready_for_delivery": bool(
            not delivery_issues
            and evaluation_mode in {"record-delivery", "delivered-consistency"}
        ),
        "missing": missing,
        "phase": project["phase"],
        "cycle_id": cycle_id,
        "cycle_status": cycle["status"],
        "delivery_issues": delivery_issues,
        "delivery_blockers": [
            blocker.as_dict() for blocker in delivery_blockers
        ],
        "delivery_evaluation_mode": evaluation_mode,
        "next_commands": next_commands,
    }


def _quickstart_unavailable_report(
    envelope: OperatorEnvelope,
    *,
    missing: str,
) -> dict[str, Any]:
    error = envelope.details.get("error", "")
    return {
        "initialized": False,
        "ready_for_delivery": False,
        "missing": [missing],
        "phase": "",
        "cycle_id": "",
        "cycle_status": "",
        "delivery_issues": [
            f"[{blocker.code}] {blocker.message}"
            for blocker in envelope.blockers
        ],
        "delivery_blockers": [
            blocker.as_dict() for blocker in envelope.blockers
        ],
        "delivery_evaluation_mode": "",
        "next_commands": list(envelope.actions),
        "operator_state": envelope.state,
        "error": str(error),
    }


def quickstart_status(root: Path) -> dict[str, Any]:
    """Return the complete legacy dict from the same pinned operator snapshot."""

    try:
        with _operator_runtime_snapshot(root) as (initialized, database_exists):
            if not initialized:
                envelope = (
                    _unavailable_existing_state_report(root)
                    if database_exists
                    else uninitialized_operator_report(root)
                )
                return _quickstart_unavailable_report(
                    envelope,
                    missing=("state" if database_exists else "init"),
                )
            return _quickstart_status_active(root)
    except OPERATOR_STATE_READ_ERRORS as exc:
        envelope = operator_error_report(root, exc, allow_init=False)
        return _quickstart_unavailable_report(
            envelope,
            missing=(
                "recovery"
                if envelope.state == "recovery-required"
                else "state"
            ),
        )


def _quickstart_status_lines_from_report(report: dict[str, Any]) -> list[str]:
    lines = [
        "# Kafa Quickstart Status",
        f"initialized: {str(report['initialized']).lower()}",
        f"ready_for_delivery: {str(report['ready_for_delivery']).lower()}",
    ]
    if report.get("phase"):
        lines.append(f"phase: {report['phase']}")
    if report.get("cycle_id"):
        lines.append(f"cycle: {report['cycle_id']} ({report['cycle_status']})")
    missing = report.get("missing", [])
    lines.append("missing: " + (", ".join(missing) if missing else "none"))
    if report.get("delivery_issues"):
        lines.append("delivery_issues:")
        lines.extend(f"- {issue}" for issue in report["delivery_issues"])
    if report.get("next_commands"):
        lines.append("next_commands:")
        lines.extend(f"- {command}" for command in report["next_commands"])
    return lines


def quickstart_status_lines(root: Path) -> list[str]:
    return _quickstart_status_lines_from_report(quickstart_status(root))


def quickstart_operator_report(root: Path) -> OperatorEnvelope:
    """Return one complete quickstart report for concise/verbose/JSON views."""

    report = quickstart_status(root)
    blockers = tuple(
        {
            "code": str(blocker["code"]),
            "message": str(blocker["message"]),
        }
        for blocker in report.get("delivery_blockers", [])
    )
    details = dict(report)
    return build_operator_envelope(
        state=(
            str(report["operator_state"])
            if report.get("operator_state")
            else
            "ready-for-delivery"
            if report.get("ready_for_delivery")
            else "needs-work"
        ),
        blockers=blockers,
        actions=tuple(str(command) for command in report.get("next_commands", [])),
        details=details,
    )


def _coerce_delivery_plan(value: object) -> DeliveryPlan:
    try:
        return parse_delivery_plan(value)
    except DeliveryPlanError as exc:
        raise HarnessError(str(exc)) from exc


def _delivery_plan_semantic_preflight(
    plan: DeliveryPlan,
    ids: dict[str, str],
) -> None:
    guard_schema(
        "validate_requirement",
        ids["requirement_id"],
        "functional",
        plan.goal,
        "active",
    )
    guard_schema(
        "validate_acceptance",
        ids["acceptance_id"],
        plan.acceptance,
    )
    guard_schema("validate_task", ids["task_id"], plan.task, "planned")
    guard_schema(
        "validate_test_target",
        ids["target_id"],
        plan.test.kind,
        plan.test.command,
        "python",
        "regex",
    )
    gateable, reason = target_gateability(plan.test.kind, plan.test.command)
    if gateable != 1:
        raise HarnessError(
            f"delivery-plan test target is not gateable: {ids['target_id']}: {reason}"
        )
    if plan.failure_mode is not None:
        guard_schema(
            "validate_failure_mode",
            ids["failure_mode_id"],
            plan.failure_mode.risk,
            "identified",
        )


def _delivery_plan_result(
    plan: DeliveryPlan,
    ids: dict[str, str],
    *,
    dry_run: bool,
    changed: bool,
    would_change: bool = False,
) -> dict[str, object]:
    return {
        "plan_id": plan.id,
        "dry_run": dry_run,
        "changed": changed,
        "ids": dict(ids),
        "validations": [],
        "mutations": (
            planned_mutations(plan) if changed or (dry_run and would_change) else []
        ),
    }


def _delivery_plan_event(
    conn: sqlite3.Connection,
    cycle_id: str,
    plan_id: str,
) -> dict[str, object] | None:
    rows = conn.execute(
        """
        select after_json from events
        where event_type = 'delivery_plan_applied'
          and entity_type = 'delivery_plan' and entity_id = ?
        order by sequence
        """,
        (f"{cycle_id}:{plan_id}",),
    ).fetchall()
    if len(rows) > 1:
        raise HarnessError(
            f"delivery-plan conflict: duplicate apply markers for {plan_id}; "
            "use a new plan ID"
        )
    if not rows:
        return None
    try:
        value = json.loads(str(rows[0]["after_json"]))
    except (TypeError, json.JSONDecodeError) as exc:
        raise HarnessError(
            f"delivery-plan marker is invalid for {plan_id}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise HarnessError(f"delivery-plan marker is invalid for {plan_id}")
    return value


def _plan_failure_mode_expected(marker: dict[str, object]) -> bool:
    kind = marker.get("kind")
    if kind not in {"delivery-plan", "delivery-plan-with-failure-mode"}:
        raise HarnessError("delivery-plan marker has an unknown kind")
    return kind == "delivery-plan-with-failure-mode"


def _required_row(
    conn: sqlite3.Connection,
    sql: str,
    values: tuple[object, ...],
    label: str,
    error: str,
) -> sqlite3.Row:
    row = conn.execute(sql, values).fetchone()
    if row is None:
        raise HarnessError(f"{error}: missing {label}")
    return row


def _persisted_delivery_plan(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    plan_id: str,
    ids: dict[str, str],
    marker: dict[str, object],
    error: str,
) -> DeliveryPlan:
    requirement = _required_row(
        conn,
        "select * from requirements where cycle_id = ? and id = ?",
        (cycle_id, ids["requirement_id"]),
        "requirement",
        error,
    )
    acceptance = _required_row(
        conn,
        "select * from acceptance where cycle_id = ? and id = ?",
        (cycle_id, ids["acceptance_id"]),
        "acceptance",
        error,
    )
    task = _required_row(
        conn,
        "select * from tasks where cycle_id = ? and id = ?",
        (cycle_id, ids["task_id"]),
        "task",
        error,
    )
    target = _required_row(
        conn,
        "select * from test_targets where id = ?",
        (ids["target_id"],),
        "test target",
        error,
    )
    qualification = _required_row(
        conn,
        "select * from acceptance_target_qualifications where id = ?",
        (ids["qualification_id"],),
        "qualification",
        error,
    )
    if (
        str(requirement["kind"]) != "functional"
        or str(requirement["priority"]) != "must"
        or str(requirement["status"]) != "active"
        or str(acceptance["priority"]) != "must"
        or str(acceptance["status"]) != "active"
        or str(task["owner"]) != "developer"
        or int(target["gateable"]) != 1
        or str(target["stack_profile"]) != "python"
        or str(target["container_image"]) != ""
        or int(target["requires_sandbox"]) != 0
        or int(target["requires_no_network"]) != 0
        or str(target["result_format"]) != "regex"
        or str(target["result_path"]) != ""
    ):
        raise HarnessError(f"{error}: generated entity semantics changed")

    required_relations = (
        (
            "requirement_acceptance",
            "select 1 from requirement_acceptance where cycle_id=? and requirement_id=? and acceptance_id=?",
            (cycle_id, ids["requirement_id"], ids["acceptance_id"]),
        ),
        (
            "task_acceptance",
            "select 1 from task_acceptance where cycle_id=? and task_id=? and acceptance_id=?",
            (cycle_id, ids["task_id"], ids["acceptance_id"]),
        ),
        (
            "task_test_target",
            "select 1 from task_test_targets where cycle_id=? and task_id=? and target_id=?",
            (cycle_id, ids["task_id"], ids["target_id"]),
        ),
    )
    for label, sql, values in required_relations:
        if conn.execute(sql, values).fetchone() is None:
            raise HarnessError(f"{error}: missing {label} relation")

    target_digest = target_definition_digest(dict(target))
    latest = latest_acceptance_target_qualification(
        conn,
        cycle_id=cycle_id,
        acceptance_id=ids["acceptance_id"],
        target_id=ids["target_id"],
    )
    if (
        latest is None
        or str(latest["id"]) != ids["qualification_id"]
        or str(qualification["cycle_id"]) != cycle_id
        or str(qualification["acceptance_id"]) != ids["acceptance_id"]
        or int(qualification["acceptance_revision"]) != int(acceptance["revision"])
        or str(qualification["target_id"]) != ids["target_id"]
        or str(qualification["target_definition_sha256"]) != target_digest
        or str(qualification["qualified_by"]) != "root-controller"
    ):
        raise HarnessError(f"{error}: qualification is stale or superseded")

    failure_mode_payload: dict[str, object] | None = None
    if _plan_failure_mode_expected(marker):
        failure_mode = _required_row(
            conn,
            "select * from failure_modes where cycle_id = ? and id = ?",
            (cycle_id, ids["failure_mode_id"]),
            "failure mode",
            error,
        )
        for label, sql, values in (
            (
                "failure_mode_acceptance",
                "select 1 from failure_mode_acceptance where cycle_id=? and failure_mode_id=? and acceptance_id=?",
                (cycle_id, ids["failure_mode_id"], ids["acceptance_id"]),
            ),
            (
                "task_failure_mode",
                "select 1 from task_failure_modes where cycle_id=? and task_id=? and failure_mode_id=?",
                (cycle_id, ids["task_id"], ids["failure_mode_id"]),
            ),
        ):
            if conn.execute(sql, values).fetchone() is None:
                raise HarnessError(f"{error}: missing {label} relation")
        failure_mode_payload = {
            "feature": str(failure_mode["feature"]),
            "scenario": str(failure_mode["scenario"]),
            "trigger": str(failure_mode["trigger"]),
            "expected": str(failure_mode["expected_behavior"]),
            "risk": str(failure_mode["risk"]),
            "recovery": str(failure_mode["recovery"]),
            "data_safety": str(failure_mode["data_safety"]),
        }

    try:
        persisted = parse_delivery_plan(
            {
                "version": 1,
                "id": plan_id,
                "goal": str(requirement["body"]),
                "acceptance": str(acceptance["criterion"]),
                "task": str(task["task"]),
                "test": {
                    "kind": str(target["kind"]),
                    "command": str(target["command_template"]),
                },
                "failure_mode": failure_mode_payload,
            }
        )
    except DeliveryPlanError as exc:
        raise HarnessError(f"{error}: persisted graph is invalid: {exc}") from exc
    if logical_plan_digest(persisted) != str(marker.get("digest") or ""):
        raise HarnessError(f"{error}: graph has different semantic content")
    return persisted


def _delivery_plan_collision(
    conn: sqlite3.Connection,
    cycle_id: str,
    ids: dict[str, str],
) -> str:
    for table, identifier in (
        ("requirements", ids["requirement_id"]),
        ("acceptance", ids["acceptance_id"]),
        ("tasks", ids["task_id"]),
        *((
            ("failure_modes", ids["failure_mode_id"]),
        ) if "failure_mode_id" in ids else ()),
    ):
        if conn.execute(
            f"select 1 from {table} where cycle_id = ? and id = ?",
            (cycle_id, identifier),
        ).fetchone():
            return f"{table}:{identifier}"
    if conn.execute(
        "select 1 from test_targets where id = ?", (ids["target_id"],)
    ).fetchone():
        return f"test_targets:{ids['target_id']}"
    if conn.execute(
        "select 1 from acceptance_target_qualifications where id = ?",
        (ids["qualification_id"],),
    ).fetchone():
        return f"acceptance_target_qualifications:{ids['qualification_id']}"
    return ""


def _delivery_plan_marker_payload(
    plan: DeliveryPlan,
    cycle_id: str,
) -> dict[str, object]:
    return {
        "id": plan.id,
        "cycle_id": cycle_id,
        "digest": logical_plan_digest(plan),
        "kind": (
            "delivery-plan-with-failure-mode"
            if plan.failure_mode is not None
            else "delivery-plan"
        ),
    }


def _delivery_plan_requires_insert(
    conn: sqlite3.Connection,
    plan: DeliveryPlan,
    ids: dict[str, str],
    cycle_id: str,
) -> bool:
    """Run the same persisted-state preflight for dry-run and real apply."""

    marker = _delivery_plan_event(conn, cycle_id, plan.id)
    if marker is not None:
        marker_has_failure_mode = _plan_failure_mode_expected(marker)
        if marker_has_failure_mode != (plan.failure_mode is not None):
            raise HarnessError(
                f"delivery-plan conflict for {plan.id}: different semantic content; "
                "use a new plan ID"
            )
        _persisted_delivery_plan(
            conn,
            cycle_id=cycle_id,
            plan_id=plan.id,
            ids=ids,
            marker=marker,
            error=f"delivery-plan conflict for {plan.id}; use a new plan ID",
        )
        if str(marker.get("digest") or "") != logical_plan_digest(plan):
            raise HarnessError(
                f"delivery-plan conflict for {plan.id}: different semantic content; "
                "use a new plan ID"
            )
        return False

    collision = _delivery_plan_collision(conn, cycle_id, ids)
    if collision:
        raise HarnessError(
            f"delivery-plan conflict for {plan.id}: generated ID already exists "
            f"({collision}); use a new plan ID"
        )
    return True


def _insert_delivery_plan_graph(
    conn: sqlite3.Connection,
    plan: DeliveryPlan,
    ids: dict[str, str],
    cycle_id: str,
) -> None:
    if current_cycle_id(conn) != cycle_id:
        raise HarnessError("delivery-plan cycle changed during apply")
    project_before = project_row(conn)
    cycle_before = current_cycle_row(conn)
    forbidden_counts = {
        table: int(conn.execute(f"select count(*) from {table}").fetchone()[0])
        for table in (
            "baselines",
            "executions",
            "validations",
            "quality_gates",
            "deliveries",
        )
    }
    _record_requirement_conn(
        conn,
        ids["requirement_id"],
        "functional",
        plan.goal,
        "must",
        "active",
        create_only=True,
    )
    _record_acceptance_conn(
        conn,
        ids["acceptance_id"],
        plan.acceptance,
        "must",
        create_only=True,
    )
    _link_requirement_acceptance_conn(
        conn,
        ids["requirement_id"],
        ids["acceptance_id"],
    )
    if plan.failure_mode is not None:
        failure = plan.failure_mode
        _record_failure_mode_conn(
            conn,
            ids["failure_mode_id"],
            failure.feature,
            failure.scenario,
            failure.trigger,
            failure.expected,
            risk=failure.risk,
            status="identified",
            acceptance=ids["acceptance_id"],
            recovery=failure.recovery,
            data_safety=failure.data_safety,
            create_only=True,
        )
    _create_task_conn(
        conn,
        ids["task_id"],
        plan.task,
        owner="developer",
        acceptance=ids["acceptance_id"],
        failure_modes=(
            ids["failure_mode_id"] if plan.failure_mode is not None else ""
        ),
    )
    _record_test_target_conn(
        conn,
        ids["target_id"],
        plan.test.kind,
        plan.test.command,
        "delivery-plan executable target",
        create_only=True,
    )
    _link_task_test_target_conn(conn, ids["task_id"], ids["target_id"])
    _qualify_test_target_conn(
        conn,
        ids["qualification_id"],
        ids["target_id"],
        ids["acceptance_id"],
        f"delivery-plan {plan.id} explicitly maps the target to the acceptance",
        "root-controller",
    )
    bump_project(conn, scope_status="unconfirmed", status="draft")
    expected_marker = _delivery_plan_marker_payload(plan, cycle_id)
    emit_audit_event(
        conn,
        "delivery_plan_applied",
        entity_type="delivery_plan",
        entity_id=f"{cycle_id}:{plan.id}",
        before=None,
        after=expected_marker,
        actor="root-controller",
        command="quickstart delivery-plan",
    )
    persisted_marker = _delivery_plan_event(conn, cycle_id, plan.id)
    if persisted_marker != expected_marker:
        raise HarnessError(
            f"delivery-plan postcondition failed for {plan.id}: "
            "apply marker is missing or inconsistent"
        )
    _persisted_delivery_plan(
        conn,
        cycle_id=cycle_id,
        plan_id=plan.id,
        ids=ids,
        marker=persisted_marker,
        error=f"delivery-plan postcondition failed for {plan.id}",
    )
    task = conn.execute(
        "select status from tasks where cycle_id = ? and id = ?",
        (cycle_id, ids["task_id"]),
    ).fetchone()
    if task is None or str(task["status"]) != "planned":
        raise HarnessError(
            f"delivery-plan postcondition failed for {plan.id}: "
            "generated task must remain planned"
        )
    if plan.failure_mode is not None:
        failure_mode = conn.execute(
            "select status from failure_modes where cycle_id = ? and id = ?",
            (cycle_id, ids["failure_mode_id"]),
        ).fetchone()
        if failure_mode is None or str(failure_mode["status"]) != "identified":
            raise HarnessError(
                f"delivery-plan postcondition failed for {plan.id}: "
                "generated failure mode must remain identified"
            )
    project_after = project_row(conn)
    cycle_after = current_cycle_row(conn)
    if (
        str(project_after["current_cycle_id"]) != cycle_id
        or str(project_after["phase"]) != str(project_before["phase"])
        or str(project_after["scope_status"]) != "unconfirmed"
        or int(project_after["revision"]) != int(project_before["revision"]) + 1
        or str(cycle_after["id"]) != cycle_id
        or str(cycle_after["status"]) != str(cycle_before["status"])
        or str(cycle_after["phase"]) != str(cycle_before["phase"])
    ):
        raise HarnessError(
            f"delivery-plan postcondition failed for {plan.id}: "
            "project/cycle lifecycle changed unexpectedly"
        )
    for table, before_count in forbidden_counts.items():
        after_count = int(
            conn.execute(f"select count(*) from {table}").fetchone()[0]
        )
        if after_count != before_count:
            raise HarnessError(
                f"delivery-plan postcondition failed for {plan.id}: "
                f"forbidden {table} fact was created"
            )


@_project_mutation
def _apply_delivery_plan_locked(
    root: Path,
    plan: DeliveryPlan,
) -> dict[str, object]:
    changed = False
    result: dict[str, object]
    touched: list[tuple[str, str]] = [("project", "1")]
    with transaction(
        root,
        touched=touched,
    ) as conn:
        cycle = current_cycle_row(conn)
        cycle_id = str(cycle["id"])
        if str(cycle["status"]) != "active":
            raise HarnessError(
                f"current cycle is closed: {cycle_id} status={cycle['status']}; "
                "start a new cycle before applying a delivery-plan"
            )
        ids = plan_ids(plan, cycle_id)
        touched.extend(
            [
                ("requirement", ids["requirement_id"]),
                ("acceptance", ids["acceptance_id"]),
                ("task", ids["task_id"]),
                ("test_target", ids["target_id"]),
                (
                    "acceptance_target_qualification",
                    ids["qualification_id"],
                ),
                *(
                    [("failure_mode", ids["failure_mode_id"])]
                    if "failure_mode_id" in ids
                    else []
                ),
            ]
        )
        _delivery_plan_semantic_preflight(plan, ids)
        if not _delivery_plan_requires_insert(conn, plan, ids, cycle_id):
            result = _delivery_plan_result(
                plan,
                ids,
                dry_run=False,
                changed=False,
            )
        else:
            _insert_delivery_plan_graph(
                conn,
                plan,
                ids,
                cycle_id,
            )
            changed = True
            result = _delivery_plan_result(
                plan,
                ids,
                dry_run=False,
                changed=True,
            )
    if changed:
        affected = [
            "project-state",
            "requirements",
            "traceability",
            "acceptance",
            "tasks",
            "test-targets",
        ]
        if plan.failure_mode is not None:
            affected.append("failure-modes")
        render_affected(root, *affected)
    return result


def _require_delivery_plan_runtime(root: Path) -> None:
    store = get_store(root)
    if isinstance(store, SqliteStore):
        with runtime_path_audit(root, store=store) as project_fs:
            initialized = bool(
                project_fs is not None
                and project_fs._snapshot(DB_PATH, allow_missing=True).exists
            )
    else:
        initialized = runtime_initialized(root)
    if not initialized:
        raise HarnessError(
            f"harness is not initialized in this project: {root}; "
            "delivery-plan never initializes implicitly"
        )


def apply_delivery_plan(
    root: Path,
    value: object,
    *,
    dry_run: bool = False,
) -> dict[str, object]:
    plan = _coerce_delivery_plan(value)
    cycle_id = DEFAULT_CYCLE_ID
    if dry_run:
        would_change = True
        if runtime_initialized(root):
            with connection(root) as conn:
                cycle = current_cycle_row(conn)
                cycle_id = str(cycle["id"])
                if str(cycle["status"]) != "active":
                    raise HarnessError(
                        f"current cycle is closed: {cycle_id} status={cycle['status']}; "
                        "start a new cycle before applying a delivery-plan"
                    )
                ids = plan_ids(plan, cycle_id)
                _delivery_plan_semantic_preflight(plan, ids)
                would_change = _delivery_plan_requires_insert(
                    conn,
                    plan,
                    ids,
                    cycle_id,
                )
        else:
            ids = plan_ids(plan, cycle_id)
            _delivery_plan_semantic_preflight(plan, ids)
        return _delivery_plan_result(
            plan,
            ids,
            dry_run=True,
            changed=False,
            would_change=would_change,
        )
    _require_delivery_plan_runtime(root)
    return _apply_delivery_plan_locked(root, plan)


def _resolve_delivery_plan(
    conn: sqlite3.Connection,
    plan_id: str,
) -> dict[str, object]:
    try:
        normalized_id = normalize_plan_id(plan_id)
    except DeliveryPlanError as exc:
        raise HarnessError(str(exc)) from exc
    cycle = current_cycle_row(conn)
    cycle_id = str(cycle["id"])
    if str(cycle["status"]) != "active":
        raise HarnessError(
            f"current cycle is closed: {cycle_id} status={cycle['status']}; "
            "start a new cycle before verified-patch"
        )
    marker = _delivery_plan_event(conn, cycle_id, normalized_id)
    if marker is None:
        raise HarnessError(f"missing delivery-plan: {normalized_id}")
    ids = derive_plan_ids(normalized_id, cycle_id)
    if _plan_failure_mode_expected(marker):
        ids["failure_mode_id"] = f"{normalized_id}-FM1"
    plan = _persisted_delivery_plan(
        conn,
        cycle_id=cycle_id,
        plan_id=normalized_id,
        ids=ids,
        marker=marker,
        error=f"stale delivery-plan {normalized_id}",
    )
    target = conn.execute(
        "select * from test_targets where id = ?", (ids["target_id"],)
    ).fetchone()
    if target is None:  # pragma: no cover - persisted graph already checks
        raise HarnessError(f"stale delivery-plan {normalized_id}: missing target")
    return {
        "plan": plan,
        "ids": ids,
        "cycle_id": cycle_id,
        "target_definition_sha256": target_definition_digest(dict(target)),
    }


@_project_mutation
def verified_patch(root: Path, plan_id: str) -> dict[str, object]:
    with connection(root) as conn:
        resolved = _resolve_delivery_plan(conn, plan_id)
    ids = dict(resolved["ids"])
    failure_modes = (
        [ids["failure_mode_id"]] if "failure_mode_id" in ids else []
    )
    execution_id, validation_id = verify_run(
        root,
        ids["target_id"],
        acceptance=ids["acceptance_id"],
        failure_modes=failure_modes,
    )
    with connection(root) as conn:
        execution = _required_row(
            conn,
            "select * from executions where id = ?",
            (execution_id,),
            "execution",
            "verified-patch result is incomplete",
        )
        validation = _required_row(
            conn,
            "select * from validations where id = ?",
            (validation_id,),
            "validation",
            "verified-patch result is incomplete",
        )
        task = _required_row(
            conn,
            "select * from tasks where cycle_id = ? and id = ?",
            (resolved["cycle_id"], ids["task_id"]),
            "task",
            "verified-patch result is incomplete",
        )
        gate = conn.execute(
            """
            select g.result from quality_gates g
            join quality_gate_qualifications qg on qg.gate_id = g.id
            where g.cycle_id = ? and g.candidate_sha = ?
              and qg.qualification_id = ? and g.gate_status = 'active'
            order by g.sequence desc limit 1
            """,
            (
                resolved["cycle_id"],
                execution["candidate_sha"],
                ids["qualification_id"],
            ),
        ).fetchone()
        delivery = conn.execute(
            """
            select d.decision_status from deliveries d
            join delivery_acceptance da on da.delivery_id = d.id
            where d.cycle_id = ? and d.candidate_sha = ?
              and da.acceptance_id = ?
            order by d.created_at desc, d.rowid desc limit 1
            """,
            (
                resolved["cycle_id"],
                execution["candidate_sha"],
                ids["acceptance_id"],
            ),
        ).fetchone()
    if (
        str(validation["qualification_id"] or "") != ids["qualification_id"]
        or str(validation["acceptance_id"] or "") != ids["acceptance_id"]
        or str(validation["candidate_sha"]) != str(execution["candidate_sha"])
        or str(validation["cycle_id"]) != str(resolved["cycle_id"])
        or str(execution["cycle_id"]) != str(resolved["cycle_id"])
        or str(execution["target_definition_sha256"])
        != str(resolved["target_definition_sha256"])
    ):
        raise HarnessError("verified-patch persisted result is stale or mismatched")
    report = {
        "kind": "verified-patch",
        "verification_status": str(validation["result"]),
        "task_status": str(task["status"]),
        "gate_status": str(gate["result"]) if gate is not None else "not-run",
        "delivery_status": (
            str(delivery["decision_status"])
            if delivery is not None
            else "not-run"
        ),
        "cycle_id": str(execution["cycle_id"]),
        "candidate_sha": str(execution["candidate_sha"]),
        "qualification_id": ids["qualification_id"],
        "target_id": str(execution["target_id"]),
        "target_definition_sha256": str(
            execution["target_definition_sha256"]
        ),
        "execution_id": execution_id,
        "validation_id": validation_id,
    }
    _before_verified_patch_envelope(root, execution_id, validation_id)
    current_candidate = current_candidate_sha(root)
    if current_candidate != str(execution["candidate_sha"]):
        raise HarnessError(
            "stale candidate: project source changed before verified-patch "
            "envelope generation"
        )
    return report


def quickstart_minimal(root: Path, quickstart_id: str, goal: str, acceptance: str, task: str, test_command: str, *, execute: bool = False) -> list[str]:
    normalized_id = re.sub(r"[^A-Za-z0-9._-]+", "-", quickstart_id).strip("-._").upper()
    if not normalized_id:
        raise HarnessError("quickstart minimal requires --id")
    req_id = f"{normalized_id}-REQ1"
    ac_id = f"{normalized_id}-AC1"
    task_id = f"{normalized_id}-T1"
    target_id = f"{normalized_id}-UNIT"
    qualification_id = f"{normalized_id}-Q1"
    baseline_id = f"{normalized_id}-BL1"
    if not execute:
        return [
            f"DRY-RUN: would initialize runtime if needed for {normalized_id}",
            f"DRY-RUN: would record {req_id}, {ac_id}, {task_id}, {target_id}, {qualification_id}",
            "DRY-RUN: would run the controller-local test command and stop before independent review",
            f"NEXT: add --execute to run: {test_command}",
        ]
    lines: list[str] = []
    if not runtime_initialized(root):
        init_runtime(root)
        lines.append("OK: project harness initialized")
    with connection(root) as conn:
        active_cycle_id = current_cycle_id(conn)
    if active_cycle_id != DEFAULT_CYCLE_ID:
        cycle_token = re.sub(
            r"[^A-Za-z0-9._-]+", "-", active_cycle_id
        ).strip("-._")
        cycle_token = cycle_token or "cycle"
        global_stem = f"{normalized_id}-{cycle_token}"
        target_id = f"{global_stem}-UNIT"
        qualification_id = f"{global_stem}-Q1"
        baseline_id = f"{global_stem}-BL1"
    add_requirement(root, req_id, "functional", goal, priority="must")
    add_acceptance(root, ac_id, acceptance, priority="must")
    link_requirement_acceptance(root, req_id, ac_id)
    add_test_target(root, target_id, "unit", test_command, "quickstart minimal executable target")
    add_task(root, task_id, task, owner="developer", acceptance=ac_id)
    link_task_test_target(root, task_id, target_id)
    qualify_test_target(
        root,
        qualification_id,
        target_id,
        ac_id,
        "procedural user-input mapping supplied through quickstart minimal",
        "quickstart-user-input",
    )
    lines.extend(
        [
            f"OK: requirement added {req_id}",
            f"OK: acceptance added {ac_id}",
            f"OK: task added {task_id}",
            f"OK: test target recorded {target_id}",
            f"OK: procedural user-input qualification recorded {qualification_id}",
        ]
    )

    for phase in ["project_bootstrap", "requirement_baseline"]:
        transition_if_needed(root, phase)
    transition_if_needed(root, "confirmation")
    confirm_baseline(
        root,
        baseline_id,
        f"{normalized_id}: {goal}",
        by="quickstart",
    )
    transition_if_needed(root, "planning")
    transition_if_needed(root, "implementation")

    start_task(root, task_id)
    execution_id, validation_id = verify_run(
        root,
        target_id,
        acceptance=ac_id,
    )
    submit_task(
        root,
        task_id,
        f"verified by immutable execution {execution_id}",
        context_id="quickstart-producer",
    )
    lines.append(
        f"OK: verify run execution={execution_id} validation={validation_id}"
    )
    lines.append(f"OK: task submitted {task_id}")
    lines.append(f"OK: quickstart minimal verified setup {normalized_id}")
    lines.append(
        f"NEXT: stop for independent review of {task_id} and qualification "
        f"{qualification_id}; do not accept the task or record a passing quality gate "
        "until reviewer findings are returned to the root controller"
    )
    lines.append(f"NEXT: {_render_harness_command(root, 'quickstart', 'status')}")
    return lines


def transition_if_needed(root: Path, phase: str) -> None:
    with connection(root) as conn:
        current = project_row(conn)["phase"]
    if current != phase:
        transition_phase(root, phase)


def _status_details_active(root: Path) -> dict[str, Any]:
    store = get_store(root)
    with runtime_path_audit(root, store=store) as project_fs:
        if isinstance(store, SqliteStore) and (
            project_fs is None
            or not project_fs._snapshot(DB_PATH, allow_missing=True).exists
        ):
            raise HarnessError(
                f"harness is not initialized in this project: {root}"
            )
        with store.connection() as conn:
            row = project_row(conn)
            task_count = conn.execute("select count(*) from tasks").fetchone()[0]
            planned_count = conn.execute("select count(*) from tasks where status = 'planned'").fetchone()[0]
            event_count = conn.execute("select count(*) from events").fetchone()[0]
    return {
        "status": str(row["status"]),
        "phase": str(row["phase"]),
        "cycle_id": str(row["current_cycle_id"]),
        "scope_status": str(row["scope_status"]),
        "current_owner": str(row["current_owner"]),
        "schema_version": int(row["schema_version"]),
        "runtime_version": str(row["runtime_version"]),
        "revision": int(row["revision"]),
        "tasks": int(task_count),
        "planned_tasks": int(planned_count),
        "events": int(event_count),
    }


def _status_lines_from_details(details: dict[str, Any]) -> list[str]:
    return [
        "# Harness Status",
        f"status: {details['status']}",
        f"phase: {details['phase']}",
        f"cycle: {details['cycle_id']}",
        f"scope_status: {details['scope_status']}",
        f"current_owner: {details['current_owner']}",
        f"schema_version: {details['schema_version']}",
        f"runtime_version: {details['runtime_version']}",
        f"revision: {details['revision']}",
        f"tasks: {details['tasks']}",
        f"planned_tasks: {details['planned_tasks']}",
        f"events: {details['events']}",
    ]


def status_lines(root: Path) -> list[str]:
    return _status_lines_from_details(_status_details_active(root))


def status_operator_report(root: Path) -> OperatorEnvelope:
    """Return status and canonical delivery blockers from one locked snapshot."""

    try:
        with _operator_runtime_snapshot(root) as (initialized, database_exists):
            if not initialized:
                return (
                    _unavailable_existing_state_report(root)
                    if database_exists
                    else uninitialized_operator_report(root)
                )
            details = _status_details_active(root)
            workflow = _quickstart_status_active(root)
    except OPERATOR_STATE_READ_ERRORS as exc:
        return operator_error_report(root, exc, allow_init=False)

    blockers = tuple(
        {
            "code": str(blocker["code"]),
            "message": str(blocker["message"]),
        }
        for blocker in workflow.get("delivery_blockers", [])
    )
    complete_details = dict(details)
    complete_details["workflow"] = workflow
    return build_operator_envelope(
        state=(
            "ready-for-delivery"
            if workflow.get("ready_for_delivery")
            else "needs-work"
        ),
        blockers=blockers,
        actions=tuple(
            str(command) for command in workflow.get("next_commands", [])
        ),
        details=complete_details,
    )


def operator_verbose_lines(report: OperatorEnvelope) -> list[str]:
    """Render the legacy complete human view from one frozen report."""

    details = report.as_dict()["details"]
    if not isinstance(details, dict):  # Defensive: the envelope enforces an object.
        raise HarnessError("operator report details must be an object")
    error = details.get("error")
    if isinstance(error, str) and error:
        lines = [line for line in error.splitlines() if line.strip()]
        if report.state == "not-initialized":
            return [
                f"ERROR: {lines[0]}",
                *(f"NEXT: {action}" for action in report.actions),
            ]
        return [f"ERROR: {line}" for line in lines]
    issues = details.get("issues")
    if isinstance(issues, list):
        return (
            [
                *(f"ERROR: {issue}" for issue in issues),
                *(f"NEXT: {action}" for action in report.actions),
            ]
            if issues
            else ["OK: harness doctor passed"]
        )
    workflow = details.get("workflow")
    if isinstance(workflow, dict):
        return [
            *_status_lines_from_details(details),
            "",
            *_quickstart_status_lines_from_report(workflow),
        ]
    if "ready_for_delivery" in details:
        return _quickstart_status_lines_from_report(details)
    raise HarnessError("operator report has no complete verbose representation")


def repair(root: Path, *, dry_run: bool = False, clear_invariant: str = "", confirm: str = "") -> list[str]:
    if clear_invariant:
        return [f"unsupported invariant repair code: {clear_invariant}"]
    if dry_run:
        issues = doctor(root)
        plan = [
            "ensure runtime .gitignore patterns",
            "initialize missing sqlite state",
            f"migrate schema to {SCHEMA_VERSION}",
            "render generated harness views",
        ]
        return issues + [f"repair action: {item}" for item in plan]
    if runtime_initialized(root):
        backup_sqlite_database(root, expected_source_version=SCHEMA_VERSION)
    init_runtime(root)
    migrate(root, str(SCHEMA_VERSION), SCHEMA_VERSION)
    render_all(root)
    return []


@_project_mutation
def render_all(root: Path) -> None:
    from core.projections import render_all as core_render_all

    core_render_all(root)


@_project_mutation
def render_affected(root: Path, *projections: str) -> None:
    from core.projections import render_affected as core_render_affected

    core_render_affected(root, projections)


def grouped(conn: sqlite3.Connection, table: str, key: str, value: str, cycle_id: str = "") -> dict[str, str]:
    where = " where cycle_id = ?" if cycle_id else ""
    params: tuple[str, ...] = (cycle_id,) if cycle_id else ()
    return {
        row[key]: row["ids"]
        for row in conn.execute(
            f"select {key}, group_concat({value}, ', ') as ids from {table}{where} group by {key}",
            params,
        )
    }

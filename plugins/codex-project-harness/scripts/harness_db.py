#!/usr/bin/env python3
"""SQLite-backed runtime for Codex Project Harness."""

from __future__ import annotations

import json
import hashlib
import os
import re
import shlex
import shutil
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

from harness_lib import ensure_parent, git_dirty, markdown_row, now_iso
from core import RUNTIME_VERSION, SCHEMA_VERSION
from core.errors import HarnessError
from core.lock_manager import parse_time
from core.cycle_ledger import (
    DEFAULT_CYCLE_ID,
    LEGACY_CYCLE_ID,
    baseline_issues,
    baseline_snapshot,
    current_candidate_sha,
    current_cycle_id,
    current_cycle_row,
    ensure_delivery_cycles,
    project_row,
    trace_rows,
    trace_snapshot,
    traceability_issues,
)
from core.schema_guard import (
    FAILURE_MODE_STATUSES,
    RESULT_FORMATS,
    SANDBOX_STATUSES,
    STACK_PROFILES,
    TASK_STATUSES,
    TEST_TARGET_KINDS,
)
from core.store import (
    DB_PATH,
    MIGRATION_SENTINEL_PATH,
    OPERATION_LOCK_PATH,
    InMemoryStore,
    SqliteStore,
    Store,
    project_db_operation,
    raise_if_project_migration_announced,
)
from core.project_fs import ProjectFS, pin_project_filesystem
from core.schema_lifecycle import (
    SCHEMA30_CATALOG_TABLES,
    SCHEMA30_TABLES,
    backup_sqlite_database,
    create_schema as create_schema29,
    create_schema30,
)


REGISTERED_SCHEMA_SOURCES = frozenset({27, 28, 29})
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
RUNTIME_GITIGNORE_PATTERNS = [
    ".ai-team/state/",
    ".ai-team/backups/",
    ".ai-team/runtime/",
    "__pycache__/",
    "*.pyc",
]
CODEX_AGENT_TEMPLATE_NAMES = frozenset({"architect.toml", "developer.toml", "qa-reviewer.toml"})
CODEX_AGENT_TEMPLATE_FIELDS = frozenset({"name", "description", "developer_instructions"})

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


def _project_mutation(function: Callable[..., Any]) -> Callable[..., Any]:
    """Keep each DB mutation and its synchronous projections in one operation lock."""

    @wraps(function)
    def locked(root: Path, *args: Any, **kwargs: Any) -> Any:
        if isinstance(get_store(root), InMemoryStore):
            return function(root, *args, **kwargs)
        if function.__name__ == "init_runtime":
            _preflight_init_paths(root)
        with project_db_operation(root):
            from core.projections import preflight_projection_paths

            preflight_projection_paths(root)
            return function(root, *args, **kwargs)

    return locked


def _preflight_init_paths(root: Path) -> None:
    """Audit every canonical init destination before the operation lock writes."""

    from core.projections import PROJECTION_ROLLBACK_PATHS

    database_family = SqliteStore._db_family()
    templates = tuple(
        Path(".codex/agents") / name
        for name in sorted(CODEX_AGENT_TEMPLATE_NAMES)
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
    """Create or validate the active schema 30 local-only Kernel."""

    tables = {
        str(row[0])
        for row in conn.execute(
            "select name from sqlite_master where type='table'"
        )
    }
    if not tables:
        create_schema30(conn)
        return
    if tables == SCHEMA30_CATALOG_TABLES:
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
        "active schema 30 table inventory mismatch: "
        f"missing={sorted(SCHEMA30_CATALOG_TABLES - tables)} "
        f"extra={sorted(tables - SCHEMA30_CATALOG_TABLES)}"
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
                exists = conn.execute("select 1 from sqlite_master where type='table' and name = 'project'").fetchone()
                if not exists:
                    return False
                return conn.execute("select 1 from project where id = 1").fetchone() is not None
        except sqlite3.Error:
            return False


def _runtime_audit_inventory() -> tuple[Path, ...]:
    from core.projections import PROJECTION_ROLLBACK_PATHS

    templates = tuple(
        Path(".codex/agents") / name
        for name in sorted(CODEX_AGENT_TEMPLATE_NAMES)
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


def audit_runtime_paths(root: Path) -> None:
    """Fail closed on the bounded canonical inventory before SQLite opens."""

    with runtime_path_audit(root):
        pass


def uninitialized_lines(root: Path) -> list[str]:
    harness_py = Path(__file__).resolve().with_name("harness.py")
    return [
        f"ERROR: harness is not initialized in this project: {root}",
        f"NEXT: python3 {harness_py} --root {root} init",
        f"NEXT: python3 {harness_py} --root {root} quickstart status",
    ]


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
def transaction(root: Path, *, validate_invariants: bool = True, touched: list[tuple[str, str]] | None = None) -> Iterator[sqlite3.Connection]:
    def before_commit(conn: sqlite3.Connection) -> None:
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


def gitignore_runtime_issues(root: Path) -> list[str]:
    issues: list[str] = []
    with ProjectFS.open(root) as project_fs:
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
    from core.event_bus import emit_audit

    emit_audit(
        conn,
        SCHEMA_VERSION,
        event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        before=before,
        after=after,
        actor=actor,
        command=command,
        extra=extra,
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
    actual = {path.name for path in template_dir.glob("*.toml") if path.is_file()}
    if actual != CODEX_AGENT_TEMPLATE_NAMES:
        raise HarnessError(
            "agent template inventory mismatch: "
            f"actual={sorted(actual)} expected={sorted(CODEX_AGENT_TEMPLATE_NAMES)}"
        )
    installed = 0
    with ProjectFS.open(root) as project_fs:
        destinations = tuple(
            Path(".codex/agents") / name
            for name in sorted(CODEX_AGENT_TEMPLATE_NAMES)
        )
        project_fs.audit(destinations, allow_missing=True)
        for name, destination in zip(
            sorted(CODEX_AGENT_TEMPLATE_NAMES),
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


def cycle_status(root: Path) -> dict[str, Any]:
    with connection(root) as conn:
        cycle = current_cycle_row(conn)
        return row_snapshot(cycle) or {}


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
        bump_project(conn, current_cycle_id=cycle_id, phase="intake", status="draft")
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
    if status not in {"delivered", "archived"}:
        raise HarnessError("cycle close status must be delivered or archived")
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
    render_affected(root, "project-state")


@_project_mutation
def transition_phase(root: Path, phase: str, *, status: str | None = None, owner: str | None = None) -> None:
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
        if phase == "delivery_readiness":
            delivery_issues = validate_delivery(conn, root, require_phase=False)
            if delivery_issues:
                raise HarnessError("delivery readiness blocked: " + "; ".join(delivery_issues))
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
    with transaction(root, touched=[("project", "1")]) as conn:
        before = project_row(conn)
        bump_project(conn, scope_status="confirmed", current_owner=by, status="scope-confirmed")
        after = project_row(conn)
        emit_audit_event(
            conn,
            "scope_confirmed",
            entity_type="project",
            entity_id=str(before["project_id"]),
            before=row_snapshot(before),
            after=row_snapshot(after),
            actor=by,
            command="scope confirm",
            extra={"summary": summary},
        )
    render_affected(root, "project-state")


def freeze_baseline(root: Path, baseline_id: str, summary: str, *, by: str = "") -> None:
    with transaction(root, touched=[("baseline", baseline_id)]) as conn:
        snapshot = baseline_snapshot(conn)
        digest = stable_digest(snapshot)
        cycle_id = current_cycle_id(conn)
        conn.execute(
            """
            insert into baselines (id, cycle_id, summary, snapshot_json, digest, project_revision, created_by, created_at)
            values (?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(id) do update set cycle_id=excluded.cycle_id, summary=excluded.summary, snapshot_json=excluded.snapshot_json,
              digest=excluded.digest, project_revision=excluded.project_revision, created_by=excluded.created_by,
              created_at=excluded.created_at
            """,
            (baseline_id, cycle_id, summary, stable_json(snapshot), digest, int(project_row(conn)["revision"]), by, now_iso()),
        )
        emit_audit_event(
            conn,
            "baseline_frozen",
            entity_type="baseline",
            entity_id=baseline_id,
            before=None,
            after={"id": baseline_id, "summary": summary, "digest": digest},
            actor=by,
            command="baseline freeze",
        )


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


@_project_mutation
def add_requirement(root: Path, requirement_id: str, kind: str, body: str, priority: str = "", status: str = "active") -> None:
    guard_schema("validate_requirement", requirement_id, kind, body, status)
    with transaction(root, touched=[("requirement", requirement_id)]) as conn:
        cycle_id = current_cycle_id(conn)
        existing = conn.execute("select * from requirements where cycle_id = ? and id = ?", (cycle_id, requirement_id)).fetchone()
        conn.execute(
            """
            insert into requirements (id, cycle_id, kind, body, priority, status, updated_at)
            values (?, ?, ?, ?, ?, ?, ?)
            on conflict(cycle_id, id) do update set kind=excluded.kind, body=excluded.body, priority=excluded.priority,
              status=excluded.status, revision=requirements.revision+1, updated_at=excluded.updated_at
            """,
            (requirement_id, cycle_id, kind, body, priority, status, now_iso()),
        )
        if existing and (existing["kind"], existing["body"], existing["priority"], existing["status"]) != (kind, body, priority, status):
            invalidate_downstream(conn, "requirement", requirement_id, "requirement changed")
        after = conn.execute("select * from requirements where cycle_id = ? and id = ?", (cycle_id, requirement_id)).fetchone()
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
    render_affected(root, "project-state", "requirements", "traceability")


@_project_mutation
def add_acceptance(root: Path, acceptance_id: str, criterion: str, priority: str = "") -> None:
    guard_schema("validate_acceptance", acceptance_id, criterion)
    with transaction(root, touched=[("acceptance", acceptance_id)]) as conn:
        cycle_id = current_cycle_id(conn)
        existing = conn.execute("select * from acceptance where cycle_id = ? and id = ?", (cycle_id, acceptance_id)).fetchone()
        conn.execute(
            """
            insert into acceptance (id, cycle_id, criterion, priority)
            values (?, ?, ?, ?)
            on conflict(cycle_id, id) do update set criterion=excluded.criterion,
                priority=excluded.priority, revision=acceptance.revision+1
            """,
            (acceptance_id, cycle_id, criterion, priority),
        )
        if existing and (existing["criterion"], existing["priority"]) != (criterion, priority):
            invalidate_downstream(conn, "acceptance", acceptance_id, "acceptance criterion changed")
        after = conn.execute("select * from acceptance where cycle_id = ? and id = ?", (cycle_id, acceptance_id)).fetchone()
        emit_audit_event(
            conn,
            "acceptance_recorded",
            entity_type="acceptance",
            entity_id=acceptance_id,
            before=row_snapshot(existing),
            after=row_snapshot(after),
            command="acceptance add",
        )
    render_affected(root, "acceptance", "traceability")


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
    guard_schema("validate_failure_mode", fm_id, risk, status)
    with transaction(root, touched=[("failure_mode", fm_id)]) as conn:
        cycle_id = current_cycle_id(conn)
        existing = conn.execute("select * from failure_modes where cycle_id = ? and id = ?", (cycle_id, fm_id)).fetchone()
        accepted_revision = None
        if status not in FAILURE_MODE_STATUSES:
            raise HarnessError("failure mode status must be identified, accepted, or exempt; coverage is derived from passing validation")
        if status in {"accepted", "exempt"}:
            if not accepted_by or not acceptance_reason or not acceptance_scope or not expires_at:
                raise HarnessError("accepted or exempt failure modes require accepted-by, acceptance-reason, acceptance-scope, and expires-at")
            accepted_revision = int(project_row(conn)["revision"])
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
        after = conn.execute("select * from failure_modes where cycle_id = ? and id = ?", (cycle_id, fm_id)).fetchone()
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
    render_affected(root, "failure-modes")


def require_acceptance(conn: sqlite3.Connection, acceptance_id: str) -> None:
    cycle_id = current_cycle_id(conn)
    if not conn.execute("select id from acceptance where cycle_id = ? and id = ?", (cycle_id, acceptance_id)).fetchone():
        raise HarnessError(f"missing acceptance: {acceptance_id}")


def require_requirement(conn: sqlite3.Connection, requirement_id: str) -> None:
    cycle_id = current_cycle_id(conn)
    if not conn.execute("select id from requirements where cycle_id = ? and id = ?", (cycle_id, requirement_id)).fetchone():
        raise HarnessError(f"missing requirement: {requirement_id}")


@_project_mutation
def link_requirement_acceptance(root: Path, requirement_id: str, acceptance_id: str) -> None:
    with transaction(root, touched=[("requirement", requirement_id), ("acceptance", acceptance_id)]) as conn:
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
    guard_schema("validate_task", task_id, task, "planned")
    with transaction(root, touched=[("task", task_id)]) as conn:
        cycle_id = current_cycle_id(conn)
        if conn.execute("select id from tasks where cycle_id = ? and id = ?", (cycle_id, task_id)).fetchone():
            raise HarnessError(f"duplicate task id: {task_id}")
        conn.execute(
            """
            insert into tasks (id, cycle_id, task, owner, status, updated_at)
            values (?, ?, ?, ?, 'planned', ?)
            """,
            (task_id, cycle_id, task, owner, now_iso()),
        )
        conn.execute("delete from task_acceptance where cycle_id = ? and task_id = ?", (cycle_id, task_id))
        for acceptance_id in parse_ids(acceptance):
            require_acceptance(conn, acceptance_id)
            conn.execute("insert into task_acceptance (cycle_id, task_id, acceptance_id) values (?, ?, ?)", (cycle_id, task_id, acceptance_id))
        conn.execute("delete from task_failure_modes where cycle_id = ? and task_id = ?", (cycle_id, task_id))
        for fm_id in parse_ids(failure_modes):
            if not conn.execute("select id from failure_modes where cycle_id = ? and id = ?", (cycle_id, fm_id)).fetchone():
                raise HarnessError(f"missing failure mode: {fm_id}")
            conn.execute("insert into task_failure_modes (cycle_id, task_id, failure_mode_id) values (?, ?, ?)", (cycle_id, task_id, fm_id))
        conn.execute("delete from task_dependencies where cycle_id = ? and task_id = ?", (cycle_id, task_id))
        for dep in parse_ids(depends_on):
            require_task(conn, dep)
            assert_no_dependency_cycle(conn, task_id, dep)
            conn.execute("insert into task_dependencies (cycle_id, task_id, depends_on) values (?, ?, ?)", (cycle_id, task_id, dep))
        after = conn.execute("select * from tasks where cycle_id = ? and id = ?", (cycle_id, task_id)).fetchone()
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
    render_affected(root, "tasks")


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
    render_affected(root, "tasks")


@_project_mutation
def record_decision(root: Path, decision: str, reason: str) -> None:
    with transaction(root) as conn:
        decision_id = str(uuid.uuid4())
        conn.execute(
            "insert into decisions (id, decision, reason, created_at) values (?, ?, ?, ?)",
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
    guard_schema("validate_test_target", target_id, kind, command_template, stack_profile, result_format)
    gateable, gate_block_reason = target_gateability(kind, command_template)
    with transaction(root, touched=[("test_target", target_id)]) as conn:
        before = conn.execute("select * from test_targets where id = ?", (target_id,)).fetchone()
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
        after = conn.execute("select * from test_targets where id = ?", (target_id,)).fetchone()
        emit_audit_event(
            conn,
            "test_target_recorded",
            entity_type="test_target",
            entity_id=target_id,
            before=row_snapshot(before),
            after=row_snapshot(after),
            command="test-target add",
        )
    render_affected(root, "test-targets")


def link_task_test_target(root: Path, task_id: str, target_id: str) -> None:
    with transaction(root, touched=[("task", task_id)]) as conn:
        cycle_id = current_cycle_id(conn)
        if not conn.execute("select id from tasks where cycle_id = ? and id = ?", (cycle_id, task_id)).fetchone():
            raise HarnessError(f"missing task: {task_id}")
        if not conn.execute("select id from test_targets where id = ?", (target_id,)).fetchone():
            raise HarnessError(f"missing test target: {target_id}")
        conn.execute("insert or ignore into task_test_targets (cycle_id, task_id, target_id) values (?, ?, ?)", (cycle_id, task_id, target_id))
        emit_audit_event(
            conn,
            "task_test_target_linked",
            entity_type="task_test_target",
            entity_id=f"{cycle_id}:{task_id}:{target_id}",
            before=None,
            after={"cycle_id": cycle_id, "task_id": task_id, "target_id": target_id},
            command="test-target link",
        )


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
    with connection(root) as conn:
        cycle_id = current_cycle_id(conn)
        candidate_sha = current_candidate_sha(root)
        target_row = conn.execute(
            "select * from test_targets where id = ?", (target_id,)
        ).fetchone()
        if not target_row:
            raise HarnessError(f"missing test target: {target_id}")
        target_data = dict(target_row)
        if int(target_data.get("gateable") or 0) != 1:
            reason = str(target_data.get("gate_block_reason") or "not gateable")
            raise HarnessError(f"test target is not gateable: {target_id}: {reason}")
        if acceptance and not conn.execute(
            "select 1 from acceptance where cycle_id = ? and id = ?",
            (cycle_id, acceptance),
        ).fetchone():
            raise HarnessError(f"missing acceptance: {acceptance}")
        for failure_mode_id in requested_failure_modes:
            if not conn.execute(
                "select 1 from failure_modes where cycle_id = ? and id = ?",
                (cycle_id, failure_mode_id),
            ).fetchone():
                raise HarnessError(f"missing failure mode: {failure_mode_id}")

    policy = target_policy_from_row(target_data)
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
            )
        else:
            result = LocalExecutor(root).run(
                policy.command_template,
                target_id=policy.id,
                target_command_template=policy.command_template,
                result_format=policy.result_format,
                result_path=policy.result_path,
            )
        validate_execution_result(root, policy, result, runner=runner)
    except ExecutionPolicyError as exc:
        raise HarnessError(str(exc)) from exc

    execution_id = f"EX-{uuid.uuid4().hex}"
    validation_id = f"VAL-{uuid.uuid4().hex}"
    surface = f"test-target:{target_id}"
    with transaction(
        root,
        touched=[("execution", execution_id), ("validation", validation_id)],
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
        if not live_target or dict(live_target) != target_data:
            raise HarnessError(
                f"stale target: registered target changed during verification: {target_id}"
            )
        if acceptance and not conn.execute(
            "select 1 from acceptance where cycle_id = ? and id = ?",
            (cycle_id, acceptance),
        ).fetchone():
            raise HarnessError(f"stale acceptance: {acceptance}")
        for failure_mode_id in requested_failure_modes:
            if not conn.execute(
                "select 1 from failure_modes where cycle_id = ? and id = ?",
                (cycle_id, failure_mode_id),
            ).fetchone():
                raise HarnessError(f"stale failure mode: {failure_mode_id}")
        try:
            validate_execution_result(root, policy, result, runner=runner)
        except ExecutionPolicyError as exc:
            raise HarnessError(str(exc)) from exc
        created_at = now_iso()
        conn.execute(
            """
            insert into executions
            (id, cycle_id, candidate_sha, target_id, command, exit_code,
             stdout_sha256, artifact_path, executed_count, result_format,
             semantic_status, runner, sandbox_status, no_network, policy_status,
             created_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                execution_id,
                cycle_id,
                candidate_sha,
                target_id,
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
                created_at,
            ),
        )
        conn.execute(
            """
            insert into validations
            (id, cycle_id, candidate_sha, acceptance_id, surface, result,
             validation_status, superseded_by, findings, residual_risk, created_at)
            values (?, ?, ?, ?, ?, 'pass', 'active', null,
                    'controller execution passed', '', ?)
            """,
            (
                validation_id,
                cycle_id,
                candidate_sha,
                acceptance or None,
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
) -> None:
    guard_schema("validate_gate", reviewer_context, result, gate)
    if reviewer_context == "fresh" and not reviewer_context_id:
        raise HarnessError("fresh reviewer context requires reviewer context metadata")
    if result == "pass" and git_dirty(root):
        raise HarnessError("cannot record a passing quality gate with a dirty git worktree")
    with transaction(root) as conn:
        cycle_id = current_cycle_id(conn)
        candidate_sha = current_candidate_sha(root)
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
        producer_context_id = ",".join(producer_contexts)
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
        for finding_id in parse_ids(findings):
            if not conn.execute("select id from findings where id = ?", (finding_id,)).fetchone():
                raise HarnessError(f"missing finding: {finding_id}")
            conn.execute("insert or ignore into quality_gate_findings (gate_id, finding_id) values (?, ?)", (gate_id, finding_id))
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
            },
        )
    render_affected(root, "gates")


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
    with transaction(root) as conn:
        from core.delivery import evaluate_schema30_delivery

        project = project_row(conn)
        cycle = current_cycle_row(conn)
        if cycle["status"] not in {"active", "delivered"}:
            raise HarnessError(f"delivery record requires active current cycle, current={cycle['status']}")
        candidate_sha = current_candidate_sha(root)
        issues, trust = evaluate_schema30_delivery(
            conn,
            root,
            is_expired=is_expired,
        )
        if issues:
            raise HarnessError("delivery record blocked: " + "; ".join(issues))
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
        for acceptance_id in parse_ids(acceptance):
            if not conn.execute(
                "select id from acceptance where cycle_id = ? and id = ?",
                (cycle["id"], acceptance_id),
            ).fetchone():
                continue
            conn.execute(
                "insert or ignore into delivery_acceptance (delivery_id, cycle_id, acceptance_id) values (?, ?, ?)",
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
    render_affected(root, "deliveries", *(["traceability"] if acceptance else []))


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
    if not db_file(root).exists():
        raise HarnessError("migration requires an initialized runtime")
    current_schema_issues: list[str] = []
    with connection(root) as conn:
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
    if dry_run:
        return {
            "dry_run": True,
            "imported": {"schema_migration": len(path)},
            "skipped": {},
            "unrecognized": [],
        }
    if not path:
        render_all(root)
        return None

    from core.local_core_migration import migrate_project_to_schema30

    def validate_staging(staging_path: Path) -> None:
        with tempfile.TemporaryDirectory(prefix="kafa-projection-dry-run-", dir=staging_path.parent) as temp:
            staging_root = Path(temp)
            staging_db = staging_root / DB_PATH
            ensure_parent(staging_db)
            shutil.copyfile(staging_path, staging_db)
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

    migrate_project_to_schema30(
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
        issues: list[str] = []
        if isinstance(store, SqliteStore):
            if project_fs is None:
                return ["missing sqlite state: .ai-team/state/harness.db"]
            if not project_fs._snapshot(
                DB_PATH,
                allow_missing=True,
            ).exists:
                return ["missing sqlite state: .ai-team/state/harness.db"]
        if require_project_files:
            issues.extend(gitignore_runtime_issues(root))
        with store.connection() as conn:
            try:
                project = project_row(conn)
            except HarnessError as exc:
                issues.append(str(exc))
            else:
                if int(project["schema_version"]) != SCHEMA_VERSION:
                    issues.append(f"schema version mismatch: expected {SCHEMA_VERSION}, actual {project['schema_version']}")
                if project["runtime_version"] != RUNTIME_VERSION:
                    issues.append(f"runtime version mismatch: expected {RUNTIME_VERSION}, actual {project['runtime_version']}")
            integrity = conn.execute("pragma integrity_check").fetchone()[0]
            if integrity != "ok":
                issues.append(f"sqlite integrity check failed: {integrity}")
            foreign_key_errors = conn.execute("pragma foreign_key_check").fetchall()
            if foreign_key_errors:
                issues.append(f"sqlite foreign key check failed: {len(foreign_key_errors)} issue(s)")
            issues.extend(runtime_schema_issues(conn))
            from core.invariant_checker import check_runtime_invariants

            issues.extend(check_runtime_invariants(conn, root))
            if require_views:
                from core.projections import projection_content_issues

                issues.extend(projection_content_issues(root))
        return issues


def runtime_schema_issues(conn: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    tables = {
        str(row[0])
        for row in conn.execute(
            "select name from sqlite_master where type='table'"
        )
    }
    if tables != SCHEMA30_CATALOG_TABLES:
        issues.append(
            "schema 30 table inventory mismatch: "
            f"missing={sorted(SCHEMA30_CATALOG_TABLES - tables)} "
            f"extra={sorted(tables - SCHEMA30_CATALOG_TABLES)}"
        )
        return issues
    enum_checks = [
        ("delivery_cycles", "status", DELIVERY_CYCLE_STATUSES, "delivery cycle status"),
        ("delivery_cycles", "phase", set(PHASES), "delivery cycle phase"),
        ("tasks", "status", TASK_STATUSES, "task status"),
        ("failure_modes", "risk", {"low", "medium", "high", "critical"}, "failure mode risk"),
        ("failure_modes", "status", FAILURE_MODE_STATUSES | {"active"}, "failure mode status"),
        ("validations", "result", {"pass", "fail", "blocked", "partial"}, "validation result"),
        ("validations", "validation_status", VALIDATION_STATUSES, "validation status"),
        ("quality_gates", "result", {"pass", "fail", "conditional", "blocked"}, "quality gate result"),
        ("test_targets", "kind", TEST_TARGET_KINDS, "test target kind"),
        ("test_targets", "stack_profile", STACK_PROFILES, "test target stack profile"),
        ("test_targets", "result_format", RESULT_FORMATS, "test target result format"),
        ("executions", "result_format", RESULT_FORMATS, "execution result format"),
        ("executions", "semantic_status", {"pass", "fail"}, "execution semantic status"),
        ("executions", "runner", {"local", "container"}, "execution runner"),
        ("executions", "sandbox_status", SANDBOX_STATUSES, "execution sandbox status"),
        ("executions", "policy_status", {"allowed", "rejected"}, "execution policy status"),
    ]
    for table, column, allowed, label in enum_checks:
        placeholders = ",".join("?" for _ in allowed)
        for row in conn.execute(
            f"select id, {column} as value from {table} where {column} not in ({placeholders})",
            tuple(allowed),
        ):
            issues.append(f"invalid {label}: {table}.{row['id']}={row['value']}")
    for row in conn.execute("select id, before_json, after_json from events"):
        for field in ("before_json", "after_json"):
            try:
                value = json.loads(row[field])
            except json.JSONDecodeError as exc:
                issues.append(f"invalid event {field}: {row['id']} {exc.msg}")
                continue
            if not isinstance(value, dict):
                issues.append(f"invalid event {field}: {row['id']} expected object")
    issues.extend(schema_contract_issues(conn))
    return issues


def schema_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "schemas"


def load_schema(name: str) -> dict[str, Any]:
    return json.loads((schema_dir() / name).read_text(encoding="utf-8"))


def json_type_matches(value: Any, expected: str | list[str]) -> bool:
    options = expected if isinstance(expected, list) else [expected]
    for option in options:
        if option == "null" and value is None:
            return True
        if option == "string" and isinstance(value, str):
            return True
        if option == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if option == "array" and isinstance(value, list):
            return True
        if option == "object" and isinstance(value, dict):
            return True
        if option == "boolean" and isinstance(value, bool):
            return True
    return False


def validate_object_against_schema(label: str, data: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    for field in schema.get("required", []):
        if field not in data or data[field] is None:
            issues.append(f"schema contract failed: {label}.{field} is required")
    properties = schema.get("properties", {})
    if schema.get("additionalProperties") is False:
        for field in data:
            if field not in properties:
                issues.append(f"schema contract failed: {label}.{field} is not declared")
    for field, definition in properties.items():
        if field not in data:
            continue
        value = data[field]
        expected_type = definition.get("type")
        if expected_type is not None and not json_type_matches(value, expected_type):
            issues.append(f"schema contract failed: {label}.{field} expected {expected_type}, got {type(value).__name__}")
            continue
        if "enum" in definition and value not in definition["enum"]:
            issues.append(f"schema contract failed: {label}.{field}={value} not in {definition['enum']}")
        if definition.get("type") == "array" and isinstance(value, list):
            item_type = definition.get("items", {}).get("type")
            if item_type:
                for index, item in enumerate(value):
                    if not json_type_matches(item, item_type):
                        issues.append(f"schema contract failed: {label}.{field}[{index}] expected {item_type}")
    return issues


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
        ("execution.schema.json", "executions", [row_snapshot(row) or {} for row in conn.execute("select * from executions")]),
        ("validation.schema.json", "validations", validations),
        ("finding.schema.json", "findings", [row_snapshot(row) or {} for row in conn.execute("select * from findings")]),
        ("quality-gate.schema.json", "quality_gates", [row_snapshot(row) or {} for row in conn.execute("select * from quality_gates")]),
        ("delivery.schema.json", "deliveries", [row_snapshot(row) or {} for row in conn.execute("select * from deliveries")]),
        ("baseline.schema.json", "baselines", [row_snapshot(row) or {} for row in conn.execute("select * from baselines")]),
        ("invalidation.schema.json", "invalidations", [row_snapshot(row) or {} for row in conn.execute("select * from invalidations")]),
        ("event.schema.json", "events", [row_snapshot(row) or {} for row in conn.execute("select * from events")]),
    ]


def schema_contract_issues(conn: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    for schema_name, entity, rows in schema_entity_rows(conn):
        schema = load_schema(schema_name)
        for row in rows:
            label = f"{entity}.{row.get('id', row.get('sequence', 'row'))}"
            issues.extend(validate_object_against_schema(label, row, schema))
    return issues






def trace_show(root: Path, requirement_id: str | None = None) -> list[str]:
    with connection(root) as conn:
        rows = trace_rows(conn, requirement_id)
        issues = traceability_issues(conn, requirement_id)
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


def trace_validate(root: Path) -> list[str]:
    with connection(root) as conn:
        return traceability_issues(conn)


def validate_delivery(conn: sqlite3.Connection, root: Path, *, require_phase: bool = False) -> list[str]:
    from core.delivery import evaluate_schema30_delivery_readiness

    return evaluate_schema30_delivery_readiness(
        conn,
        root,
        is_expired=is_expired,
    )


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
    command = [sys.executable, str(Path(__file__).resolve().with_name("harness.py")), "--root", str(root), *args]
    return subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)


def _quickstart_status_schema30(root: Path) -> dict[str, Any]:
    missing: list[str] = []
    next_commands: list[str] = []
    with connection(root) as conn:
        project = project_row(conn)
        cycle = current_cycle_row(conn)
        cycle_id = str(cycle["id"])
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
        target_rows = conn.execute(
            "select id from test_targets order by id"
        ).fetchall()
        linked_target_count = int(
            conn.execute(
                "select count(*) from task_test_targets where cycle_id=?",
                (cycle_id,),
            ).fetchone()[0]
        )
        execution_count = int(
            conn.execute(
                """
                select count(distinct e.id) from executions e
                join validation_executions ve on ve.execution_id=e.id
                join validations v on v.id=ve.validation_id
                where e.cycle_id=? and e.candidate_sha=? and e.exit_code=0
                  and e.executed_count>0 and e.semantic_status='pass'
                  and e.policy_status='allowed' and v.validation_status='active'
                  and v.result='pass'
                """,
                (cycle_id, candidate_sha),
            ).fetchone()[0]
        )
        quality_gate_count = int(
            conn.execute(
                """
                select count(*) from quality_gates
                where cycle_id=? and candidate_sha=? and gate_status='active'
                  and result='pass'
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
        delivery_issues = validate_delivery(conn, root)

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
    if baseline_missing:
        missing.append("baseline")
        next_commands.append(
            _render_harness_command(
                root, "baseline", "freeze", "--id", "BL1", "--summary", "current scope"
            )
        )
    if execution_count == 0:
        missing.append("controller_execution")
        verify_args = ["verify", "run", "--target", target_id]
        if acceptance_rows:
            verify_args.extend(["--acceptance", str(acceptance_rows[0]["id"])])
        next_commands.append(_render_harness_command(root, *verify_args))
    unresolved_tasks = [
        row for row in task_rows if row["status"] not in {"accepted", "cancelled"}
    ]
    if unresolved_tasks:
        missing.append("accepted_task")
        for row in unresolved_tasks:
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
        if execution_count and not unresolved_tasks:
            next_commands.append(
                _render_harness_command(
                    root,
                    "gate",
                    "record",
                    "--reviewer-context",
                    "same-context-degraded",
                    "--result",
                    "pass",
                )
            )
    if delivery_count == 0:
        missing.append("delivery")
        if not delivery_issues:
            next_commands.append(
                _render_harness_command(
                    root,
                    "delivery",
                    "record",
                    "--scope",
                    "verified local handoff",
                )
            )
    return {
        "initialized": True,
        "ready_for_delivery": bool(
            not delivery_issues
            and cycle["status"] in {"active", "delivered"}
        ),
        "missing": missing,
        "phase": project["phase"],
        "cycle_id": cycle_id,
        "cycle_status": cycle["status"],
        "delivery_issues": delivery_issues,
        "next_commands": next_commands,
    }


def quickstart_status(root: Path) -> dict[str, Any]:
    if not runtime_initialized(root):
        return {
            "initialized": False,
            "ready_for_delivery": False,
            "missing": ["init"],
            "phase": "",
            "cycle_id": "",
            "cycle_status": "",
            "next_commands": [
                _render_harness_command(root, "init"),
                _render_harness_command(root, "quickstart", "status"),
            ],
        }
    return _quickstart_status_schema30(root)

def quickstart_status_lines(root: Path) -> list[str]:
    report = quickstart_status(root)
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


def quickstart_minimal(root: Path, quickstart_id: str, goal: str, acceptance: str, task: str, test_command: str, *, execute: bool = False) -> list[str]:
    normalized_id = re.sub(r"[^A-Za-z0-9._-]+", "-", quickstart_id).strip("-._").upper()
    if not normalized_id:
        raise HarnessError("quickstart minimal requires --id")
    req_id = f"{normalized_id}-REQ1"
    ac_id = f"{normalized_id}-AC1"
    task_id = f"{normalized_id}-T1"
    target_id = f"{normalized_id}-UNIT"
    if not execute:
        return [
            f"DRY-RUN: would initialize runtime if needed for {normalized_id}",
            f"DRY-RUN: would record {req_id}, {ac_id}, {task_id}, {target_id}",
            "DRY-RUN: would run the controller-local test command and stop before independent review",
            f"NEXT: add --execute to run: {test_command}",
        ]
    lines: list[str] = []
    if not runtime_initialized(root):
        init_runtime(root)
        lines.append("OK: project harness initialized")
    add_requirement(root, req_id, "functional", goal, priority="must")
    add_acceptance(root, ac_id, acceptance, priority="must")
    link_requirement_acceptance(root, req_id, ac_id)
    add_test_target(root, target_id, "unit", test_command, "quickstart minimal executable target")
    add_task(root, task_id, task, owner="developer", acceptance=ac_id)
    link_task_test_target(root, task_id, target_id)
    lines.extend([f"OK: requirement added {req_id}", f"OK: acceptance added {ac_id}", f"OK: task added {task_id}", f"OK: test target recorded {target_id}"])

    for phase in ["project_bootstrap", "requirement_baseline"]:
        transition_if_needed(root, phase)
    freeze_baseline(root, f"{normalized_id}-BL1", "quickstart minimal baseline", by="quickstart")
    transition_if_needed(root, "confirmation")
    confirm_scope(root, "quickstart", f"{normalized_id}: {goal}")
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
        f"NEXT: stop for independent review of {task_id}; do not accept the task or "
        "record a passing quality gate until reviewer findings are returned to the root controller"
    )
    lines.append(f"NEXT: {_render_harness_command(root, 'quickstart', 'status')}")
    return lines


def transition_if_needed(root: Path, phase: str) -> None:
    with connection(root) as conn:
        current = project_row(conn)["phase"]
    if current != phase:
        transition_phase(root, phase)


def status_lines(root: Path) -> list[str]:
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
    return [
        "# Harness Status",
        f"status: {row['status']}",
        f"phase: {row['phase']}",
        f"scope_status: {row['scope_status']}",
        f"current_owner: {row['current_owner']}",
        f"schema_version: {row['schema_version']}",
        f"runtime_version: {row['runtime_version']}",
        f"revision: {row['revision']}",
        f"tasks: {task_count}",
        f"planned_tasks: {planned_count}",
        f"events: {event_count}",
    ]


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

#!/usr/bin/env python3
"""SQLite-backed runtime for Codex Project Harness."""

from __future__ import annotations

import json
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from harness_lib import ensure_parent, git_base_commit, git_dirty, git_head_sha, git_source_tree_hash, git_tracked_diff_hash, markdown_row, now_iso, write_state


SCHEMA_VERSION = 6
RUNTIME_VERSION = "2.4.0"
DB_PATH = Path(".ai-team/state/harness.db")
ADAPTER_MODES = {"read-only", "draft-write", "write-confirm", "write-auto", "disabled"}
LEASE_TTL_SECONDS = 3600

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

TASK_STATUSES = {
    "ready",
    "claimed",
    "in_progress",
    "submitted",
    "review",
    "blocked",
    "accepted",
    "failed",
    "cancelled",
    "skipped",
}

FAILURE_MODE_STATUSES = {"identified", "accepted", "exempt"}


class HarnessError(Exception):
    """User-facing runtime error."""


def db_file(root: Path) -> Path:
    return root / DB_PATH


def connect(root: Path) -> sqlite3.Connection:
    path = db_file(root)
    ensure_parent(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma journal_mode = wal")
    conn.execute("pragma foreign_keys = on")
    conn.execute("pragma busy_timeout = 5000")
    return conn


@contextmanager
def connection(root: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(root)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(root: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(root)
    try:
        conn.execute("begin immediate")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        create table if not exists project (
            id integer primary key check (id = 1),
            project_id text not null,
            schema_version integer not null,
            runtime_version text not null,
            phase text not null,
            status text not null,
            scope_status text not null,
            current_owner text not null,
            revision integer not null,
            updated_at text not null
        );
        create table if not exists acceptance (
            id text primary key,
            criterion text not null,
            priority text not null default '',
            tool_link text not null default '',
            status text not null default 'active',
            revision integer not null default 1
        );
        create table if not exists requirements (
            id text primary key,
            kind text not null,
            body text not null,
            priority text not null default '',
            status text not null default 'active',
            tool_link text not null default '',
            revision integer not null default 1,
            updated_at text not null
        );
        create table if not exists failure_modes (
            id text primary key,
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
            revision integer not null default 1
        );
        create table if not exists failure_mode_acceptance (
            failure_mode_id text not null references failure_modes(id) on delete cascade,
            acceptance_id text not null references acceptance(id) on delete cascade,
            primary key (failure_mode_id, acceptance_id)
        );
        create table if not exists tasks (
            id text primary key,
            task text not null,
            owner text not null,
            status text not null,
            evidence text not null default '',
            tool_link text not null default '',
            lease_agent text,
            lease_token text,
            lease_heartbeat_at text,
            lease_expires_at text,
            retry_count integer not null default 0,
            retry_budget integer not null default 2,
            revision integer not null default 1,
            updated_at text not null
        );
        create table if not exists task_acceptance (
            task_id text not null references tasks(id) on delete cascade,
            acceptance_id text not null references acceptance(id) on delete cascade,
            primary key (task_id, acceptance_id)
        );
        create table if not exists task_failure_modes (
            task_id text not null references tasks(id) on delete cascade,
            failure_mode_id text not null references failure_modes(id) on delete cascade,
            primary key (task_id, failure_mode_id)
        );
        create table if not exists task_dependencies (
            task_id text not null references tasks(id) on delete cascade,
            depends_on text not null references tasks(id) on delete restrict,
            primary key (task_id, depends_on),
            check (task_id != depends_on)
        );
        create table if not exists validations (
            id text primary key,
            surface text not null,
            acceptance_id text not null default '',
            commands text not null default '',
            findings text not null,
            result text not null,
            residual_risk text not null default '',
            head_commit text not null default '',
            source_tree_hash text not null default '',
            tracked_diff_hash text not null default '',
            project_revision integer not null default 0,
            created_at text not null
        );
        create table if not exists validation_failure_modes (
            validation_id text not null references validations(id) on delete cascade,
            failure_mode_id text not null references failure_modes(id) on delete cascade,
            primary key (validation_id, failure_mode_id)
        );
        create table if not exists quality_gates (
            id text primary key,
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
            created_at text not null
        );
        create table if not exists deliveries (
            id text primary key,
            scope text not null,
            acceptance text not null default '',
            changed_files text not null default '',
            validation text not null default '',
            qa text not null default '',
            failure_mode_coverage text not null default '',
            quality_gate text not null default '',
            data_config_notes text not null default '',
            collaboration_links text not null default '',
            known_gaps text not null default '',
            handoff text not null default '',
            created_at text not null
        );
        create table if not exists evidence (
            id text primary key,
            kind text not null,
            summary text not null,
            uri text not null default '',
            hash text not null default '',
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
            surface text not null,
            severity text not null,
            status text not null,
            summary text not null,
            evidence_id text not null default '',
            created_at text not null
        );
        create table if not exists decisions (
            id text primary key,
            decision text not null,
            reason text not null,
            created_at text not null
        );
        create table if not exists adapters (
            id text primary key,
            tool text not null,
            mode text not null,
            artifact text not null,
            external_id text not null default '',
            external_link text not null default '',
            idempotency_key text not null,
            evidence text not null default '',
            fallback text not null default '',
            confirmation_needed text not null default 'no',
            updated_at text not null,
            unique(tool, idempotency_key)
        );
        create table if not exists invalidations (
            id text primary key,
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
    ensure_column(conn, "failure_modes", "acceptance_scope", "text not null default ''")
    ensure_column(conn, "failure_modes", "accepted_revision", "integer")
    ensure_column(conn, "tasks", "lease_heartbeat_at", "text")
    ensure_column(conn, "tasks", "lease_expires_at", "text")
    ensure_column(conn, "quality_gates", "base_commit", "text not null default ''")
    ensure_column(conn, "quality_gates", "head_commit", "text not null default ''")
    ensure_column(conn, "quality_gates", "tracked_diff_hash", "text not null default ''")
    ensure_column(conn, "quality_gates", "project_revision", "integer not null default 0")
    ensure_column(conn, "validations", "head_commit", "text not null default ''")
    ensure_column(conn, "validations", "source_tree_hash", "text not null default ''")
    ensure_column(conn, "validations", "tracked_diff_hash", "text not null default ''")
    ensure_column(conn, "validations", "project_revision", "integer not null default 0")


def ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    columns = {row["name"] for row in conn.execute(f"pragma table_info({table})")}
    if column not in columns:
        conn.execute(f"alter table {table} add column {column} {ddl}")


def initialize_project(conn: sqlite3.Connection) -> None:
    existing = conn.execute("select id from project where id = 1").fetchone()
    if existing:
        return
    conn.execute(
        """
        insert into project
        (id, project_id, schema_version, runtime_version, phase, status, scope_status, current_owner, revision, updated_at)
        values (1, ?, ?, ?, 'intake', 'draft', 'unconfirmed', 'project-manager', 1, ?)
        """,
        (str(uuid.uuid4()), SCHEMA_VERSION, RUNTIME_VERSION, now_iso()),
    )


def emit_event(
    conn: sqlite3.Connection,
    event_type: str,
    payload_json: str,
    *,
    source: str = "harness-runtime",
    target: str = "project",
    idempotency_key: str = "",
    correlation_id: str = "",
    causation_id: str = "",
) -> None:
    conn.execute(
        """
        insert into events
        (id, schema_version, type, source, target, correlation_id, causation_id, idempotency_key, payload_json, created_at)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            SCHEMA_VERSION,
            event_type,
            source,
            target,
            correlation_id,
            causation_id,
            idempotency_key,
            payload_json,
            now_iso(),
        ),
    )


def payload(**values: object) -> str:
    return json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def lease_deadline() -> str:
    return (datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=LEASE_TTL_SECONDS)).isoformat()


def is_expired(value: str | None) -> bool:
    parsed = parse_time(value)
    return bool(parsed and parsed <= datetime.now(timezone.utc))


def normalize_failure_mode_status(status: str) -> str:
    return status if status in FAILURE_MODE_STATUSES else "identified"


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
    targets: list[tuple[str, str]] = []
    if source_type == "acceptance":
        targets.extend(("task", row["task_id"]) for row in conn.execute("select task_id from task_acceptance where acceptance_id = ?", (source_id,)))
        targets.extend(("validation", row["id"]) for row in conn.execute("select id from validations where acceptance_id = ?", (source_id,)))
        targets.extend(("quality_gate", row["id"]) for row in conn.execute("select id from quality_gates"))
    elif source_type == "failure_mode":
        targets.extend(("task", row["task_id"]) for row in conn.execute("select task_id from task_failure_modes where failure_mode_id = ?", (source_id,)))
        targets.extend(
            ("validation", row["validation_id"])
            for row in conn.execute("select validation_id from validation_failure_modes where failure_mode_id = ?", (source_id,))
        )
        targets.extend(("quality_gate", row["id"]) for row in conn.execute("select id from quality_gates"))
    elif source_type == "requirement":
        targets.extend(("quality_gate", row["id"]) for row in conn.execute("select id from quality_gates"))
    for target_type, target_id in targets:
        exists = conn.execute(
            """
            select 1 from invalidations
            where source_type = ? and source_id = ? and target_type = ? and target_id = ? and resolved_at is null
            """,
            (source_type, source_id, target_type, target_id),
        ).fetchone()
        if exists:
            continue
        conn.execute(
            """
            insert into invalidations (id, source_type, source_id, target_type, target_id, reason, created_at)
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            (str(uuid.uuid4()), source_type, source_id, target_type, target_id, reason, now_iso()),
        )


def resolve_invalidations(conn: sqlite3.Connection, *, source_type: str | None = None, source_id: str | None = None, target_type: str | None = None) -> None:
    clauses = ["resolved_at is null"]
    values: list[object] = []
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


def init_runtime(root: Path) -> None:
    if not db_file(root).exists() and has_legacy_markdown_data(root):
        migrate_markdown_v1(root)
        return
    with transaction(root) as conn:
        create_schema(conn)
        initialize_project(conn)
        emit_event(conn, "runtime_initialized", payload())
    render_all(root)
    install_agents(root)


def backup_runtime(root: Path, reason: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = root / ".ai-team" / "backups" / f"{stamp}-{reason}-{uuid.uuid4().hex[:8]}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    for relpath in [
        ".ai-team/state",
        ".ai-team/control",
        ".ai-team/requirements",
        ".ai-team/planning",
        "docs/harness",
    ]:
        source = root / relpath
        if not source.exists():
            continue
        target = backup_dir / relpath
        ensure_parent(target)
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            shutil.copy2(source, target)
    return backup_dir


def has_legacy_markdown_data(root: Path) -> bool:
    for relpath in [
        ".ai-team/requirements/acceptance.md",
        ".ai-team/requirements/failure-modes.md",
        ".ai-team/planning/task-board.md",
        "docs/harness/validation.md",
        "docs/harness/quality-gates.md",
        "docs/harness/delivery.md",
    ]:
        path = root / relpath
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        data_lines = [
            line
            for line in text.splitlines()
            if line.startswith("|") and "---" not in line and not line.lower().startswith("| id ")
        ]
        if data_lines:
            return True
    return False


def markdown_table_rows(path: Path) -> list[list[str]]:
    if not path.exists():
        return []
    rows: list[list[str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or "---" in stripped:
            continue
        cells = [cell.strip().replace("\\|", "|") for cell in stripped.strip("|").split("|")]
        if not cells or cells[0].lower() in {"id", "surface", "gate", "date"}:
            continue
        rows.append(cells)
    return rows


def migrate_markdown_v1(root: Path) -> None:
    backup_runtime(root, "markdown-v1")
    with transaction(root) as conn:
        create_schema(conn)
        initialize_project(conn)
        for cells in markdown_table_rows(root / ".ai-team/requirements/acceptance.md"):
            if len(cells) < 2:
                continue
            conn.execute(
                """
                insert into acceptance (id, criterion, priority, tool_link, status)
                values (?, ?, ?, ?, ?)
                on conflict(id) do nothing
                """,
                (cells[0], cells[1], cells[2] if len(cells) > 2 else "", cells[3] if len(cells) > 3 else "", cells[4] if len(cells) > 4 else "active"),
            )
        for cells in markdown_table_rows(root / ".ai-team/requirements/failure-modes.md"):
            if len(cells) < 8:
                continue
            conn.execute(
                """
                insert into failure_modes
                (id, feature, scenario, trigger, expected_behavior, recovery, data_safety, risk, status)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(id) do nothing
                """,
                (
                    cells[0],
                    cells[1],
                    cells[2],
                    cells[3],
                    cells[4],
                    cells[5] if len(cells) > 5 else "",
                    cells[6] if len(cells) > 6 else "",
                    cells[7] if len(cells) > 7 else "medium",
                    normalize_failure_mode_status(cells[9] if len(cells) > 9 else "identified"),
                ),
            )
            if len(cells) > 8:
                for acceptance_id in parse_ids(cells[8]):
                    if conn.execute("select id from acceptance where id = ?", (acceptance_id,)).fetchone():
                        conn.execute(
                            "insert or ignore into failure_mode_acceptance (failure_mode_id, acceptance_id) values (?, ?)",
                            (cells[0], acceptance_id),
                        )
        for cells in markdown_table_rows(root / ".ai-team/planning/task-board.md"):
            if len(cells) < 4:
                continue
            status = cells[3] if cells[3] in TASK_STATUSES else "ready"
            conn.execute(
                """
                insert into tasks (id, task, owner, status, tool_link, evidence, updated_at)
                values (?, ?, ?, ?, ?, ?, ?)
                on conflict(id) do nothing
                """,
                (
                    cells[0],
                    cells[1],
                    cells[2] if len(cells) > 2 else "unassigned",
                    status,
                    cells[7] if len(cells) > 7 else "",
                    cells[8] if len(cells) > 8 else "",
                    now_iso(),
                ),
            )
            for acceptance_id in parse_ids(cells[4] if len(cells) > 4 else ""):
                if conn.execute("select id from acceptance where id = ?", (acceptance_id,)).fetchone():
                    conn.execute("insert or ignore into task_acceptance (task_id, acceptance_id) values (?, ?)", (cells[0], acceptance_id))
            for fm_id in parse_ids(cells[5] if len(cells) > 5 else ""):
                if conn.execute("select id from failure_modes where id = ?", (fm_id,)).fetchone():
                    conn.execute("insert or ignore into task_failure_modes (task_id, failure_mode_id) values (?, ?)", (cells[0], fm_id))
        conn.execute(
            "insert into migrations (from_version, to_version, applied_at) values (?, ?, ?)",
            (1, SCHEMA_VERSION, now_iso()),
        )
        emit_event(conn, "markdown_v1_migrated", payload(to=SCHEMA_VERSION))
    render_all(root)
    install_agents(root)


def install_agents(root: Path) -> None:
    template_dir = Path(__file__).resolve().parents[1] / "templates" / "agents"
    agent_dir = root / ".codex" / "agents"
    agent_dir.mkdir(parents=True, exist_ok=True)
    with transaction(root) as conn:
        for template in sorted(template_dir.glob("*.toml")):
            target = agent_dir / template.name
            if not target.exists():
                shutil.copyfile(template, target)
            role = template.stem
            conn.execute(
                """
                insert into agents (id, role, template_path, status, updated_at)
                values (?, ?, ?, 'available', ?)
                on conflict(id) do update set template_path=excluded.template_path, status='available', updated_at=excluded.updated_at
                """,
                (role, role, str(target), now_iso()),
            )
        emit_event(conn, "agents_installed", payload())


def project_row(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute("select * from project where id = 1").fetchone()
    if not row:
        raise HarnessError("project is not initialized")
    return row


def transition_phase(root: Path, phase: str, *, status: str | None = None, owner: str | None = None) -> None:
    with transaction(root) as conn:
        row = project_row(conn)
        current = row["phase"]
        if phase not in PHASES:
            raise HarnessError(f"unknown phase: {phase}")
        if phase != current and phase not in PHASE_TRANSITIONS[current]:
            raise HarnessError(f"illegal phase transition: {current} -> {phase}")
        issues = phase_prerequisite_issues(conn, phase)
        if issues:
            raise HarnessError(f"phase prerequisites blocked: {'; '.join(issues)}")
        if phase == "delivery_readiness":
            delivery_issues = validate_delivery(conn, root)
            if delivery_issues:
                raise HarnessError("delivery readiness blocked: " + "; ".join(delivery_issues))
        updates: dict[str, str] = {"phase": phase}
        if status:
            updates["status"] = status
        if owner:
            updates["current_owner"] = owner
        bump_project(conn, **updates)
        emit_event(conn, "phase_updated", payload(**{"from": current, "to": phase}))
    render_all(root)


def phase_prerequisite_issues(conn: sqlite3.Connection, phase: str) -> list[str]:
    issues: list[str] = []
    requirement_count = conn.execute("select count(*) from requirements where status != 'cancelled'").fetchone()[0]
    acceptance_count = conn.execute("select count(*) from acceptance").fetchone()[0]
    task_count = conn.execute("select count(*) from tasks").fetchone()[0]
    if phase in {"confirmation", "team_architecture", "planning"} and requirement_count == 0:
        issues.append(f"{phase} requires at least one requirement baseline record")
    if phase in {"confirmation", "team_architecture", "planning"} and acceptance_count == 0:
        issues.append(f"{phase} requires at least one acceptance criterion")
    if phase in {"implementation", "qa"} and task_count == 0:
        issues.append(f"{phase} requires at least one task")
    if phase == "qa":
        active = conn.execute(
            "select id, status from tasks where status in ('ready', 'claimed', 'in_progress', 'blocked') order by id"
        ).fetchall()
        for task in active:
            issues.append(f"qa requires implementation task submitted or accepted: {task['id']} status={task['status']}")
    return issues


def add_requirement(root: Path, requirement_id: str, kind: str, body: str, priority: str = "", status: str = "active", tool_link: str = "") -> None:
    with transaction(root) as conn:
        existing = conn.execute("select * from requirements where id = ?", (requirement_id,)).fetchone()
        conn.execute(
            """
            insert into requirements (id, kind, body, priority, status, tool_link, updated_at)
            values (?, ?, ?, ?, ?, ?, ?)
            on conflict(id) do update set kind=excluded.kind, body=excluded.body, priority=excluded.priority,
              status=excluded.status, tool_link=excluded.tool_link, revision=requirements.revision+1, updated_at=excluded.updated_at
            """,
            (requirement_id, kind, body, priority, status, tool_link, now_iso()),
        )
        if existing and (existing["kind"], existing["body"], existing["priority"], existing["status"], existing["tool_link"]) != (kind, body, priority, status, tool_link):
            invalidate_downstream(conn, "requirement", requirement_id, "requirement changed")
        emit_event(conn, "requirement_recorded", payload(id=requirement_id, kind=kind))
    render_all(root)


def add_acceptance(root: Path, acceptance_id: str, criterion: str, priority: str = "", tool_link: str = "") -> None:
    with transaction(root) as conn:
        existing = conn.execute("select * from acceptance where id = ?", (acceptance_id,)).fetchone()
        conn.execute(
            """
            insert into acceptance (id, criterion, priority, tool_link)
            values (?, ?, ?, ?)
            on conflict(id) do update set criterion=excluded.criterion, priority=excluded.priority, tool_link=excluded.tool_link,
                revision=acceptance.revision+1
            """,
            (acceptance_id, criterion, priority, tool_link),
        )
        if existing and (existing["criterion"], existing["priority"], existing["tool_link"]) != (criterion, priority, tool_link):
            invalidate_downstream(conn, "acceptance", acceptance_id, "acceptance criterion changed")
        emit_event(conn, "acceptance_added", payload(id=acceptance_id))
    render_all(root)


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
    with transaction(root) as conn:
        existing = conn.execute("select * from failure_modes where id = ?", (fm_id,)).fetchone()
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
            (id, feature, scenario, trigger, expected_behavior, recovery, data_safety, risk, status,
             accepted_by, acceptance_reason, acceptance_scope, accepted_revision, expires_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(id) do update set feature=excluded.feature, scenario=excluded.scenario, trigger=excluded.trigger,
              expected_behavior=excluded.expected_behavior, recovery=excluded.recovery, data_safety=excluded.data_safety,
              risk=excluded.risk, status=excluded.status, accepted_by=excluded.accepted_by,
              acceptance_reason=excluded.acceptance_reason, acceptance_scope=excluded.acceptance_scope,
              accepted_revision=excluded.accepted_revision, expires_at=excluded.expires_at, revision=failure_modes.revision+1
            """,
            (
                fm_id,
                feature,
                scenario,
                trigger,
                expected,
                recovery,
                data_safety,
                risk,
                status,
                accepted_by or None,
                acceptance_reason or None,
                acceptance_scope,
                accepted_revision,
                expires_at or None,
            ),
        )
        if acceptance:
            require_acceptance(conn, acceptance)
            conn.execute(
                "insert or ignore into failure_mode_acceptance (failure_mode_id, acceptance_id) values (?, ?)",
                (fm_id, acceptance),
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
        emit_event(conn, "failure_mode_added", payload(id=fm_id, risk=risk))
    render_all(root)


def require_acceptance(conn: sqlite3.Connection, acceptance_id: str) -> None:
    if not conn.execute("select id from acceptance where id = ?", (acceptance_id,)).fetchone():
        raise HarnessError(f"missing acceptance: {acceptance_id}")


def require_task(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    row = conn.execute("select * from tasks where id = ?", (task_id,)).fetchone()
    if not row:
        raise HarnessError(f"missing task: {task_id}")
    return row


def require_agent(conn: sqlite3.Connection, agent: str) -> sqlite3.Row:
    row = conn.execute("select * from agents where id = ?", (agent,)).fetchone()
    if not row:
        raise HarnessError(f"unknown agent: {agent}")
    return row


def require_revision(row: sqlite3.Row, expected_revision: int | None) -> None:
    if expected_revision is not None and int(row["revision"]) != expected_revision:
        raise HarnessError(f"revision mismatch: expected {expected_revision}, actual {row['revision']}")


def require_lease(row: sqlite3.Row, agent: str, lease_token: str | None) -> None:
    if row["lease_agent"] != agent:
        raise HarnessError(f"task is not leased by agent: {row['id']} agent={agent}")
    if not lease_token or row["lease_token"] != lease_token:
        raise HarnessError(f"lease token mismatch: {row['id']}")
    if is_expired(row["lease_expires_at"]):
        raise HarnessError(f"lease expired: {row['id']}")


def parse_ids(value: str) -> list[str]:
    return [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]


def assert_no_dependency_cycle(conn: sqlite3.Connection, task_id: str, depends_on: str) -> None:
    stack = [depends_on]
    seen: set[str] = set()
    while stack:
        current = stack.pop()
        if current == task_id:
            raise HarnessError(f"dependency cycle detected for {task_id}")
        if current in seen:
            continue
        seen.add(current)
        stack.extend(
            row["depends_on"]
            for row in conn.execute("select depends_on from task_dependencies where task_id = ?", (current,))
        )


def add_task(
    root: Path,
    task_id: str,
    task: str,
    *,
    owner: str = "unassigned",
    acceptance: str = "",
    failure_modes: str = "",
    depends_on: str = "",
    status: str = "ready",
    evidence: str = "",
    tool_link: str = "",
) -> None:
    with transaction(root) as conn:
        if status not in TASK_STATUSES:
            raise HarnessError(f"invalid task status: {status}")
        if status == "accepted":
            raise HarnessError("new tasks cannot be created as accepted; use task complete with evidence")
        if conn.execute("select id from tasks where id = ?", (task_id,)).fetchone():
            raise HarnessError(f"duplicate task id: {task_id}")
        conn.execute(
            """
            insert into tasks (id, task, owner, status, evidence, tool_link, updated_at)
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            (task_id, task, owner, status, evidence, tool_link, now_iso()),
        )
        conn.execute("delete from task_acceptance where task_id = ?", (task_id,))
        for acceptance_id in parse_ids(acceptance):
            require_acceptance(conn, acceptance_id)
            conn.execute("insert into task_acceptance (task_id, acceptance_id) values (?, ?)", (task_id, acceptance_id))
        conn.execute("delete from task_failure_modes where task_id = ?", (task_id,))
        for fm_id in parse_ids(failure_modes):
            if not conn.execute("select id from failure_modes where id = ?", (fm_id,)).fetchone():
                raise HarnessError(f"missing failure mode: {fm_id}")
            conn.execute("insert into task_failure_modes (task_id, failure_mode_id) values (?, ?)", (task_id, fm_id))
        conn.execute("delete from task_dependencies where task_id = ?", (task_id,))
        for dep in parse_ids(depends_on):
            require_task(conn, dep)
            assert_no_dependency_cycle(conn, task_id, dep)
            conn.execute("insert into task_dependencies (task_id, depends_on) values (?, ?)", (task_id, dep))
        emit_event(conn, "task_created", payload(id=task_id))
    render_all(root)


def update_task(root: Path, task_id: str, *, depends_on: str | None = None, status: str | None = None) -> None:
    with transaction(root) as conn:
        row = require_task(conn, task_id)
        if status and status not in TASK_STATUSES:
            raise HarnessError(f"invalid task status: {status}")
        if status == "accepted":
            raise HarnessError("task acceptance must use task complete with evidence")
        if depends_on is not None:
            conn.execute("delete from task_dependencies where task_id = ?", (task_id,))
            for dep in parse_ids(depends_on):
                require_task(conn, dep)
                assert_no_dependency_cycle(conn, task_id, dep)
                conn.execute("insert into task_dependencies (task_id, depends_on) values (?, ?)", (task_id, dep))
        if status:
            conn.execute(
                "update tasks set status = ?, revision = revision + 1, updated_at = ? where id = ?",
                (status, now_iso(), task_id),
            )
        else:
            conn.execute("update tasks set revision = revision + 1, updated_at = ? where id = ?", (now_iso(), task_id))
        emit_event(conn, "task_updated", payload(id=row["id"]))
    render_all(root)


def ready_tasks(root: Path) -> list[str]:
    with connection(root) as conn:
        rows = conn.execute("select id from tasks where status = 'ready' order by id").fetchall()
        ready: list[str] = []
        for row in rows:
            blocked = conn.execute(
                """
                select 1 from task_dependencies d
                join tasks t on t.id = d.depends_on
                where d.task_id = ? and t.status != 'accepted'
                limit 1
                """,
                (row["id"],),
            ).fetchone()
            if not blocked:
                ready.append(row["id"])
        return ready


def dependency_blockers(conn: sqlite3.Connection, task_id: str) -> list[str]:
    return [
        f"{row['depends_on']}={row['status']}"
        for row in conn.execute(
            """
            select d.depends_on, t.status from task_dependencies d
            join tasks t on t.id = d.depends_on
            where d.task_id = ? and t.status != 'accepted'
            order by d.depends_on
            """,
            (task_id,),
        )
    ]


def require_task_runnable(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    if row["status"] not in {"ready", "claimed"}:
        raise HarnessError(f"task status is not runnable: {row['id']} status={row['status']}")
    blockers = dependency_blockers(conn, row["id"])
    if blockers:
        raise HarnessError(f"task dependencies are not accepted: {row['id']} blockers={', '.join(blockers)}")


def claim_task(root: Path, task_id: str, agent: str, expected_revision: int) -> str:
    with transaction(root) as conn:
        row = require_task(conn, task_id)
        require_revision(row, expected_revision)
        active_lease = require_agent(conn, agent)
        if active_lease and active_lease["lease_task_id"] and active_lease["lease_task_id"] != task_id:
            raise HarnessError(f"agent already leased to {active_lease['lease_task_id']}")
        if row["lease_agent"]:
            raise HarnessError(f"task already leased by {row['lease_agent']}")
        if row["status"] != "ready":
            raise HarnessError(f"task status is not ready: {task_id} status={row['status']}")
        require_task_runnable(conn, row)
        token = str(uuid.uuid4())
        conn.execute(
            """
            update tasks set lease_agent = ?, lease_token = ?, lease_heartbeat_at = ?, lease_expires_at = ?, status = 'claimed',
              revision = revision + 1, updated_at = ? where id = ?
            """,
            (agent, token, now_iso(), lease_deadline(), now_iso(), task_id),
        )
        conn.execute(
            """
            update agents set lease_task_id = ?, status = 'leased', updated_at = ?
            where id = ?
            """,
            (task_id, now_iso(), agent),
        )
        emit_event(conn, "task_claimed", payload(id=task_id, agent=agent))
    render_all(root)
    return token


def heartbeat_task(root: Path, task_id: str, agent: str, lease_token: str, expected_revision: int) -> None:
    with transaction(root) as conn:
        row = require_task(conn, task_id)
        require_agent(conn, agent)
        require_revision(row, expected_revision)
        require_lease(row, agent, lease_token)
        conn.execute(
            "update tasks set lease_heartbeat_at = ?, lease_expires_at = ?, revision = revision + 1, updated_at = ? where id = ?",
            (now_iso(), lease_deadline(), now_iso(), task_id),
        )
        emit_event(conn, "task_heartbeat", payload(id=task_id, agent=agent))
    render_all(root)


def recover_stale_leases(root: Path) -> int:
    recovered = 0
    with transaction(root) as conn:
        rows = conn.execute(
            """
            select id, status, lease_agent from tasks
            where lease_expires_at is not null and lease_expires_at <= ? and lease_agent is not null
            order by id
            """,
            (now_iso(),),
        ).fetchall()
        for row in rows:
            next_status = "submitted" if row["status"] == "review" else "ready"
            conn.execute(
                """
                update tasks set status = ?, lease_agent = null, lease_token = null, lease_heartbeat_at = null,
                  lease_expires_at = null, revision = revision + 1, updated_at = ? where id = ?
                """,
                (next_status, now_iso(), row["id"]),
            )
            if row["lease_agent"]:
                conn.execute(
                    "update agents set lease_task_id = '', status = 'available', updated_at = ? where id = ?",
                    (now_iso(), row["lease_agent"]),
                )
            recovered += 1
        if recovered:
            emit_event(conn, "stale_leases_recovered", payload(count=recovered))
    render_all(root)
    return recovered


def release_task(root: Path, task_id: str, agent: str, *, lease_token: str | None = None, expected_revision: int | None = None) -> None:
    with transaction(root) as conn:
        row = require_task(conn, task_id)
        require_agent(conn, agent)
        require_revision(row, expected_revision)
        require_lease(row, agent, lease_token)
        conn.execute(
            """
            update tasks set lease_agent = null, lease_token = null, lease_heartbeat_at = null, lease_expires_at = null,
              status = 'ready', revision = revision + 1, updated_at = ? where id = ?
            """,
            (now_iso(), task_id),
        )
        conn.execute("update agents set lease_task_id = '', status = 'available', updated_at = ? where id = ?", (now_iso(), agent))
        emit_event(conn, "task_released", payload(id=task_id, agent=agent))
    render_all(root)


def start_task(root: Path, task_id: str, agent: str, *, lease_token: str | None = None, expected_revision: int | None = None) -> None:
    with transaction(root) as conn:
        row = require_task(conn, task_id)
        require_agent(conn, agent)
        require_revision(row, expected_revision)
        if row["status"] != "claimed":
            raise HarnessError(f"task status is not startable: {task_id} status={row['status']}")
        require_lease(row, agent, lease_token)
        require_task_runnable(conn, row)
        conn.execute(
            """
            update tasks set status = 'in_progress', owner = ?, revision = revision + 1, updated_at = ? where id = ?
            """,
            (agent, now_iso(), task_id),
        )
        emit_event(conn, "task_started", payload(id=task_id, agent=agent))
    render_all(root)


def submit_task(root: Path, task_id: str, evidence: str, *, agent: str, lease_token: str | None = None, expected_revision: int | None = None) -> None:
    with transaction(root) as conn:
        row = require_task(conn, task_id)
        require_agent(conn, agent)
        require_revision(row, expected_revision)
        require_lease(row, agent, lease_token)
        if row["status"] != "in_progress":
            raise HarnessError(f"task status is not submittable: {task_id} status={row['status']}")
        conn.execute(
            """
            update tasks set status = 'submitted', evidence = ?, lease_agent = null, lease_token = null,
              lease_heartbeat_at = null, lease_expires_at = null,
              revision = revision + 1, updated_at = ? where id = ?
            """,
            (evidence, now_iso(), task_id),
        )
        conn.execute(
            "update agents set lease_task_id = '', status = 'available', updated_at = ? where id = ?",
            (now_iso(), agent),
        )
        emit_event(conn, "task_submitted", payload(id=task_id, agent=agent))
    render_all(root)


def complete_task(root: Path, task_id: str, evidence: str, *, agent: str, lease_token: str | None = None, expected_revision: int | None = None) -> None:
    submit_task(root, task_id, evidence, agent=agent, lease_token=lease_token, expected_revision=expected_revision)


def review_task(root: Path, task_id: str, agent: str, expected_revision: int) -> str:
    with transaction(root) as conn:
        row = require_task(conn, task_id)
        active_lease = require_agent(conn, agent)
        require_revision(row, expected_revision)
        if row["status"] != "submitted":
            raise HarnessError(f"task status is not reviewable: {task_id} status={row['status']}")
        if row["owner"] == agent:
            raise HarnessError("producer cannot review own task")
        if active_lease["lease_task_id"] and active_lease["lease_task_id"] != task_id:
            raise HarnessError(f"agent already leased to {active_lease['lease_task_id']}")
        token = str(uuid.uuid4())
        conn.execute(
            """
            update tasks set status = 'review', lease_agent = ?, lease_token = ?, lease_heartbeat_at = ?, lease_expires_at = ?,
              revision = revision + 1, updated_at = ? where id = ?
            """,
            (agent, token, now_iso(), lease_deadline(), now_iso(), task_id),
        )
        conn.execute(
            "update agents set lease_task_id = ?, status = 'leased', updated_at = ? where id = ?",
            (task_id, now_iso(), agent),
        )
        emit_event(conn, "task_review_started", payload(id=task_id, agent=agent))
    render_all(root)
    return token


def accept_task(root: Path, task_id: str, evidence: str, *, agent: str, lease_token: str | None = None, expected_revision: int | None = None) -> None:
    with transaction(root) as conn:
        row = require_task(conn, task_id)
        require_agent(conn, agent)
        require_revision(row, expected_revision)
        require_lease(row, agent, lease_token)
        if row["status"] != "review":
            raise HarnessError(f"task status is not acceptable: {task_id} status={row['status']}")
        conn.execute(
            """
            update tasks set status = 'accepted', evidence = ?, lease_agent = null, lease_token = null,
              lease_heartbeat_at = null, lease_expires_at = null,
              revision = revision + 1, updated_at = ? where id = ?
            """,
            (evidence, now_iso(), task_id),
        )
        conn.execute(
            "update agents set lease_task_id = '', status = 'available', updated_at = ? where id = ?",
            (now_iso(), agent),
        )
        emit_event(conn, "task_accepted", payload(id=task_id, agent=agent))
    render_all(root)


def block_task(root: Path, task_id: str, reason: str, *, agent: str, lease_token: str | None = None, expected_revision: int | None = None) -> None:
    with transaction(root) as conn:
        row = require_task(conn, task_id)
        require_agent(conn, agent)
        require_revision(row, expected_revision)
        require_lease(row, agent, lease_token)
        conn.execute(
            """
            update tasks set status = 'blocked', evidence = ?, lease_agent = null, lease_token = null,
              lease_heartbeat_at = null, lease_expires_at = null, revision = revision + 1, updated_at = ? where id = ?
            """,
            (reason, now_iso(), task_id),
        )
        conn.execute(
            "update agents set lease_task_id = '', status = 'available', updated_at = ? where id = ?",
            (now_iso(), agent),
        )
        emit_event(conn, "task_blocked", payload(id=task_id))
    render_all(root)


def record_decision(root: Path, decision: str, reason: str) -> None:
    with transaction(root) as conn:
        conn.execute(
            "insert into decisions (id, decision, reason, created_at) values (?, ?, ?, ?)",
            (str(uuid.uuid4()), decision, reason, now_iso()),
        )
        emit_event(conn, "decision_recorded", payload(decision=decision, reason=reason))
    render_all(root)


def record_validation(root: Path, surface: str, findings: str, result: str, *, acceptance: str = "", commands: str = "", risk: str = "", failure_modes: str = "") -> None:
    current_sha = git_head_sha(root) or "no-git"
    source_hash = git_source_tree_hash(root) or ""
    tracked_diff_hash = git_tracked_diff_hash(root) or ""
    with transaction(root) as conn:
        validation_id = str(uuid.uuid4())
        project_revision = int(project_row(conn)["revision"])
        conn.execute(
            """
            insert into validations
            (id, surface, acceptance_id, commands, findings, result, residual_risk, head_commit,
             source_tree_hash, tracked_diff_hash, project_revision, created_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                validation_id,
                surface,
                acceptance,
                commands,
                findings,
                result,
                risk,
                current_sha,
                source_hash,
                tracked_diff_hash,
                project_revision,
                now_iso(),
            ),
        )
        if acceptance:
            resolve_invalidations(conn, source_type="acceptance", source_id=acceptance)
        for fm_id in parse_ids(failure_modes):
            if not conn.execute("select id from failure_modes where id = ?", (fm_id,)).fetchone():
                raise HarnessError(f"missing failure mode: {fm_id}")
            conn.execute(
                "insert into validation_failure_modes (validation_id, failure_mode_id) values (?, ?)",
                (validation_id, fm_id),
            )
            resolve_invalidations(conn, source_type="failure_mode", source_id=fm_id)
        emit_event(conn, "validation_recorded", payload(surface=surface, result=result))
    render_all(root)


def record_evidence(root: Path, evidence_id: str, kind: str, summary: str, *, uri: str = "", artifact_hash: str = "") -> None:
    with transaction(root) as conn:
        conn.execute(
            """
            insert into evidence (id, kind, summary, uri, hash, created_at)
            values (?, ?, ?, ?, ?, ?)
            on conflict(id) do update set kind=excluded.kind, summary=excluded.summary, uri=excluded.uri,
              hash=excluded.hash, created_at=excluded.created_at
            """,
            (evidence_id, kind, summary, uri, artifact_hash, now_iso()),
        )
        emit_event(conn, "evidence_recorded", payload(id=evidence_id, kind=kind))
    render_all(root)


def record_test(root: Path, test_id: str, surface: str, command: str, result: str, *, evidence_id: str = "") -> None:
    with transaction(root) as conn:
        if evidence_id and not conn.execute("select id from evidence where id = ?", (evidence_id,)).fetchone():
            raise HarnessError(f"missing evidence: {evidence_id}")
        conn.execute(
            """
            insert into tests (id, surface, command, result, evidence_id, created_at)
            values (?, ?, ?, ?, ?, ?)
            on conflict(id) do update set surface=excluded.surface, command=excluded.command, result=excluded.result,
              evidence_id=excluded.evidence_id, created_at=excluded.created_at
            """,
            (test_id, surface, command, result, evidence_id, now_iso()),
        )
        emit_event(conn, "test_recorded", payload(id=test_id, result=result))
    render_all(root)


def record_finding(root: Path, finding_id: str, surface: str, severity: str, status: str, summary: str, *, evidence_id: str = "") -> None:
    with transaction(root) as conn:
        if evidence_id and not conn.execute("select id from evidence where id = ?", (evidence_id,)).fetchone():
            raise HarnessError(f"missing evidence: {evidence_id}")
        conn.execute(
            """
            insert into findings (id, surface, severity, status, summary, evidence_id, created_at)
            values (?, ?, ?, ?, ?, ?, ?)
            on conflict(id) do update set surface=excluded.surface, severity=excluded.severity, status=excluded.status,
              summary=excluded.summary, evidence_id=excluded.evidence_id, created_at=excluded.created_at
            """,
            (finding_id, surface, severity, status, summary, evidence_id, now_iso()),
        )
        emit_event(conn, "finding_recorded", payload(id=finding_id, severity=severity, status=status))
    render_all(root)


def record_gate(root: Path, reviewer_context: str, result: str, *, gate: str = "independent_qa", commands: str = "", evidence: str = "", blocking_findings: str = "", residual_risk: str = "") -> None:
    current_sha = git_head_sha(root) or "no-git"
    base_commit = git_base_commit(root) or current_sha
    source_hash = git_source_tree_hash(root) or ""
    tracked_diff_hash = git_tracked_diff_hash(root) or ""
    if result == "pass" and git_dirty(root):
        raise HarnessError("cannot record a passing quality gate with a dirty git worktree")
    with transaction(root) as conn:
        project_revision = int(project_row(conn)["revision"])
        conn.execute(
            """
            insert into quality_gates
            (id, gate, reviewed_commit, evidence_commit, diff_hash, base_commit, head_commit, tracked_diff_hash,
             project_revision, reviewer_context, result, blocking_findings, commands, evidence, residual_risk, created_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                gate,
                current_sha,
                current_sha,
                source_hash,
                base_commit,
                current_sha,
                tracked_diff_hash,
                project_revision,
                reviewer_context,
                result,
                blocking_findings,
                commands,
                evidence,
                residual_risk,
                now_iso(),
            ),
        )
        if result == "pass":
            resolve_invalidations(conn, target_type="quality_gate")
        emit_event(conn, "quality_gate_recorded", payload(gate=gate, result=result))
    render_all(root)


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
    collaboration_links: str = "",
    known_gaps: str = "",
    handoff: str = "",
) -> None:
    with transaction(root) as conn:
        issues = validate_delivery(conn, root)
        if issues:
            raise HarnessError("delivery record blocked: " + "; ".join(issues))
        conn.execute(
            """
            insert into deliveries
            (id, scope, acceptance, changed_files, validation, qa, failure_mode_coverage, quality_gate,
             data_config_notes, collaboration_links, known_gaps, handoff, created_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                scope,
                acceptance,
                changed_files,
                validation,
                qa,
                failure_mode_coverage,
                quality_gate,
                data_config_notes,
                collaboration_links,
                known_gaps,
                handoff,
                now_iso(),
            ),
        )
        emit_event(conn, "delivery_recorded", payload(scope=scope))
    render_all(root)


def record_adapter(root: Path, tool: str, mode: str, artifact: str, external_id: str, idempotency_key: str, *, external_link: str = "", evidence: str = "", fallback: str = "", confirmation_needed: str = "no") -> None:
    if mode not in ADAPTER_MODES:
        raise HarnessError(f"invalid adapter mode: {mode}")
    with transaction(root) as conn:
        conn.execute(
            """
            insert into adapters
            (id, tool, mode, artifact, external_id, external_link, idempotency_key, evidence, fallback, confirmation_needed, updated_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(tool, idempotency_key) do update set mode=excluded.mode, artifact=excluded.artifact,
              external_id=excluded.external_id, external_link=excluded.external_link, evidence=excluded.evidence,
              fallback=excluded.fallback, confirmation_needed=excluded.confirmation_needed, updated_at=excluded.updated_at
            """,
            (
                str(uuid.uuid4()),
                tool,
                mode,
                artifact,
                external_id,
                external_link,
                idempotency_key,
                evidence,
                fallback,
                confirmation_needed,
                now_iso(),
            ),
        )
        emit_event(conn, "adapter_recorded", payload(tool=tool, mode=mode), idempotency_key=idempotency_key)
    render_tooling_map(root)


def migrate(root: Path, from_version: str, to_version: int) -> None:
    if from_version == "markdown-v1":
        migrate_markdown_v1(root)
        return
    backup_runtime(root, "migrate")
    from_version_int = int(from_version)
    with transaction(root) as conn:
        create_schema(conn)
        initialize_project(conn)
        conn.execute(
            "insert into migrations (from_version, to_version, applied_at) values (?, ?, ?)",
            (from_version_int, to_version, now_iso()),
        )
        conn.execute("update project set schema_version = ?, runtime_version = ?, revision = revision + 1, updated_at = ? where id = 1", (to_version, RUNTIME_VERSION, now_iso()))
        emit_event(conn, "migration_applied", payload(**{"from": from_version_int, "to": to_version}))
    render_all(root)


def doctor(root: Path) -> list[str]:
    issues: list[str] = []
    path = db_file(root)
    if not path.exists():
        return ["missing sqlite state: .ai-team/state/harness.db"]
    with connection(root) as conn:
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
        for relpath in [
            ".ai-team/control/project-state.yaml",
            ".ai-team/planning/task-board.md",
            ".ai-team/requirements/acceptance.md",
            ".ai-team/requirements/failure-modes.md",
            "docs/harness/quality-gates.md",
        ]:
            if not (root / relpath).exists():
                issues.append(f"missing view: {relpath}")
    return issues


def runtime_schema_issues(conn: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    enum_checks = [
        ("tasks", "status", TASK_STATUSES, "task status"),
        ("failure_modes", "risk", {"low", "medium", "high", "critical"}, "failure mode risk"),
        ("failure_modes", "status", FAILURE_MODE_STATUSES, "failure mode status"),
        ("validations", "result", {"pass", "fail", "blocked", "partial"}, "validation result"),
        ("quality_gates", "reviewer_context", {"fresh", "same-context-degraded", "external"}, "quality gate reviewer context"),
        ("quality_gates", "result", {"pass", "fail", "conditional", "blocked"}, "quality gate result"),
        ("adapters", "mode", ADAPTER_MODES, "adapter mode"),
        ("agents", "status", {"available", "leased", "disabled"}, "agent status"),
    ]
    for table, column, allowed, label in enum_checks:
        for row in conn.execute(f"select id, {column} as value from {table} where {column} not in ({','.join('?' for _ in allowed)})", tuple(allowed)):
            issues.append(f"invalid {label}: {table}.{row['id']}={row['value']}")
    for row in conn.execute("select id, payload_json from events"):
        try:
            json.loads(row["payload_json"])
        except json.JSONDecodeError as exc:
            issues.append(f"invalid event payload_json: {row['id']} {exc.msg}")
    return issues


def validate_delivery(conn: sqlite3.Connection, root: Path) -> list[str]:
    issues: list[str] = []
    stale_rows = conn.execute(
        "select source_type, source_id, target_type, target_id, reason from invalidations where resolved_at is null order by created_at, id"
    ).fetchall()
    for stale in stale_rows:
        issues.append(
            f"stale runtime artifact: {stale['source_type']}:{stale['source_id']} -> {stale['target_type']}:{stale['target_id']} reason={stale['reason']}"
        )

    active_tasks = conn.execute(
        "select id, status from tasks where status not in ('accepted', 'cancelled', 'skipped') order by id"
    ).fetchall()
    for task in active_tasks:
        issues.append(f"task is not accepted: {task['id']} status={task['status']}")

    current_sha = git_head_sha(root)
    current_source_hash = (git_source_tree_hash(root) or "") if current_sha else ""
    validations = conn.execute("select surface, result, source_tree_hash from validations order by created_at, id").fetchall()
    if not validations:
        issues.append("delivery requires validation evidence")
    for validation in validations:
        if validation["result"] != "pass":
            issues.append(f"validation is not pass: {validation['surface']}={validation['result']}")
        if current_sha and validation["source_tree_hash"] and validation["source_tree_hash"] != current_source_hash:
            issues.append(
                f"validation source tree hash does not match current code: {validation['surface']} "
                f"validation={validation['source_tree_hash']} current={current_source_hash}"
            )

    risky_failure_modes = conn.execute(
        """
        select id, risk, status, accepted_by, acceptance_reason, acceptance_scope, accepted_revision, expires_at from failure_modes
        where risk in ('high', 'critical')
        order by id
        """
    ).fetchall()
    for failure_mode in risky_failure_modes:
        if failure_mode["status"] in {"accepted", "exempt"}:
            if not failure_mode["accepted_by"] or not failure_mode["acceptance_reason"] or not failure_mode["acceptance_scope"] or not failure_mode["accepted_revision"] or not failure_mode["expires_at"]:
                issues.append(f"{failure_mode['risk']} failure mode acceptance is incomplete: {failure_mode['id']}")
            elif is_expired(failure_mode["expires_at"]):
                issues.append(f"{failure_mode['risk']} failure mode risk acceptance expired: {failure_mode['id']} expires_at={failure_mode['expires_at']}")
            continue
        covered = conn.execute(
            """
            select 1 from validation_failure_modes vfm
            join validations v on v.id = vfm.validation_id
            where vfm.failure_mode_id = ? and v.result = 'pass'
            limit 1
            """,
            (failure_mode["id"],),
        ).fetchone()
        if not covered:
            issues.append(
                f"{failure_mode['risk']} failure mode is not covered by passing validation: {failure_mode['id']} status={failure_mode['status']}"
            )

    latest_gate = conn.execute("select * from quality_gates order by created_at desc, id desc limit 1").fetchone()
    if not latest_gate:
        issues.append("delivery requires a quality gate record")
    else:
        if latest_gate["result"] != "pass":
            issues.append(f"latest quality gate is not pass: {latest_gate['gate']}={latest_gate['result']}")
        if latest_gate["blocking_findings"]:
            issues.append(f"latest quality gate has blocking findings: {latest_gate['blocking_findings']}")
        high_risk_present = conn.execute(
            "select 1 from failure_modes where risk in ('high', 'critical') limit 1"
        ).fetchone()
        if high_risk_present and latest_gate["reviewer_context"] == "same-context-degraded":
            issues.append("high/critical risk delivery requires fresh or external quality gate reviewer context")
        if current_sha:
            if git_dirty(root):
                issues.append("git worktree is dirty after quality gate")
            if latest_gate["diff_hash"] and latest_gate["diff_hash"] != current_source_hash:
                issues.append(
                    f"latest quality gate source tree hash does not match current code: gate={latest_gate['diff_hash']} current={current_source_hash}"
                )
    return issues


def validate_runtime(root: Path, *, delivery: bool = False) -> list[str]:
    issues = doctor(root)
    if issues:
        return issues
    with connection(root) as conn:
        project = project_row(conn)
        if delivery or project["phase"] in {"delivery_readiness", "retrospective"}:
            issues.extend(validate_delivery(conn, root))
    return issues


def status_lines(root: Path) -> list[str]:
    with connection(root) as conn:
        row = project_row(conn)
        task_count = conn.execute("select count(*) from tasks").fetchone()[0]
        ready_count = conn.execute("select count(*) from tasks where status = 'ready'").fetchone()[0]
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
        f"ready_tasks: {ready_count}",
        f"events: {event_count}",
    ]


def repair(root: Path) -> None:
    backup_runtime(root, "repair")
    init_runtime(root)
    migrate(root, str(SCHEMA_VERSION), SCHEMA_VERSION)
    render_all(root)


def render_all(root: Path) -> None:
    render_project_state(root)
    render_requirements(root)
    render_acceptance(root)
    render_failure_modes(root)
    render_tasks(root)
    render_validation(root)
    render_evidence(root)
    render_findings(root)
    render_gates(root)
    render_deliveries(root)
    render_decisions(root)
    render_tooling_map(root)


def render_project_state(root: Path) -> None:
    with connection(root) as conn:
        row = project_row(conn)
    write_state(
        root,
        {
            "status": row["status"],
            "phase": row["phase"],
            "scope_status": row["scope_status"],
            "current_owner": row["current_owner"],
            "schema_version": row["schema_version"],
            "runtime_version": row["runtime_version"],
            "project_id": row["project_id"],
            "revision": row["revision"],
        },
    )


def write_view(root: Path, relpath: str, content: str) -> None:
    path = root / relpath
    ensure_parent(path)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


def render_requirements(root: Path) -> None:
    with connection(root) as conn:
        rows = conn.execute("select * from requirements order by id").fetchall()
    lines = ["# Requirements", "", "| ID | Kind | Body | Priority | Status | Tool Link | Revision |", "| --- | --- | --- | --- | --- | --- | --- |"]
    lines.extend(markdown_row([row["id"], row["kind"], row["body"], row["priority"], row["status"], row["tool_link"], row["revision"]]) for row in rows)
    write_view(root, ".ai-team/requirements/requirements.md", "\n".join(lines))


def render_acceptance(root: Path) -> None:
    with connection(root) as conn:
        rows = conn.execute("select * from acceptance order by id").fetchall()
    lines = ["# Acceptance Criteria", "", "| ID | Criterion | Priority | Tool Link | Status |", "| --- | --- | --- | --- | --- |"]
    lines.extend(markdown_row([row["id"], row["criterion"], row["priority"], row["tool_link"], row["status"]]) for row in rows)
    write_view(root, ".ai-team/requirements/acceptance.md", "\n".join(lines))


def render_failure_modes(root: Path) -> None:
    with connection(root) as conn:
        rows = conn.execute("select * from failure_modes order by id").fetchall()
        mappings = {
            row["failure_mode_id"]: row["ids"]
            for row in conn.execute(
                "select failure_mode_id, group_concat(acceptance_id, ', ') as ids from failure_mode_acceptance group by failure_mode_id"
            )
        }
        covered = {
            row["failure_mode_id"]
            for row in conn.execute(
                """
                select distinct vfm.failure_mode_id
                from validation_failure_modes vfm
                join validations v on v.id = vfm.validation_id
                where v.result = 'pass'
                """
            )
        }
    lines = ["# Failure Modes", "", "| ID | Feature | Scenario | Trigger | Expected Behavior | Recovery | Data Safety | Risk | Test Mapping | Status | Derived Coverage | Accepted By | Acceptance Reason | Acceptance Scope | Accepted Revision | Expires At |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for row in rows:
        lines.append(
            markdown_row(
                [
                    row["id"],
                    row["feature"],
                    row["scenario"],
                    row["trigger"],
                    row["expected_behavior"],
                    row["recovery"],
                    row["data_safety"],
                    row["risk"],
                    mappings.get(row["id"], ""),
                    row["status"],
                    "covered" if row["id"] in covered else "",
                    row["accepted_by"] or "",
                    row["acceptance_reason"] or "",
                    row["acceptance_scope"] or "",
                    row["accepted_revision"] or "",
                    row["expires_at"] or "",
                ]
            )
        )
    write_view(root, ".ai-team/requirements/failure-modes.md", "\n".join(lines))


def render_tasks(root: Path) -> None:
    with connection(root) as conn:
        rows = conn.execute("select * from tasks order by id").fetchall()
        acceptance = grouped(conn, "task_acceptance", "task_id", "acceptance_id")
        failure_modes = grouped(conn, "task_failure_modes", "task_id", "failure_mode_id")
        dependencies = grouped(conn, "task_dependencies", "task_id", "depends_on")
    lines = ["# Task Board", "", "| ID | Task | Owner | Status | Acceptance | Failure Modes | Depends On | Tool Link | Evidence | Revision | Lease |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for row in rows:
        lines.append(
            markdown_row(
                [
                    row["id"],
                    row["task"],
                    row["owner"],
                    row["status"],
                    acceptance.get(row["id"], ""),
                    failure_modes.get(row["id"], ""),
                    dependencies.get(row["id"], ""),
                    row["tool_link"],
                    row["evidence"],
                    row["revision"],
                    row["lease_agent"] or "",
                ]
            )
        )
    write_view(root, ".ai-team/planning/task-board.md", "\n".join(lines))


def grouped(conn: sqlite3.Connection, table: str, key: str, value: str) -> dict[str, str]:
    return {
        row[key]: row["ids"]
        for row in conn.execute(f"select {key}, group_concat({value}, ', ') as ids from {table} group by {key}")
    }


def render_validation(root: Path) -> None:
    with connection(root) as conn:
        rows = conn.execute("select * from validations order by created_at, id").fetchall()
        failure_modes = grouped(conn, "validation_failure_modes", "validation_id", "failure_mode_id")
    lines = ["# Validation", "", "| Surface | Acceptance | Failure Modes | Head | Source Hash | Diff Hash | Project Revision | Tool Context | Commands | Findings | Pass/Fail | Residual Risk |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    lines.extend(
        markdown_row(
            [
                row["surface"],
                row["acceptance_id"],
                failure_modes.get(row["id"], ""),
                row["head_commit"],
                row["source_tree_hash"],
                row["tracked_diff_hash"],
                row["project_revision"],
                "",
                row["commands"],
                row["findings"],
                row["result"],
                row["residual_risk"],
            ]
        )
        for row in rows
    )
    write_view(root, "docs/harness/validation.md", "\n".join(lines))


def render_evidence(root: Path) -> None:
    with connection(root) as conn:
        evidence_rows = conn.execute("select * from evidence order by created_at, id").fetchall()
        test_rows = conn.execute("select * from tests order by created_at, id").fetchall()
    lines = ["# Evidence", "", "## Evidence Records", "", "| ID | Kind | Summary | URI | Hash | Created At |", "| --- | --- | --- | --- | --- | --- |"]
    lines.extend(markdown_row([row["id"], row["kind"], row["summary"], row["uri"], row["hash"], row["created_at"]]) for row in evidence_rows)
    lines.extend(["", "## Test Records", "", "| ID | Surface | Command | Result | Evidence | Created At |", "| --- | --- | --- | --- | --- | --- |"])
    lines.extend(markdown_row([row["id"], row["surface"], row["command"], row["result"], row["evidence_id"], row["created_at"]]) for row in test_rows)
    write_view(root, "docs/harness/evidence.md", "\n".join(lines))


def render_findings(root: Path) -> None:
    with connection(root) as conn:
        rows = conn.execute("select * from findings order by created_at, id").fetchall()
    lines = ["# Findings", "", "| ID | Surface | Severity | Status | Summary | Evidence | Created At |", "| --- | --- | --- | --- | --- | --- | --- |"]
    lines.extend(markdown_row([row["id"], row["surface"], row["severity"], row["status"], row["summary"], row["evidence_id"], row["created_at"]]) for row in rows)
    write_view(root, "docs/harness/findings.md", "\n".join(lines))


def render_gates(root: Path) -> None:
    with connection(root) as conn:
        rows = conn.execute("select * from quality_gates order by created_at, id").fetchall()
    lines = ["# Quality Gates", "", "| Gate | Commit | Base | Head | Source Hash | Diff Hash | Project Revision | Reviewer Context | Result | Blocking Findings | Commands | Evidence | Residual Risk |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    lines.extend(markdown_row([row["gate"], row["reviewed_commit"], row["base_commit"], row["head_commit"], row["diff_hash"], row["tracked_diff_hash"], row["project_revision"], row["reviewer_context"], row["result"], row["blocking_findings"], row["commands"], row["evidence"], row["residual_risk"]]) for row in rows)
    write_view(root, "docs/harness/quality-gates.md", "\n".join(lines))


def render_deliveries(root: Path) -> None:
    with connection(root) as conn:
        rows = conn.execute("select * from deliveries order by created_at, id").fetchall()
    lines = ["# Delivery", ""]
    for row in rows:
        lines.extend(
            [
                f"## Delivery Record {row['created_at']}",
                "",
                "### Scope",
                row["scope"],
                "",
                "### Acceptance Mapping",
                row["acceptance"],
                "",
                "### Changed Files",
                row["changed_files"],
                "",
                "### Validation",
                row["validation"],
                "",
                "### Independent QA",
                row["qa"],
                "",
                "### Collaboration Links",
                row["collaboration_links"],
                "",
                "### Failure Mode Coverage",
                row["failure_mode_coverage"],
                "",
                "### Quality Gate",
                row["quality_gate"],
                "",
                "### Data / Config Notes",
                row["data_config_notes"],
                "",
                "### Known Gaps",
                row["known_gaps"],
                "",
                "### Handoff Notes",
                row["handoff"],
                "",
                "### Out Of Scope",
                "Deployment, production release, infrastructure provisioning, production migrations, secret changes, and paid-resource creation.",
                "",
            ]
        )
    write_view(root, "docs/harness/delivery.md", "\n".join(lines))


def render_decisions(root: Path) -> None:
    with connection(root) as conn:
        rows = conn.execute("select * from decisions order by created_at, id").fetchall()
    lines = ["# Decision Log", "", "| Date | Decision | Reason |", "| --- | --- | --- |"]
    lines.extend(markdown_row([row["created_at"], row["decision"], row["reason"]]) for row in rows)
    write_view(root, ".ai-team/control/decision-log.md", "\n".join(lines))


def render_tooling_map(root: Path) -> None:
    with connection(root) as conn:
        rows = conn.execute("select * from adapters order by tool, artifact").fetchall()
    lines = ["# Tooling Map", "", "| Artifact | Source Of Truth | External Tool | External ID / Link | Fallback | Mode | Idempotency Key |", "| --- | --- | --- | --- | --- | --- | --- |"]
    if not rows:
        defaults = [
            ("Requirements", "local", "", "", ".ai-team/requirements/requirements.md", "", ""),
            ("Tasks", "local", "", "", ".ai-team/planning/task-board.md", "", ""),
            ("Validation", "local", "", "", "docs/harness/validation.md", "", ""),
            ("Delivery", "local", "", "", "docs/harness/delivery.md", "", ""),
        ]
        lines.extend(markdown_row(list(row)) for row in defaults)
    else:
        lines.extend(
            markdown_row(
                [
                    row["artifact"],
                    "local",
                    row["tool"],
                    row["external_link"] or row["external_id"],
                    row["fallback"],
                    row["mode"],
                    row["idempotency_key"],
                ]
            )
            for row in rows
        )
    write_view(root, ".ai-team/control/tooling-map.md", "\n".join(lines))

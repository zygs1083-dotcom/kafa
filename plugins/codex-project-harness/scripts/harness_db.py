#!/usr/bin/env python3
"""SQLite-backed runtime for Codex Project Harness."""

from __future__ import annotations

import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from harness_lib import ensure_parent, git_dirty, git_head_sha, markdown_row, now_iso, write_state


SCHEMA_VERSION = 2
RUNTIME_VERSION = "2.1.0"
DB_PATH = Path(".ai-team/state/harness.db")

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
    "blocked",
    "accepted",
    "failed",
    "cancelled",
    "skipped",
}


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
            created_at text not null
        );
        create table if not exists quality_gates (
            id text primary key,
            gate text not null,
            reviewed_commit text not null,
            evidence_commit text not null default '',
            diff_hash text not null default '',
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


def init_runtime(root: Path) -> None:
    with transaction(root) as conn:
        create_schema(conn)
        initialize_project(conn)
        emit_event(conn, "runtime_initialized", "{}")
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
        emit_event(conn, "agents_installed", "{}")


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
        updates: dict[str, str] = {"phase": phase}
        if status:
            updates["status"] = status
        if owner:
            updates["current_owner"] = owner
        bump_project(conn, **updates)
        emit_event(conn, "phase_updated", f'{{"from":"{current}","to":"{phase}"}}')
    render_all(root)


def add_acceptance(root: Path, acceptance_id: str, criterion: str, priority: str = "", tool_link: str = "") -> None:
    with transaction(root) as conn:
        conn.execute(
            """
            insert into acceptance (id, criterion, priority, tool_link)
            values (?, ?, ?, ?)
            on conflict(id) do update set criterion=excluded.criterion, priority=excluded.priority, tool_link=excluded.tool_link,
                revision=acceptance.revision+1
            """,
            (acceptance_id, criterion, priority, tool_link),
        )
        emit_event(conn, "acceptance_added", f'{{"id":"{acceptance_id}"}}')
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
    expires_at: str = "",
) -> None:
    with transaction(root) as conn:
        conn.execute(
            """
            insert into failure_modes
            (id, feature, scenario, trigger, expected_behavior, recovery, data_safety, risk, status,
             accepted_by, acceptance_reason, expires_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(id) do update set feature=excluded.feature, scenario=excluded.scenario, trigger=excluded.trigger,
              expected_behavior=excluded.expected_behavior, recovery=excluded.recovery, data_safety=excluded.data_safety,
              risk=excluded.risk, status=excluded.status, accepted_by=excluded.accepted_by,
              acceptance_reason=excluded.acceptance_reason, expires_at=excluded.expires_at, revision=failure_modes.revision+1
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
                expires_at or None,
            ),
        )
        if acceptance:
            require_acceptance(conn, acceptance)
            conn.execute(
                "insert or ignore into failure_mode_acceptance (failure_mode_id, acceptance_id) values (?, ?)",
                (fm_id, acceptance),
            )
        emit_event(conn, "failure_mode_added", f'{{"id":"{fm_id}","risk":"{risk}"}}')
    render_all(root)


def require_acceptance(conn: sqlite3.Connection, acceptance_id: str) -> None:
    if not conn.execute("select id from acceptance where id = ?", (acceptance_id,)).fetchone():
        raise HarnessError(f"missing acceptance: {acceptance_id}")


def require_task(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    row = conn.execute("select * from tasks where id = ?", (task_id,)).fetchone()
    if not row:
        raise HarnessError(f"missing task: {task_id}")
    return row


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
        emit_event(conn, "task_created", f'{{"id":"{task_id}"}}')
    render_all(root)


def update_task(root: Path, task_id: str, *, depends_on: str | None = None, status: str | None = None) -> None:
    with transaction(root) as conn:
        row = require_task(conn, task_id)
        if status and status not in TASK_STATUSES:
            raise HarnessError(f"invalid task status: {status}")
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
        emit_event(conn, "task_updated", f'{{"id":"{row["id"]}"}}')
    render_all(root)


def ready_tasks(root: Path) -> list[str]:
    with connect(root) as conn:
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


def claim_task(root: Path, task_id: str, agent: str, expected_revision: int) -> str:
    with transaction(root) as conn:
        row = require_task(conn, task_id)
        if int(row["revision"]) != expected_revision:
            raise HarnessError(f"revision mismatch: expected {expected_revision}, actual {row['revision']}")
        if row["lease_agent"]:
            raise HarnessError(f"task already leased by {row['lease_agent']}")
        token = str(uuid.uuid4())
        conn.execute(
            """
            update tasks set lease_agent = ?, lease_token = ?, status = 'claimed',
              revision = revision + 1, updated_at = ? where id = ?
            """,
            (agent, token, now_iso(), task_id),
        )
        conn.execute(
            """
            update agents set lease_task_id = ?, status = 'leased', updated_at = ?
            where id = ?
            """,
            (task_id, now_iso(), agent),
        )
        emit_event(conn, "task_claimed", f'{{"id":"{task_id}","agent":"{agent}"}}')
    render_all(root)
    return token


def release_task(root: Path, task_id: str, agent: str) -> None:
    with transaction(root) as conn:
        row = require_task(conn, task_id)
        if row["lease_agent"] and row["lease_agent"] != agent:
            raise HarnessError(f"task leased by {row['lease_agent']}")
        conn.execute(
            "update tasks set lease_agent = null, lease_token = null, status = 'ready', revision = revision + 1, updated_at = ? where id = ?",
            (now_iso(), task_id),
        )
        conn.execute("update agents set lease_task_id = '', status = 'available', updated_at = ? where id = ?", (now_iso(), agent))
        emit_event(conn, "task_released", f'{{"id":"{task_id}","agent":"{agent}"}}')
    render_all(root)


def start_task(root: Path, task_id: str, agent: str) -> None:
    with transaction(root) as conn:
        row = require_task(conn, task_id)
        if row["lease_agent"] and row["lease_agent"] != agent:
            raise HarnessError(f"task leased by {row['lease_agent']}")
        conn.execute(
            "update tasks set status = 'in_progress', owner = ?, revision = revision + 1, updated_at = ? where id = ?",
            (agent, now_iso(), task_id),
        )
        emit_event(conn, "task_started", f'{{"id":"{task_id}","agent":"{agent}"}}')
    render_all(root)


def complete_task(root: Path, task_id: str, evidence: str) -> None:
    with transaction(root) as conn:
        require_task(conn, task_id)
        conn.execute(
            """
            update tasks set status = 'accepted', evidence = ?, lease_agent = null, lease_token = null,
              revision = revision + 1, updated_at = ? where id = ?
            """,
            (evidence, now_iso(), task_id),
        )
        emit_event(conn, "task_completed", f'{{"id":"{task_id}"}}')
    render_all(root)


def block_task(root: Path, task_id: str, reason: str) -> None:
    with transaction(root) as conn:
        require_task(conn, task_id)
        conn.execute(
            "update tasks set status = 'blocked', evidence = ?, revision = revision + 1, updated_at = ? where id = ?",
            (reason, now_iso(), task_id),
        )
        emit_event(conn, "task_blocked", f'{{"id":"{task_id}"}}')
    render_all(root)


def record_validation(root: Path, surface: str, findings: str, result: str, *, acceptance: str = "", commands: str = "", risk: str = "") -> None:
    with transaction(root) as conn:
        conn.execute(
            """
            insert into validations (id, surface, acceptance_id, commands, findings, result, residual_risk, created_at)
            values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (str(uuid.uuid4()), surface, acceptance, commands, findings, result, risk, now_iso()),
        )
        emit_event(conn, "validation_recorded", f'{{"surface":"{surface}","result":"{result}"}}')
    render_all(root)


def record_gate(root: Path, reviewer_context: str, result: str, *, gate: str = "independent_qa", commands: str = "", evidence: str = "", blocking_findings: str = "", residual_risk: str = "") -> None:
    current_sha = git_head_sha(root) or "no-git"
    if result == "pass" and git_dirty(root):
        raise HarnessError("cannot record a passing quality gate with a dirty git worktree")
    with transaction(root) as conn:
        conn.execute(
            """
            insert into quality_gates
            (id, gate, reviewed_commit, evidence_commit, reviewer_context, result, blocking_findings, commands, evidence, residual_risk, created_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                gate,
                current_sha,
                current_sha,
                reviewer_context,
                result,
                blocking_findings,
                commands,
                evidence,
                residual_risk,
                now_iso(),
            ),
        )
        emit_event(conn, "quality_gate_recorded", f'{{"gate":"{gate}","result":"{result}"}}')
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
        emit_event(conn, "delivery_recorded", f'{{"scope":"{scope}"}}')
    render_all(root)


def record_adapter(root: Path, tool: str, mode: str, artifact: str, external_id: str, idempotency_key: str, *, external_link: str = "", evidence: str = "", fallback: str = "", confirmation_needed: str = "no") -> None:
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
        emit_event(conn, "adapter_recorded", f'{{"tool":"{tool}","mode":"{mode}"}}', idempotency_key=idempotency_key)
    render_tooling_map(root)


def migrate(root: Path, from_version: int, to_version: int) -> None:
    with transaction(root) as conn:
        create_schema(conn)
        initialize_project(conn)
        conn.execute(
            "insert into migrations (from_version, to_version, applied_at) values (?, ?, ?)",
            (from_version, to_version, now_iso()),
        )
        conn.execute("update project set schema_version = ?, runtime_version = ?, revision = revision + 1, updated_at = ? where id = 1", (to_version, RUNTIME_VERSION, now_iso()))
        emit_event(conn, "migration_applied", f'{{"from":{from_version},"to":{to_version}}}')
    render_all(root)


def doctor(root: Path) -> list[str]:
    issues: list[str] = []
    path = db_file(root)
    if not path.exists():
        return ["missing sqlite state: .ai-team/state/harness.db"]
    with connect(root) as conn:
        try:
            project_row(conn)
        except HarnessError as exc:
            issues.append(str(exc))
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


def status_lines(root: Path) -> list[str]:
    with connect(root) as conn:
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
    init_runtime(root)
    migrate(root, SCHEMA_VERSION, SCHEMA_VERSION)
    render_all(root)


def render_all(root: Path) -> None:
    render_project_state(root)
    render_acceptance(root)
    render_failure_modes(root)
    render_tasks(root)
    render_validation(root)
    render_gates(root)
    render_deliveries(root)
    render_tooling_map(root)


def render_project_state(root: Path) -> None:
    with connect(root) as conn:
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


def render_acceptance(root: Path) -> None:
    with connect(root) as conn:
        rows = conn.execute("select * from acceptance order by id").fetchall()
    lines = ["# Acceptance Criteria", "", "| ID | Criterion | Priority | Tool Link | Status |", "| --- | --- | --- | --- | --- |"]
    lines.extend(markdown_row([row["id"], row["criterion"], row["priority"], row["tool_link"], row["status"]]) for row in rows)
    write_view(root, ".ai-team/requirements/acceptance.md", "\n".join(lines))


def render_failure_modes(root: Path) -> None:
    with connect(root) as conn:
        rows = conn.execute("select * from failure_modes order by id").fetchall()
        mappings = {
            row["failure_mode_id"]: row["ids"]
            for row in conn.execute(
                "select failure_mode_id, group_concat(acceptance_id, ', ') as ids from failure_mode_acceptance group by failure_mode_id"
            )
        }
    lines = ["# Failure Modes", "", "| ID | Feature | Scenario | Trigger | Expected Behavior | Recovery | Data Safety | Risk | Test Mapping | Status | Accepted By | Acceptance Reason | Expires At |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
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
                    row["accepted_by"] or "",
                    row["acceptance_reason"] or "",
                    row["expires_at"] or "",
                ]
            )
        )
    write_view(root, ".ai-team/requirements/failure-modes.md", "\n".join(lines))


def render_tasks(root: Path) -> None:
    with connect(root) as conn:
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
    with connect(root) as conn:
        rows = conn.execute("select * from validations order by created_at, id").fetchall()
    lines = ["# Validation", "", "| Surface | Acceptance | Tool Context | Commands | Findings | Pass/Fail | Residual Risk |", "| --- | --- | --- | --- | --- | --- | --- |"]
    lines.extend(markdown_row([row["surface"], row["acceptance_id"], "", row["commands"], row["findings"], row["result"], row["residual_risk"]]) for row in rows)
    write_view(root, "docs/harness/validation.md", "\n".join(lines))


def render_gates(root: Path) -> None:
    with connect(root) as conn:
        rows = conn.execute("select * from quality_gates order by created_at, id").fetchall()
    lines = ["# Quality Gates", "", "| Gate | Commit | Reviewer Context | Result | Blocking Findings | Commands | Evidence | Residual Risk |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    lines.extend(markdown_row([row["gate"], row["reviewed_commit"], row["reviewer_context"], row["result"], row["blocking_findings"], row["commands"], row["evidence"], row["residual_risk"]]) for row in rows)
    write_view(root, "docs/harness/quality-gates.md", "\n".join(lines))


def render_deliveries(root: Path) -> None:
    with connect(root) as conn:
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


def render_tooling_map(root: Path) -> None:
    with connect(root) as conn:
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

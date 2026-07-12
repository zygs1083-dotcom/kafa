"""Cycle-scoped facts, baseline identity, and traceability read models."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from harness_lib import content_source_tree_hash, git_source_tree_hash, now_iso
from .errors import HarnessError


DEFAULT_CYCLE_ID = "CYCLE-current"
LEGACY_CYCLE_ID = "CYCLE-legacy"


def _row_snapshot(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def _stable_digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def current_candidate_sha(root: Path) -> str:
    return git_source_tree_hash(root) or content_source_tree_hash(root)


def project_row(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute("select * from project where id = 1").fetchone()
    if not row:
        raise HarnessError("project is not initialized")
    return row


def current_cycle_row(conn: sqlite3.Connection) -> sqlite3.Row:
    project = project_row(conn)
    cycle_id = project["current_cycle_id"]
    if not cycle_id:
        raise HarnessError("current delivery cycle is not configured")
    cycle = conn.execute("select * from delivery_cycles where id = ?", (cycle_id,)).fetchone()
    if not cycle:
        raise HarnessError(f"current delivery cycle is missing: {cycle_id}")
    return cycle


def current_cycle_id(conn: sqlite3.Connection) -> str:
    return current_cycle_row(conn)["id"]


def ensure_delivery_cycles(conn: sqlite3.Connection) -> None:
    now = now_iso()
    project = conn.execute("select * from project where id = 1").fetchone()
    existing_cycle = conn.execute("select 1 from delivery_cycles limit 1").fetchone()
    if not existing_cycle:
        has_audit_rows = any(
            conn.execute(f"select 1 from {table} limit 1").fetchone()
            for table in [
                "requirements",
                "acceptance",
                "tasks",
                "failure_modes",
                "validations",
                "quality_gates",
                "deliveries",
                "invalidations",
            ]
        )
        if has_audit_rows:
            conn.execute(
                """
                insert into delivery_cycles
                (id, name, goal, status, phase, base_ref, candidate_sha, started_at, closed_at, created_at, updated_at)
                values (?, 'Legacy Audit Cycle', 'Imported schema 24 runtime records for audit only.', 'archived', 'archived', '', '', ?, ?, ?, ?)
                """,
                (LEGACY_CYCLE_ID, now, now, now, now),
            )
        conn.execute(
            """
            insert into delivery_cycles
            (id, name, goal, status, phase, base_ref, candidate_sha, started_at, closed_at, created_at, updated_at)
            values (?, 'Current Delivery Cycle', 'Current active delivery candidate.', 'active', 'intake', '', '', ?, '', ?, ?)
            on conflict(id) do nothing
            """,
            (DEFAULT_CYCLE_ID, now, now, now),
        )
        legacy_target = LEGACY_CYCLE_ID if has_audit_rows else DEFAULT_CYCLE_ID
        for table in ["requirements", "acceptance", "tasks", "failure_modes", "validations", "quality_gates", "deliveries", "invalidations"]:
            conn.execute(f"update {table} set cycle_id = ? where cycle_id = ''", (legacy_target,))
    if project:
        current_cycle_id = project["current_cycle_id"] if "current_cycle_id" in project.keys() else ""
        if not current_cycle_id or not conn.execute("select 1 from delivery_cycles where id = ?", (current_cycle_id,)).fetchone():
            current_cycle_id = DEFAULT_CYCLE_ID
            conn.execute(
                "update project set current_cycle_id = ?, phase = coalesce(nullif(phase, ''), 'intake'), updated_at = ? where id = 1",
                (DEFAULT_CYCLE_ID, now),
            )
        for table in ["requirements", "acceptance", "tasks", "failure_modes", "validations", "quality_gates", "deliveries", "invalidations"]:
            conn.execute(f"update {table} set cycle_id = ? where cycle_id = ''", (current_cycle_id,))


def baseline_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    cycle_id = current_cycle_id(conn)
    return {
        "requirements": [_row_snapshot(row) or {} for row in conn.execute("select * from requirements where cycle_id = ? order by id", (cycle_id,))],
        "acceptance": [_row_snapshot(row) or {} for row in conn.execute("select * from acceptance where cycle_id = ? order by id", (cycle_id,))],
        "requirement_acceptance": [
            _row_snapshot(row) or {}
            for row in conn.execute(
                """
                select ra.* from requirement_acceptance ra
                join requirements r on r.cycle_id = ra.cycle_id and r.id = ra.requirement_id
                where ra.cycle_id = ?
                order by ra.requirement_id, ra.acceptance_id
                """,
                (cycle_id,),
            )
        ],
        "failure_modes": [_row_snapshot(row) or {} for row in conn.execute("select * from failure_modes where cycle_id = ? order by id", (cycle_id,))],
        "failure_mode_acceptance": [
            _row_snapshot(row) or {}
            for row in conn.execute(
                """
                select fma.* from failure_mode_acceptance fma
                join failure_modes fm on fm.cycle_id = fma.cycle_id and fm.id = fma.failure_mode_id
                where fma.cycle_id = ?
                order by fma.failure_mode_id, fma.acceptance_id
                """,
                (cycle_id,),
            )
        ],
    }


def baseline_digest(conn: sqlite3.Connection) -> str:
    return _stable_digest(baseline_snapshot(conn))


def latest_baseline(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("select * from baselines order by created_at desc, id desc limit 1").fetchone()


def baseline_issues(conn: sqlite3.Connection) -> list[str]:
    row = latest_baseline(conn)
    if not row:
        return ["missing frozen baseline"]
    current = baseline_digest(conn)
    if row["digest"] != current:
        return [f"frozen baseline is stale: {row['id']}"]
    return []


def trace_snapshot(conn: sqlite3.Connection, requirement_id: str) -> dict[str, Any]:
    cycle_id = current_cycle_id(conn)
    return {
        "requirement_id": requirement_id,
        "acceptance_ids": [
            row["acceptance_id"]
            for row in conn.execute(
                "select acceptance_id from requirement_acceptance where cycle_id = ? and requirement_id = ? order by acceptance_id",
                (cycle_id, requirement_id),
            )
        ],
    }


def trace_rows(conn: sqlite3.Connection, requirement_id: str | None = None) -> list[dict[str, str]]:
    cycle_id = current_cycle_id(conn)
    clauses = ["r.status != 'cancelled'", "r.cycle_id = ?"]
    values: list[object] = [cycle_id]
    if requirement_id:
        clauses.append("r.id = ?")
        values.append(requirement_id)
    rows = conn.execute(
        f"""
        select
          r.id as requirement_id,
          r.kind as requirement_kind,
          r.body as requirement_body,
          ra.acceptance_id,
          a.criterion as acceptance_criterion,
          group_concat(distinct t.id) as task_ids,
          group_concat(distinct v.id) as validation_ids,
          group_concat(distinct vfm.failure_mode_id) as failure_mode_ids,
          group_concat(distinct d.id) as delivery_ids
        from requirements r
        left join requirement_acceptance ra on ra.cycle_id = r.cycle_id and ra.requirement_id = r.id
        left join acceptance a on a.cycle_id = ra.cycle_id and a.id = ra.acceptance_id
        left join task_acceptance ta on ta.cycle_id = ra.cycle_id and ta.acceptance_id = ra.acceptance_id
        left join tasks t on t.cycle_id = ta.cycle_id and t.id = ta.task_id
        left join validations v on v.acceptance_id = ra.acceptance_id and v.cycle_id = r.cycle_id and v.result = 'pass'
        left join validation_failure_modes vfm on vfm.validation_id = v.id and vfm.cycle_id = v.cycle_id
        left join delivery_acceptance da on da.cycle_id = ra.cycle_id and da.acceptance_id = ra.acceptance_id
        left join deliveries d on d.id = da.delivery_id and d.cycle_id = da.cycle_id
        where {' and '.join(clauses)}
        group by r.id, ra.acceptance_id
        order by r.id, ra.acceptance_id
        """,
        values,
    ).fetchall()
    return [{key: row[key] or "" for key in row.keys()} for row in rows]


def traceability_issues(conn: sqlite3.Connection, requirement_id: str | None = None) -> list[str]:
    issues: list[str] = []
    cycle_id = current_cycle_id(conn)
    req_clause = "where cycle_id = ? and status != 'cancelled'"
    values: list[object] = [cycle_id]
    if requirement_id:
        req_clause += " and id = ?"
        values.append(requirement_id)
    requirements = conn.execute(f"select id from requirements {req_clause} order by id", values).fetchall()
    for requirement in requirements:
        links = conn.execute(
            "select acceptance_id from requirement_acceptance where cycle_id = ? and requirement_id = ? order by acceptance_id",
            (cycle_id, requirement["id"]),
        ).fetchall()
        if not links:
            issues.append(f"requirement has no acceptance link: {requirement['id']}")
            continue
        for link in links:
            acceptance_id = link["acceptance_id"]
            accepted_task = conn.execute(
                """
                select 1 from task_acceptance ta
                join tasks t on t.cycle_id = ta.cycle_id and t.id = ta.task_id
                where ta.cycle_id = ? and ta.acceptance_id = ? and t.status in ('accepted', 'cancelled')
                limit 1
                """,
                (cycle_id, acceptance_id),
            ).fetchone()
            if not accepted_task:
                issues.append(f"acceptance has no completed task in trace: {requirement['id']} -> {acceptance_id}")
            passing_validation = conn.execute(
                "select 1 from validations where acceptance_id = ? and cycle_id = ? and validation_status = 'active' and result = 'pass' limit 1",
                (acceptance_id, cycle_id),
            ).fetchone()
            if not passing_validation:
                issues.append(f"acceptance has no passing validation in trace: {requirement['id']} -> {acceptance_id}")
    return issues

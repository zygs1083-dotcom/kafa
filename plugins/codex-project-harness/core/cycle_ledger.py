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


def baseline_snapshot(
    conn: sqlite3.Connection,
    cycle_id: str | None = None,
) -> dict[str, Any]:
    cycle_id = cycle_id or current_cycle_id(conn)
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


def baseline_digest(
    conn: sqlite3.Connection,
    cycle_id: str | None = None,
) -> str:
    return _stable_digest(baseline_snapshot(conn, cycle_id))


def latest_baseline(
    conn: sqlite3.Connection,
    cycle_id: str | None = None,
) -> sqlite3.Row | None:
    cycle_id = cycle_id or current_cycle_id(conn)
    return conn.execute(
        """
        select * from baselines
        where cycle_id = ?
        order by created_at desc, rowid desc
        limit 1
        """,
        (cycle_id,),
    ).fetchone()


def baseline_issues(
    conn: sqlite3.Connection,
    cycle_id: str | None = None,
) -> list[str]:
    row = latest_baseline(conn, cycle_id)
    if not row:
        return ["missing frozen baseline"]
    current = baseline_digest(conn, cycle_id)
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


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "select 1 from sqlite_master where type='table' and name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def _qualified_validation_ids(
    conn: sqlite3.Connection,
    cycle_id: str,
    acceptance_id: str,
    *,
    root: Path | None = None,
    candidate_override: str | None = None,
) -> list[str]:
    """Return only acceptance validations backed by a current qualified execution.

    Fixed schema-30 compatibility databases predate qualifications. Their trace
    projection retains the historical read model, while active schema 31 never
    promotes judgment-only validation rows to acceptance coverage.
    """

    validation_columns = {
        str(row[1]) for row in conn.execute("pragma table_info(validations)")
    }
    if (
        "qualification_id" not in validation_columns
        or not _table_exists(conn, "acceptance_target_qualifications")
    ):
        return [
            str(row["id"])
            for row in conn.execute(
                "select id from validations where acceptance_id = ? and cycle_id = ? "
                "and validation_status = 'active' and result = 'pass' "
                "order by created_at, id",
                (acceptance_id, cycle_id),
            ).fetchall()
        ]

    if root is None:
        return []

    # Import lazily because delivery owns the complete execution/provenance
    # eligibility contract and imports this module for its graph read models.
    # Runtime calls happen only after both modules are initialized.
    from .delivery import qualified_validation_execution_issues

    rows = conn.execute(
        """
        select * from validations
        where cycle_id = ? and acceptance_id = ?
          and validation_status = 'active' and result = 'pass'
          and qualification_id is not null
        order by created_at, id
        """,
        (cycle_id, acceptance_id),
    ).fetchall()
    eligible: list[str] = []
    candidate = candidate_override or current_candidate_sha(root)
    for validation in rows:
        qualification = conn.execute(
            "select * from acceptance_target_qualifications where id = ?",
            (validation["qualification_id"],),
        ).fetchone()
        if qualification is None:
            continue
        if not qualified_validation_execution_issues(
            conn,
            root,
            validation,
            qualification,
            candidate,
        ):
            eligible.append(str(validation["id"]))
    return eligible


def trace_rows(
    conn: sqlite3.Connection,
    requirement_id: str | None = None,
    *,
    root: Path | None = None,
    candidate_override: str | None = None,
) -> list[dict[str, str]]:
    cycle_id = current_cycle_id(conn)
    clauses = ["r.status = 'active'", "r.cycle_id = ?"]
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
          a.id as acceptance_id,
          a.criterion as acceptance_criterion,
          group_concat(distinct t.id) as task_ids,
          group_concat(distinct d.id) as delivery_ids
        from requirements r
        left join requirement_acceptance ra on ra.cycle_id = r.cycle_id and ra.requirement_id = r.id
        left join acceptance a on a.cycle_id = ra.cycle_id and a.id = ra.acceptance_id
          and a.status = 'active'
        left join task_acceptance ta on ta.cycle_id = a.cycle_id and ta.acceptance_id = a.id
        left join tasks t on t.cycle_id = ta.cycle_id and t.id = ta.task_id
          and t.status = 'accepted'
        left join delivery_acceptance da on da.cycle_id = ra.cycle_id and da.acceptance_id = ra.acceptance_id
        left join deliveries d on d.id = da.delivery_id and d.cycle_id = da.cycle_id
        where {' and '.join(clauses)}
        group by r.id, a.id
        order by r.id, a.id
        """,
        values,
    ).fetchall()
    result: list[dict[str, str]] = []
    for row in rows:
        rendered = {key: row[key] or "" for key in row.keys()}
        acceptance_id = str(row["acceptance_id"] or "")
        validation_ids = (
            _qualified_validation_ids(
                conn,
                cycle_id,
                acceptance_id,
                root=root,
                candidate_override=candidate_override,
            )
            if acceptance_id
            else []
        )
        rendered["validation_ids"] = ",".join(validation_ids)
        failure_mode_ids: list[str] = []
        if validation_ids:
            placeholders = ",".join("?" for _ in validation_ids)
            failure_mode_ids = [
                str(item["failure_mode_id"])
                for item in conn.execute(
                    f"select distinct failure_mode_id from validation_failure_modes "
                    f"where cycle_id = ? and validation_id in ({placeholders}) "
                    "order by failure_mode_id",
                    (cycle_id, *validation_ids),
                ).fetchall()
            ]
        rendered["failure_mode_ids"] = ",".join(failure_mode_ids)
        result.append(rendered)
    return result


def traceability_issues(
    conn: sqlite3.Connection,
    requirement_id: str | None = None,
    *,
    root: Path | None = None,
    candidate_override: str | None = None,
) -> list[str]:
    issues: list[str] = []
    cycle_id = current_cycle_id(conn)
    req_clause = "where cycle_id = ? and status = 'active'"
    values: list[object] = [cycle_id]
    if requirement_id:
        req_clause += " and id = ?"
        values.append(requirement_id)
    requirements = conn.execute(f"select id from requirements {req_clause} order by id", values).fetchall()
    for requirement in requirements:
        links = conn.execute(
            """
            select ra.acceptance_id
            from requirement_acceptance ra
            join acceptance a
              on a.cycle_id = ra.cycle_id and a.id = ra.acceptance_id
            where ra.cycle_id = ? and ra.requirement_id = ?
              and a.status = 'active'
            order by ra.acceptance_id
            """,
            (cycle_id, requirement["id"]),
        ).fetchall()
        if not links:
            issues.append(
                "[requirement-acceptance-link-missing] requirement has no "
                f"acceptance link: {requirement['id']} (no active acceptance)"
            )
            continue
        for link in links:
            acceptance_id = link["acceptance_id"]
            accepted_task = conn.execute(
                """
                select 1 from task_acceptance ta
                join tasks t on t.cycle_id = ta.cycle_id and t.id = ta.task_id
                where ta.cycle_id = ? and ta.acceptance_id = ? and t.status = 'accepted'
                limit 1
                """,
                (cycle_id, acceptance_id),
            ).fetchone()
            if not accepted_task:
                issues.append(
                    "[accepted-task-missing] acceptance has no accepted task in trace: "
                    f"{requirement['id']} -> {acceptance_id}"
                )
            if not _qualified_validation_ids(
                conn,
                cycle_id,
                acceptance_id,
                root=root,
                candidate_override=candidate_override,
            ):
                issues.append(f"acceptance has no passing validation in trace: {requirement['id']} -> {acceptance_id}")
    if requirement_id is None:
        orphaned = conn.execute(
            """
            select a.id
            from acceptance a
            where a.cycle_id = ? and a.status = 'active'
              and not exists (
                select 1
                from requirement_acceptance ra
                join requirements r
                  on r.cycle_id = ra.cycle_id and r.id = ra.requirement_id
                where ra.cycle_id = a.cycle_id
                  and ra.acceptance_id = a.id
                  and r.status = 'active'
              )
            order by a.id
            """,
            (cycle_id,),
        ).fetchall()
        issues.extend(
            "[acceptance-orphaned] active acceptance is not linked from an "
            f"active requirement: {row['id']}"
            for row in orphaned
        )
    return issues

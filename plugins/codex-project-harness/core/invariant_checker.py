"""Runtime invariant checker for the consistency kernel."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .event_bus import validate_audit_events
from .execution import target_definition_digest
from .schema_guard import (
    ACCEPTANCE_STATUSES,
    FAILURE_MODE_STATUSES,
    REQUIREMENT_STATUSES,
    TASK_STATUSES,
)


IMMUTABLE_TRIGGER_CONTRACT = frozenset(
    {
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
)


@dataclass(frozen=True)
class InvariantIssue:
    code: str
    entity_type: str
    entity_id: str
    message: str

    def __str__(self) -> str:
        return self.message


def issue(code: str, entity_type: str, entity_id: str, message: str) -> InvariantIssue:
    return InvariantIssue(code=code, entity_type=entity_type, entity_id=entity_id, message=message)


def scoped_ids(scope: Iterable[tuple[str, str]] | None, entity_type: str) -> set[str] | None:
    if scope is None:
        return None
    return {entity_id for item_type, entity_id in scope if item_type == entity_type and entity_id}


def event_waterline_issues(conn: sqlite3.Connection) -> list[InvariantIssue]:
    row = conn.execute("select count(*) as count, coalesce(max(sequence), 0) as max_sequence from events").fetchone()
    if int(row["count"]) != int(row["max_sequence"]):
        return [
            issue(
                "event-waterline",
                "event",
                "sequence",
                f"invariant failed: event sequence is not continuous count={row['count']} max={row['max_sequence']}",
            )
        ]
    return []


def immutable_trigger_issues(conn: sqlite3.Connection) -> list[InvariantIssue]:
    actual = {
        str(row[0])
        for row in conn.execute(
            "select name from sqlite_master where type='trigger'"
        ).fetchall()
    }
    return [
        issue(
            "immutable-trigger-missing",
            "schema",
            name,
            f"invariant failed: immutable trigger is missing {name}",
        )
        for name in sorted(IMMUTABLE_TRIGGER_CONTRACT - actual)
    ]


def query_scoped(conn: sqlite3.Connection, sql_all: str, sql_scoped: str, ids: set[str] | None) -> list[sqlite3.Row]:
    if ids is None:
        return conn.execute(sql_all).fetchall()
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    return conn.execute(sql_scoped.format(placeholders=placeholders), tuple(sorted(ids))).fetchall()


def _has_task_accept_event(
    conn: sqlite3.Connection,
    task_id: str,
    cycle_id: str,
) -> bool:
    import json

    for event in conn.execute(
        """
        select after_json from events
        where event_type = 'task_accepted' and entity_id = ?
        order by sequence desc
        """,
        (task_id,),
    ).fetchall():
        try:
            payload = json.loads(str(event["after_json"] or "{}"))
        except (TypeError, json.JSONDecodeError):
            continue
        if (
            isinstance(payload, dict)
            and str(payload.get("cycle_id") or "") == cycle_id
        ):
            return True
    return False


def check_cycle_invariants(
    conn: sqlite3.Connection,
    root: Path,
    cycle_id: str,
) -> list[InvariantIssue]:
    """Validate only facts owned by one persisted delivery cycle."""

    del root  # Reserved for parity with the full invariant interface.
    issues: list[InvariantIssue] = immutable_trigger_issues(conn)
    state_contracts = (
        ("requirements", REQUIREMENT_STATUSES, "requirement"),
        ("acceptance", ACCEPTANCE_STATUSES, "acceptance"),
        ("tasks", TASK_STATUSES, "task"),
        ("failure_modes", FAILURE_MODE_STATUSES, "failure_mode"),
    )
    for table, allowed, entity_type in state_contracts:
        for row in conn.execute(
            f"select id, status from {table} where cycle_id = ? order by id",
            (cycle_id,),
        ).fetchall():
            if row["status"] not in allowed:
                issues.append(
                    issue(
                        f"invalid-{entity_type.replace('_', '-')}-status",
                        entity_type,
                        str(row["id"]),
                        "invariant failed: invalid "
                        f"{entity_type.replace('_', ' ')} status "
                        f"{row['id']}={row['status']}",
                    )
                )

    for row in conn.execute(
        """
        select id, evidence, accepted_by from tasks
        where cycle_id = ? and status = 'accepted'
        order by id
        """,
        (cycle_id,),
    ).fetchall():
        if not str(row["evidence"] or "").strip():
            issues.append(
                issue(
                    "accepted-task-missing-evidence",
                    "task",
                    str(row["id"]),
                    f"invariant failed: accepted task has no evidence {row['id']}",
                )
            )
        if not str(row["accepted_by"] or "").strip() and not _has_task_accept_event(
            conn,
            str(row["id"]),
            cycle_id,
        ):
            issues.append(
                issue(
                    "accepted-task-missing-actor",
                    "task",
                    str(row["id"]),
                    "invariant failed: accepted task has no accept actor/event "
                    f"{row['id']}",
                )
            )

    for row in conn.execute(
        "select id, acceptance from deliveries where cycle_id = ? order by created_at, id",
        (cycle_id,),
    ).fetchall():
        if not str(row["acceptance"] or "").strip():
            continue
        if conn.execute(
            "select 1 from delivery_acceptance where delivery_id = ? limit 1",
            (row["id"],),
        ).fetchone() is None:
            issues.append(
                issue(
                    "delivery-missing-acceptance-link",
                    "delivery",
                    str(row["id"]),
                    f"invariant failed: delivery has no linked acceptance {row['id']}",
                )
            )

    for row in conn.execute(
        """
        select q.id, q.target_id, q.target_definition_sha256
        from acceptance_target_qualifications q
        where q.cycle_id = ?
        order by q.id
        """,
        (cycle_id,),
    ).fetchall():
        target = conn.execute(
            "select * from test_targets where id = ?",
            (row["target_id"],),
        ).fetchone()
        actual = target_definition_digest(dict(target)) if target is not None else ""
        if actual != str(row["target_definition_sha256"]):
            issues.append(
                issue(
                    "closed-cycle-target-definition-changed",
                    "acceptance_target_qualification",
                    str(row["id"]),
                    "invariant failed: closed-cycle qualification target "
                    f"definition changed {cycle_id}:{row['id']} "
                    f"target={row['target_id']}",
                )
            )

    for row in conn.execute(
        """
        select g.id as gate_id, f.id as finding_id,
               f.cycle_id as finding_cycle, f.candidate_sha as finding_candidate,
               g.candidate_sha as gate_candidate
        from quality_gate_findings link
        join quality_gates g on g.id = link.gate_id
        join findings f on f.id = link.finding_id
        where g.cycle_id = ?
          and (f.cycle_id != g.cycle_id or f.candidate_sha != g.candidate_sha)
        order by g.id, f.id
        """,
        (cycle_id,),
    ).fetchall():
        issues.append(
            issue(
                "cross-cycle-gate-finding",
                "quality_gate",
                str(row["gate_id"]),
                "invariant failed: quality gate links a finding from a "
                "different cycle or candidate: "
                f"gate={row['gate_id']}({cycle_id},{row['gate_candidate']}) "
                f"finding={row['finding_id']}({row['finding_cycle']},"
                f"{row['finding_candidate']})",
            )
        )
    return issues


def check_runtime_invariants(
    conn: sqlite3.Connection,
    root: Path,
    scope: Iterable[tuple[str, str]] | None = None,
    *,
    full: bool = True,
) -> list[InvariantIssue]:
    issues: list[InvariantIssue] = []
    task_ids = scoped_ids(scope, "task") if scope is not None else None
    requirement_ids = scoped_ids(scope, "requirement") if scope is not None else None
    acceptance_ids = scoped_ids(scope, "acceptance") if scope is not None else None
    failure_mode_ids = scoped_ids(scope, "failure_mode") if scope is not None else None
    delivery_ids = scoped_ids(scope, "delivery") if scope is not None else None

    for row in query_scoped(
        conn,
        "select id, status from requirements order by id",
        "select id, status from requirements where cycle_id = (select current_cycle_id from project where id = 1) and id in ({placeholders}) order by id",
        requirement_ids,
    ):
        if row["status"] not in REQUIREMENT_STATUSES:
            issues.append(
                issue(
                    "invalid-requirement-status",
                    "requirement",
                    row["id"],
                    f"invariant failed: invalid requirement status {row['id']}={row['status']}",
                )
            )

    for row in query_scoped(
        conn,
        "select id, status from acceptance order by id",
        "select id, status from acceptance where cycle_id = (select current_cycle_id from project where id = 1) and id in ({placeholders}) order by id",
        acceptance_ids,
    ):
        if row["status"] not in ACCEPTANCE_STATUSES:
            issues.append(
                issue(
                    "invalid-acceptance-status",
                    "acceptance",
                    row["id"],
                    f"invariant failed: invalid acceptance status {row['id']}={row['status']}",
                )
            )

    for row in query_scoped(
        conn,
        "select id, status from tasks order by id",
        "select id, status from tasks where cycle_id = (select current_cycle_id from project where id = 1) and id in ({placeholders}) order by id",
        task_ids,
    ):
        if row["status"] not in TASK_STATUSES:
            issues.append(issue("invalid-task-status", "task", row["id"], f"invariant failed: invalid task status {row['id']}={row['status']}"))

    for row in query_scoped(
        conn,
        "select cycle_id, id, evidence, owner, accepted_by from tasks where status = 'accepted' order by id",
        "select cycle_id, id, evidence, owner, accepted_by from tasks where cycle_id = (select current_cycle_id from project where id = 1) and id in ({placeholders}) and status = 'accepted' order by id",
        task_ids,
    ):
        if not row["evidence"]:
            issues.append(issue("accepted-task-missing-evidence", "task", row["id"], f"invariant failed: accepted task has no evidence {row['id']}"))
        accepted_by = row["accepted_by"] if "accepted_by" in row.keys() else ""
        accept_event = _has_task_accept_event(
            conn,
            str(row["id"]),
            str(row["cycle_id"]),
        )
        if not accepted_by and not accept_event:
            issues.append(issue("accepted-task-missing-actor", "task", row["id"], f"invariant failed: accepted task has no accept actor/event {row['id']}"))
    for row in query_scoped(
        conn,
        "select id, status from failure_modes order by id",
        "select id, status from failure_modes where cycle_id = (select current_cycle_id from project where id = 1) and id in ({placeholders}) order by id",
        failure_mode_ids,
    ):
        if row["status"] not in FAILURE_MODE_STATUSES:
            issues.append(issue("invalid-failure-mode-status", "failure_mode", row["id"], f"invariant failed: invalid failure mode status {row['id']}={row['status']}"))

    for row in query_scoped(
        conn,
        "select id, scope, acceptance from deliveries order by created_at, id",
        "select id, scope, acceptance from deliveries where id in ({placeholders}) order by created_at, id",
        delivery_ids,
    ):
        if not row["acceptance"]:
            continue
        linked_acceptance = conn.execute("select 1 from delivery_acceptance where delivery_id = ? limit 1", (row["id"],)).fetchone()
        if not linked_acceptance:
            issues.append(issue("delivery-missing-acceptance-link", "delivery", row["id"], f"invariant failed: delivery has no linked acceptance {row['id']}"))

    if scope is None or full:
        project = conn.execute(
            "select schema_version from project where id = 1"
        ).fetchone()
        schema_version = int(project["schema_version"]) if project else 0
        if schema_version >= 31:
            for row in conn.execute(
                """
                select q.id, q.cycle_id, q.target_id,
                       q.target_definition_sha256
                from acceptance_target_qualifications q
                join delivery_cycles c on c.id = q.cycle_id
                where c.status in ('delivered', 'archived')
                order by q.cycle_id, q.id
                """
            ).fetchall():
                target = conn.execute(
                    "select * from test_targets where id = ?",
                    (row["target_id"],),
                ).fetchone()
                actual = (
                    target_definition_digest(dict(target)) if target is not None else ""
                )
                if actual != str(row["target_definition_sha256"]):
                    issues.append(
                        issue(
                            "closed-cycle-target-definition-changed",
                            "acceptance_target_qualification",
                            str(row["id"]),
                            "invariant failed: closed-cycle qualification target "
                            f"definition changed {row['cycle_id']}:{row['id']} "
                            f"target={row['target_id']}",
                        )
                    )
            for row in conn.execute(
                """
                select g.id as gate_id, g.cycle_id as gate_cycle,
                       g.candidate_sha as gate_candidate,
                       f.id as finding_id, f.cycle_id as finding_cycle,
                       f.candidate_sha as finding_candidate
                from quality_gate_findings link
                join quality_gates g on g.id = link.gate_id
                join findings f on f.id = link.finding_id
                where g.cycle_id != f.cycle_id
                   or g.candidate_sha != f.candidate_sha
                order by g.id, f.id
                """
            ).fetchall():
                issues.append(
                    issue(
                        "cross-cycle-gate-finding",
                        "quality_gate",
                        str(row["gate_id"]),
                        "invariant failed: quality gate links a finding from a "
                        "different cycle or candidate: "
                        f"gate={row['gate_id']}({row['gate_cycle']},"
                        f"{row['gate_candidate']}) finding={row['finding_id']}"
                        f"({row['finding_cycle']},{row['finding_candidate']})",
                    )
                )
        issues.extend(issue("event-payload", "event", "", str(event_issue)) for event_issue in validate_audit_events(conn))
        issues.extend(immutable_trigger_issues(conn))
    else:
        issues.extend(event_waterline_issues(conn))
    return issues

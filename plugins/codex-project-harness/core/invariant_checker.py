"""Runtime invariant checker for the consistency kernel."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .event_bus import validate_replay_compatible_events
from .lock_manager import is_expired
from .schema_guard import FAILURE_MODE_STATUSES, TASK_STATUSES, adapter_action_payload_hash


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


def query_scoped(conn: sqlite3.Connection, sql_all: str, sql_scoped: str, ids: set[str] | None) -> list[sqlite3.Row]:
    if ids is None:
        return conn.execute(sql_all).fetchall()
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    return conn.execute(sql_scoped.format(placeholders=placeholders), tuple(sorted(ids))).fetchall()


def check_runtime_invariants(
    conn: sqlite3.Connection,
    root: Path,
    scope: Iterable[tuple[str, str]] | None = None,
    *,
    full: bool = True,
) -> list[InvariantIssue]:
    issues: list[InvariantIssue] = []
    task_ids = scoped_ids(scope, "task") if scope is not None else None
    failure_mode_ids = scoped_ids(scope, "failure_mode") if scope is not None else None
    delivery_ids = scoped_ids(scope, "delivery") if scope is not None else None

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
        "select id, lease_agent, lease_expires_at from tasks where lease_agent is not null and lease_expires_at is not null order by id",
        "select id, lease_agent, lease_expires_at from tasks where cycle_id = (select current_cycle_id from project where id = 1) and id in ({placeholders}) and lease_agent is not null and lease_expires_at is not null order by id",
        task_ids,
    ):
        if is_expired(row["lease_expires_at"]):
            issues.append(issue("expired-lease", "task", row["id"], f"invariant failed: expired lease remains active {row['id']} agent={row['lease_agent']}"))

    for row in query_scoped(
        conn,
        "select cycle_id, id, evidence, owner, accepted_by from tasks where status = 'accepted' order by id",
        "select cycle_id, id, evidence, owner, accepted_by from tasks where cycle_id = (select current_cycle_id from project where id = 1) and id in ({placeholders}) and status = 'accepted' order by id",
        task_ids,
    ):
        if not row["evidence"]:
            issues.append(issue("accepted-task-missing-evidence", "task", row["id"], f"invariant failed: accepted task has no evidence {row['id']}"))
        accepted_by = row["accepted_by"] if "accepted_by" in row.keys() else ""
        accept_event = conn.execute(
            """
            select 1 from events
            where type = 'task_accepted' and json_extract(payload_json, '$.entity_id') = ?
              and json_extract(payload_json, '$.after.cycle_id') = ?
            limit 1
            """,
            (row["id"], row["cycle_id"]),
        ).fetchone()
        if not accepted_by and not accept_event:
            issues.append(issue("accepted-task-missing-actor", "task", row["id"], f"invariant failed: accepted task has no accept actor/event {row['id']}"))
        if accepted_by and accepted_by == row["owner"]:
            issues.append(issue("producer-self-accepted", "task", row["id"], f"invariant failed: producer accepted own task {row['id']} actor={accepted_by}"))

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
        adapter_columns = {row[1] for row in conn.execute("pragma table_info(adapter_actions)")}
        if "payload_hash" not in adapter_columns:
            issues.append(
                issue(
                    "adapter-payload-hash-column",
                    "adapter_action",
                    "",
                    "invariant failed: adapter_actions payload_hash column is missing",
                )
            )
        else:
            for row in conn.execute(
                "select id, tool, mode, artifact, action, payload_json, payload_hash from adapter_actions order by id"
            ):
                expected_hash = adapter_action_payload_hash(
                    str(row["tool"]),
                    str(row["mode"]),
                    str(row["artifact"]),
                    str(row["action"]),
                    str(row["payload_json"]),
                )
                if row["payload_hash"] != expected_hash:
                    issues.append(
                        issue(
                            "adapter-payload-hash-mismatch",
                            "adapter_action",
                            row["id"],
                            f"invariant failed: adapter action payload hash mismatch {row['id']}",
                        )
                    )

    if scope is None or full:
        issues.extend(issue("event-payload", "event", "", str(event_issue)) for event_issue in validate_replay_compatible_events(conn))
    else:
        issues.extend(event_waterline_issues(conn))
    return issues

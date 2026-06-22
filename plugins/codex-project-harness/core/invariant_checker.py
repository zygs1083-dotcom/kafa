"""Runtime invariant checker for the consistency kernel."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .event_bus import validate_replay_compatible_events
from .lock_manager import is_expired
from .schema_guard import FAILURE_MODE_STATUSES, TASK_STATUSES


def check_runtime_invariants(conn: sqlite3.Connection, root: Path) -> list[str]:
    issues: list[str] = []

    for row in conn.execute("select id, status from tasks order by id"):
        if row["status"] not in TASK_STATUSES:
            issues.append(f"invariant failed: invalid task status {row['id']}={row['status']}")

    for row in conn.execute(
        "select id, lease_agent, lease_expires_at from tasks where lease_agent is not null and lease_expires_at is not null order by id"
    ):
        if is_expired(row["lease_expires_at"]):
            issues.append(f"invariant failed: expired lease remains active {row['id']} agent={row['lease_agent']}")

    for row in conn.execute("select id, evidence, owner, accepted_by from tasks where status = 'accepted' order by id"):
        if not row["evidence"]:
            issues.append(f"invariant failed: accepted task has no evidence {row['id']}")
        accepted_by = row["accepted_by"] if "accepted_by" in row.keys() else ""
        accept_event = conn.execute(
            """
            select 1 from events
            where type = 'task_accepted' and json_extract(payload_json, '$.entity_id') = ?
            limit 1
            """,
            (row["id"],),
        ).fetchone()
        if not accepted_by and not accept_event:
            issues.append(f"invariant failed: accepted task has no accept actor/event {row['id']}")
        if accepted_by and accepted_by == row["owner"]:
            issues.append(f"invariant failed: producer accepted own task {row['id']} actor={accepted_by}")

    for row in conn.execute("select id, status from failure_modes order by id"):
        if row["status"] not in FAILURE_MODE_STATUSES:
            issues.append(f"invariant failed: invalid failure mode status {row['id']}={row['status']}")

    for row in conn.execute("select id, scope, acceptance from deliveries order by created_at, id"):
        if not row["acceptance"]:
            continue
        linked_acceptance = conn.execute("select 1 from delivery_acceptance where delivery_id = ? limit 1", (row["id"],)).fetchone()
        if not linked_acceptance:
            issues.append(f"invariant failed: delivery has no linked acceptance {row['id']}")

    issues.extend(validate_replay_compatible_events(conn))
    return issues

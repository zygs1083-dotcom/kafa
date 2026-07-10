"""Event bus and checkpoint-based replay support."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness_lib import now_iso


@dataclass(frozen=True)
class EventEnvelope:
    event_type: str
    payload_json: str
    source: str = "harness-runtime"
    target: str = "project"
    idempotency_key: str = ""
    correlation_id: str = ""
    causation_id: str = ""


def payload(**values: object) -> str:
    return json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def store(conn: sqlite3.Connection, schema_version: int, envelope: EventEnvelope) -> None:
    conn.execute(
        """
        insert into events
        (id, schema_version, type, source, target, correlation_id, causation_id, idempotency_key, payload_json, created_at)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            schema_version,
            envelope.event_type,
            envelope.source,
            envelope.target,
            envelope.correlation_id,
            envelope.causation_id,
            envelope.idempotency_key,
            envelope.payload_json,
            now_iso(),
        ),
    )


def dispatch(conn: sqlite3.Connection, envelope: EventEnvelope) -> None:
    # Reserved for local projection/replay metadata. External writes are intentionally out of scope.
    return None


def emit(
    conn: sqlite3.Connection,
    schema_version: int,
    event_type: str,
    payload_json: str,
    *,
    source: str = "harness-runtime",
    target: str = "project",
    idempotency_key: str = "",
    correlation_id: str = "",
    causation_id: str = "",
) -> None:
    envelope = EventEnvelope(
        event_type=event_type,
        payload_json=payload_json,
        source=source,
        target=target,
        idempotency_key=idempotency_key,
        correlation_id=correlation_id,
        causation_id=causation_id,
    )
    store(conn, schema_version, envelope)
    dispatch(conn, envelope)


def emit_audit(
    conn: sqlite3.Connection,
    schema_version: int,
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
    correlation_id = str(uuid.uuid4())
    data: dict[str, Any] = {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "previous_status": before.get("status") if before else None,
        "new_status": after.get("status") if after else None,
        "previous_revision": before.get("revision") if before else None,
        "new_revision": after.get("revision") if after else None,
        "actor": actor,
        "command": command,
        "correlation_id": correlation_id,
        "before": before,
        "after": after,
    }
    if extra:
        data.update(extra)
    emit(
        conn,
        schema_version,
        event_type,
        payload(**data),
        source="harness-runtime",
        target=f"{entity_type}:{entity_id}",
        correlation_id=correlation_id,
    )


def validate_replay_compatible_events(conn: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    checkpoint = conn.execute(
        "select event_sequence from runtime_snapshots order by event_sequence desc, created_at desc limit 1"
    ).fetchone()
    lower_bound = int(checkpoint["event_sequence"]) if checkpoint else None
    query = "select sequence, id, schema_version, payload_json from events order by sequence"
    for row in conn.execute(query):
        if lower_bound is not None and int(row["sequence"]) <= lower_bound:
            continue
        try:
            payload_data = json.loads(row["payload_json"])
        except json.JSONDecodeError as exc:
            issues.append(f"event {row['sequence']} invalid payload: {exc.msg}")
            continue
        if int(row["schema_version"]) >= 29 and "canonical_mutations" not in payload_data and not conn.in_transaction:
            issues.append(f"event {row['sequence']} missing canonical_mutations")
        if payload_data.get("entity_type"):
            for field in ["entity_id", "after", "correlation_id", "command"]:
                if field not in payload_data:
                    issues.append(f"event {row['sequence']} missing {field}")
    return issues


def apply_event_after(conn: sqlite3.Connection, event: sqlite3.Row) -> None:
    from harness_db import SNAPSHOT_TABLES, table_columns

    payload_data = json.loads(event["payload_json"])
    if int(event["schema_version"]) >= 29 and "canonical_mutations" not in payload_data:
        raise ValueError(f"event {event['sequence']} missing canonical_mutations")
    if payload_data.get("replay_boundary"):
        raise ValueError(str(payload_data["replay_boundary"]))
    if "canonical_mutations" in payload_data:
        allowed_tables = set(SNAPSHOT_TABLES) - {"events"}
        conn.execute("pragma defer_foreign_keys = on")
        for mutation in payload_data["canonical_mutations"]:
            table = str(mutation.get("table", ""))
            if table not in allowed_tables:
                raise ValueError(f"event mutation table is not replayable: {table}")
            key = mutation.get("key") or {}
            if not isinstance(key, dict) or not key:
                raise ValueError(f"event mutation has no key: {table}")
            if mutation.get("op") == "delete":
                clauses = " and ".join(f"{column} = ?" for column in key)
                conn.execute(f"delete from {table} where {clauses}", tuple(key.values()))
                continue
            if mutation.get("op") != "upsert" or not isinstance(mutation.get("row"), dict):
                raise ValueError(f"event mutation operation is invalid: {table}")
            row = mutation["row"]
            columns = [column for column in table_columns(conn, table) if column in row]
            if not columns:
                raise ValueError(f"event mutation row has no writable columns: {table}")
            conflict_columns = list(key)
            assignments = ", ".join(
                f"{column}=excluded.{column}" for column in columns if column not in conflict_columns
            )
            conflict_action = f"do update set {assignments}" if assignments else "do nothing"
            conn.execute(
                f"insert into {table} ({','.join(columns)}) values ({','.join('?' for _ in columns)}) "
                f"on conflict({','.join(conflict_columns)}) {conflict_action}",
                [row[column] for column in columns],
            )
        return
    entity_type = payload_data.get("entity_type")
    after = payload_data.get("after")
    if not entity_type or after is None:
        return
    table_by_entity = {
        "project": "project",
        "requirement": "requirements",
        "acceptance": "acceptance",
        "failure_mode": "failure_modes",
        "task": "tasks",
        "validation": "validations",
        "quality_gate": "quality_gates",
        "delivery": "deliveries",
    }
    table = table_by_entity.get(entity_type)
    if not table:
        return
    if table == "project":
        after["id"] = 1
    columns = [column for column in table_columns(conn, table) if column in after]
    if not columns:
        return
    cycle_local = table in {"requirements", "acceptance", "failure_modes", "tasks"}
    conflict_columns = ("cycle_id", "id") if cycle_local else ("id",)
    values = [after.get(column) for column in columns]
    immutable_columns = set(conflict_columns) | ({"uid"} if cycle_local else set())
    assignments = ", ".join(f"{column}=excluded.{column}" for column in columns if column not in immutable_columns)
    conn.execute(
        f"insert into {table} ({','.join(columns)}) values ({','.join('?' for _ in columns)}) "
        f"on conflict({','.join(conflict_columns)}) do update set {assignments}",
        values,
    )


def rebuild_state_from_events(root: Path, to_sequence: int, out: Path) -> None:
    from harness_db import connection, ensure_parent, restore_snapshot

    with connection(root) as conn:
        checkpoint = conn.execute(
            "select * from runtime_snapshots where event_sequence <= ? order by event_sequence desc, created_at desc limit 1",
            (to_sequence,),
        ).fetchone()
        if not checkpoint:
            raise ValueError("event replay requires a checkpoint at or before target sequence")
        snapshot = json.loads(checkpoint["snapshot_json"])
        events = conn.execute(
            "select * from events where sequence > ? and sequence <= ? order by sequence",
            (checkpoint["event_sequence"], to_sequence),
        ).fetchall()
    ensure_parent(out)
    if out.exists():
        out.unlink()
    replay_conn = sqlite3.connect(out)
    replay_conn.row_factory = sqlite3.Row
    completed = False
    try:
        replay_conn.execute("pragma foreign_keys = on")
        replay_conn.execute("begin immediate")
        restore_snapshot(replay_conn, snapshot)
        for event in events:
            apply_event_after(replay_conn, event)
            columns = list(event.keys())
            replay_conn.execute(
                f"insert into events ({','.join(columns)}) values ({','.join('?' for _ in columns)})",
                [event[column] for column in columns],
            )
        replay_conn.commit()
        completed = True
    except Exception:
        replay_conn.rollback()
        raise
    finally:
        replay_conn.close()
        if not completed:
            out.unlink(missing_ok=True)

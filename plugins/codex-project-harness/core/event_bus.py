"""Append-only local audit events.

Schema 30 stores compact entity summaries. Events are an audit trail, not a
database recovery or replay source; recovery uses verified SQLite backups.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from harness_lib import now_iso
from . import SCHEMA_VERSION


SUMMARY_FIELDS = (
    "id",
    "uid",
    "project_id",
    "cycle_id",
    "current_cycle_id",
    "candidate_sha",
    "target_id",
    "task_id",
    "acceptance_id",
    "qualification_id",
    "execution_id",
    "schema_version",
    "runtime_version",
    "kind",
    "gateable",
    "stack_profile",
    "requires_sandbox",
    "requires_no_network",
    "result_format",
    "phase",
    "status",
    "scope_status",
    "priority",
    "risk",
    "severity",
    "surface",
    "result",
    "validation_status",
    "gate_status",
    "review_status",
    "reviewed_revision",
    "semantic_status",
    "policy_status",
    "sandbox_status",
    "runner",
    "no_network",
    "executed_count",
    "revision",
    "project_revision",
    "accepted_by",
    "acceptance_reason",
    "acceptance_scope",
    "accepted_revision",
    "expires_at",
    "waived_by",
    "waiver_reason",
    "waiver_scope",
    "waived_revision",
    "waiver_expires_at",
    "digest",
    "stdout_sha256",
    "artifact_path",
    "decision",
    "reason",
    "summary",
)
SUMMARY_STRING_LIMIT = 512


def payload(**values: object) -> str:
    return json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _event_columns(conn: sqlite3.Connection) -> set[str]:
    return {str(row[1]) for row in conn.execute("pragma table_info(events)")}


def compact_summary(value: dict[str, Any] | None) -> dict[str, Any]:
    """Keep audit rows bounded and exclude arbitrary runtime payloads."""

    if not value:
        return {}
    summary: dict[str, Any] = {}
    for field in SUMMARY_FIELDS:
        if field not in value:
            continue
        item = value[field]
        if isinstance(item, str):
            summary[field] = item[:SUMMARY_STRING_LIMIT]
        elif item is None or isinstance(item, (bool, int, float)):
            summary[field] = item
    return summary


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
    after_summary = dict(after or {})
    if extra:
        after_summary.update(extra)
    conn.execute(
        """
        insert into events
        (id, schema_version, event_type, entity_type, entity_id, actor, command,
         before_json, after_json, correlation_id, created_at)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            schema_version,
            event_type,
            entity_type,
            entity_id,
            actor or "root-controller",
            command or event_type,
            payload(**compact_summary(before)),
            payload(**compact_summary(after_summary)),
            correlation_id,
            now_iso(),
        ),
    )


def validate_audit_events(
    conn: sqlite3.Connection,
    *,
    expected_schema_version: int = SCHEMA_VERSION,
) -> list[str]:
    """Validate the compact audit contract without replay semantics."""

    issues: list[str] = []
    columns = _event_columns(conn)
    required_columns = {
        "sequence",
        "id",
        "schema_version",
        "event_type",
        "entity_type",
        "entity_id",
        "actor",
        "command",
        "before_json",
        "after_json",
        "correlation_id",
        "created_at",
    }
    missing = sorted(required_columns - columns)
    if missing:
        return [f"audit event schema missing columns: {', '.join(missing)}"]
    for row in conn.execute(
        """
        select sequence, schema_version, event_type, entity_type, entity_id,
               actor, command, before_json, after_json, correlation_id, created_at
        from events order by sequence
        """
    ):
        sequence = int(row["sequence"])
        if int(row["schema_version"]) != expected_schema_version:
            issues.append(
                "event "
                f"{sequence} schema_version={row['schema_version']} "
                f"expected={expected_schema_version}"
            )
        for field in (
            "event_type",
            "entity_type",
            "entity_id",
            "actor",
            "command",
            "correlation_id",
            "created_at",
        ):
            if not str(row[field] or "").strip():
                issues.append(f"event {sequence} missing {field}")
        for field in ("before_json", "after_json"):
            try:
                value = json.loads(row[field])
            except (TypeError, json.JSONDecodeError) as exc:
                detail = exc.msg if isinstance(exc, json.JSONDecodeError) else str(exc)
                issues.append(f"event {sequence} invalid {field}: {detail}")
                continue
            if not isinstance(value, dict):
                issues.append(f"event {sequence} {field} must be an object")
    return issues

"""Versioned, local-only outcome metric definitions and calculations."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .execution import target_definition_digest


OUTCOME_METRICS_VERSION = "kafa-outcome-metrics-v1"
OUTCOME_METRIC_VERSION = "kafa-outcome-metric-v1"
OUTCOME_EVIDENCE_MODE = "field"
OUTCOME_METRIC_IDS = (
    "false_green_prevented_count",
    "escaped_defect_count",
    "rework_rate_per_delivery",
    "migration_recovery_success_rate",
    "time_to_verified_delivery_seconds",
    "qualification_coverage_rate",
)
RECOVERY_ATTEMPT_STATUSES = frozenset(
    {"rolled-back", "rollback-incomplete", "recovery-required"}
)
VERIFIED_DELIVERY_STATUSES = frozenset(
    {"delivered", "accepted-risk", "same-context-degraded"}
)


def _parse_timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def _format_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _snapshot_window(
    *,
    cycle_id: str,
    generated_at: str,
) -> dict[str, Any]:
    timestamp = _parse_timestamp(generated_at)
    canonical = _format_timestamp(timestamp)
    return {
        "kind": "current-cycle-snapshot",
        "cycle_id": cycle_id,
        "start_at": canonical,
        "end_at": canonical,
        "complete": canonical is not None,
    }


def _cycle_as_of_window(
    cycle: Mapping[str, Any],
    *,
    generated_at: str,
) -> dict[str, Any]:
    start = _parse_timestamp(cycle.get("started_at"))
    closed_text = str(cycle.get("closed_at") or "").strip()
    end = _parse_timestamp(closed_text or generated_at)
    complete = start is not None and end is not None and end >= start
    return {
        "kind": "current-cycle-field-window",
        "cycle_id": str(cycle["id"]),
        "start_at": _format_timestamp(start),
        "end_at": _format_timestamp(end),
        "complete": complete,
        "completion": "closed-cycle" if closed_text else "as-of-snapshot",
        "time_field": "observed_at",
        "boundary": "inclusive",
    }


def _within_window(value: object, window: Mapping[str, Any]) -> bool:
    timestamp = _parse_timestamp(value)
    start = _parse_timestamp(window.get("start_at"))
    end = _parse_timestamp(window.get("end_at"))
    return bool(timestamp and start and end and start <= timestamp <= end)


def _verified_delivery_rows(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
) -> list[sqlite3.Row]:
    placeholders = ", ".join("?" for _ in VERIFIED_DELIVERY_STATUSES)
    return list(
        conn.execute(
            f"""
            select id, decision_status, created_at from deliveries
            where cycle_id = ? and decision_status in ({placeholders})
            order by created_at, id
            """,
            (cycle_id, *sorted(VERIFIED_DELIVERY_STATUSES)),
        ).fetchall()
    )


def _metric(
    *,
    event_definition: str,
    unit: str,
    status: str,
    value: int | float | None,
    numerator_definition: str,
    numerator_value: int | float | None,
    denominator_definition: str,
    denominator_value: int | float | None,
    denominator_applicability: str,
    window: Mapping[str, Any],
    not_applicable_when: str,
    reason: str,
    fact_count: int | None = None,
) -> dict[str, Any]:
    numerator: dict[str, Any] = {
        "definition": numerator_definition,
        "value": numerator_value,
    }
    if fact_count is not None:
        numerator["fact_count"] = fact_count
    return {
        "metric_version": OUTCOME_METRIC_VERSION,
        "evidence_mode": OUTCOME_EVIDENCE_MODE,
        "event_definition": event_definition,
        "unit": unit,
        "status": status,
        "value": value,
        "numerator": numerator,
        "denominator": {
            "definition": denominator_definition,
            "value": denominator_value,
            "applicability": denominator_applicability,
        },
        "window": dict(window),
        "missing_data_semantics": "insufficient-data",
        "not_applicable_when": not_applicable_when,
        "reason": reason,
    }


def _observation_count_metric(
    observations: Sequence[Mapping[str, Any]],
    *,
    cycle_id: str,
    kind: str,
    event_definition: str,
    numerator_definition: str,
    not_applicable_when: str,
    window: Mapping[str, Any],
) -> dict[str, Any]:
    facts = [
        row
        for row in observations
        if str(row.get("kind", "")) == kind
        and _within_window(row.get("observed_at"), window)
    ]
    if not facts or not window["complete"]:
        return _metric(
            event_definition=event_definition,
            unit="count",
            status="insufficient-data",
            value=None,
            numerator_definition=numerator_definition,
            numerator_value=None,
            denominator_definition="not applicable to an observed event count",
            denominator_value=None,
            denominator_applicability="not-applicable",
            window=window,
            not_applicable_when=not_applicable_when,
            reason=f"no valid {kind} observation window for the current cycle",
            fact_count=len(facts),
        )
    value = sum(int(row["value"]) for row in facts)
    return _metric(
        event_definition=event_definition,
        unit="count",
        status="observed",
        value=value,
        numerator_definition=numerator_definition,
        numerator_value=value,
        denominator_definition="not applicable to an observed event count",
        denominator_value=None,
        denominator_applicability="not-applicable",
        window=window,
        not_applicable_when=not_applicable_when,
        reason="",
        fact_count=len(facts),
    )


def _escaped_defect_metric(
    conn: sqlite3.Connection,
    observations: Sequence[Mapping[str, Any]],
    *,
    cycle_id: str,
    generated_at: str,
) -> dict[str, Any]:
    deliveries = _verified_delivery_rows(conn, cycle_id=cycle_id)
    delivery_times = [_parse_timestamp(row["created_at"]) for row in deliveries]
    valid_delivery_times = [value for value in delivery_times if value is not None]
    start = min(valid_delivery_times) if valid_delivery_times else None
    end = _parse_timestamp(generated_at)
    window_complete = (
        bool(deliveries)
        and len(valid_delivery_times) == len(deliveries)
        and start is not None
        and end is not None
        and end >= start
    )
    window = {
        "kind": "post-delivery-as-of-snapshot",
        "cycle_id": cycle_id,
        "start_at": _format_timestamp(start),
        "end_at": _format_timestamp(end),
        "complete": window_complete,
        "completion": "as-of-snapshot",
        "time_field": "observed_at",
        "boundary": "inclusive",
    }
    facts = [
        row
        for row in observations
        if str(row.get("kind", "")) == "escaped-defect"
        and _within_window(row.get("observed_at"), window)
    ]
    if not facts or not window_complete:
        if not deliveries:
            reason = "the current cycle has no verified delivery for a post-delivery window"
        elif not window_complete:
            reason = "the post-delivery observation window is invalid"
        else:
            reason = "no escaped-defect observation exists in the post-delivery window"
        return _metric(
            event_definition=(
                "a bounded local observation records a defect discovered after verified delivery"
            ),
            unit="count",
            status="insufficient-data",
            value=None,
            numerator_definition="sum of post-delivery escaped-defect observation values",
            numerator_value=None,
            denominator_definition="not applicable to an observed event count",
            denominator_value=None,
            denominator_applicability="not-applicable",
            window=window,
            not_applicable_when=(
                "there is no verified delivery or no bounded post-delivery observation"
            ),
            reason=reason,
            fact_count=len(facts),
        )
    value = sum(int(row["value"]) for row in facts)
    return _metric(
        event_definition=(
            "a bounded local observation records a defect discovered after verified delivery"
        ),
        unit="count",
        status="observed",
        value=value,
        numerator_definition="sum of post-delivery escaped-defect observation values",
        numerator_value=value,
        denominator_definition="not applicable to an observed event count",
        denominator_value=None,
        denominator_applicability="not-applicable",
        window=window,
        not_applicable_when=(
            "there is no verified delivery or no bounded post-delivery observation"
        ),
        reason="",
        fact_count=len(facts),
    )


def _rework_rate_metric(
    conn: sqlite3.Connection,
    observations: Sequence[Mapping[str, Any]],
    *,
    cycle_id: str,
    window: Mapping[str, Any],
) -> dict[str, Any]:
    facts = [
        row
        for row in observations
        if str(row.get("kind", "")) == "rework"
        and _within_window(row.get("observed_at"), window)
    ]
    numerator = sum(int(row["value"]) for row in facts) if facts else None
    delivery_rows = _verified_delivery_rows(conn, cycle_id=cycle_id)
    valid_delivery_rows = [
        row for row in delivery_rows if _within_window(row["created_at"], window)
    ]
    all_delivery_timestamps_valid = all(
        _parse_timestamp(row["created_at"]) is not None for row in delivery_rows
    )
    denominator = len(valid_delivery_rows)
    available = (
        numerator is not None
        and denominator > 0
        and bool(window["complete"])
        and all_delivery_timestamps_valid
    )
    value = round(numerator / denominator, 6) if available else None
    if numerator is None:
        reason = "no valid rework observation window for the current cycle"
    elif denominator == 0 or not all_delivery_timestamps_valid:
        reason = "the current cycle has no verified delivery denominator"
    elif not window["complete"]:
        reason = "the rework observation window is incomplete"
    else:
        reason = ""
    return _metric(
        event_definition=(
            "sum bounded rework units explicitly recorded against the current cycle"
        ),
        unit="rework-units-per-delivery",
        status="computed" if available else "insufficient-data",
        value=value,
        numerator_definition="sum of current-cycle rework observation values",
        numerator_value=numerator,
        denominator_definition="verified delivery records in the current cycle",
        denominator_value=denominator,
        denominator_applicability="required",
        window=window,
        not_applicable_when=(
            "no rework observation exists or the cycle has no verified delivery record"
        ),
        reason=reason,
        fact_count=len(facts),
    )


def _recovery_success_metric(
    conn: sqlite3.Connection,
    *,
    generated_at: str,
) -> dict[str, Any]:
    placeholders = ", ".join("?" for _ in RECOVERY_ATTEMPT_STATUSES)
    rows = conn.execute(
        f"""
        select status, applied_at from migrations
        where status in ({placeholders})
        order by applied_at, id
        """,
        tuple(sorted(RECOVERY_ATTEMPT_STATUSES)),
    ).fetchall()
    denominator = len(rows)
    numerator = sum(1 for row in rows if str(row["status"]) == "rolled-back")
    report_end = _parse_timestamp(generated_at)
    applied_times = [_parse_timestamp(row["applied_at"]) for row in rows]
    valid_applied_times = [value for value in applied_times if value is not None]
    if report_end is None:
        report_end = max(valid_applied_times) if valid_applied_times else None
    complete = (
        bool(rows)
        and len(valid_applied_times) == len(rows)
        and report_end is not None
        and all(value <= report_end for value in valid_applied_times)
    )
    window = {
        "kind": "project-migration-recovery-history",
        "cycle_id": None,
        "start_at": _format_timestamp(min(valid_applied_times))
        if valid_applied_times
        else None,
        "end_at": _format_timestamp(report_end),
        "complete": complete,
        "completion": "as-of-snapshot",
        "time_field": "applied_at",
        "boundary": "inclusive",
    }
    available = denominator > 0 and bool(window["complete"])
    value = round(numerator / denominator, 6) if available else None
    reason = "" if available else "no complete migration recovery-attempt window exists"
    return _metric(
        event_definition=(
            "a recovery attempt is a migration ending rolled-back, rollback-incomplete, "
            "or recovery-required; only rolled-back is successful"
        ),
        unit="ratio",
        status="computed" if available else "insufficient-data",
        value=value,
        numerator_definition="migration recovery attempts with status rolled-back",
        numerator_value=numerator if denominator else None,
        denominator_definition=(
            "migration recovery attempts ending rolled-back, rollback-incomplete, "
            "or recovery-required"
        ),
        denominator_value=denominator,
        denominator_applicability="required",
        window=window,
        not_applicable_when="the project has no completed recovery attempt facts",
        reason=reason,
        fact_count=denominator,
    )


def _time_to_delivery_metric(
    conn: sqlite3.Connection,
    cycle: Mapping[str, Any],
    *,
    generated_at: str,
) -> dict[str, Any]:
    cycle_id = str(cycle["id"])
    delivery_rows = _verified_delivery_rows(conn, cycle_id=cycle_id)
    start = _parse_timestamp(cycle.get("started_at"))
    delivery_times = [_parse_timestamp(row["created_at"]) for row in delivery_rows]
    valid_delivery_times = [value for value in delivery_times if value is not None]
    end = min(valid_delivery_times) if valid_delivery_times else None
    all_delivery_times_valid = len(valid_delivery_times) == len(delivery_times)
    elapsed = int((end - start).total_seconds()) if start and end else None
    report_window = _cycle_as_of_window(cycle, generated_at=generated_at)
    report_end = _parse_timestamp(report_window.get("end_at"))
    available = (
        elapsed is not None
        and elapsed >= 0
        and bool(delivery_rows)
        and all_delivery_times_valid
        and report_end is not None
        and end is not None
        and end <= report_end
    )
    window = {
        "kind": "current-cycle-to-first-verified-delivery",
        "cycle_id": cycle_id,
        "start_at": _format_timestamp(start),
        "end_at": _format_timestamp(end),
        "complete": available,
    }
    if not delivery_rows:
        reason = "the current cycle has no verified delivery record"
    elif not all_delivery_times_valid or start is None or end is None:
        reason = "cycle or delivery timestamps do not form a complete valid window"
    elif elapsed is not None and elapsed < 0:
        reason = "the earliest delivery predates the cycle start"
    elif end is not None and report_end is not None and end > report_end:
        reason = "the earliest delivery is outside the report observation window"
    else:
        reason = ""
    return _metric(
        event_definition=(
            "elapsed persisted time from current cycle start to its earliest verified delivery"
        ),
        unit="seconds",
        status="computed" if available else "insufficient-data",
        value=elapsed if available else None,
        numerator_definition="non-negative seconds from cycle start to earliest delivery",
        numerator_value=elapsed if available else None,
        denominator_definition="one completed current-cycle verified-delivery interval",
        denominator_value=1 if available else 0,
        denominator_applicability="required",
        window=window,
        not_applicable_when=(
            "the cycle has no delivery or its persisted timestamps do not form a non-negative interval"
        ),
        reason=reason,
        fact_count=len(delivery_rows),
    )


def _qualification_coverage_metric(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    generated_at: str,
) -> dict[str, Any]:
    acceptances = conn.execute(
        "select id, revision from acceptance where cycle_id = ? and status = 'active' order by id",
        (cycle_id,),
    ).fetchall()
    total = len(acceptances)
    covered = 0
    for acceptance in acceptances:
        qualification_rows = conn.execute(
            """
            select q.* from acceptance_target_qualifications q
            where q.cycle_id = ? and q.acceptance_id = ?
            order by q.target_id, q.created_at desc, q.rowid desc
            """,
            (cycle_id, acceptance["id"]),
        ).fetchall()
        latest_by_target: dict[str, sqlite3.Row] = {}
        for qualification in qualification_rows:
            latest_by_target.setdefault(str(qualification["target_id"]), qualification)
        acceptance_is_covered = False
        for target_id, qualification in latest_by_target.items():
            target = conn.execute(
                "select * from test_targets where id = ?",
                (target_id,),
            ).fetchone()
            if target is None:
                continue
            if (
                int(qualification["acceptance_revision"])
                == int(acceptance["revision"])
                and str(qualification["target_definition_sha256"])
                == target_definition_digest(dict(target))
            ):
                acceptance_is_covered = True
                break
        if acceptance_is_covered:
            covered += 1
    window = _snapshot_window(cycle_id=cycle_id, generated_at=generated_at)
    available = total > 0 and bool(window["complete"])
    value = round(covered / total, 6) if available else None
    reason = "" if available else "the current cycle has no active acceptance denominator"
    return _metric(
        event_definition=(
            "an active acceptance is covered only when a newest acceptance-target "
            "qualification matches its current revision and the live target digest"
        ),
        unit="ratio",
        status="computed" if available else "insufficient-data",
        value=value,
        numerator_definition="active current-cycle acceptances with a current qualification",
        numerator_value=covered if total else None,
        denominator_definition="all active acceptances in the current cycle",
        denominator_value=total,
        denominator_applicability="required",
        window=window,
        not_applicable_when="the current cycle has no active acceptances",
        reason=reason,
        fact_count=total,
    )


def build_outcome_metrics(
    conn: sqlite3.Connection,
    *,
    cycle: Mapping[str, Any],
    observations: Sequence[Mapping[str, Any]],
    generated_at: str,
) -> dict[str, dict[str, Any]]:
    """Return the exact v1 field-evidence metric inventory."""

    cycle_id = str(cycle["id"])
    cycle_window = _cycle_as_of_window(cycle, generated_at=generated_at)
    metrics = {
        "false_green_prevented_count": _observation_count_metric(
            observations,
            cycle_id=cycle_id,
            kind="false-green-prevented",
            event_definition=(
                "a bounded local observation records a delivery attempt that the hardened "
                "kernel prevented from becoming a false green"
            ),
            numerator_definition="sum of current-cycle false-green-prevented values",
            not_applicable_when="no bounded false-green-prevented observation was recorded",
            window=cycle_window,
        ),
        "escaped_defect_count": _escaped_defect_metric(
            conn,
            observations,
            cycle_id=cycle_id,
            generated_at=generated_at,
        ),
        "rework_rate_per_delivery": _rework_rate_metric(
            conn,
            observations,
            cycle_id=cycle_id,
            window=cycle_window,
        ),
        "migration_recovery_success_rate": _recovery_success_metric(
            conn,
            generated_at=generated_at,
        ),
        "time_to_verified_delivery_seconds": _time_to_delivery_metric(
            conn,
            cycle,
            generated_at=generated_at,
        ),
        "qualification_coverage_rate": _qualification_coverage_metric(
            conn,
            cycle_id=cycle_id,
            generated_at=generated_at,
        ),
    }
    if tuple(metrics) != OUTCOME_METRIC_IDS:
        raise RuntimeError("outcome metric inventory drift")
    return metrics

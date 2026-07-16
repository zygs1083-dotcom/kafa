"""Honest local trust policy for delivery decisions.

Context identifiers are self-reported audit metadata.  They can demonstrate that
the recorded producer and reviewer contexts differ, but they are never treated as
cryptographic identities or external receipts.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from harness_lib import git_dirty, git_head_sha, now_iso

from .cycle_ledger import (
    baseline_issues,
    current_candidate_sha,
    current_cycle_row,
    traceability_issues,
)
from .execution import command_matches_template
from .project_fs import ProjectFS, ProjectPathSafetyError


HIGH_RISK_LEVELS = frozenset({"high", "critical"})
REVIEW_STATUSES = frozenset({"reviewed-local", "same-context-degraded"})


@dataclass(frozen=True)
class LocalTrustDecision:
    """A local trust classification and its delivery consequence."""

    status: str
    trust_level: str
    delivery_allowed: bool
    reasons: tuple[str, ...]


def _sqlite_integer(value: object) -> int | None:
    return value if type(value) is int else None


def _positive_integer(value: object) -> int | None:
    integer = _sqlite_integer(value)
    if integer is None or integer <= 0:
        return None
    return integer


def _sqlite_flag(value: object) -> int | None:
    integer = _sqlite_integer(value)
    return integer if integer in {0, 1} else None


def _timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _risk_key(acceptance: Mapping[str, object]) -> str:
    for field in ("risk_id", "failure_mode_id", "id"):
        value = str(acceptance.get(field) or "").strip()
        if value:
            return value
    return ""


def _acceptance_issues(
    *,
    risk_levels: frozenset[str],
    risk_acceptances: Sequence[Mapping[str, object]],
    required_risk_ids: frozenset[str],
    now: datetime,
    current_revision: int | None,
) -> list[str]:
    issues: list[str] = []
    if required_risk_ids:
        requirements = [
            (risk_id, next((item for item in risk_acceptances if _risk_key(item) == risk_id), None))
            for risk_id in sorted(required_risk_ids)
        ]
    else:
        requirements = [
            (
                risk,
                next(
                    (
                        item
                        for item in risk_acceptances
                        if str(item.get("risk") or "").strip().lower() == risk
                    ),
                    None,
                ),
            )
            for risk in sorted(risk_levels & HIGH_RISK_LEVELS)
        ]

    for risk_key, acceptance in requirements:
        if acceptance is None:
            issues.append(f"explicit accepted/exempt risk record is missing: {risk_key}")
            continue
        status = str(acceptance.get("status") or "").strip().lower()
        if status not in {"accepted", "exempt"}:
            issues.append(f"risk record is not accepted or exempt: {risk_key} status={status or 'empty'}")
        required_fields = {
            "actor": acceptance.get("actor"),
            "reason": acceptance.get("reason"),
            "scope": acceptance.get("scope"),
            "revision": acceptance.get("revision"),
            "expires_at": acceptance.get("expires_at"),
        }
        missing = [
            field
            for field, value in required_fields.items()
            if value is None or (isinstance(value, str) and not value.strip())
        ]
        if missing:
            issues.append(f"risk acceptance is incomplete: {risk_key} missing={','.join(missing)}")
            continue
        revision = _positive_integer(required_fields["revision"])
        if revision is None:
            issues.append(f"risk acceptance revision is invalid: {risk_key}")
            continue
        if current_revision is not None and revision != current_revision:
            issues.append(
                f"risk acceptance revision is stale: {risk_key} accepted={revision} current={current_revision}"
            )
        expiry = _timestamp(required_fields["expires_at"])
        if expiry is None:
            issues.append(f"risk acceptance expiry is invalid: {risk_key}")
        elif expiry <= now:
            issues.append(f"risk acceptance expired: {risk_key} expires_at={required_fields['expires_at']}")
    return issues


def evaluate_local_trust(
    *,
    risk_levels: Iterable[str],
    structured_current_execution: bool,
    producer_context_id: str,
    reviewer_context_id: str,
    review_status: str,
    risk_acceptances: Sequence[Mapping[str, object]],
    now: str | datetime,
    required_risk_ids: Iterable[str] = (),
    current_revision: int | None = None,
) -> LocalTrustDecision:
    """Classify local evidence without accepting local tokens as trust anchors.

    High and critical work is never autonomously approved merely because context
    ids differ.  It remains ``human-review-required`` unless every such risk has
    a complete, unexpired accepted/exempt record; that path is explicitly
    procedural.  Low and medium work may use same-context review, but it is
    labelled as degraded.
    """

    levels = frozenset(str(level).strip().lower() for level in risk_levels if str(level).strip())
    high_risk = bool(levels & HIGH_RISK_LEVELS)
    producer = producer_context_id.strip()
    reviewer = reviewer_context_id.strip()
    recorded_review_status = review_status if isinstance(review_status, str) else ""
    distinct_contexts = bool(producer and reviewer and producer != reviewer)
    current_revision_value = _positive_integer(current_revision)
    observed_now = _timestamp(now)
    if observed_now is None:
        return LocalTrustDecision(
            status="human-review-required",
            trust_level="human-review-required",
            delivery_allowed=False,
            reasons=("delivery trust evaluation time is invalid",),
        )

    reasons: list[str] = []
    if not structured_current_execution:
        reasons.append("controller-verified structured execution for the current candidate is required")

    if high_risk:
        if recorded_review_status != "reviewed-local":
            reasons.append(
                "high/critical delivery requires review_status=reviewed-local; "
                f"actual={recorded_review_status or 'empty'}"
            )
        if not distinct_contexts:
            reasons.append(
                "high/critical delivery requires distinct reviewer and producer context metadata"
            )
        if current_revision_value is None:
            reasons.append(
                "current project revision is required for high/critical risk acceptance"
            )
        reasons.extend(
            _acceptance_issues(
                risk_levels=levels,
                risk_acceptances=risk_acceptances,
                required_risk_ids=frozenset(
                    str(risk_id).strip() for risk_id in required_risk_ids if str(risk_id).strip()
                ),
                now=observed_now,
                current_revision=current_revision_value,
            )
        )
        if reasons:
            return LocalTrustDecision(
                status="human-review-required",
                trust_level="human-review-required",
                delivery_allowed=False,
                reasons=tuple(reasons),
            )
        return LocalTrustDecision(
            status="accepted-risk",
            trust_level="procedural",
            delivery_allowed=True,
            reasons=(
                "quality gate review_status is reviewed-local with distinct context metadata",
                "all high/critical risks have complete unexpired accepted/exempt records",
                "context identifiers are self-reported audit metadata, not cryptographic proof",
            ),
        )

    if reasons:
        return LocalTrustDecision(
            status="human-review-required",
            trust_level="human-review-required",
            delivery_allowed=False,
            reasons=tuple(reasons),
        )
    if recorded_review_status == "same-context-degraded":
        return LocalTrustDecision(
            status="same-context-degraded",
            trust_level="same-context-degraded",
            delivery_allowed=True,
            reasons=("degraded review is permitted only for low/medium risk",),
        )
    if recorded_review_status == "reviewed-local":
        if distinct_contexts:
            return LocalTrustDecision(
                status="reviewed-local",
                trust_level="reviewed-local",
                delivery_allowed=True,
                reasons=("producer and reviewer context metadata are distinct",),
            )
        return LocalTrustDecision(
            status="human-review-required",
            trust_level="human-review-required",
            delivery_allowed=False,
            reasons=(
                "review_status=reviewed-local requires distinct non-empty producer and reviewer context metadata",
            ),
        )
    if recorded_review_status == "controller-verified":
        return LocalTrustDecision(
            status="controller-verified",
            trust_level="controller-verified",
            delivery_allowed=True,
            reasons=("current-candidate execution was run by the controller",),
        )
    return LocalTrustDecision(
        status="human-review-required",
        trust_level="human-review-required",
        delivery_allowed=False,
        reasons=(
            "review_status must be exactly reviewed-local, same-context-degraded, or controller-verified; "
            f"actual={recorded_review_status or 'empty'}",
        ),
    )


def _artifact_issues(root: Path, execution: sqlite3.Row) -> list[str]:
    relative = str(execution["artifact_path"] or "").strip()
    expected = str(execution["stdout_sha256"] or "").strip().lower()
    if not relative:
        return [f"execution artifact path is empty: {execution['id']}"]
    try:
        with ProjectFS.open(root) as project_fs:
            candidate = project_fs.relative_to_root(Path(relative))
            snapshot = project_fs._snapshot(
                candidate,
                allow_missing=True,
            )
            if not snapshot.exists:
                return [
                    f"execution artifact is unavailable: {execution['id']} path={relative}"
                ]
            data = project_fs.read_bytes(candidate)
    except ProjectPathSafetyError as exc:
        return [f"execution artifact path is unsafe: {execution['id']}: {exc}"]
    if not data:
        return [f"execution artifact is empty: {execution['id']} path={relative}"]
    actual = hashlib.sha256(data).hexdigest()
    if not expected or actual != expected:
        return [
            f"execution artifact digest mismatch: {execution['id']} stored={expected or 'empty'} actual={actual}"
        ]
    return []


def execution_issues(
    conn: sqlite3.Connection,
    root: Path,
    execution: sqlite3.Row,
    current_candidate: str,
    *,
    require_structured: bool = False,
) -> list[str]:
    """Validate one immutable schema 30 execution for delivery use."""

    label = f"execution {execution['id']}"
    issues: list[str] = []
    if str(execution["candidate_sha"] or "") != current_candidate:
        issues.append(
            f"{label} candidate is stale: execution={execution['candidate_sha']} current={current_candidate}"
        )
    target_id = str(execution["target_id"] or "").strip()
    target = (
        conn.execute("select * from test_targets where id = ?", (target_id,)).fetchone()
        if target_id
        else None
    )
    if target is None:
        issues.append(f"{label} has no registered test target")
    else:
        gateable = _sqlite_flag(target["gateable"])
        requires_sandbox = _sqlite_flag(target["requires_sandbox"])
        requires_no_network = _sqlite_flag(target["requires_no_network"])
        if gateable != 1:
            issues.append(
                f"{label} target gateable must be the exact SQLite integer 1: "
                f"{target_id} actual={target['gateable']!r} {target['gate_block_reason']}"
            )
        if requires_sandbox is None:
            issues.append(
                f"{label} target requires_sandbox is not an exact SQLite flag: "
                f"{target['requires_sandbox']!r}"
            )
        if requires_no_network is None:
            issues.append(
                f"{label} target requires_no_network is not an exact SQLite flag: "
                f"{target['requires_no_network']!r}"
            )
        if not command_matches_template(
            str(execution["command"] or ""),
            str(target["command_template"] or ""),
        ):
            issues.append(f"{label} command does not match target {target_id}")
        if str(execution["result_format"] or "") != str(target["result_format"] or "regex"):
            issues.append(f"{label} result format does not match target {target_id}")
        if requires_sandbox == 1 and str(execution["sandbox_status"] or "") != "available":
            issues.append(f"{label} target requires an available sandbox")
        if requires_no_network == 1 and (
            str(execution["sandbox_status"] or "") != "available"
            or _sqlite_flag(execution["no_network"]) != 1
        ):
            issues.append(f"{label} target requires an available no-network sandbox")
    if _sqlite_flag(execution["no_network"]) is None:
        issues.append(
            f"{label} no_network is not an exact SQLite flag: {execution['no_network']!r}"
        )
    if _sqlite_integer(execution["exit_code"]) != 0:
        issues.append(
            f"{label} exit_code is not the exact SQLite integer zero: "
            f"{execution['exit_code']!r}"
        )
    if _positive_integer(execution["executed_count"]) is None:
        issues.append(
            f"{label} executed_count is not a positive SQLite integer: "
            f"{execution['executed_count']!r}"
        )
    if str(execution["semantic_status"] or "") != "pass":
        issues.append(f"{label} semantic_status={execution['semantic_status'] or 'empty'}")
    if str(execution["policy_status"] or "") not in {
        "allowed",
        "pass",
        "controller-verified",
    }:
        issues.append(f"{label} policy_status={execution['policy_status'] or 'empty'}")
    if require_structured and str(execution["result_format"] or "") == "regex":
        issues.append(f"{label} high/critical coverage requires structured result")
    issues.extend(_artifact_issues(root, execution))
    return issues


def validation_execution_issues(
    conn: sqlite3.Connection,
    root: Path,
    validation: sqlite3.Row,
    current_candidate: str,
    *,
    require_structured: bool = False,
) -> list[str]:
    executions = conn.execute(
        """
        select e.* from validation_executions ve
        join executions e on e.id = ve.execution_id
        where ve.validation_id = ?
        order by e.created_at, e.id
        """,
        (validation["id"],),
    ).fetchall()
    if not executions:
        return [f"validation has no linked immutable execution: {validation['id']}"]
    issues: list[str] = []
    for execution in executions:
        if str(execution["cycle_id"] or "") != str(validation["cycle_id"] or ""):
            issues.append(
                f"execution cycle does not match validation: {execution['id']} -> {validation['id']}"
            )
        if str(execution["candidate_sha"] or "") != str(validation["candidate_sha"] or ""):
            issues.append(
                f"execution candidate does not match validation: {execution['id']} -> {validation['id']}"
            )
        issues.extend(
            execution_issues(
                conn,
                root,
                execution,
                current_candidate,
                require_structured=require_structured,
            )
        )
    return issues


def _finding_blocks(
    finding: sqlite3.Row,
    *,
    cycle_id: str,
    candidate: str,
    revision: int,
    is_expired: Callable[[str], bool],
) -> bool:
    if finding["status"] in {"resolved", "false-positive"}:
        return False
    if finding["status"] != "accepted":
        return True
    complete = all(
        str(finding[field] or "").strip()
        for field in (
            "waived_by",
            "waiver_reason",
            "waiver_scope",
            "waiver_expires_at",
        )
    )
    waived_revision = _positive_integer(finding["waived_revision"])
    if waived_revision is None:
        return True
    matches = (
        finding["cycle_id"] == cycle_id
        and finding["candidate_sha"] == candidate
        and waived_revision == revision
    )
    expiry = str(finding["waiver_expires_at"] or "")
    valid_expiry = _timestamp(expiry) is not None
    return not (
        complete
        and matches
        and valid_expiry
        and not is_expired(expiry)
    )


def _human_review_decision(reason: str) -> LocalTrustDecision:
    return LocalTrustDecision(
        status="human-review-required",
        trust_level="human-review-required",
        delivery_allowed=False,
        reasons=(reason,),
    )


def evaluate_schema30_delivery(
    conn: sqlite3.Connection,
    root: Path,
    *,
    is_expired: Callable[[str], bool],
    observed_at: str | None = None,
) -> tuple[list[str], LocalTrustDecision]:
    """Evaluate final local-only delivery facts in a schema 30 database."""

    issues: list[str] = []
    project = conn.execute("select * from project where id = 1").fetchone()
    if project is None:
        reason = "project is not initialized"
        return [reason], _human_review_decision(reason)
    try:
        cycle = current_cycle_row(conn)
    except Exception as exc:
        reason = str(exc)
        return [reason], _human_review_decision(reason)
    cycle_id = str(cycle["id"])
    candidate = current_candidate_sha(root)
    revision = _positive_integer(project["revision"])
    if revision is None:
        issues.append(
            f"project revision must be a positive SQLite integer: {project['revision']!r}"
        )
    if cycle["status"] not in {"active", "delivered"}:
        issues.append(
            f"current cycle is not active or delivered: {cycle_id} status={cycle['status']}"
        )

    if conn.execute(
        "select 1 from requirements where cycle_id = ? and status != 'cancelled' limit 1",
        (cycle_id,),
    ).fetchone():
        issues.extend(traceability_issues(conn))
        issues.extend(baseline_issues(conn))

    for stale in conn.execute(
        """
        select source_type, source_id, target_type, target_id, reason
        from invalidations
        where cycle_id = ? and resolved_at is null
        order by created_at, id
        """,
        (cycle_id,),
    ).fetchall():
        issues.append(
            f"stale runtime artifact: {stale['source_type']}:{stale['source_id']} -> "
            f"{stale['target_type']}:{stale['target_id']} reason={stale['reason']}"
        )

    for task in conn.execute(
        "select id, status from tasks where cycle_id = ? "
        "and status not in ('accepted', 'cancelled') order by id",
        (cycle_id,),
    ).fetchall():
        issues.append(f"task is not accepted: {task['id']} status={task['status']}")

    validations = conn.execute(
        """
        select * from validations
        where cycle_id = ? and candidate_sha = ? and validation_status = 'active'
        order by created_at, id
        """,
        (cycle_id, candidate),
    ).fetchall()
    if not validations:
        issues.append("delivery requires validation evidence for the current candidate")
    for validation in validations:
        if validation["result"] != "pass":
            issues.append(
                f"validation is not pass: {validation['surface']}={validation['result']}"
            )
        else:
            issues.extend(
                validation_execution_issues(
                    conn,
                    root,
                    validation,
                    candidate,
                )
            )

    for acceptance in conn.execute(
        "select id from acceptance where cycle_id = ? and status != 'cancelled' order by id",
        (cycle_id,),
    ).fetchall():
        candidates = conn.execute(
            """
            select * from validations
            where cycle_id = ? and candidate_sha = ? and acceptance_id = ?
              and validation_status = 'active' and result = 'pass'
            order by created_at desc, id desc
            """,
            (cycle_id, candidate, acceptance["id"]),
        ).fetchall()
        trusted = False
        candidate_issues: list[str] = []
        for validation in candidates:
            found = validation_execution_issues(conn, root, validation, candidate)
            if not found:
                trusted = True
                break
            candidate_issues.extend(found)
        if not trusted:
            suffix = f" ({'; '.join(candidate_issues)})" if candidate_issues else ""
            issues.append(
                f"acceptance has no passing immutable execution for current candidate: "
                f"{acceptance['id']}{suffix}"
            )

    risky_modes = conn.execute(
        """
        select id, risk, status, accepted_by, acceptance_reason, acceptance_scope,
               accepted_revision, expires_at
        from failure_modes
        where cycle_id = ? and risk in ('high', 'critical')
        order by id
        """,
        (cycle_id,),
    ).fetchall()
    risk_levels = {
        str(row[0])
        for row in conn.execute(
            "select risk from failure_modes where cycle_id = ? order by id",
            (cycle_id,),
        ).fetchall()
    }
    risk_acceptances: list[dict[str, object]] = []
    required_risk_ids = {str(row["id"]) for row in risky_modes}
    for failure_mode in risky_modes:
        if failure_mode["status"] in {"accepted", "exempt"}:
            risk_acceptances.append(
                {
                    "risk_id": failure_mode["id"],
                    "risk": failure_mode["risk"],
                    "status": failure_mode["status"],
                    "actor": failure_mode["accepted_by"],
                    "reason": failure_mode["acceptance_reason"],
                    "scope": failure_mode["acceptance_scope"],
                    "revision": failure_mode["accepted_revision"],
                    "expires_at": failure_mode["expires_at"],
                }
            )
            continue
        covered = conn.execute(
            """
            select v.* from validation_failure_modes vfm
            join validations v on v.id = vfm.validation_id
            where vfm.cycle_id = ? and vfm.failure_mode_id = ?
              and v.cycle_id = vfm.cycle_id and v.candidate_sha = ?
              and v.validation_status = 'active' and v.result = 'pass'
            order by v.created_at desc, v.id desc
            """,
            (cycle_id, failure_mode["id"], candidate),
        ).fetchall()
        coverage_issues: list[str] = []
        if not any(
            not (
                found := validation_execution_issues(
                    conn,
                    root,
                    validation,
                    candidate,
                    require_structured=True,
                )
            )
            for validation in covered
        ):
            for validation in covered:
                coverage_issues.extend(
                    validation_execution_issues(
                        conn,
                        root,
                        validation,
                        candidate,
                        require_structured=True,
                    )
                )
            suffix = f" ({'; '.join(coverage_issues)})" if coverage_issues else ""
            issues.append(
                f"{failure_mode['risk']} failure mode is not covered by a structured "
                f"current-candidate controller execution: {failure_mode['id']}{suffix}"
            )

    findings = conn.execute(
        """
        select * from findings
        where cycle_id = ? and candidate_sha = ? and severity in ('high', 'critical')
        order by id
        """,
        (cycle_id, candidate),
    ).fetchall()
    for finding in findings:
        if finding["status"] not in {"resolved", "false-positive"}:
            finding_risk_id = f"finding:{finding['id']}"
            risk_levels.add(str(finding["severity"]))
            required_risk_ids.add(finding_risk_id)
            if finding["status"] == "accepted":
                risk_acceptances.append(
                    {
                        "risk_id": finding_risk_id,
                        "risk": finding["severity"],
                        "status": finding["status"],
                        "actor": finding["waived_by"],
                        "reason": finding["waiver_reason"],
                        "scope": finding["waiver_scope"],
                        "revision": finding["waived_revision"],
                        "expires_at": finding["waiver_expires_at"],
                    }
                )
        if _finding_blocks(
            finding,
            cycle_id=cycle_id,
            candidate=candidate,
            revision=revision or 0,
            is_expired=is_expired,
        ):
            issues.append(
                f"{finding['severity']} finding blocks delivery: {finding['id']} status={finding['status']}"
            )

    latest_gate = conn.execute(
        """
        select * from quality_gates
        where cycle_id = ? and candidate_sha = ? and gate_status = 'active'
        order by sequence desc limit 1
        """,
        (cycle_id, candidate),
    ).fetchone()
    producer_context_id = ""
    reviewer_context_id = ""
    review_status = ""
    if latest_gate is None:
        issues.append("delivery requires a quality gate record for current candidate")
    else:
        producer_context_id = str(latest_gate["producer_context_id"] or "").strip()
        reviewer_context_id = str(latest_gate["reviewer_context_id"] or "").strip()
        raw_review_status = latest_gate["review_status"]
        review_status = raw_review_status if isinstance(raw_review_status, str) else ""
        if review_status not in REVIEW_STATUSES:
            issues.append(
                "quality gate review_status must be exactly reviewed-local or "
                f"same-context-degraded: actual={review_status or 'empty'}"
            )
        if latest_gate["result"] != "pass":
            issues.append(
                f"latest quality gate is not pass: {latest_gate['gate']}={latest_gate['result']}"
            )
        if latest_gate["blocking_findings"]:
            issues.append(
                f"latest quality gate has blocking findings: {latest_gate['blocking_findings']}"
            )
        gate_revision = _positive_integer(latest_gate["reviewed_revision"])
        if gate_revision is None:
            issues.append(
                "latest quality gate reviewed_revision must be a positive SQLite integer: "
                f"{latest_gate['reviewed_revision']!r}"
            )
        elif revision is None or gate_revision != revision:
            issues.append(
                f"latest quality gate revision is stale: gate={latest_gate['reviewed_revision']} current={revision}"
            )
        if latest_gate["review_status"] == "reviewed-local" and (
            not producer_context_id
            or not reviewer_context_id
            or producer_context_id == reviewer_context_id
        ):
            issues.append(
                "reviewed-local quality gate requires distinct producer and reviewer context metadata"
            )
        if git_head_sha(root) and git_dirty(root):
            issues.append("git worktree is dirty after quality gate")

    current_execution = any(
        not validation_execution_issues(
            conn,
            root,
            validation,
            candidate,
            require_structured=bool(risk_levels & HIGH_RISK_LEVELS),
        )
        for validation in validations
        if validation["result"] == "pass"
    )
    trust = evaluate_local_trust(
        risk_levels=risk_levels,
        structured_current_execution=current_execution,
        producer_context_id=producer_context_id,
        reviewer_context_id=reviewer_context_id,
        review_status=review_status,
        risk_acceptances=risk_acceptances,
        required_risk_ids=required_risk_ids,
        current_revision=revision,
        now=observed_at or now_iso(),
    )
    if not trust.delivery_allowed:
        issues.append(f"{trust.status}: {'; '.join(trust.reasons)}")
    return issues, trust


def evaluate_schema30_delivery_readiness(
    conn: sqlite3.Connection,
    root: Path,
    *,
    is_expired: Callable[[str], bool],
    observed_at: str | None = None,
) -> list[str]:
    issues, _ = evaluate_schema30_delivery(
        conn,
        root,
        is_expired=is_expired,
        observed_at=observed_at,
    )
    return issues

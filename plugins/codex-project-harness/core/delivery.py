"""Honest local trust policy for delivery decisions.

Context identifiers are self-reported audit metadata.  They can demonstrate that
the recorded producer and reviewer contexts differ, but they are never treated as
cryptographic identities or external receipts.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal

from harness_lib import (
    SOURCE_ENVIRONMENT_ROOTS,
    git_dirty,
    git_head_sha,
    git_source_snapshot,
    isolated_git_environment,
    now_iso,
    source_path_excluded,
)

from .cycle_ledger import (
    baseline_issues,
    current_candidate_sha,
    current_cycle_row,
    latest_baseline,
    traceability_issues,
)
from .execution import (
    command_matches_template,
    latest_acceptance_target_qualification,
    recorded_execution_provenance_issues,
    target_definition_digest,
)
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


@dataclass(frozen=True, slots=True)
class DeliveryBlocker:
    """One stable, machine-readable reason delivery cannot progress."""

    code: str
    message: str
    entity_type: str
    entity_id: str

    def render(self) -> str:
        return f"[{self.code}] {self.message}"

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "message": self.message,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
        }


DeliveryEvaluationMode = Literal[
    "enter-readiness",
    "record-delivery",
    "delivered-consistency",
]


@dataclass(frozen=True, slots=True)
class DeliveryPrerequisiteReport:
    blockers: tuple[DeliveryBlocker, ...]
    trust: LocalTrustDecision
    cycle_id: str
    candidate_sha: str
    proven_acceptance_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HistoricalDeliveryReport:
    """One closed-cycle prerequisite and trust decision at delivery time."""

    blockers: tuple[DeliveryBlocker, ...]
    trust: LocalTrustDecision
    cycle_id: str
    candidate_sha: str


@dataclass(frozen=True, slots=True)
class DeliveryValidationFact:
    """One cycle-bound validation and its immutable execution relations."""

    id: str
    surface: str
    result: str
    acceptance_id: str
    qualification_id: str
    execution_ids: tuple[str, ...]
    eligibility_issues: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DeliveryFailureModeFact:
    """One cycle-bound failure mode and execution-backed coverage relations."""

    id: str
    risk: str
    status: str
    validation_ids: tuple[str, ...]
    accepted_by: str
    acceptance_reason: str
    acceptance_scope: str
    accepted_revision: int | None
    expires_at: str


@dataclass(frozen=True, slots=True)
class DeliveryFindingFact:
    """One candidate-bound reviewer finding."""

    id: str
    surface: str
    severity: str
    status: str
    waived_by: str
    waiver_reason: str
    waiver_scope: str
    waived_revision: int | None
    waiver_expires_at: str


@dataclass(frozen=True, slots=True)
class DeliveryGateFact:
    """The persisted gate reviewed for one delivered candidate."""

    id: str
    result: str
    review_status: str
    producer_context_id: str
    reviewer_context_id: str
    residual_risk: str
    reviewed_revision: int
    qualification_ids: tuple[str, ...]
    finding_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DeliveryNarrativeFacts:
    """Immutable, delivery-ID-bound facts used by the human projection."""

    delivery_id: str
    cycle_id: str
    candidate_sha: str
    recorded_at: str
    cycle_status: str
    cycle_phase: str
    base_ref: str
    decision_status: str
    trust_status: str
    requirement_ids: tuple[str, ...]
    acceptance_ids: tuple[str, ...]
    task_ids: tuple[str, ...]
    qualification_ids: tuple[str, ...]
    target_ids: tuple[str, ...]
    execution_ids: tuple[str, ...]
    validation_ids: tuple[str, ...]
    ineligible_validation_ids: tuple[str, ...]
    judgment_validation_ids: tuple[str, ...]
    failure_mode_ids: tuple[str, ...]
    finding_ids: tuple[str, ...]
    gate_ids: tuple[str, ...]
    requirement_acceptance_links: tuple[tuple[str, str], ...]
    task_acceptance_links: tuple[tuple[str, str], ...]
    qualification_links: tuple[tuple[str, str, str], ...]
    validation_facts: tuple[DeliveryValidationFact, ...]
    ineligible_validation_facts: tuple[DeliveryValidationFact, ...]
    judgment_validation_facts: tuple[DeliveryValidationFact, ...]
    failure_mode_facts: tuple[DeliveryFailureModeFact, ...]
    finding_facts: tuple[DeliveryFindingFact, ...]
    gate: DeliveryGateFact | None
    changed_files_status: str
    changed_files: tuple[str, ...]


_BLOCKER_ORDER = {
    code: index
    for index, code in enumerate(
        (
            "candidate-snapshot-changed",
            "requirement-missing",
            "acceptance-missing",
            "requirement-acceptance-link-missing",
            "acceptance-orphaned",
            "baseline-missing",
            "baseline-stale",
            "scope-unconfirmed",
            "accepted-task-missing",
            "qualification-missing",
            "qualification-stale",
            "qualification-unreviewed",
            "current-validation-missing",
            "current-execution-missing",
            "delivery-acceptance-set-mismatch",
            "delivery-decision-trust-mismatch",
            "medium-failure-mode-uncovered",
            "medium-finding-open",
            "risk-acceptance-invalid",
            "degraded-residual-risk-missing",
            "quality-gate-invalid",
            "quality-gate-missing",
            "phase-not-ready",
            "cycle-not-active",
            "delivery-row-missing",
            "delivery-row-count-invalid",
            "delivered-candidate-inconsistent",
            "delivered-phase-inconsistent",
            "delivered-cycle-not-closed",
            "historical-event-chain-invalid",
        )
    )
}


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
    residual_risk: str = "",
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
    medium_risk = "medium" in levels
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

    if (
        not high_risk
        and recorded_review_status == "same-context-degraded"
        and not str(residual_risk or "").strip()
    ):
        reasons.append(
            "same-context-degraded low/medium review requires explicit non-empty residual-risk text"
        )
    if not high_risk:
        if recorded_review_status == "reviewed-local" and not distinct_contexts:
            reasons.append(
                "review_status=reviewed-local requires distinct non-empty producer and reviewer context metadata"
            )
        elif recorded_review_status not in {
            "reviewed-local",
            "same-context-degraded",
            "controller-verified",
        }:
            reasons.append(
                "review_status must be exactly reviewed-local, same-context-degraded, or controller-verified; "
                f"actual={recorded_review_status or 'empty'}"
            )

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

    medium_acceptance_ids = frozenset(
        str(risk_id).strip()
        for risk_id in required_risk_ids
        if str(risk_id).strip()
    )
    if medium_risk and medium_acceptance_ids:
        if current_revision_value is None:
            reasons.append(
                "current project revision is required for medium risk acceptance"
            )
        reasons.extend(
            _acceptance_issues(
                risk_levels=levels,
                risk_acceptances=risk_acceptances,
                required_risk_ids=medium_acceptance_ids,
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
                "all remaining medium risks have complete current unexpired accepted/exempt records",
                "medium risk acceptance is procedural and does not waive delivery prerequisites",
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
    """Validate one immutable execution under its persisted schema generation."""

    label = f"execution {execution['id']}"
    issues: list[str] = []
    schema_row = conn.execute(
        "select schema_version from project where id = 1"
    ).fetchone()
    schema_version = (
        _sqlite_integer(schema_row[0])
        if schema_row is not None
        else None
    )
    requires_schema31_provenance = (
        schema_version is None or schema_version >= 31
    )
    execution_columns = set(execution.keys())
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
        if requires_schema31_provenance:
            if "target_definition_sha256" not in execution_columns:
                issues.append(
                    f"{label} target_definition_sha256 column is missing"
                )
            elif str(
                execution["target_definition_sha256"] or ""
            ) != target_definition_digest(dict(target)):
                issues.append(
                    f"{label} target_definition_sha256 does not match target {target_id}"
                )
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
        issues.append(
            f"{label} medium/high/critical coverage requires structured result"
        )
    if requires_schema31_provenance:
        issues.extend(
            f"{label} {issue}"
            for issue in recorded_execution_provenance_issues(execution)
        )
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


def qualified_validation_execution_issues(
    conn: sqlite3.Connection,
    root: Path,
    validation: sqlite3.Row,
    qualification: sqlite3.Row,
    current_candidate: str,
    *,
    require_structured: bool = False,
) -> list[str]:
    """Validate the complete immutable qualification-to-execution join."""

    issues: list[str] = []
    qualification_id = str(qualification["id"])
    acceptance_id = str(qualification["acceptance_id"])
    target_id = str(qualification["target_id"])
    latest = latest_acceptance_target_qualification(
        conn,
        cycle_id=str(qualification["cycle_id"]),
        acceptance_id=acceptance_id,
        target_id=target_id,
    )
    if latest is None or str(latest["id"]) != qualification_id:
        issues.append(
            f"qualification {qualification_id} is superseded by "
            f"{latest['id'] if latest else 'missing'}"
        )
    if str(validation["qualification_id"] or "") != qualification_id:
        issues.append(
            f"validation {validation['id']} does not reference qualification {qualification_id}"
        )
    if str(validation["acceptance_id"] or "") != acceptance_id:
        issues.append(
            f"validation {validation['id']} acceptance does not match qualification {qualification_id}"
        )
    if str(validation["cycle_id"] or "") != str(qualification["cycle_id"]):
        issues.append(
            f"validation {validation['id']} cycle does not match qualification {qualification_id}"
        )
    if str(validation["candidate_sha"] or "") != current_candidate:
        issues.append(
            f"validation {validation['id']} is not for the current candidate"
        )

    acceptance = conn.execute(
        "select * from acceptance where cycle_id = ? and id = ?",
        (qualification["cycle_id"], acceptance_id),
    ).fetchone()
    if acceptance is None or str(acceptance["status"]) != "active":
        issues.append(
            f"qualification {qualification_id} acceptance is missing or inactive: {acceptance_id}"
        )
    elif int(qualification["acceptance_revision"]) != int(acceptance["revision"]):
        issues.append(
            f"qualification {qualification_id} acceptance revision is stale: "
            f"qualified={qualification['acceptance_revision']} current={acceptance['revision']}"
        )

    target = conn.execute(
        "select * from test_targets where id = ?",
        (target_id,),
    ).fetchone()
    if target is None:
        issues.append(
            f"qualification {qualification_id} target is missing: {target_id}"
        )
    else:
        live_digest = target_definition_digest(dict(target))
        if live_digest != str(qualification["target_definition_sha256"]):
            issues.append(
                f"qualification {qualification_id} target definition is stale: {target_id}"
            )

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
        issues.append(
            f"validation has no linked immutable execution: {validation['id']}"
        )
    for execution in executions:
        if str(execution["target_id"] or "") != target_id:
            issues.append(
                f"execution {execution['id']} target does not match qualification {qualification_id}"
            )
        if "target_definition_sha256" not in execution.keys() or str(
            execution["target_definition_sha256"] or ""
        ) != str(qualification["target_definition_sha256"]):
            issues.append(
                f"execution {execution['id']} target digest does not match qualification {qualification_id}"
            )
    issues.extend(
        validation_execution_issues(
            conn,
            root,
            validation,
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


def _evaluate_local_delivery_policy(
    conn: sqlite3.Connection,
    root: Path,
    *,
    is_expired: Callable[[str], bool],
    observed_at: str | None = None,
    include_graph_issues: bool = True,
    cycle_override: sqlite3.Row | None = None,
    candidate_override: str | None = None,
    revision_override: int | None = None,
    historical: bool = False,
) -> tuple[list[str], LocalTrustDecision]:
    """Evaluate execution, risk, and review policy for the current local facts."""

    issues: list[str] = []
    project = conn.execute("select * from project where id = 1").fetchone()
    if project is None:
        reason = "project is not initialized"
        return [reason], _human_review_decision(reason)
    try:
        cycle = cycle_override or current_cycle_row(conn)
    except Exception as exc:
        reason = str(exc)
        return [reason], _human_review_decision(reason)
    cycle_id = str(cycle["id"])
    candidate = (
        candidate_override
        if candidate_override is not None
        else current_candidate_sha(root)
    )
    revision_source = (
        revision_override
        if historical or revision_override is not None
        else project["revision"]
    )
    revision = _positive_integer(revision_source)
    if revision is None:
        if historical:
            issues.append(
                "historical baseline confirmation project_revision must be a "
                f"positive SQLite integer: {revision_source!r}"
            )
        else:
            issues.append(
                f"project revision must be a positive SQLite integer: {project['revision']!r}"
            )
    if cycle["status"] not in {"active", "delivered"}:
        issues.append(
            f"current cycle is not active or delivered: {cycle_id} status={cycle['status']}"
        )

    if include_graph_issues and conn.execute(
        "select 1 from requirements where cycle_id = ? and status != 'cancelled' limit 1",
        (cycle_id,),
    ).fetchone():
        issues.extend(traceability_issues(conn, root=root))
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

    validations = conn.execute(
        """
        select * from validations
        where cycle_id = ? and candidate_sha = ? and validation_status = 'active'
        order by created_at, id
        """,
        (cycle_id, candidate),
    ).fetchall()
    validation_columns = {
        str(row[1]) for row in conn.execute("pragma table_info(validations)")
    }
    has_qualification_column = "qualification_id" in validation_columns
    delivery_validations = (
        [
            validation
            for validation in validations
            if str(validation["qualification_id"] or "").strip()
        ]
        if has_qualification_column
        else list(validations)
    )
    if include_graph_issues:
        for validation in delivery_validations:
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
        where cycle_id = ? and risk in ('medium', 'high', 'critical')
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
    required_risk_ids = {
        str(row["id"])
        for row in risky_modes
        if str(row["risk"]) in HIGH_RISK_LEVELS
        or str(row["status"]) in {"accepted", "exempt"}
    }
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
        covered = (
            conn.execute(
                """
                select v.* from validation_failure_modes vfm
                join validations v on v.id = vfm.validation_id
                join failure_mode_acceptance fma
                  on fma.cycle_id = vfm.cycle_id
                 and fma.failure_mode_id = vfm.failure_mode_id
                 and fma.acceptance_id = v.acceptance_id
                where vfm.cycle_id = ? and vfm.failure_mode_id = ?
                  and v.cycle_id = vfm.cycle_id and v.candidate_sha = ?
                  and v.validation_status = 'active' and v.result = 'pass'
                  and v.qualification_id is not null
                order by v.created_at desc, v.id desc
                """,
                (cycle_id, failure_mode["id"], candidate),
            ).fetchall()
            if has_qualification_column
            else []
        )
        coverage_issues: list[str] = []
        covered_by_qualified_execution = False
        for validation in covered:
            qualification = conn.execute(
                "select * from acceptance_target_qualifications where id = ?",
                (validation["qualification_id"],),
            ).fetchone()
            found = (
                [
                    f"validation qualification is missing: {validation['qualification_id']}"
                ]
                if qualification is None
                else qualified_validation_execution_issues(
                    conn,
                    root,
                    validation,
                    qualification,
                    candidate,
                    require_structured=True,
                )
            )
            if not found:
                covered_by_qualified_execution = True
                break
            coverage_issues.extend(found)
        if not covered_by_qualified_execution:
            suffix = f" ({'; '.join(coverage_issues)})" if coverage_issues else ""
            issues.append(
                f"{failure_mode['risk']} failure mode is not covered by a structured "
                f"current-candidate controller execution: {failure_mode['id']}{suffix}"
            )
            if str(failure_mode["risk"]) == "medium":
                required_risk_ids.add(str(failure_mode["id"]))

    findings = conn.execute(
        """
        select * from findings
        where cycle_id = ? and candidate_sha = ? and severity in ('medium', 'high', 'critical')
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
        gate_id = str(latest_gate["id"])
        producer_context_id = str(latest_gate["producer_context_id"] or "").strip()
        reviewer_context_id = str(latest_gate["reviewer_context_id"] or "").strip()
        raw_review_status = latest_gate["review_status"]
        review_status = raw_review_status if isinstance(raw_review_status, str) else ""
        if review_status not in REVIEW_STATUSES:
            issues.append(
                "quality gate review_status must be exactly reviewed-local or "
                f"same-context-degraded: actual={review_status or 'empty'} "
                f"gate_id={gate_id}"
            )
        if latest_gate["result"] != "pass":
            issues.append(
                "latest quality gate is not pass: "
                f"{latest_gate['gate']}={latest_gate['result']} gate_id={gate_id}"
            )
        if latest_gate["blocking_findings"]:
            issues.append(
                "latest quality gate has blocking findings: "
                f"{latest_gate['blocking_findings']} gate_id={gate_id}"
            )
        gate_revision = _positive_integer(latest_gate["reviewed_revision"])
        if gate_revision is None:
            issues.append(
                "latest quality gate reviewed_revision must be a positive SQLite integer: "
                f"{latest_gate['reviewed_revision']!r} gate_id={gate_id}"
            )
        elif revision is None or gate_revision != revision:
            issues.append(
                "latest quality gate revision is stale: "
                f"gate={latest_gate['reviewed_revision']} current={revision} "
                f"gate_id={gate_id}"
            )
        if latest_gate["review_status"] == "reviewed-local" and (
            not producer_context_id
            or not reviewer_context_id
            or producer_context_id == reviewer_context_id
        ):
            issues.append(
                "reviewed-local quality gate requires distinct producer and "
                f"reviewer context metadata gate_id={gate_id}"
            )
        if not historical and git_head_sha(root) and git_dirty(root):
            issues.append("git worktree is dirty after quality gate")

    current_execution = False
    for validation in delivery_validations:
        if validation["result"] != "pass":
            continue
        if has_qualification_column:
            qualification = conn.execute(
                "select * from acceptance_target_qualifications where id = ?",
                (validation["qualification_id"],),
            ).fetchone()
            eligible = qualification is not None and not qualified_validation_execution_issues(
                conn,
                root,
                validation,
                qualification,
                candidate,
                require_structured=bool(risk_levels & HIGH_RISK_LEVELS),
            )
        else:
            eligible = not validation_execution_issues(
                conn,
                root,
                validation,
                candidate,
                require_structured=bool(risk_levels & HIGH_RISK_LEVELS),
            )
        if eligible:
            current_execution = True
            break
    trust = evaluate_local_trust(
        risk_levels=risk_levels,
        structured_current_execution=current_execution,
        producer_context_id=producer_context_id,
        reviewer_context_id=reviewer_context_id,
        review_status=review_status,
        residual_risk=(
            str(latest_gate["residual_risk"] or "")
            if latest_gate is not None
            else ""
        ),
        risk_acceptances=risk_acceptances,
        required_risk_ids=required_risk_ids,
        current_revision=revision,
        now=observed_at or now_iso(),
    )
    if not trust.delivery_allowed:
        issues.append(f"{trust.status}: {'; '.join(trust.reasons)}")
    return issues, trust


def _blocker(
    code: str,
    message: str,
    entity_type: str,
    entity_id: object,
) -> DeliveryBlocker:
    return DeliveryBlocker(
        code=code,
        message=message,
        entity_type=entity_type,
        entity_id=str(entity_id),
    )


def _delivery_decision_trust_blocker(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    trust: LocalTrustDecision,
) -> DeliveryBlocker | None:
    """Require the persisted decision to match the canonical policy trust."""

    if not trust.delivery_allowed:
        return None
    deliveries = conn.execute(
        "select id, decision_status from deliveries where cycle_id = ? order by id",
        (cycle_id,),
    ).fetchall()
    if len(deliveries) != 1:
        return None
    delivery = deliveries[0]
    expected = (
        trust.status
        if trust.status in {"accepted-risk", "same-context-degraded"}
        else "delivered"
    )
    actual = str(delivery["decision_status"] or "")
    if actual == expected:
        return None
    return _blocker(
        "delivery-decision-trust-mismatch",
        "persisted delivery decision does not match the canonical trust "
        f"decision: expected={expected} actual={actual}",
        "delivery",
        str(delivery["id"]),
    )


def _ordered_blockers(
    blockers: Iterable[DeliveryBlocker],
) -> tuple[DeliveryBlocker, ...]:
    unique: dict[tuple[str, str, str, str], DeliveryBlocker] = {}
    for blocker in blockers:
        unique.setdefault(
            (
                blocker.code,
                blocker.entity_type,
                blocker.entity_id,
                blocker.message,
            ),
            blocker,
        )
    return tuple(
        sorted(
            unique.values(),
            key=lambda item: (
                _BLOCKER_ORDER.get(item.code, len(_BLOCKER_ORDER)),
                item.entity_type,
                item.entity_id,
                item.message,
            ),
        )
    )


_FULL_GIT_OID = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")


def _local_git(
    root: Path,
    *arguments: str,
) -> subprocess.CompletedProcess[bytes] | None:
    """Run one bounded, non-fetching local Git read."""

    try:
        return subprocess.run(
            ["git", "-c", "core.fsmonitor=false", *arguments],
            cwd=root,
            env=isolated_git_environment(work_tree=root),
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _resolved_commit(root: Path, reference: str) -> str:
    result = _local_git(
        root,
        "rev-parse",
        "--verify",
        "--end-of-options",
        f"{reference}^{{commit}}",
    )
    if result is None or result.returncode != 0:
        return ""
    try:
        resolved = result.stdout.decode("ascii", errors="strict").strip()
    except UnicodeDecodeError:
        return ""
    return resolved.lower() if _FULL_GIT_OID.fullmatch(resolved) else ""


def _has_cycle_bound_task_accept_event(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    cycle_id: str,
) -> bool:
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


def _eligible_accepted_tasks_for_acceptance(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    acceptance_id: str,
) -> tuple[sqlite3.Row, ...]:
    """Return tasks satisfying the same evidence/actor rule used by delivery."""

    rows = conn.execute(
        """
        select t.* from task_acceptance ta
        join tasks t on t.cycle_id = ta.cycle_id and t.id = ta.task_id
        where ta.cycle_id = ? and ta.acceptance_id = ?
          and t.status = 'accepted'
        order by t.id
        """,
        (cycle_id, acceptance_id),
    ).fetchall()
    return tuple(
        task
        for task in rows
        if str(task["evidence"] or "").strip()
        and (
            bool(str(task["accepted_by"] or "").strip())
            or _has_cycle_bound_task_accept_event(
                conn,
                task_id=str(task["id"]),
                cycle_id=cycle_id,
            )
        )
    )


def derive_delivery_changed_files(
    root: Path,
    *,
    base_ref: str,
    delivery_candidate: str,
    candidate_override: str | None = None,
) -> tuple[str, tuple[str, ...]]:
    """Conservatively derive source changes for a provably comparable Git base.

    ``candidate_sha`` is Kafa's framed source digest, not a Git object name.  A
    changed-file list is therefore authoritative only while a clean local HEAD
    still hashes to the delivered candidate and ``base_ref`` is an immutable
    full commit object ID.  Every other case is explicitly unknown.
    """

    unknown = ("unknown/not derivable", ())
    immutable_base = str(base_ref or "").strip()
    if not _FULL_GIT_OID.fullmatch(immutable_base):
        return unknown
    try:
        before_snapshot = git_source_snapshot(root)
    except Exception:
        return unknown
    if before_snapshot is None:
        return unknown
    before, before_dirty, before_head_comparable = before_snapshot
    expected_candidate = (
        candidate_override
        if candidate_override is not None
        else before
    )
    if (
        before_dirty
        or not before_head_comparable
        or before != delivery_candidate
        or expected_candidate != delivery_candidate
    ):
        return unknown

    base_commit = _resolved_commit(root, immutable_base)
    head_commit = _resolved_commit(root, "HEAD")
    if (
        not base_commit
        or immutable_base.lower() != base_commit
        or not head_commit
    ):
        return unknown
    ancestor = _local_git(
        root,
        "merge-base",
        "--is-ancestor",
        base_commit,
        head_commit,
    )
    if ancestor is None or ancestor.returncode != 0:
        return unknown
    changed = _local_git(
        root,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "--name-only",
        "-z",
        "--diff-filter=ACDMRTUXB",
        base_commit,
        head_commit,
        "--",
    )
    if changed is None or changed.returncode != 0:
        return unknown

    paths: set[str] = set()
    for raw_path in changed.stdout.split(b"\0"):
        if not raw_path:
            continue
        try:
            relative = raw_path.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return unknown
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            return unknown
        if source_path_excluded(
            relative,
            versioned_environment_roots=SOURCE_ENVIRONMENT_ROOTS,
        ):
            continue
        paths.add(relative)
    try:
        after_snapshot = git_source_snapshot(root)
    except Exception:
        return unknown
    if after_snapshot != (delivery_candidate, False, True):
        return unknown
    if _resolved_commit(root, immutable_base) != base_commit:
        return unknown
    if _resolved_commit(root, "HEAD") != head_commit:
        return unknown
    try:
        final_snapshot = git_source_snapshot(root)
    except Exception:
        return unknown
    if final_snapshot != after_snapshot:
        return unknown
    return "derived", tuple(sorted(paths))


def derive_delivery_narrative_facts(
    conn: sqlite3.Connection,
    root: Path,
    delivery_id: str,
    *,
    evidence_root: Path | None = None,
    git_root: Path | None = None,
    candidate_override: str | None = None,
) -> DeliveryNarrativeFacts:
    """Build one immutable delivery-ID and cycle-bound narrative read model."""

    delivery = conn.execute(
        "select * from deliveries where id = ?",
        (delivery_id,),
    ).fetchone()
    if delivery is None:
        raise ValueError(f"delivery is missing: {delivery_id}")
    cycle_id = str(delivery["cycle_id"])
    candidate_sha = str(delivery["candidate_sha"])
    cycle = conn.execute(
        "select * from delivery_cycles where id = ?",
        (cycle_id,),
    ).fetchone()
    if cycle is None:
        raise ValueError(f"delivery cycle is missing: {cycle_id}")

    acceptance_ids = tuple(
        str(row[0])
        for row in conn.execute(
            """
            select da.acceptance_id
            from delivery_acceptance da
            join acceptance a
              on a.cycle_id = da.cycle_id and a.id = da.acceptance_id
            where da.delivery_id = ? and da.cycle_id = ?
              and a.status = 'active'
            order by da.acceptance_id
            """,
            (delivery_id, cycle_id),
        ).fetchall()
    )
    requirement_acceptance_links = tuple(
        (str(row[0]), str(row[1]))
        for row in conn.execute(
            """
            select ra.requirement_id, ra.acceptance_id
            from delivery_acceptance da
            join requirement_acceptance ra
              on ra.cycle_id = da.cycle_id
             and ra.acceptance_id = da.acceptance_id
            join requirements r
              on r.cycle_id = ra.cycle_id and r.id = ra.requirement_id
            where da.delivery_id = ? and da.cycle_id = ?
              and r.status = 'active'
            order by ra.requirement_id, ra.acceptance_id
            """,
            (delivery_id, cycle_id),
        ).fetchall()
    )
    task_acceptance_links = tuple(
        sorted(
            (str(task["id"]), acceptance_id)
            for acceptance_id in acceptance_ids
            for task in _eligible_accepted_tasks_for_acceptance(
                conn,
                cycle_id=cycle_id,
                acceptance_id=acceptance_id,
            )
        )
    )

    gate_row = conn.execute(
        """
        select * from quality_gates
        where cycle_id = ? and candidate_sha = ? and gate_status = 'active'
        order by sequence desc limit 1
        """,
        (cycle_id, candidate_sha),
    ).fetchone()
    qualification_rows = (
        conn.execute(
            """
            select q.*
            from quality_gate_qualifications qg
            join acceptance_target_qualifications q
              on q.id = qg.qualification_id
             and q.cycle_id = qg.cycle_id
            join delivery_acceptance da
              on da.delivery_id = ? and da.cycle_id = q.cycle_id
             and da.acceptance_id = q.acceptance_id
            where qg.gate_id = ? and qg.cycle_id = ?
              and qg.candidate_sha = ?
            order by q.id
            """,
            (delivery_id, gate_row["id"], cycle_id, candidate_sha),
        ).fetchall()
        if gate_row is not None
        else []
    )
    qualification_links = tuple(
        (
            str(row["id"]),
            str(row["acceptance_id"]),
            str(row["target_id"]),
        )
        for row in qualification_rows
    )
    qualification_ids = tuple(link[0] for link in qualification_links)
    qualification_by_id = {link[0]: link for link in qualification_links}
    qualification_row_by_id = {
        str(row["id"]): row for row in qualification_rows
    }

    validation_facts: list[DeliveryValidationFact] = []
    ineligible_validation_facts: list[DeliveryValidationFact] = []
    judgment_validation_facts: list[DeliveryValidationFact] = []
    execution_evidence_root = evidence_root or root
    for validation in conn.execute(
        """
        select * from validations
        where cycle_id = ? and candidate_sha = ?
          and validation_status = 'active'
        order by id
        """,
        (cycle_id, candidate_sha),
    ).fetchall():
        execution_rows = conn.execute(
            """
            select e.* from validation_executions ve
            join executions e
              on e.id = ve.execution_id
             and e.cycle_id = ve.cycle_id
             and e.candidate_sha = ve.candidate_sha
            where ve.validation_id = ? and ve.cycle_id = ?
              and ve.candidate_sha = ?
            order by e.id
            """,
            (validation["id"], cycle_id, candidate_sha),
        ).fetchall()
        execution_ids = tuple(
            str(execution["id"]) for execution in execution_rows
        )
        validation_id = str(validation["id"])
        result = str(validation["result"])
        acceptance_id = str(validation["acceptance_id"] or "")
        qualification_id = str(validation["qualification_id"] or "")
        qualification = qualification_by_id.get(qualification_id)
        qualification_row = qualification_row_by_id.get(
            qualification_id
        )
        eligibility_issues: list[str] = []
        if result != "pass":
            eligibility_issues.append(f"validation result is not pass: {result}")
        if not execution_ids:
            eligibility_issues.append("validation has no linked immutable execution")
        if qualification is None or qualification_row is None:
            eligibility_issues.append(
                "validation has no gate-reviewed qualification relation"
            )
        elif acceptance_id not in acceptance_ids or qualification[1] != acceptance_id:
            eligibility_issues.append(
                "validation acceptance does not match delivered qualification"
            )
        elif not all(
                str(execution["target_id"] or "") == qualification[2]
                and str(execution["target_definition_sha256"] or "")
                == str(qualification_row["target_definition_sha256"] or "")
                for execution in execution_rows
        ):
            eligibility_issues.append(
                "execution target identity does not match qualification"
            )
        else:
            eligibility_issues.extend(
                qualified_validation_execution_issues(
                    conn,
                    execution_evidence_root,
                    validation,
                    qualification_row,
                    candidate_sha,
                )
            )
        fact = DeliveryValidationFact(
            id=validation_id,
            surface=str(validation["surface"]),
            result=result,
            acceptance_id=acceptance_id,
            qualification_id=qualification_id,
            execution_ids=execution_ids,
            eligibility_issues=tuple(eligibility_issues),
        )
        if not eligibility_issues:
            validation_facts.append(fact)
        elif execution_ids:
            ineligible_validation_facts.append(fact)
        else:
            judgment_validation_facts.append(fact)

    validation_ids = tuple(fact.id for fact in validation_facts)
    execution_ids = tuple(
        sorted(
            {
                execution_id
                for fact in validation_facts
                for execution_id in fact.execution_ids
            }
        )
    )
    execution_target_ids = tuple(
        str(row[0])
        for execution_id in execution_ids
        for row in conn.execute(
            """
            select target_id from executions
            where id = ? and cycle_id = ? and candidate_sha = ?
              and target_id is not null
            """,
            (execution_id, cycle_id, candidate_sha),
        ).fetchall()
    )
    target_ids = tuple(
        sorted({link[2] for link in qualification_links} | set(execution_target_ids))
    )

    authoritative_validation_ids = frozenset(validation_ids)
    failure_mode_fact_list: list[DeliveryFailureModeFact] = []
    for row in conn.execute(
        """
        select id, risk, status, accepted_by, acceptance_reason,
               acceptance_scope, accepted_revision, expires_at
        from failure_modes where cycle_id = ? order by id
        """,
        (cycle_id,),
    ).fetchall():
        risk = str(row["risk"])
        coverage_ids: list[str] = []
        for validation in conn.execute(
            """
            select v.*
            from validation_failure_modes vfm
            join validations v
              on v.id = vfm.validation_id
             and v.cycle_id = vfm.cycle_id
            join failure_mode_acceptance fma
              on fma.cycle_id = vfm.cycle_id
             and fma.failure_mode_id = vfm.failure_mode_id
             and fma.acceptance_id = v.acceptance_id
            where vfm.cycle_id = ? and vfm.failure_mode_id = ?
            order by vfm.validation_id
            """,
            (cycle_id, row["id"]),
        ).fetchall():
            validation_id = str(validation["id"])
            if validation_id not in authoritative_validation_ids:
                continue
            if risk in {"medium", "high", "critical"}:
                qualification = qualification_row_by_id.get(
                    str(validation["qualification_id"] or "")
                )
                if qualification is None or qualified_validation_execution_issues(
                    conn,
                    execution_evidence_root,
                    validation,
                    qualification,
                    candidate_sha,
                    require_structured=True,
                ):
                    continue
            coverage_ids.append(validation_id)
        failure_mode_fact_list.append(
            DeliveryFailureModeFact(
                id=str(row["id"]),
                risk=risk,
                status=str(row["status"]),
                validation_ids=tuple(coverage_ids),
                accepted_by=str(row["accepted_by"] or ""),
                acceptance_reason=str(row["acceptance_reason"] or ""),
                acceptance_scope=str(row["acceptance_scope"] or ""),
                accepted_revision=(
                    row["accepted_revision"]
                    if type(row["accepted_revision"]) is int
                    else None
                ),
                expires_at=str(row["expires_at"] or ""),
            )
        )
    failure_mode_facts = tuple(failure_mode_fact_list)
    finding_facts = tuple(
        DeliveryFindingFact(
            id=str(row["id"]),
            surface=str(row["surface"]),
            severity=str(row["severity"]),
            status=str(row["status"]),
            waived_by=str(row["waived_by"] or ""),
            waiver_reason=str(row["waiver_reason"] or ""),
            waiver_scope=str(row["waiver_scope"] or ""),
            waived_revision=(
                row["waived_revision"]
                if type(row["waived_revision"]) is int
                else None
            ),
            waiver_expires_at=str(row["waiver_expires_at"] or ""),
        )
        for row in conn.execute(
            """
            select id, surface, severity, status, waived_by, waiver_reason,
                   waiver_scope, waived_revision, waiver_expires_at
            from findings
            where cycle_id = ? and candidate_sha = ? order by id
            """,
            (cycle_id, candidate_sha),
        ).fetchall()
    )
    gate_fact: DeliveryGateFact | None = None
    if gate_row is not None:
        gate_finding_ids = tuple(
            str(row[0])
            for row in conn.execute(
                """
                select qgf.finding_id
                from quality_gate_findings qgf
                join findings f on f.id = qgf.finding_id
                where qgf.gate_id = ? and f.cycle_id = ?
                  and f.candidate_sha = ?
                order by qgf.finding_id
                """,
                (gate_row["id"], cycle_id, candidate_sha),
            ).fetchall()
        )
        reviewed_revision = (
            gate_row["reviewed_revision"]
            if type(gate_row["reviewed_revision"]) is int
            else 0
        )
        gate_fact = DeliveryGateFact(
            id=str(gate_row["id"]),
            result=str(gate_row["result"]),
            review_status=str(gate_row["review_status"]),
            producer_context_id=str(gate_row["producer_context_id"] or ""),
            reviewer_context_id=str(gate_row["reviewer_context_id"] or ""),
            residual_risk=str(gate_row["residual_risk"] or ""),
            reviewed_revision=reviewed_revision,
            qualification_ids=qualification_ids,
            finding_ids=gate_finding_ids,
        )

    historical_report = evaluate_historical_cycle_report(
        conn,
        execution_evidence_root,
        cycle_id,
    )
    delivery_events = conn.execute(
        """
        select created_at from events
        where event_type = 'delivery_recorded'
          and entity_type = 'delivery' and entity_id = ?
        order by sequence
        """,
        (delivery_id,),
    ).fetchall()
    recorded_at = (
        str(delivery_events[0]["created_at"])
        if len(delivery_events) == 1
        else "unknown/not corroborated"
    )
    decision_status = str(delivery["decision_status"])
    trust_status = historical_report.trust.status

    changed_files_status, changed_files = derive_delivery_changed_files(
        git_root or root,
        base_ref=str(cycle["base_ref"] or ""),
        delivery_candidate=candidate_sha,
        candidate_override=candidate_override,
    )
    return DeliveryNarrativeFacts(
        delivery_id=str(delivery["id"]),
        cycle_id=cycle_id,
        candidate_sha=candidate_sha,
        recorded_at=recorded_at,
        cycle_status=str(cycle["status"]),
        cycle_phase=str(cycle["phase"]),
        base_ref=str(cycle["base_ref"] or ""),
        decision_status=decision_status,
        trust_status=trust_status,
        requirement_ids=tuple(sorted({link[0] for link in requirement_acceptance_links})),
        acceptance_ids=acceptance_ids,
        task_ids=tuple(sorted({link[0] for link in task_acceptance_links})),
        qualification_ids=qualification_ids,
        target_ids=target_ids,
        execution_ids=execution_ids,
        validation_ids=validation_ids,
        ineligible_validation_ids=tuple(
            fact.id for fact in ineligible_validation_facts
        ),
        judgment_validation_ids=tuple(
            fact.id for fact in judgment_validation_facts
        ),
        failure_mode_ids=tuple(fact.id for fact in failure_mode_facts),
        finding_ids=tuple(fact.id for fact in finding_facts),
        gate_ids=(gate_fact.id,) if gate_fact is not None else (),
        requirement_acceptance_links=requirement_acceptance_links,
        task_acceptance_links=task_acceptance_links,
        qualification_links=qualification_links,
        validation_facts=tuple(validation_facts),
        ineligible_validation_facts=tuple(ineligible_validation_facts),
        judgment_validation_facts=tuple(judgment_validation_facts),
        failure_mode_facts=failure_mode_facts,
        finding_facts=finding_facts,
        gate=gate_fact,
        changed_files_status=changed_files_status,
        changed_files=changed_files,
    )


def _baseline_confirmation_payload(
    conn: sqlite3.Connection,
    baseline: sqlite3.Row,
    cycle_id: str,
) -> dict[str, object] | None:
    event = conn.execute(
        """
        select after_json from events
        where event_type = 'baseline_confirmed'
          and entity_type = 'baseline' and entity_id = ?
        order by sequence desc limit 1
        """,
        (baseline["id"],),
    ).fetchone()
    if event is None:
        return None
    try:
        payload = json.loads(str(event["after_json"] or "{}"))
    except (TypeError, json.JSONDecodeError):
        return None
    if not (
        isinstance(payload, dict)
        and str(payload.get("id") or "") == str(baseline["id"])
        and str(payload.get("digest") or "") == str(baseline["digest"])
        and str(payload.get("cycle_id") or "") == cycle_id
    ):
        return None
    confirmed_revision = _positive_integer(payload.get("project_revision"))
    baseline_revision = _positive_integer(baseline["project_revision"])
    if (
        confirmed_revision is None
        or baseline_revision is None
        or confirmed_revision not in {baseline_revision, baseline_revision + 1}
    ):
        return None
    return payload


def _baseline_confirmation_matches(
    conn: sqlite3.Connection,
    baseline: sqlite3.Row,
    cycle_id: str,
) -> bool:
    return _baseline_confirmation_payload(conn, baseline, cycle_id) is not None


def _event_after_payload(event: sqlite3.Row) -> dict[str, object] | None:
    try:
        payload = json.loads(str(event["after_json"] or "{}"))
    except (TypeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def historical_cycle_event_facts(
    conn: sqlite3.Connection,
    cycle_id: str,
) -> tuple[dict[str, object], ...]:
    """Return the cycle-explicit audit rows consumed by historical review.

    Project events that merely point at a current cycle are intentionally
    excluded, so starting a later cycle does not rewrite an older cycle's fact
    digest. Domain events emitted for requirements, tasks, gates, and delivery
    carry an explicit ``cycle_id`` in ``after_json`` and remain in scope.
    """

    facts: list[dict[str, object]] = []
    for event in conn.execute("select * from events order by sequence").fetchall():
        payload = _event_after_payload(event)
        if payload is None or str(payload.get("cycle_id") or "") != cycle_id:
            continue
        facts.append({str(key): event[key] for key in event.keys()})
    return tuple(facts)


def _historical_event_chain(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    baseline: sqlite3.Row | None,
    gate: sqlite3.Row | None,
    delivery: sqlite3.Row | None,
) -> tuple[dict[str, object] | None, list[DeliveryBlocker]]:
    """Validate ordered audit corroboration without treating events as replay state."""

    blockers: list[DeliveryBlocker] = []

    def invalid(message: str, entity_id: object) -> None:
        blockers.append(
            _blocker(
                "historical-event-chain-invalid",
                message,
                "event",
                entity_id,
            )
        )

    if baseline is None or gate is None or delivery is None:
        return None, blockers

    gate_events = conn.execute(
        """
        select * from events
        where event_type = 'quality_gate_recorded'
          and entity_type = 'quality_gate' and entity_id = ?
        order by sequence
        """,
        (gate["id"],),
    ).fetchall()
    delivery_events = conn.execute(
        """
        select * from events
        where event_type = 'delivery_recorded'
          and entity_type = 'delivery' and entity_id = ?
        order by sequence
        """,
        (delivery["id"],),
    ).fetchall()
    if len(gate_events) != 1:
        invalid(
            "historical cycle requires exactly one gate-recorded event: "
            f"gate={gate['id']} actual={len(gate_events)}",
            gate["id"],
        )
    if len(delivery_events) != 1:
        invalid(
            "historical cycle requires exactly one delivery-recorded event: "
            f"delivery={delivery['id']} actual={len(delivery_events)}",
            delivery["id"],
        )
    if len(gate_events) != 1 or len(delivery_events) != 1:
        return None, blockers

    gate_event = gate_events[0]
    delivery_event = delivery_events[0]
    gate_payload = _event_after_payload(gate_event)
    delivery_payload = _event_after_payload(delivery_event)
    expected_gate = {
        "id": gate["id"],
        "cycle_id": cycle_id,
        "candidate_sha": gate["candidate_sha"],
        "result": gate["result"],
        "review_status": gate["review_status"],
    }
    expected_delivery = {
        "id": delivery["id"],
        "cycle_id": cycle_id,
        "candidate_sha": delivery["candidate_sha"],
    }
    if gate_payload is None or any(
        str(gate_payload.get(field) or "") != str(value or "")
        for field, value in expected_gate.items()
    ):
        invalid(
            f"gate-recorded event does not corroborate gate row: {gate['id']}",
            gate_event["id"],
        )
    if delivery_payload is None or any(
        str(delivery_payload.get(field) or "") != str(value or "")
        for field, value in expected_delivery.items()
    ):
        invalid(
            "delivery-recorded event does not corroborate delivery row: "
            f"{delivery['id']}",
            delivery_event["id"],
        )
    if int(gate_event["sequence"]) >= int(delivery_event["sequence"]):
        invalid(
            "historical event order must be gate-recorded before "
            f"delivery-recorded: gate={gate['id']} delivery={delivery['id']}",
            delivery_event["id"],
        )

    confirmation_events = conn.execute(
        """
        select * from events
        where event_type = 'baseline_confirmed'
          and entity_type = 'baseline' and entity_id = ?
        order by sequence
        """,
        (baseline["id"],),
    ).fetchall()
    matching: list[tuple[sqlite3.Row, dict[str, object]]] = []
    for event in confirmation_events:
        payload = _event_after_payload(event)
        if payload is None:
            invalid(
                f"baseline-confirmed event has invalid payload: {event['id']}",
                event["id"],
            )
            continue
        if (
            str(payload.get("id") or "") == str(baseline["id"])
            and str(payload.get("digest") or "") == str(baseline["digest"])
            and str(payload.get("cycle_id") or "") == cycle_id
        ):
            matching.append((event, payload))

    before_gate = [
        item
        for item in matching
        if int(item[0]["sequence"]) < int(gate_event["sequence"])
    ]
    after_gate = [
        item
        for item in matching
        if int(item[0]["sequence"]) >= int(gate_event["sequence"])
    ]
    if after_gate:
        invalid(
            "baseline-confirmed event was appended after the reviewed gate: "
            + ",".join(str(item[0]["id"]) for item in after_gate),
            after_gate[0][0]["id"],
        )
    if not before_gate:
        invalid(
            "historical cycle has no matching baseline-confirmed event before "
            f"gate {gate['id']}",
            baseline["id"],
        )
        return None, blockers

    confirmation_event, confirmation = before_gate[-1]
    confirmed_revision = _positive_integer(confirmation.get("project_revision"))
    baseline_revision = _positive_integer(baseline["project_revision"])
    if (
        confirmed_revision is None
        or baseline_revision is None
        or confirmed_revision not in {baseline_revision, baseline_revision + 1}
    ):
        invalid(
            "baseline-confirmed revision is not a legal confirmation transition: "
            f"baseline={baseline_revision} confirmed={confirmed_revision}",
            confirmation_event["id"],
        )
        return None, blockers
    return confirmation, blockers


def _delivered_consistency_blockers(
    conn: sqlite3.Connection,
    root: Path,
    project: sqlite3.Row,
    cycle: sqlite3.Row,
    *,
    historical: bool = False,
    current_candidate_override: str | None = None,
) -> list[DeliveryBlocker]:
    cycle_id = str(cycle["id"])
    current_candidate = (
        str(cycle["candidate_sha"] or "")
        if historical
        else (
            current_candidate_override
            if current_candidate_override is not None
            else current_candidate_sha(root)
        )
    )
    blockers: list[DeliveryBlocker] = []
    delivery_rows = conn.execute(
        """
        select * from deliveries
        where cycle_id = ?
        order by created_at desc, id desc
        """,
        (cycle_id,),
    ).fetchall()
    delivery = delivery_rows[0] if delivery_rows else None
    if delivery is None:
        blockers.append(
            _blocker(
                "delivery-row-missing",
                f"delivered cycle has no delivery row: {cycle_id}",
                "delivery_cycle",
                cycle_id,
            )
        )
    else:
        if len(delivery_rows) != 1:
            blockers.append(
                _blocker(
                    "delivery-row-count-invalid",
                    "delivered cycle requires exactly one delivery row: "
                    f"cycle={cycle_id} actual={len(delivery_rows)} "
                    f"ids={[str(row['id']) for row in delivery_rows]}",
                    "delivery_cycle",
                    cycle_id,
                )
            )
        expected_acceptance_ids = {
            str(row[0])
            for row in conn.execute(
                """
                select id from acceptance
                where cycle_id = ? and status = 'active'
                order by id
                """,
                (cycle_id,),
            ).fetchall()
        }
        relation_rows = conn.execute(
            """
            select cycle_id, acceptance_id from delivery_acceptance
            where delivery_id = ? order by cycle_id, acceptance_id
            """,
            (delivery["id"],),
        ).fetchall()
        linked_acceptance_ids = {
            str(row["acceptance_id"])
            for row in relation_rows
            if str(row["cycle_id"]) == cycle_id
        }
        cross_cycle_links = tuple(
            f"{row['cycle_id']}:{row['acceptance_id']}"
            for row in relation_rows
            if str(row["cycle_id"]) != cycle_id
        )
        if (
            linked_acceptance_ids != expected_acceptance_ids
            or cross_cycle_links
        ):
            blockers.append(
                _blocker(
                    "delivery-acceptance-set-mismatch",
                    "delivery acceptance relation must equal the complete "
                    "active proven set for its own cycle: "
                    f"expected={sorted(expected_acceptance_ids)} "
                    f"actual={sorted(linked_acceptance_ids)} "
                    f"cross_cycle={list(cross_cycle_links)}",
                    "delivery",
                    delivery["id"],
                )
            )
    cycle_candidate = str(cycle["candidate_sha"] or "")
    delivery_candidate = str(delivery["candidate_sha"] or "") if delivery else ""
    if (
        not cycle_candidate
        or (not historical and cycle_candidate != current_candidate)
        or (delivery is not None and delivery_candidate != cycle_candidate)
    ):
        blockers.append(
            _blocker(
                "delivered-candidate-inconsistent",
                "delivered candidate identity is inconsistent: "
                f"cycle={cycle_candidate or 'empty'} "
                f"delivery={delivery_candidate or 'missing'} "
                f"current={current_candidate or 'historical-snapshot'}",
                "delivery_cycle",
                cycle_id,
            )
        )
    if (
        (not historical and str(project["phase"]) != "delivery_readiness")
        or str(cycle["phase"]) != "delivery_readiness"
    ):
        blockers.append(
            _blocker(
                "delivered-phase-inconsistent",
                "delivered project and cycle must remain in delivery_readiness: "
                f"project={project['phase']} cycle={cycle['phase']}",
                "delivery_cycle",
                cycle_id,
            )
        )
    if str(cycle["status"]) != "delivered" or not str(
        cycle["closed_at"] or ""
    ).strip():
        blockers.append(
            _blocker(
                "delivered-cycle-not-closed",
                "delivered cycle must have status=delivered and a non-empty closed_at: "
                f"{cycle_id} status={cycle['status']}",
                "delivery_cycle",
                cycle_id,
            )
        )
    return blockers


def _structured_prerequisite_blockers(
    conn: sqlite3.Connection,
    root: Path,
    *,
    mode: DeliveryEvaluationMode,
    cycle_id_override: str | None = None,
    historical: bool = False,
    candidate_override: str | None = None,
) -> tuple[list[DeliveryBlocker], str, str]:
    project = conn.execute("select * from project where id = 1").fetchone()
    if project is None:
        return (
            [
                _blocker(
                    "requirement-missing",
                    "project is not initialized",
                    "project",
                    "1",
                )
            ],
            "",
            "",
        )
    try:
        cycle = (
            conn.execute(
                "select * from delivery_cycles where id = ?",
                (cycle_id_override,),
            ).fetchone()
            if cycle_id_override
            else current_cycle_row(conn)
        )
        if cycle is None:
            raise ValueError(f"delivery cycle is missing: {cycle_id_override}")
    except Exception as exc:
        return (
            [
                _blocker(
                    "cycle-not-active",
                    str(exc),
                    "delivery_cycle",
                    str(project["current_cycle_id"] or ""),
                )
            ],
            str(project["current_cycle_id"] or ""),
            (
                candidate_override
                if candidate_override is not None
                else current_candidate_sha(root)
            ),
        )
    cycle_id = str(cycle["id"])
    candidate = (
        str(cycle["candidate_sha"] or "")
        if historical
        else (
            candidate_override
            if candidate_override is not None
            else current_candidate_sha(root)
        )
    )
    blockers: list[DeliveryBlocker] = (
        _delivered_consistency_blockers(
            conn,
            root,
            project,
            cycle,
            historical=historical,
            current_candidate_override=candidate,
        )
        if mode == "delivered-consistency"
        else []
    )
    requirements = conn.execute(
        "select * from requirements where cycle_id = ? and status = 'active' order by id",
        (cycle_id,),
    ).fetchall()
    acceptances = conn.execute(
        "select * from acceptance where cycle_id = ? and status = 'active' order by id",
        (cycle_id,),
    ).fetchall()
    if not requirements:
        blockers.append(
            _blocker(
                "requirement-missing",
                f"active cycle has no active requirement: {cycle_id}",
                "delivery_cycle",
                cycle_id,
            )
        )
    if not acceptances:
        blockers.append(
            _blocker(
                "acceptance-missing",
                f"active cycle has no active acceptance: {cycle_id}",
                "delivery_cycle",
                cycle_id,
            )
        )

    for requirement in requirements:
        linked = conn.execute(
            """
            select 1 from requirement_acceptance ra
            join acceptance a
              on a.cycle_id = ra.cycle_id and a.id = ra.acceptance_id
            where ra.cycle_id = ? and ra.requirement_id = ?
              and a.status = 'active'
            limit 1
            """,
            (cycle_id, requirement["id"]),
        ).fetchone()
        if linked is None:
            blockers.append(
                _blocker(
                    "requirement-acceptance-link-missing",
                    f"active requirement has no active acceptance link: {requirement['id']}",
                    "requirement",
                    requirement["id"],
                )
            )
    for acceptance in acceptances:
        linked = conn.execute(
            """
            select 1 from requirement_acceptance ra
            join requirements r
              on r.cycle_id = ra.cycle_id and r.id = ra.requirement_id
            where ra.cycle_id = ? and ra.acceptance_id = ?
              and r.status = 'active'
            limit 1
            """,
            (cycle_id, acceptance["id"]),
        ).fetchone()
        if linked is None:
            blockers.append(
                _blocker(
                    "acceptance-orphaned",
                    f"active acceptance is not linked from an active requirement: {acceptance['id']}",
                    "acceptance",
                    acceptance["id"],
                )
            )

    baseline = latest_baseline(conn, cycle_id)
    if baseline is None:
        blockers.append(
            _blocker(
                "baseline-missing",
                f"active cycle has no frozen baseline: {cycle_id}",
                "delivery_cycle",
                cycle_id,
            )
        )
    else:
        from .cycle_ledger import baseline_digest

        current_digest = baseline_digest(conn, cycle_id)
        if str(baseline["digest"]) != current_digest:
            blockers.append(
                _blocker(
                    "baseline-stale",
                    f"frozen baseline is stale: {baseline['id']}",
                    "baseline",
                    baseline["id"],
                )
            )
    if (
        (not historical and str(project["scope_status"]) != "confirmed")
        or baseline is None
        or not _baseline_confirmation_matches(conn, baseline, cycle_id)
    ):
        blockers.append(
            _blocker(
                "scope-unconfirmed",
                "scope is not confirmed against the latest baseline identity",
                "baseline",
                baseline["id"] if baseline else cycle_id,
            )
        )

    for acceptance in acceptances:
        eligible_tasks = _eligible_accepted_tasks_for_acceptance(
            conn,
            cycle_id=cycle_id,
            acceptance_id=str(acceptance["id"]),
        )
        if not eligible_tasks:
            blockers.append(
                _blocker(
                    "accepted-task-missing",
                    "active acceptance has no accepted task with non-empty "
                    f"evidence and accept actor/event: {acceptance['id']}",
                    "acceptance",
                    acceptance["id"],
                )
            )

    qualification_table = conn.execute(
        "select 1 from sqlite_master where type='table' "
        "and name='acceptance_target_qualifications'"
    ).fetchone()
    eligible_by_acceptance: dict[str, list[str]] = {}
    for acceptance in acceptances:
        acceptance_id = str(acceptance["id"])
        target_ids = (
            [
                str(row["target_id"])
                for row in conn.execute(
                    """
                    select distinct target_id
                    from acceptance_target_qualifications
                    where cycle_id = ? and acceptance_id = ?
                    order by target_id
                    """,
                    (cycle_id, acceptance_id),
                ).fetchall()
            ]
            if qualification_table
            else []
        )
        qualifications = [
            qualification
            for target_id in target_ids
            if (
                qualification := latest_acceptance_target_qualification(
                    conn,
                    cycle_id=cycle_id,
                    acceptance_id=acceptance_id,
                    target_id=target_id,
                )
            )
            is not None
        ]
        if not qualifications:
            blockers.append(
                _blocker(
                    "qualification-missing",
                    f"active acceptance has no qualification: {acceptance_id}",
                    "acceptance",
                    acceptance_id,
                )
            )
            continue
        current_qualifications: list[sqlite3.Row] = []
        for qualification in qualifications:
            target = conn.execute(
                "select * from test_targets where id = ?",
                (qualification["target_id"],),
            ).fetchone()
            if (
                target is not None
                and int(qualification["acceptance_revision"])
                == int(acceptance["revision"])
                and str(qualification["target_definition_sha256"])
                == target_definition_digest(dict(target))
            ):
                current_qualifications.append(qualification)
        if not current_qualifications:
            stale = qualifications[0]
            blockers.append(
                _blocker(
                    "qualification-stale",
                    f"qualification is stale for acceptance {acceptance_id}: {stale['id']}",
                    "acceptance_target_qualification",
                    stale["id"],
                )
            )
            continue

        eligible: list[str] = []
        saw_validation = False
        execution_failures: list[str] = []
        for qualification in current_qualifications:
            validations = conn.execute(
                """
                select * from validations
                where cycle_id = ? and candidate_sha = ?
                  and acceptance_id = ? and qualification_id = ?
                  and validation_status = 'active' and result = 'pass'
                order by created_at desc, id desc
                """,
                (cycle_id, candidate, acceptance_id, qualification["id"]),
            ).fetchall()
            saw_validation = saw_validation or bool(validations)
            for validation in validations:
                found = qualified_validation_execution_issues(
                    conn,
                    root,
                    validation,
                    qualification,
                    candidate,
                )
                if not found:
                    eligible.append(str(qualification["id"]))
                    break
                execution_failures.extend(found)
        if eligible:
            eligible_by_acceptance[acceptance_id] = eligible
        elif not saw_validation:
            blockers.append(
                _blocker(
                    "current-validation-missing",
                    f"active acceptance has no qualified passing validation for current candidate: {acceptance_id}",
                    "acceptance",
                    acceptance_id,
                )
            )
        else:
            suffix = f": {execution_failures[0]}" if execution_failures else ""
            blockers.append(
                _blocker(
                    "current-execution-missing",
                    "qualified validation has no eligible immutable execution "
                    f"for acceptance {acceptance_id}{suffix}",
                    "acceptance",
                    acceptance_id,
                )
            )

    latest_gate = conn.execute(
        """
        select * from quality_gates
        where cycle_id = ? and candidate_sha = ? and gate_status = 'active'
        order by sequence desc limit 1
        """,
        (cycle_id, candidate),
    ).fetchone()
    if latest_gate is None:
        blockers.append(
            _blocker(
                "quality-gate-missing",
                "delivery requires a quality gate record for current candidate",
                "delivery_cycle",
                cycle_id,
            )
        )
    else:
        has_gate_qualifications = conn.execute(
            "select 1 from sqlite_master where type='table' "
            "and name='quality_gate_qualifications'"
        ).fetchone()
        linked_qualifications = {
            str(row[0])
            for row in (
                conn.execute(
                    "select qualification_id from quality_gate_qualifications "
                    "where gate_id = ? and cycle_id = ? and candidate_sha = ?",
                    (latest_gate["id"], cycle_id, candidate),
                ).fetchall()
                if has_gate_qualifications
                else []
            )
        }
        for acceptance_id, eligible in sorted(eligible_by_acceptance.items()):
            if not linked_qualifications.intersection(eligible):
                qualification_id = eligible[0]
                blockers.append(
                    _blocker(
                        "qualification-unreviewed",
                        "latest quality gate did not review the exact eligible "
                        f"qualification {qualification_id} for acceptance {acceptance_id}",
                        "acceptance_target_qualification",
                        qualification_id,
                    )
                )

    if mode == "record-delivery" and (
        str(project["phase"]) != "delivery_readiness"
        or str(cycle["phase"]) != "delivery_readiness"
    ):
        blockers.append(
            _blocker(
                "phase-not-ready",
                "delivery record requires project and cycle phase=delivery_readiness: "
                f"project={project['phase']} cycle={cycle['phase']}",
                "delivery_cycle",
                cycle_id,
            )
        )
    if mode != "delivered-consistency" and str(cycle["status"]) != "active":
        blockers.append(
            _blocker(
                "cycle-not-active",
                f"delivery prerequisites require an active cycle: {cycle_id} status={cycle['status']}",
                "delivery_cycle",
                cycle_id,
            )
        )
    return blockers, cycle_id, candidate


def _policy_blocker(issue: str, cycle_id: str) -> DeliveryBlocker:
    message = re.sub(r"^\[[a-z0-9-]+\]\s*", "", issue.strip())
    lower = message.lower()
    entity_id = cycle_id
    gate_match = re.search(r"\bgate_id=([^\s;]+)", message)
    if "medium failure mode is not covered" in lower:
        code = "medium-failure-mode-uncovered"
        entity_type = "failure_mode"
        match = re.search(
            r"current-candidate controller execution:\s+([^\s(]+)",
            message,
        )
        if match:
            entity_id = match.group(1)
    elif "medium finding blocks delivery" in lower and "status=open" in lower:
        code = "medium-finding-open"
        entity_type = "finding"
        match = re.search(r"finding blocks delivery:\s+([^\s]+)", message)
        if match:
            entity_id = match.group(1)
    elif "residual-risk" in lower and "same-context-degraded" in lower:
        code = "degraded-residual-risk-missing"
        entity_type = "quality_gate"
    elif "risk acceptance" in lower or "risk record" in lower or "accepted/exempt risk" in lower:
        code = "risk-acceptance-invalid"
        entity_type = "failure_mode"
        match = re.search(
            r"(?:missing|incomplete|stale|expired|invalid|exempt):\s+([^\s;]+)",
            message,
        )
        if match:
            entity_id = match.group(1)
    elif "baseline" in lower:
        code = "baseline-missing" if "missing" in lower else "baseline-stale"
        entity_type = "baseline"
    elif "task" in lower or "completed task" in lower:
        code = "accepted-task-missing"
        entity_type = "acceptance"
    elif "execution" in lower or "artifact" in lower or "sandbox" in lower:
        code = "current-execution-missing"
        entity_type = "execution"
    elif "validation" in lower:
        code = "current-validation-missing"
        entity_type = "validation"
    elif "requirement has no acceptance" in lower:
        code = "requirement-acceptance-link-missing"
        entity_type = "requirement"
    elif "requires a quality gate record" in lower:
        code = "quality-gate-missing"
        entity_type = "quality_gate"
    elif (
        "quality gate" in lower
        or "review_status" in lower
        or "reviewer and producer context" in lower
        or "producer and reviewer context" in lower
    ):
        code = "quality-gate-invalid"
        entity_type = "quality_gate"
        if gate_match:
            entity_id = gate_match.group(1)
    else:
        code = "quality-gate-missing"
        entity_type = "quality_gate"
    return _blocker(code, message, entity_type, entity_id)


def _invariant_blockers(issues: Iterable[object]) -> list[DeliveryBlocker]:
    blockers: list[DeliveryBlocker] = []
    for found in issues:
        code = str(getattr(found, "code", "runtime-invariant"))
        entity_type = str(getattr(found, "entity_type", "project"))
        entity_id = str(getattr(found, "entity_id", "1"))
        blockers.append(
            _blocker(
                f"invariant-{code}",
                str(found),
                entity_type,
                entity_id,
            )
        )
    return blockers


def evaluate_delivery_report(
    conn: sqlite3.Connection,
    root: Path,
    *,
    mode: DeliveryEvaluationMode,
    is_expired: Callable[[str], bool],
    observed_at: str | None = None,
    candidate_override: str | None = None,
) -> DeliveryPrerequisiteReport:
    if mode not in {
        "enter-readiness",
        "record-delivery",
        "delivered-consistency",
    }:
        raise ValueError(f"unknown delivery prerequisite mode: {mode}")
    candidate_snapshot = (
        candidate_override
        if candidate_override is not None
        else current_candidate_sha(root)
    )
    blockers, cycle_id, candidate = _structured_prerequisite_blockers(
        conn,
        root,
        mode=mode,
        candidate_override=candidate_snapshot,
    )
    from .invariant_checker import check_runtime_invariants

    blockers.extend(
        _invariant_blockers(check_runtime_invariants(conn, root))
    )
    policy_issues, policy_trust = _evaluate_local_delivery_policy(
        conn,
        root,
        is_expired=is_expired,
        observed_at=observed_at,
        include_graph_issues=False,
        candidate_override=candidate,
    )
    existing_identities = {
        (blocker.code, blocker.entity_type, blocker.entity_id)
        for blocker in blockers
    }
    for issue in policy_issues:
        policy_blocker = _policy_blocker(issue, cycle_id)
        identity = (
            policy_blocker.code,
            policy_blocker.entity_type,
            policy_blocker.entity_id,
        )
        if identity in existing_identities:
            continue
        blockers.append(policy_blocker)
        existing_identities.add(identity)
    if mode == "delivered-consistency":
        decision_blocker = _delivery_decision_trust_blocker(
            conn,
            cycle_id=cycle_id,
            trust=policy_trust,
        )
        if decision_blocker is not None:
            blockers.append(decision_blocker)
            existing_identities.add(
                (
                    decision_blocker.code,
                    decision_blocker.entity_type,
                    decision_blocker.entity_id,
                )
            )
        historical_report = evaluate_historical_cycle_report(
            conn,
            root,
            cycle_id,
        )
        for historical_blocker in historical_report.blockers:
            identity = (
                historical_blocker.code,
                historical_blocker.entity_type,
                historical_blocker.entity_id,
            )
            if identity in existing_identities:
                continue
            blockers.append(historical_blocker)
            existing_identities.add(identity)
    final_candidate = current_candidate_sha(root)
    if final_candidate != candidate_snapshot:
        candidate_change_reason = (
            "current candidate changed while delivery prerequisites were evaluated: "
            f"started={candidate_snapshot} finished={final_candidate}"
        )
        blockers.append(
            _blocker(
                "candidate-snapshot-changed",
                candidate_change_reason,
                "delivery_cycle",
                cycle_id,
            )
        )
        policy_trust = _human_review_decision(candidate_change_reason)
    if mode == "delivered-consistency":
        allowed = not blockers and policy_trust.delivery_allowed
        trust = LocalTrustDecision(
            status="delivered-consistency",
            trust_level="local-record-consistency",
            delivery_allowed=allowed,
            reasons=(
                "delivery record, graph, policy, and cycle facts are mutually consistent",
            )
            if allowed
            else (
                "delivered facts or current delivery prerequisites are inconsistent",
                *policy_trust.reasons,
            ),
        )
        return DeliveryPrerequisiteReport(
            blockers=_ordered_blockers(blockers),
            trust=trust,
            cycle_id=cycle_id,
            candidate_sha=candidate,
            proven_acceptance_ids=(),
        )
    proven_acceptance_ids = (
        tuple(
            str(row[0])
            for row in conn.execute(
                """
                select id from acceptance
                where cycle_id = ? and status = 'active'
                order by id
                """,
                (cycle_id,),
            ).fetchall()
        )
        if not blockers and policy_trust.delivery_allowed
        else ()
    )
    return DeliveryPrerequisiteReport(
        blockers=_ordered_blockers(blockers),
        trust=policy_trust,
        cycle_id=cycle_id,
        candidate_sha=candidate,
        proven_acceptance_ids=proven_acceptance_ids,
    )


def evaluate_delivery_prerequisites(
    conn: sqlite3.Connection,
    root: Path,
    *,
    mode: DeliveryEvaluationMode,
    is_expired: Callable[[str], bool],
    observed_at: str | None = None,
    candidate_override: str | None = None,
) -> tuple[DeliveryBlocker, ...]:
    """Return the one canonical read-only delivery prerequisite decision."""

    return evaluate_delivery_report(
        conn,
        root,
        mode=mode,
        is_expired=is_expired,
        observed_at=observed_at,
        candidate_override=candidate_override,
    ).blockers


def evaluate_historical_cycle_report(
    conn: sqlite3.Connection,
    root: Path,
    cycle_id: str,
) -> HistoricalDeliveryReport:
    """Audit one closed cycle and preserve its canonical trust decision.

    Historical audit binds execution eligibility to the candidate persisted on
    that cycle. It never compares an old delivery to the source tree of a later
    active cycle and never mutates ``project.current_cycle_id``.
    """

    blockers, resolved_cycle_id, candidate = _structured_prerequisite_blockers(
        conn,
        root,
        mode="delivered-consistency",
        cycle_id_override=cycle_id,
        historical=True,
    )
    cycle = conn.execute(
        "select * from delivery_cycles where id = ?",
        (resolved_cycle_id,),
    ).fetchone()
    if cycle is None:
        ordered = _ordered_blockers(blockers)
        reason = (
            ordered[0].render()
            if ordered
            else f"historical cycle is missing: {resolved_cycle_id}"
        )
        return HistoricalDeliveryReport(
            blockers=ordered,
            trust=_human_review_decision(reason),
            cycle_id=resolved_cycle_id,
            candidate_sha=candidate,
        )

    baseline = latest_baseline(conn, resolved_cycle_id)
    delivery = conn.execute(
        """
        select * from deliveries
        where cycle_id = ?
        order by created_at desc, id desc limit 1
        """,
        (resolved_cycle_id,),
    ).fetchone()
    gate = conn.execute(
        """
        select * from quality_gates
        where cycle_id = ? and candidate_sha = ? and gate_status = 'active'
        order by sequence desc limit 1
        """,
        (resolved_cycle_id, candidate),
    ).fetchone()
    confirmation, event_blockers = _historical_event_chain(
        conn,
        cycle_id=resolved_cycle_id,
        baseline=baseline,
        gate=gate,
        delivery=delivery,
    )
    blockers.extend(event_blockers)
    from .invariant_checker import check_cycle_invariants

    blockers.extend(
        _invariant_blockers(
            check_cycle_invariants(conn, root, resolved_cycle_id)
        )
    )
    revision = (
        _positive_integer(confirmation.get("project_revision"))
        if confirmation is not None
        else None
    )
    delivery_events = (
        conn.execute(
            """
            select created_at from events
            where event_type = 'delivery_recorded'
              and entity_type = 'delivery' and entity_id = ?
            order by sequence
            """,
            (delivery["id"],),
        ).fetchall()
        if delivery is not None
        else []
    )
    observed_at = (
        str(delivery_events[0]["created_at"] or "")
        if len(delivery_events) == 1
        else str(cycle["closed_at"] or "")
    )
    observed_timestamp = _timestamp(observed_at)

    def expired_at_delivery(value: str) -> bool:
        expiry = _timestamp(value)
        return (
            observed_timestamp is None
            or expiry is None
            or expiry <= observed_timestamp
        )

    policy_issues, policy_trust = _evaluate_local_delivery_policy(
        conn,
        root,
        is_expired=expired_at_delivery,
        observed_at=observed_at,
        include_graph_issues=False,
        cycle_override=cycle,
        candidate_override=candidate,
        revision_override=revision,
        historical=True,
    )
    existing_identities = {
        (blocker.code, blocker.entity_type, blocker.entity_id)
        for blocker in blockers
    }
    for issue in policy_issues:
        policy_blocker = _policy_blocker(issue, resolved_cycle_id)
        identity = (
            policy_blocker.code,
            policy_blocker.entity_type,
            policy_blocker.entity_id,
        )
        if identity in existing_identities:
            continue
        blockers.append(policy_blocker)
        existing_identities.add(identity)
    decision_blocker = _delivery_decision_trust_blocker(
        conn,
        cycle_id=resolved_cycle_id,
        trust=policy_trust,
    )
    if decision_blocker is not None:
        blockers.append(decision_blocker)
    ordered = _ordered_blockers(blockers)
    effective_trust = (
        policy_trust
        if not ordered
        else _human_review_decision(ordered[0].render())
    )
    return HistoricalDeliveryReport(
        blockers=ordered,
        trust=effective_trust,
        cycle_id=resolved_cycle_id,
        candidate_sha=candidate,
    )


def evaluate_historical_cycle_prerequisites(
    conn: sqlite3.Connection,
    root: Path,
    cycle_id: str,
) -> tuple[DeliveryBlocker, ...]:
    """Return the compatibility blocker view of the historical report."""

    return evaluate_historical_cycle_report(
        conn,
        root,
        cycle_id,
    ).blockers


def evaluate_schema30_delivery(
    conn: sqlite3.Connection,
    root: Path,
    *,
    is_expired: Callable[[str], bool],
    observed_at: str | None = None,
) -> tuple[list[str], LocalTrustDecision]:
    """Render legacy schema-30 policy or the active structured decision.

    Schema 30 remains a read-only compatibility contract for migration and
    historical policy tests. Active schema 31 surfaces always use the canonical
    structured prerequisite evaluator.
    """

    project = conn.execute("select schema_version from project where id = 1").fetchone()
    if project is not None and int(project["schema_version"]) == 30:
        return _evaluate_local_delivery_policy(
            conn,
            root,
            is_expired=is_expired,
            observed_at=observed_at,
        )

    report = evaluate_delivery_report(
        conn,
        root,
        mode="record-delivery",
        is_expired=is_expired,
        observed_at=observed_at,
    )
    return [blocker.render() for blocker in report.blockers], report.trust


def evaluate_schema30_delivery_readiness(
    conn: sqlite3.Connection,
    root: Path,
    *,
    is_expired: Callable[[str], bool],
    observed_at: str | None = None,
) -> list[str]:
    project = conn.execute("select schema_version from project where id = 1").fetchone()
    if project is not None and int(project["schema_version"]) == 30:
        issues, _ = _evaluate_local_delivery_policy(
            conn,
            root,
            is_expired=is_expired,
            observed_at=observed_at,
        )
        return issues
    try:
        cycle = current_cycle_row(conn)
        mode: DeliveryEvaluationMode = (
            "delivered-consistency"
            if str(cycle["status"]) == "delivered"
            else "record-delivery"
        )
    except Exception:
        mode = "record-delivery"
    blockers = evaluate_delivery_prerequisites(
        conn,
        root,
        mode=mode,
        is_expired=is_expired,
        observed_at=observed_at,
    )
    return [blocker.render() for blocker in blockers]

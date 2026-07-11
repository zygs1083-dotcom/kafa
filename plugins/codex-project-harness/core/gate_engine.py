"""Fail-closed delivery gate engine."""

from __future__ import annotations

import sqlite3
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from harness_lib import git_dirty, git_head_sha
from .connector_trust import agent_session_payload, ci_payload, external_session_payload, verify_connector_record
from .cycle_ledger import (
    baseline_issues,
    current_candidate_sha,
    current_cycle_row,
    traceability_issues,
    validation_has_test_or_evidence,
)
from .executor import command_matches_template


ACTIVE_REVIEWER_SESSION_STATUSES = {"active", "running", "reported", "verified"}
REVIEWER_SESSION_ROLES = {"qa-reviewer", "reviewer", "architect", "security"}


@dataclass(frozen=True)
class DeliveryDecisionServices:
    """Non-ledger policy required by the delivery decision module."""

    is_expired: Callable[[str], bool]


def _value(row: sqlite3.Row, field: str) -> object:
    return row[field] if field in row.keys() else None


def _fresh_reviewer_issues(conn: sqlite3.Connection, gate: sqlite3.Row, cycle_id: str) -> list[str]:
    if str(_value(gate, "reviewer_context") or "") != "fresh":
        return []
    session_id = str(_value(gate, "reviewer_session_id") or "")
    attestation_id = str(_value(gate, "reviewer_attestation_id") or "")
    issues: list[str] = []
    if not session_id or not attestation_id:
        return ["fresh quality gate requires reviewer session and attestation"]
    session = conn.execute("select * from agent_sessions where session_id = ?", (session_id,)).fetchone()
    if not session:
        issues.append(f"fresh quality gate reviewer session missing: {session_id}")
    else:
        if session["status"] not in ACTIVE_REVIEWER_SESSION_STATUSES:
            issues.append(f"fresh quality gate reviewer session inactive: {session_id} status={session['status']}")
        if session["role"] not in REVIEWER_SESSION_ROLES:
            issues.append(f"fresh quality gate reviewer role invalid: {session_id} role={session['role']}")
    attestation = conn.execute(
        "select * from session_attestations where id = ? and session_id = ?",
        (attestation_id, session_id),
    ).fetchone()
    if not attestation:
        issues.append(f"fresh quality gate reviewer attestation missing or mismatched: {attestation_id}")
    producer = conn.execute(
        """
        select id from tasks
        where cycle_id = ? and submitted_session_id = ? and submitted_session_id != ''
        limit 1
        """,
        (cycle_id, session_id),
    ).fetchone()
    if producer:
        issues.append(f"fresh quality gate reviewer session matches producer session: {session_id}")
    return issues


def _artifact_digest(root: Path, artifact_path: str) -> tuple[bool, str, str]:
    if not artifact_path:
        return False, "", ""
    relative = Path(artifact_path)
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        control_root = (root / ".ai-team").resolve()
        is_runtime_artifact = relative.parts[:2] == (".ai-team", "runtime")
        try:
            candidate.relative_to(control_root)
        except ValueError:
            return False, "", ""
        if not is_runtime_artifact:
            return False, "", ""
    if not candidate.exists() or not candidate.is_file():
        return False, "", ""
    data = candidate.read_bytes()
    if not data:
        return True, "", "empty"
    return True, hashlib.sha256(data).hexdigest(), ""


def _command_row_issues(root: Path, row: sqlite3.Row, current_source_hash: str, *, label: str) -> list[str]:
    command = str(_value(row, "command") or "")
    exit_code = _value(row, "exit_code")
    stdout_sha256 = str(_value(row, "stdout_sha256") or "")
    artifact_path = str(_value(row, "artifact_path") or "")
    source_tree_hash = str(_value(row, "source_tree_hash") or "")
    target_id = str(_value(row, "target_id") or "")
    executed_count = int(_value(row, "executed_count") or 0)
    executed_count_source = str(_value(row, "executed_count_source") or "")
    result_format = str(_value(row, "result_format") or "regex")
    semantic_status = str(_value(row, "semantic_status") or "")
    policy_status = str(_value(row, "policy_status") or "")
    issues: list[str] = []
    if not command:
        issues.append(f"{label} missing command")
    if exit_code is None:
        issues.append(f"{label} missing exit_code")
    elif int(exit_code) != 0:
        issues.append(f"{label} exit_code={exit_code}")
    if not stdout_sha256:
        issues.append(f"{label} missing stdout_sha256")
    if not artifact_path:
        issues.append(f"{label} missing artifact_path")
    else:
        artifact_available, artifact_sha, artifact_issue = _artifact_digest(root, artifact_path)
        if not artifact_available:
            issues.append(f"{label} artifact unavailable: {artifact_path}")
        elif artifact_issue == "empty":
            issues.append(f"{label} artifact is empty: {artifact_path}")
        elif stdout_sha256 and artifact_sha != stdout_sha256:
            issues.append(f"{label} stdout_sha256 mismatch: stored={stdout_sha256} artifact={artifact_sha}")
    if not source_tree_hash:
        issues.append(f"{label} missing source_tree_hash")
    elif not current_source_hash:
        issues.append("delivery requires a committed code identity")
    elif source_tree_hash != current_source_hash:
        issues.append(f"{label} source_tree_hash mismatch: evidence={source_tree_hash} current={current_source_hash}")
    if not target_id:
        issues.append(f"{label} missing target")
    if executed_count <= 0:
        issues.append(f"{label} executed_count={executed_count}")
    if executed_count_source not in {"parsed", "structured"}:
        issues.append(f"{label} executed_count_source={executed_count_source or 'empty'}")
    if result_format != "regex":
        if executed_count_source != "structured":
            issues.append(f"{label} structured result is not authoritative: executed_count_source={executed_count_source or 'empty'}")
        if semantic_status != "pass":
            issues.append(f"{label} semantic_status={semantic_status or 'empty'}")
    if policy_status == "rejected":
        issues.append(f"{label} command policy rejected")
    return issues


def _trust_anchor_issues(conn: sqlite3.Connection, row: sqlite3.Row, root: Path, current_sha: str | None, *, require_external: bool) -> list[str]:
    trust_anchor = str(_value(row, "trust_anchor") or "local-only")
    trust_anchor_id = str(_value(row, "trust_anchor_id") or "")
    issues: list[str] = []
    if require_external and trust_anchor not in {"ci", "external-session"}:
        issues.append(f"requires ci or external-session trust anchor: trust_anchor={trust_anchor}")
    if trust_anchor == "external-session":
        if not trust_anchor_id:
            issues.append("external-session trust anchor requires trust_anchor_id")
        else:
            verification = conn.execute("select * from external_session_verifications where id = ?", (trust_anchor_id,)).fetchone()
            if not verification:
                issues.append(f"missing external-session verification: {trust_anchor_id}")
            else:
                if verification["conclusion"] != "verified":
                    issues.append(f"external-session verification is not verified: {verification['conclusion']}")
                if require_external and verification["origin"] != "connector":
                    issues.append(f"external-session verification origin is not connector: {verification['origin']}")
                if not current_sha:
                    issues.append("external-session verification requires git HEAD")
                elif verification["commit_sha"] != current_sha:
                    issues.append(f"external-session verification sha mismatch: session={verification['commit_sha']} current={current_sha}")
                if require_external and verification["origin"] == "connector":
                    ok, reason = verify_connector_record(
                        root,
                        verification["verification_token"],
                        external_session_payload(
                            verification["session_id"],
                            verification["verifier"],
                            verification["commit_sha"],
                            verification["conclusion"],
                        ),
                    )
                    if not ok:
                        issues.append(f"external-session connector HMAC invalid: {reason}")
    if trust_anchor == "ci":
        if not trust_anchor_id:
            issues.append("ci trust anchor requires trust_anchor_id")
        else:
            ci = conn.execute("select * from ci_verifications where id = ?", (trust_anchor_id,)).fetchone()
            if not ci:
                issues.append(f"missing ci verification: {trust_anchor_id}")
            else:
                if ci["conclusion"] != "success":
                    issues.append(f"ci verification is not success: {ci['conclusion']}")
                if require_external and ci["origin"] != "connector":
                    issues.append(f"ci verification origin is not connector: {ci['origin']}")
                if not current_sha:
                    issues.append("ci verification requires git HEAD")
                elif ci["commit_sha"] != current_sha:
                    issues.append(f"ci verification sha mismatch: ci={ci['commit_sha']} current={current_sha}")
                if require_external and ci["origin"] == "connector":
                    ok, reason = verify_connector_record(
                        root,
                        ci["verification_token"],
                        ci_payload(ci["provider"], ci["run_id"], ci["commit_sha"], ci["conclusion"]),
                    )
                    if not ok:
                        issues.append(f"ci connector HMAC invalid: {reason}")
    return issues


def validation_trusted_command_issues(
    conn: sqlite3.Connection,
    validation: sqlite3.Row,
    root: Path,
    current_source_hash: str,
    current_sha: str | None = None,
    require_external_anchor: bool = False,
) -> list[str]:
    issues = _command_row_issues(root, validation, current_source_hash, label="validation")
    target_id = str(_value(validation, "target_id") or "")
    command = str(_value(validation, "command") or "")
    if target_id:
        target = conn.execute(
            """
            select command_template, gateable, gate_block_reason, requires_sandbox, requires_no_network, result_format
            from test_targets where id = ?
            """,
            (target_id,),
        ).fetchone()
        if not target:
            issues.append(f"validation unknown target: {target_id}")
        elif command and not command_matches_template(command, target["command_template"]):
            issues.append(f"validation command does not match target {target_id}")
        elif int(target["gateable"] or 0) != 1:
            issues.append(f"validation target is not gateable: {target_id} {target['gate_block_reason']}")
        else:
            sandbox_status = str(_value(validation, "sandbox_status") or "")
            no_network = int(_value(validation, "no_network") or 0)
            row_result_format = str(_value(validation, "result_format") or "regex")
            row_count_source = str(_value(validation, "executed_count_source") or "")
            if int(target["requires_sandbox"] or 0) and sandbox_status != "available":
                issues.append("target requires sandbox")
            if int(target["requires_no_network"] or 0) and (sandbox_status != "available" or no_network != 1):
                issues.append("target requires no-network sandbox")
            if str(target["result_format"] or "regex") != "regex":
                if row_result_format != str(target["result_format"]):
                    issues.append(f"validation result_format does not match target {target_id}")
                if row_count_source != "structured":
                    issues.append("target requires structured result")
    issues.extend(_trust_anchor_issues(conn, validation, root, current_sha, require_external=require_external_anchor))
    return issues


def evaluate_delivery_readiness(
    conn: sqlite3.Connection,
    root: Path,
    services: DeliveryDecisionServices,
) -> list[str]:
    issues: list[str] = []
    try:
        cycle = current_cycle_row(conn)
    except Exception as exc:
        return [str(exc)]
    cycle_id = cycle["id"]
    if cycle["status"] not in {"active", "delivered"}:
        issues.append(f"current cycle is not active or delivered: {cycle_id} status={cycle['status']}")

    current_sha = git_head_sha(root)
    current_source_hash = current_candidate_sha(root)

    if conn.execute("select 1 from requirements where cycle_id = ? and status != 'cancelled' limit 1", (cycle_id,)).fetchone():
        issues.extend(traceability_issues(conn))
        issues.extend(baseline_issues(conn))

    stale_rows = conn.execute(
        """
        select source_type, source_id, target_type, target_id, reason
        from invalidations
        where cycle_id = ? and resolved_at is null
        order by created_at, id
        """,
        (cycle_id,),
    ).fetchall()
    for stale in stale_rows:
        issues.append(
            f"stale runtime artifact: {stale['source_type']}:{stale['source_id']} -> {stale['target_type']}:{stale['target_id']} reason={stale['reason']}"
        )

    active_tasks = conn.execute(
        "select id, status from tasks where cycle_id = ? and status not in ('accepted', 'cancelled', 'skipped') order by id",
        (cycle_id,),
    ).fetchall()
    for task in active_tasks:
        issues.append(f"task is not accepted: {task['id']} status={task['status']}")

    validations = conn.execute(
        """
        select id, surface, result, source_tree_hash
        from validations
        where cycle_id = ? and candidate_sha = ? and validation_status = 'active'
        order by created_at, id
        """,
        (cycle_id, current_source_hash),
    ).fetchall()
    if not validations:
        issues.append("delivery requires validation evidence")
    has_content_identity = False
    for validation in validations:
        if validation["result"] != "pass":
            issues.append(f"validation is not pass: {validation['surface']}={validation['result']}")
        if str(validation["source_tree_hash"] or "").startswith("content:"):
            has_content_identity = True
        if validation["result"] == "pass" and not validation["source_tree_hash"]:
            issues.append(f"validation source tree hash is empty: {validation['surface']}")
        if validation["source_tree_hash"] and validation["source_tree_hash"] != current_source_hash:
            issues.append(
                f"validation source tree hash does not match current code: {validation['surface']} "
                f"validation={validation['source_tree_hash']} current={current_source_hash}"
            )
    if not current_sha and not has_content_identity:
        issues.append("delivery requires a committed code identity")

    active_acceptance = conn.execute("select id from acceptance where cycle_id = ? and status != 'cancelled' order by id", (cycle_id,)).fetchall()
    for acceptance in active_acceptance:
        candidates = conn.execute(
            """
            select * from validations
            where cycle_id = ? and candidate_sha = ? and validation_status = 'active'
              and acceptance_id = ? and result = 'pass'
            order by created_at desc, id desc
            """,
            (cycle_id, current_source_hash, acceptance["id"]),
        ).fetchall()
        if not candidates:
            issues.append(f"acceptance has no passing validation for current candidate: {acceptance['id']}")
        else:
            candidate_issues: list[str] = []
            trusted = False
            for validation in candidates:
                if not validation_has_test_or_evidence(conn, validation["id"]):
                    candidate_issues.append(f"{validation['id']}: lacks linked passing test or evidence")
                    continue
                trusted_issues = validation_trusted_command_issues(conn, validation, root, current_source_hash, current_sha)
                if trusted_issues:
                    candidate_issues.append(f"{validation['id']}: {'; '.join(trusted_issues)}")
                    continue
                trusted = True
                break
            if not trusted:
                issues.append(
                    f"acceptance validation lacks trusted command evidence: {acceptance['id']} ({'; '.join(candidate_issues)})"
                )

    risky_failure_modes = conn.execute(
        """
        select id, risk, status, accepted_by, acceptance_reason, acceptance_scope, accepted_revision, expires_at from failure_modes
        where cycle_id = ? and risk in ('high', 'critical')
        order by id
        """,
        (cycle_id,),
    ).fetchall()
    for failure_mode in risky_failure_modes:
        if failure_mode["status"] in {"accepted", "exempt"}:
            if not failure_mode["accepted_by"] or not failure_mode["acceptance_reason"] or not failure_mode["acceptance_scope"] or not failure_mode["accepted_revision"] or not failure_mode["expires_at"]:
                issues.append(f"{failure_mode['risk']} failure mode acceptance is incomplete: {failure_mode['id']}")
            elif services.is_expired(failure_mode["expires_at"]):
                issues.append(f"{failure_mode['risk']} failure mode risk acceptance expired: {failure_mode['id']} expires_at={failure_mode['expires_at']}")
            continue
        covered = conn.execute(
            """
            select v.* from validation_failure_modes vfm
            join validations v on v.id = vfm.validation_id
            where vfm.cycle_id = ? and vfm.failure_mode_id = ? and v.cycle_id = vfm.cycle_id and v.candidate_sha = ?
              and v.validation_status = 'active' and v.result = 'pass'
            order by v.created_at desc, v.id desc
            """,
            (cycle_id, failure_mode["id"], current_source_hash),
        ).fetchall()
        coverage_issues: list[str] = []
        covered_with_evidence = False
        for row in covered:
            if not validation_has_test_or_evidence(conn, row["id"]):
                coverage_issues.append("validation lacks linked test/evidence")
                continue
            trusted_issues = validation_trusted_command_issues(conn, row, root, current_source_hash, current_sha, require_external_anchor=True)
            if trusted_issues:
                coverage_issues.extend(trusted_issues)
                continue
            covered_with_evidence = True
            break
        if not covered_with_evidence:
            suffix = f" ({'; '.join(coverage_issues)})" if coverage_issues else ""
            issues.append(
                f"{failure_mode['risk']} failure mode is not covered by passing validation with linked test/evidence: {failure_mode['id']} status={failure_mode['status']}{suffix}"
            )

    latest_gate = conn.execute(
        """
        select * from quality_gates
        where cycle_id = ? and candidate_sha = ? and gate_status = 'active'
        order by sequence desc limit 1
        """,
        (cycle_id, current_source_hash),
    ).fetchone()
    if not latest_gate:
        issues.append("delivery requires a quality gate record for current candidate")
    else:
        if latest_gate["result"] != "pass":
            issues.append(f"latest quality gate is not pass: {latest_gate['gate']}={latest_gate['result']}")
        if latest_gate["blocking_findings"]:
            issues.append(f"latest quality gate has blocking findings: {latest_gate['blocking_findings']}")
        linked_findings = conn.execute(
            """
            select f.* from quality_gate_findings qgf
            join findings f on f.id = qgf.finding_id
            where qgf.gate_id = ? and f.severity in ('high', 'critical')
              and (f.cycle_id = ? or f.cycle_id = '')
              and (f.candidate_sha = ? or f.candidate_sha = '')
            order by f.id
            """,
            (latest_gate["id"], cycle_id, current_source_hash),
        ).fetchall()
        for finding in linked_findings:
            if finding["status"] in {"resolved", "false-positive"}:
                continue
            if finding["status"] == "accepted":
                waiver_complete = all(
                    [
                        finding["waived_by"], finding["waiver_reason"], finding["waiver_scope"],
                        finding["waived_revision"], finding["waiver_expires_at"],
                    ]
                )
                waiver_matches = (
                    finding["cycle_id"] == cycle_id
                    and finding["candidate_sha"] == current_source_hash
                    and int(finding["waived_revision"] or 0) == int(latest_gate["project_revision"])
                )
                if waiver_complete and waiver_matches and not services.is_expired(finding["waiver_expires_at"]):
                    continue
            issues.append(
                f"linked {finding['severity']} finding blocks delivery: {finding['id']} status={finding['status']}"
            )
        issues.extend(_fresh_reviewer_issues(conn, latest_gate, cycle_id))
        high_risk_present = conn.execute("select 1 from failure_modes where cycle_id = ? and risk in ('high', 'critical') limit 1", (cycle_id,)).fetchone()
        if high_risk_present and latest_gate["reviewer_context"] == "same-context-degraded":
            issues.append("high/critical risk delivery requires fresh or external quality gate reviewer context")
        if high_risk_present:
            review_trust_level = str(_value(latest_gate, "review_trust_level") or "local-only")
            reviewer_attestation_id = str(_value(latest_gate, "reviewer_attestation_id") or "")
            if review_trust_level != "connector" or not reviewer_attestation_id:
                issues.append("high/critical risk delivery requires connector(HMAC) reviewer session attestation")
            else:
                attestation = conn.execute("select * from session_attestations where id = ?", (reviewer_attestation_id,)).fetchone()
                if not attestation:
                    issues.append(f"missing reviewer session attestation: {reviewer_attestation_id}")
                elif attestation["origin"] != "connector":
                    issues.append(f"reviewer session attestation origin is not connector: {attestation['origin']}")
                else:
                    ok, reason = verify_connector_record(
                        root,
                        attestation["verification_token"],
                        agent_session_payload(attestation["session_id"], attestation["agent_id"], attestation["role"], attestation["context_id"]),
                    )
                    if not ok:
                        issues.append(f"reviewer session connector HMAC invalid: {reason}")
        if current_sha:
            if git_dirty(root):
                issues.append("git worktree is dirty after quality gate")
            if latest_gate["diff_hash"] and latest_gate["diff_hash"] != current_source_hash:
                issues.append(
                    f"latest quality gate source tree hash does not match current code: gate={latest_gate['diff_hash']} current={current_source_hash}"
                )
    return issues

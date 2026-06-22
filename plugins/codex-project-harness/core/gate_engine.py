"""Fail-closed delivery gate engine."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from harness_lib import git_dirty, git_head_sha, git_source_tree_hash


def evaluate_delivery_readiness(conn: sqlite3.Connection, root: Path) -> list[str]:
    from harness_db import baseline_issues, is_expired, traceability_issues, validation_has_test_or_evidence

    issues: list[str] = []
    if conn.execute("select 1 from requirements where status != 'cancelled' limit 1").fetchone():
        issues.extend(traceability_issues(conn))
        issues.extend(baseline_issues(conn))

    stale_rows = conn.execute(
        "select source_type, source_id, target_type, target_id, reason from invalidations where resolved_at is null order by created_at, id"
    ).fetchall()
    for stale in stale_rows:
        issues.append(
            f"stale runtime artifact: {stale['source_type']}:{stale['source_id']} -> {stale['target_type']}:{stale['target_id']} reason={stale['reason']}"
        )

    active_tasks = conn.execute(
        "select id, status from tasks where status not in ('accepted', 'cancelled', 'skipped') order by id"
    ).fetchall()
    for task in active_tasks:
        issues.append(f"task is not accepted: {task['id']} status={task['status']}")

    current_sha = git_head_sha(root)
    current_source_hash = (git_source_tree_hash(root) or "") if current_sha else ""
    validations = conn.execute("select id, surface, result, source_tree_hash from validations order by created_at, id").fetchall()
    if not validations:
        issues.append("delivery requires validation evidence")
    for validation in validations:
        if validation["result"] != "pass":
            issues.append(f"validation is not pass: {validation['surface']}={validation['result']}")
        if current_sha and validation["source_tree_hash"] and validation["source_tree_hash"] != current_source_hash:
            issues.append(
                f"validation source tree hash does not match current code: {validation['surface']} "
                f"validation={validation['source_tree_hash']} current={current_source_hash}"
            )

    active_acceptance = conn.execute("select id from acceptance where status != 'cancelled' order by id").fetchall()
    for acceptance in active_acceptance:
        validation = conn.execute(
            """
            select id from validations
            where acceptance_id = ? and result = 'pass'
            order by created_at desc, id desc
            limit 1
            """,
            (acceptance["id"],),
        ).fetchone()
        if not validation:
            issues.append(f"acceptance has no passing validation: {acceptance['id']}")
        elif not validation_has_test_or_evidence(conn, validation["id"]):
            issues.append(f"acceptance validation lacks linked passing test or evidence: {acceptance['id']}")

    risky_failure_modes = conn.execute(
        """
        select id, risk, status, accepted_by, acceptance_reason, acceptance_scope, accepted_revision, expires_at from failure_modes
        where risk in ('high', 'critical')
        order by id
        """
    ).fetchall()
    for failure_mode in risky_failure_modes:
        if failure_mode["status"] in {"accepted", "exempt"}:
            if not failure_mode["accepted_by"] or not failure_mode["acceptance_reason"] or not failure_mode["acceptance_scope"] or not failure_mode["accepted_revision"] or not failure_mode["expires_at"]:
                issues.append(f"{failure_mode['risk']} failure mode acceptance is incomplete: {failure_mode['id']}")
            elif is_expired(failure_mode["expires_at"]):
                issues.append(f"{failure_mode['risk']} failure mode risk acceptance expired: {failure_mode['id']} expires_at={failure_mode['expires_at']}")
            continue
        covered = conn.execute(
            """
            select v.id from validation_failure_modes vfm
            join validations v on v.id = vfm.validation_id
            where vfm.failure_mode_id = ? and v.result = 'pass'
            order by v.created_at desc, v.id desc
            """,
            (failure_mode["id"],),
        ).fetchall()
        covered_with_evidence = any(validation_has_test_or_evidence(conn, row["id"]) for row in covered)
        if not covered_with_evidence:
            issues.append(
                f"{failure_mode['risk']} failure mode is not covered by passing validation with linked test/evidence: {failure_mode['id']} status={failure_mode['status']}"
            )

    latest_gate = conn.execute("select * from quality_gates order by created_at desc, id desc limit 1").fetchone()
    if not latest_gate:
        issues.append("delivery requires a quality gate record")
    else:
        if latest_gate["result"] != "pass":
            issues.append(f"latest quality gate is not pass: {latest_gate['gate']}={latest_gate['result']}")
        if latest_gate["blocking_findings"]:
            issues.append(f"latest quality gate has blocking findings: {latest_gate['blocking_findings']}")
        high_risk_present = conn.execute("select 1 from failure_modes where risk in ('high', 'critical') limit 1").fetchone()
        if high_risk_present and latest_gate["reviewer_context"] == "same-context-degraded":
            issues.append("high/critical risk delivery requires fresh or external quality gate reviewer context")
        if current_sha:
            if git_dirty(root):
                issues.append("git worktree is dirty after quality gate")
            if latest_gate["diff_hash"] and latest_gate["diff_hash"] != current_source_hash:
                issues.append(
                    f"latest quality gate source tree hash does not match current code: gate={latest_gate['diff_hash']} current={current_source_hash}"
                )
    return issues

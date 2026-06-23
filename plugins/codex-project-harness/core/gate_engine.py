"""Fail-closed delivery gate engine."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from harness_lib import git_dirty, git_head_sha, git_source_tree_hash
from .executor import command_matches_template


def _value(row: sqlite3.Row, field: str) -> object:
    return row[field] if field in row.keys() else None


def _artifact_is_available(root: Path, artifact_path: str) -> bool:
    if not artifact_path:
        return False
    candidate = (root / artifact_path).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return False
    return candidate.exists() and candidate.is_file()


def _command_row_issues(root: Path, row: sqlite3.Row, current_source_hash: str, *, label: str) -> list[str]:
    command = str(_value(row, "command") or "")
    exit_code = _value(row, "exit_code")
    stdout_sha256 = str(_value(row, "stdout_sha256") or "")
    artifact_path = str(_value(row, "artifact_path") or "")
    source_tree_hash = str(_value(row, "source_tree_hash") or "")
    target_id = str(_value(row, "target_id") or "")
    executed_count = int(_value(row, "executed_count") or 0)
    executed_count_source = str(_value(row, "executed_count_source") or "")
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
    elif not _artifact_is_available(root, artifact_path):
        issues.append(f"{label} artifact unavailable: {artifact_path}")
    if current_source_hash and source_tree_hash != current_source_hash:
        issues.append(f"{label} source_tree_hash mismatch: evidence={source_tree_hash} current={current_source_hash}")
    if not target_id:
        issues.append(f"{label} missing target")
    if executed_count <= 0:
        issues.append(f"{label} executed_count={executed_count}")
    if executed_count_source != "parsed":
        issues.append(f"{label} executed_count_source={executed_count_source or 'empty'}")
    if policy_status == "rejected":
        issues.append(f"{label} command policy rejected")
    return issues


def _trust_anchor_issues(conn: sqlite3.Connection, row: sqlite3.Row, current_sha: str | None, *, require_external: bool) -> list[str]:
    trust_anchor = str(_value(row, "trust_anchor") or "local-only")
    trust_anchor_id = str(_value(row, "trust_anchor_id") or "")
    issues: list[str] = []
    if require_external and trust_anchor not in {"ci", "external-session"}:
        issues.append(f"requires ci or external-session trust anchor: trust_anchor={trust_anchor}")
    if trust_anchor == "external-session" and not trust_anchor_id:
        issues.append("external-session trust anchor requires trust_anchor_id")
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
                if not current_sha:
                    issues.append("ci verification requires git HEAD")
                elif ci["commit_sha"] != current_sha:
                    issues.append(f"ci verification sha mismatch: ci={ci['commit_sha']} current={current_sha}")
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
        target = conn.execute("select command_template, gateable, gate_block_reason from test_targets where id = ?", (target_id,)).fetchone()
        if not target:
            issues.append(f"validation unknown target: {target_id}")
        elif command and not command_matches_template(command, target["command_template"]):
            issues.append(f"validation command does not match target {target_id}")
        elif int(target["gateable"] or 0) != 1:
            issues.append(f"validation target is not gateable: {target_id} {target['gate_block_reason']}")
    issues.extend(_trust_anchor_issues(conn, validation, current_sha, require_external=require_external_anchor))
    return issues


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
        else:
            validation_row = conn.execute("select * from validations where id = ?", (validation["id"],)).fetchone()
            trusted_issues = validation_trusted_command_issues(conn, validation_row, root, current_source_hash, current_sha)
            if trusted_issues:
                issues.append(
                    f"acceptance validation lacks trusted command evidence: {acceptance['id']} ({'; '.join(trusted_issues)})"
                )

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
            select v.* from validation_failure_modes vfm
            join validations v on v.id = vfm.validation_id
            where vfm.failure_mode_id = ? and v.result = 'pass'
            order by v.created_at desc, v.id desc
            """,
            (failure_mode["id"],),
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

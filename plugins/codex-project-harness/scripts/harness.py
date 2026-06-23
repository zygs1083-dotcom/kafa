#!/usr/bin/env python3
"""Unified Codex Project Harness runtime CLI."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from core.api import (
    HarnessError,
    accept_task,
    adapter_plan,
    adapter_reconcile,
    adapter_transition,
    add_agent_capability,
    add_acceptance,
    add_failure_mode,
    add_requirement,
    add_task,
    add_executor_prefix,
    add_test_target,
    baseline_diff,
    baseline_validate,
    block_task,
    claim_task,
    complete_task,
    confirm_scope,
    connection,
    create_checkpoint,
    dispatch_claim_next,
    dispatch_file_claim_add,
    dispatch_file_claim_list,
    dispatch_file_claim_release,
    dispatch_export_csv,
    dispatch_import_csv,
    dispatch_integrate,
    dispatch_plan,
    dispatch_provider_cancel,
    dispatch_provider_collect,
    dispatch_provider_reconcile,
    dispatch_provider_start,
    dispatch_provider_status,
    dispatch_recover_stale,
    dispatch_run,
    dispatch_status,
    dispatch_verify_attempt,
    doctor,
    export_checkpoint,
    export_events,
    freeze_baseline,
    import_checkpoint,
    invariant_validate,
    init_runtime,
    install_agents,
    kernel_doctor,
    link_requirement_acceptance,
    link_task_test_target,
    list_checkpoints,
    list_executor_prefixes,
    list_test_targets,
    migrate,
    ready_tasks,
    record_adapter,
    record_ci_verification,
    record_decision,
    record_delivery,
    record_evidence,
    record_external_session_verification,
    record_finding,
    record_gate,
    record_session_attestation,
    record_test,
    record_validation,
    projection_rebuild,
    heartbeat_task,
    recover_stale_leases,
    release_task,
    repair,
    close_agent_session,
    session_status_lines,
    start_task,
    status_lines,
    submit_task,
    sweep_expired_risks,
    trace_show,
    trace_validate,
    transition_phase,
    update_task,
    validate_events,
    validate_runtime,
    review_task,
    run_idempotent,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="Target project root")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_request_id(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument("--request-id")

    init_parser = sub.add_parser("init")
    init_parser.add_argument("--dry-run", action="store_true")
    sub.add_parser("status")
    sub.add_parser("doctor")
    validate_parser = sub.add_parser("validate")
    validate_parser.add_argument("--delivery", action="store_true")
    repair_parser = sub.add_parser("repair")
    repair_parser.add_argument("--dry-run", action="store_true")
    repair_parser.add_argument("--clear-invariant", default="")
    repair_parser.add_argument("--confirm", default="")

    migrate_parser = sub.add_parser("migrate")
    migrate_parser.add_argument("--from-version", required=True)
    migrate_parser.add_argument("--to-version", type=int, required=True)
    migrate_parser.add_argument("--dry-run", action="store_true")

    phase = sub.add_parser("phase")
    phase.add_argument("phase")
    phase.add_argument("--status")
    phase.add_argument("--owner")
    add_request_id(phase)

    scope = sub.add_parser("scope")
    scope_sub = scope.add_subparsers(dest="scope_command", required=True)
    scope_confirm = scope_sub.add_parser("confirm")
    scope_confirm.add_argument("--by", required=True)
    scope_confirm.add_argument("--summary", required=True)
    add_request_id(scope_confirm)

    baseline = sub.add_parser("baseline")
    baseline_sub = baseline.add_subparsers(dest="baseline_command", required=True)
    baseline_freeze = baseline_sub.add_parser("freeze")
    baseline_freeze.add_argument("--id", required=True)
    baseline_freeze.add_argument("--summary", required=True)
    baseline_freeze.add_argument("--by", default="")
    add_request_id(baseline_freeze)
    baseline_diff_parser = baseline_sub.add_parser("diff")
    baseline_diff_parser.add_argument("--from", dest="from_id", required=True)
    baseline_diff_parser.add_argument("--to", default="current")
    baseline_sub.add_parser("validate")

    acceptance = sub.add_parser("acceptance")
    acceptance_sub = acceptance.add_subparsers(dest="acceptance_command", required=True)
    acceptance_add = acceptance_sub.add_parser("add")
    acceptance_add.add_argument("--id", required=True)
    acceptance_add.add_argument("--criterion", required=True)
    acceptance_add.add_argument("--priority", default="")
    acceptance_add.add_argument("--tool-link", default="")
    add_request_id(acceptance_add)

    requirement = sub.add_parser("requirement")
    requirement_sub = requirement.add_subparsers(dest="requirement_command", required=True)
    requirement_add = requirement_sub.add_parser("add")
    requirement_add.add_argument("--id", required=True)
    requirement_add.add_argument("--kind", required=True, choices=["goal", "functional", "non-functional", "non-goal", "assumption", "open-question", "architecture"])
    requirement_add.add_argument("--body", required=True)
    requirement_add.add_argument("--priority", default="")
    requirement_add.add_argument("--status", default="active")
    requirement_add.add_argument("--tool-link", default="")
    add_request_id(requirement_add)
    requirement_link = requirement_sub.add_parser("link")
    requirement_link.add_argument("--requirement", required=True)
    requirement_link.add_argument("--acceptance", required=True)
    add_request_id(requirement_link)

    trace = sub.add_parser("trace")
    trace_sub = trace.add_subparsers(dest="trace_command", required=True)
    trace_show_parser = trace_sub.add_parser("show")
    trace_show_parser.add_argument("--requirement")
    trace_sub.add_parser("validate")

    fm = sub.add_parser("failure-mode")
    fm_sub = fm.add_subparsers(dest="failure_mode_command", required=True)
    fm_add = fm_sub.add_parser("add")
    fm_add.add_argument("--id", required=True)
    fm_add.add_argument("--feature", required=True)
    fm_add.add_argument("--scenario", required=True)
    fm_add.add_argument("--trigger", required=True)
    fm_add.add_argument("--expected", required=True)
    fm_add.add_argument("--risk", default="medium", choices=["low", "medium", "high", "critical"])
    fm_add.add_argument("--status", default="identified", choices=["identified", "accepted", "exempt"])
    fm_add.add_argument("--acceptance", default="")
    fm_add.add_argument("--recovery", default="")
    fm_add.add_argument("--data-safety", default="")
    fm_add.add_argument("--accepted-by", default="")
    fm_add.add_argument("--acceptance-reason", default="")
    fm_add.add_argument("--acceptance-scope", default="")
    fm_add.add_argument("--expires-at", default="")
    add_request_id(fm_add)

    task = sub.add_parser("task")
    task_sub = task.add_subparsers(dest="task_command", required=True)
    task_add = task_sub.add_parser("add")
    task_add.add_argument("--id", required=True)
    task_add.add_argument("--task", required=True)
    task_add.add_argument("--owner", default="unassigned")
    task_add.add_argument("--acceptance", default="")
    task_add.add_argument("--failure-mode", action="append", default=[])
    task_add.add_argument("--depends-on", default="")
    task_add.add_argument("--status", default="ready")
    task_add.add_argument("--evidence", default="")
    task_add.add_argument("--tool-link", default="")
    add_request_id(task_add)

    task_update = task_sub.add_parser("update")
    task_update.add_argument("id")
    task_update.add_argument("--depends-on")
    task_update.add_argument("--status")
    add_request_id(task_update)

    task_sub.add_parser("next")

    task_claim = task_sub.add_parser("claim")
    task_claim.add_argument("id")
    task_claim.add_argument("--agent", required=True)
    task_claim.add_argument("--expected-revision", type=int, required=True)
    add_request_id(task_claim)

    task_heartbeat = task_sub.add_parser("heartbeat")
    task_heartbeat.add_argument("id")
    task_heartbeat.add_argument("--agent", required=True)
    task_heartbeat.add_argument("--lease-token", required=True)
    task_heartbeat.add_argument("--expected-revision", type=int, required=True)
    task_heartbeat.add_argument("--fence", type=int)
    add_request_id(task_heartbeat)

    task_recover = task_sub.add_parser("recover-stale")
    add_request_id(task_recover)

    task_start = task_sub.add_parser("start")
    task_start.add_argument("id")
    task_start.add_argument("--agent", required=True)
    task_start.add_argument("--lease-token", required=True)
    task_start.add_argument("--expected-revision", type=int, required=True)
    task_start.add_argument("--fence", type=int)
    add_request_id(task_start)

    task_submit = task_sub.add_parser("submit")
    task_submit.add_argument("id")
    task_submit.add_argument("--agent", required=True)
    task_submit.add_argument("--lease-token", required=True)
    task_submit.add_argument("--expected-revision", type=int, required=True)
    task_submit.add_argument("--fence", type=int)
    task_submit.add_argument("--evidence", required=True)
    task_submit.add_argument("--session-id", default="")
    add_request_id(task_submit)

    task_complete = task_sub.add_parser("complete")
    task_complete.add_argument("id")
    task_complete.add_argument("--agent", required=True)
    task_complete.add_argument("--lease-token", required=True)
    task_complete.add_argument("--expected-revision", type=int, required=True)
    task_complete.add_argument("--fence", type=int)
    task_complete.add_argument("--evidence", required=True)
    task_complete.add_argument("--session-id", default="")
    add_request_id(task_complete)

    task_review = task_sub.add_parser("review")
    task_review.add_argument("id")
    task_review.add_argument("--agent", required=True)
    task_review.add_argument("--expected-revision", type=int, required=True)
    task_review.add_argument("--session-id", default="")
    add_request_id(task_review)

    task_accept = task_sub.add_parser("accept")
    task_accept.add_argument("id")
    task_accept.add_argument("--agent", required=True)
    task_accept.add_argument("--lease-token", required=True)
    task_accept.add_argument("--expected-revision", type=int, required=True)
    task_accept.add_argument("--fence", type=int)
    task_accept.add_argument("--evidence", required=True)
    task_accept.add_argument("--session-id", default="")
    add_request_id(task_accept)

    task_block = task_sub.add_parser("block")
    task_block.add_argument("id")
    task_block.add_argument("--agent", required=True)
    task_block.add_argument("--lease-token", required=True)
    task_block.add_argument("--expected-revision", type=int, required=True)
    task_block.add_argument("--fence", type=int)
    task_block.add_argument("--reason", required=True)
    add_request_id(task_block)

    task_release = task_sub.add_parser("release")
    task_release.add_argument("id")
    task_release.add_argument("--agent", required=True)
    task_release.add_argument("--lease-token", required=True)
    task_release.add_argument("--expected-revision", type=int, required=True)
    task_release.add_argument("--fence", type=int)
    add_request_id(task_release)

    validation = sub.add_parser("validation")
    validation_sub = validation.add_subparsers(dest="validation_command", required=True)
    validation_record = validation_sub.add_parser("record")
    validation_record.add_argument("--surface", required=True)
    validation_record.add_argument("--acceptance", default="")
    validation_record.add_argument("--commands", default="")
    validation_record.add_argument("--findings", required=True)
    validation_record.add_argument("--result", required=True, choices=["pass", "fail", "blocked", "partial"])
    validation_record.add_argument("--risk", default="")
    validation_record.add_argument("--failure-mode", action="append", default=[])
    validation_record.add_argument("--test", action="append", default=[])
    validation_record.add_argument("--evidence", action="append", default=[])
    validation_record.add_argument("--command", dest="validation_command_text", default="")
    validation_record.add_argument("--exit-code", type=int)
    validation_record.add_argument("--stdout-sha256", default="")
    validation_record.add_argument("--artifact-path", default="")
    validation_record.add_argument("--target", default="")
    validation_record.add_argument("--executed-count", type=int)
    validation_record.add_argument("--allow-unlisted", action="store_true")
    validation_record.add_argument("--no-network", action="store_true")
    validation_record.add_argument("--trust-anchor", default="local-only", choices=["local-only", "human-confirmed", "external-session", "ci"])
    validation_record.add_argument("--trust-anchor-id", default="")
    validation_record.add_argument("--sandbox-profile", default="none", choices=["none", "no-network"])
    validation_record.add_argument("--reason", default="")
    validation_record.add_argument("--code-identity", default="auto", choices=["auto", "git", "content-hash"])
    add_request_id(validation_record)

    test_target = sub.add_parser("test-target")
    test_target_sub = test_target.add_subparsers(dest="test_target_command", required=True)
    test_target_add = test_target_sub.add_parser("add")
    test_target_add.add_argument("--id", required=True)
    test_target_add.add_argument("--kind", required=True, choices=["unit", "integration", "lint", "build"])
    test_target_add.add_argument("--command-template", required=True)
    test_target_add.add_argument("--description", default="")
    add_request_id(test_target_add)
    test_target_link = test_target_sub.add_parser("link")
    test_target_link.add_argument("--task", required=True)
    test_target_link.add_argument("--target", required=True)
    add_request_id(test_target_link)
    test_target_sub.add_parser("list")

    decision = sub.add_parser("decision")
    decision_sub = decision.add_subparsers(dest="decision_command", required=True)
    decision_record = decision_sub.add_parser("record")
    decision_record.add_argument("--decision", required=True)
    decision_record.add_argument("--reason", required=True)
    add_request_id(decision_record)

    evidence = sub.add_parser("evidence")
    evidence_sub = evidence.add_subparsers(dest="evidence_command", required=True)
    evidence_record = evidence_sub.add_parser("record")
    evidence_record.add_argument("--id", required=True)
    evidence_record.add_argument("--kind", required=True)
    evidence_record.add_argument("--summary", required=True)
    evidence_record.add_argument("--uri", default="")
    evidence_record.add_argument("--hash", default="")
    evidence_record.add_argument("--command", dest="evidence_command_text", default="")
    evidence_record.add_argument("--exit-code", type=int)
    evidence_record.add_argument("--stdout-sha256", default="")
    evidence_record.add_argument("--artifact-path", default="")
    evidence_record.add_argument("--target", default="")
    evidence_record.add_argument("--executed-count", type=int)
    evidence_record.add_argument("--allow-unlisted", action="store_true")
    evidence_record.add_argument("--no-network", action="store_true")
    evidence_record.add_argument("--trust-anchor", default="local-only", choices=["local-only", "human-confirmed", "external-session", "ci"])
    evidence_record.add_argument("--trust-anchor-id", default="")
    evidence_record.add_argument("--sandbox-profile", default="none", choices=["none", "no-network"])
    evidence_record.add_argument("--reason", default="")
    evidence_record.add_argument("--code-identity", default="auto", choices=["auto", "git", "content-hash"])
    add_request_id(evidence_record)

    test = sub.add_parser("test")
    test_sub = test.add_subparsers(dest="test_command", required=True)
    test_record = test_sub.add_parser("record")
    test_record.add_argument("--id", required=True)
    test_record.add_argument("--surface", required=True)
    test_record.add_argument("--command", dest="test_command_text", default="")
    test_record.add_argument("--result", required=True, choices=["pass", "fail", "blocked", "partial"])
    test_record.add_argument("--evidence", default="")
    add_request_id(test_record)

    finding = sub.add_parser("finding")
    finding_sub = finding.add_subparsers(dest="finding_command", required=True)
    finding_record = finding_sub.add_parser("record")
    finding_record.add_argument("--id", required=True)
    finding_record.add_argument("--surface", required=True)
    finding_record.add_argument("--severity", required=True, choices=["low", "medium", "high", "critical"])
    finding_record.add_argument("--status", required=True, choices=["open", "resolved", "accepted", "false-positive"])
    finding_record.add_argument("--summary", required=True)
    finding_record.add_argument("--evidence", default="")
    add_request_id(finding_record)

    gate = sub.add_parser("gate")
    gate_sub = gate.add_subparsers(dest="gate_command", required=True)
    gate_record = gate_sub.add_parser("record")
    gate_record.add_argument("--reviewer-context", required=True, choices=["fresh", "same-context-degraded", "external"])
    gate_record.add_argument("--result", required=True, choices=["pass", "fail", "conditional", "blocked"])
    gate_record.add_argument("--gate", default="independent_qa")
    gate_record.add_argument("--commands", default="")
    gate_record.add_argument("--evidence", default="")
    gate_record.add_argument("--blocking-findings", default="")
    gate_record.add_argument("--residual-risk", default="")
    gate_record.add_argument("--finding", action="append", default=[])
    gate_record.add_argument("--reviewer-session-id", default="")
    gate_record.add_argument("--reviewer-attestation-id", default="")
    add_request_id(gate_record)

    delivery = sub.add_parser("delivery")
    delivery_sub = delivery.add_subparsers(dest="delivery_command", required=True)
    delivery_record = delivery_sub.add_parser("record")
    delivery_record.add_argument("--scope", required=True)
    delivery_record.add_argument("--acceptance", default="")
    delivery_record.add_argument("--changed-files", default="")
    delivery_record.add_argument("--validation", default="")
    delivery_record.add_argument("--qa", default="")
    delivery_record.add_argument("--failure-mode-coverage", default="")
    delivery_record.add_argument("--quality-gate", default="")
    delivery_record.add_argument("--data-config-notes", default="")
    delivery_record.add_argument("--collaboration-links", default="")
    delivery_record.add_argument("--known-gaps", default="")
    delivery_record.add_argument("--handoff", default="")
    add_request_id(delivery_record)

    adapter = sub.add_parser("adapter")
    adapter_sub = adapter.add_subparsers(dest="adapter_command", required=True)
    adapter_record = adapter_sub.add_parser("record")
    adapter_record.add_argument("--tool", required=True)
    adapter_record.add_argument("--mode", required=True, choices=["read-only", "draft-write", "write-confirm", "write-auto", "disabled"])
    adapter_record.add_argument("--artifact", required=True)
    adapter_record.add_argument("--external-id", default="")
    adapter_record.add_argument("--external-link", default="")
    adapter_record.add_argument("--idempotency-key", required=True)
    adapter_record.add_argument("--evidence", default="")
    adapter_record.add_argument("--fallback", default="")
    adapter_record.add_argument("--confirmation-needed", default="no")
    add_request_id(adapter_record)
    adapter_plan_parser = adapter_sub.add_parser("plan")
    adapter_plan_parser.add_argument("--tool", required=True)
    adapter_plan_parser.add_argument("--mode", required=True, choices=["read-only", "draft-write", "write-confirm", "write-auto", "disabled"])
    adapter_plan_parser.add_argument("--artifact", required=True)
    adapter_plan_parser.add_argument("--action", required=True)
    adapter_plan_parser.add_argument("--payload-json", default="{}")
    adapter_plan_parser.add_argument("--idempotency-key", default="")
    add_request_id(adapter_plan_parser)
    adapter_draft = adapter_sub.add_parser("draft")
    adapter_draft.add_argument("--id", required=True)
    add_request_id(adapter_draft)
    adapter_confirm = adapter_sub.add_parser("confirm")
    adapter_confirm.add_argument("--id", required=True)
    adapter_confirm.add_argument("--confirmation", default="confirmed")
    add_request_id(adapter_confirm)
    adapter_complete = adapter_sub.add_parser("complete")
    adapter_complete.add_argument("--id", required=True)
    adapter_complete.add_argument("--external-id", default="")
    adapter_complete.add_argument("--external-link", default="")
    add_request_id(adapter_complete)
    adapter_ci = adapter_sub.add_parser("ci-verify")
    adapter_ci.add_argument("--provider", required=True)
    adapter_ci.add_argument("--run-id", required=True)
    adapter_ci.add_argument("--conclusion", required=True, choices=["success", "failure", "cancelled", "skipped"])
    adapter_ci.add_argument("--commit-sha", required=True)
    adapter_ci.add_argument("--external-link", default="")
    adapter_ci.add_argument("--origin", default="manual", choices=["manual", "connector"])
    adapter_ci.add_argument("--verification-token", default="")
    add_request_id(adapter_ci)
    adapter_session = adapter_sub.add_parser("external-session-verify")
    adapter_session.add_argument("--session-id", required=True)
    adapter_session.add_argument("--verifier", required=True)
    adapter_session.add_argument("--conclusion", required=True, choices=["verified", "failed"])
    adapter_session.add_argument("--commit-sha", required=True)
    adapter_session.add_argument("--external-link", default="")
    adapter_session.add_argument("--origin", default="manual", choices=["manual", "connector"])
    adapter_session.add_argument("--verification-token", default="")
    add_request_id(adapter_session)
    adapter_sub.add_parser("reconcile")

    risk = sub.add_parser("risk")
    risk_sub = risk.add_subparsers(dest="risk_command", required=True)
    risk_sweep = risk_sub.add_parser("sweep-expired")
    add_request_id(risk_sweep)

    checkpoint = sub.add_parser("checkpoint")
    checkpoint_sub = checkpoint.add_subparsers(dest="checkpoint_command", required=True)
    checkpoint_create = checkpoint_sub.add_parser("create")
    checkpoint_create.add_argument("--label", required=True)
    checkpoint_sub.add_parser("list")
    checkpoint_export = checkpoint_sub.add_parser("export")
    checkpoint_export.add_argument("--out", required=True)
    checkpoint_import = checkpoint_sub.add_parser("import")
    checkpoint_import.add_argument("--file", required=True)
    checkpoint_import.add_argument("--dry-run", action="store_true")
    checkpoint_import.add_argument("--apply", action="store_true")

    event = sub.add_parser("event")
    event_sub = event.add_subparsers(dest="event_command", required=True)
    event_export = event_sub.add_parser("export")
    event_export.add_argument("--out", required=True)
    event_sub.add_parser("validate")

    agent = sub.add_parser("agent")
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)
    agent_capability = agent_sub.add_parser("capability")
    agent_capability_sub = agent_capability.add_subparsers(dest="agent_capability_command", required=True)
    agent_capability_add = agent_capability_sub.add_parser("add")
    agent_capability_add.add_argument("--agent", required=True)
    agent_capability_add.add_argument("--capability", required=True)
    add_request_id(agent_capability_add)

    agents = sub.add_parser("agents")
    agents_sub = agents.add_subparsers(dest="agents_command", required=True)
    agents_install = agents_sub.add_parser("install")
    agents_install.add_argument("--dir", default=".codex/agents")
    agents_install.add_argument("--force", action="store_true")
    add_request_id(agents_install)

    session = sub.add_parser("session")
    session_sub = session.add_subparsers(dest="session_command", required=True)
    session_attest = session_sub.add_parser("attest")
    session_attest.add_argument("--session-id", required=True)
    session_attest.add_argument("--agent", required=True)
    session_attest.add_argument("--role", required=True, choices=["developer", "qa-reviewer", "reviewer", "architect", "product", "security"])
    session_attest.add_argument("--context-id", required=True)
    session_attest.add_argument("--provider-session-id", default="")
    session_attest.add_argument("--origin", default="manual", choices=["manual", "connector"])
    session_attest.add_argument("--verification-token", default="")
    add_request_id(session_attest)
    session_status = session_sub.add_parser("status")
    session_status.add_argument("--agent", default="")
    session_close = session_sub.add_parser("close")
    session_close.add_argument("--session-id", required=True)
    add_request_id(session_close)

    dispatch = sub.add_parser("dispatch")
    dispatch_sub = dispatch.add_subparsers(dest="dispatch_command", required=True)
    dispatch_plan_parser = dispatch_sub.add_parser("plan")
    dispatch_plan_parser.add_argument("--scope", required=True)
    add_request_id(dispatch_plan_parser)
    dispatch_export = dispatch_sub.add_parser("export-csv")
    dispatch_export.add_argument("run_id")
    dispatch_export.add_argument("--out-dir", default="")
    dispatch_export.add_argument("--max-concurrency", type=int, default=6)
    dispatch_export.add_argument("--max-runtime-seconds", type=int, default=1800)
    add_request_id(dispatch_export)
    dispatch_import = dispatch_sub.add_parser("import-csv")
    dispatch_import.add_argument("run_id")
    dispatch_import.add_argument("--result", required=True)
    add_request_id(dispatch_import)
    dispatch_verify = dispatch_sub.add_parser("verify-attempt")
    dispatch_verify.add_argument("--run-id", required=True)
    dispatch_verify.add_argument("--task", required=True)
    dispatch_verify.add_argument("--runner", default="local", choices=["local", "container"])
    add_request_id(dispatch_verify)
    dispatch_claim = dispatch_sub.add_parser("claim-next")
    dispatch_claim.add_argument("--agent", required=True)
    dispatch_run = dispatch_sub.add_parser("run")
    dispatch_run.add_argument("--agent", required=True)
    dispatch_run.add_argument("--target", default="")
    dispatch_run.add_argument("--command", dest="dispatch_command_text", required=True)
    dispatch_run.add_argument("--timeout", type=int, default=120)
    dispatch_run.add_argument("--allow-unlisted", action="store_true")
    dispatch_run.add_argument("--reason", default="")
    dispatch_run.add_argument("--no-network", action="store_true")
    dispatch_run.add_argument("--sandbox-profile", default="none", choices=["none", "no-network"])
    dispatch_run.add_argument("--executed-count", type=int)
    dispatch_run.add_argument("--code-identity", default="auto", choices=["auto", "git", "content-hash"])
    dispatch_run.add_argument("--runner", default="null", choices=["null", "local-process"])
    dispatch_run.add_argument("--claim-file", action="append", default=[])
    add_request_id(dispatch_run)
    dispatch_recover = dispatch_sub.add_parser("recover-stale")
    add_request_id(dispatch_recover)
    dispatch_file_claim = dispatch_sub.add_parser("file-claim")
    dispatch_file_claim_sub = dispatch_file_claim.add_subparsers(dest="dispatch_file_claim_command", required=True)
    dispatch_file_claim_add_parser = dispatch_file_claim_sub.add_parser("add")
    dispatch_file_claim_add_parser.add_argument("--task", required=True)
    dispatch_file_claim_add_parser.add_argument("--agent", required=True)
    dispatch_file_claim_add_parser.add_argument("--path", required=True)
    add_request_id(dispatch_file_claim_add_parser)
    dispatch_file_claim_list_parser = dispatch_file_claim_sub.add_parser("list")
    dispatch_file_claim_list_parser.add_argument("--task", default="")
    dispatch_file_claim_list_parser.add_argument("--agent", default="")
    dispatch_file_claim_release_parser = dispatch_file_claim_sub.add_parser("release")
    dispatch_file_claim_release_parser.add_argument("--task", required=True)
    dispatch_file_claim_release_parser.add_argument("--agent", required=True)
    dispatch_file_claim_release_parser.add_argument("--path", default="")
    add_request_id(dispatch_file_claim_release_parser)
    dispatch_integrate_parser = dispatch_sub.add_parser("integrate")
    dispatch_integrate_parser.add_argument("--run-id", required=True)
    dispatch_integrate_parser.add_argument("--target-branch", default="")
    add_request_id(dispatch_integrate_parser)
    dispatch_provider = dispatch_sub.add_parser("provider")
    dispatch_provider_sub = dispatch_provider.add_subparsers(dest="dispatch_provider_command", required=True)
    dispatch_provider_start_parser = dispatch_provider_sub.add_parser("start")
    dispatch_provider_start_parser.add_argument("--run-id", required=True)
    dispatch_provider_start_parser.add_argument("--provider", required=True, choices=["manual-csv", "fixture", "host-codex"])
    dispatch_provider_start_parser.add_argument("--max-concurrency", type=int, default=6)
    add_request_id(dispatch_provider_start_parser)
    dispatch_provider_status_parser = dispatch_provider_sub.add_parser("status")
    dispatch_provider_status_parser.add_argument("--run-id", required=True)
    dispatch_provider_collect_parser = dispatch_provider_sub.add_parser("collect")
    dispatch_provider_collect_parser.add_argument("--run-id", required=True)
    add_request_id(dispatch_provider_collect_parser)
    dispatch_provider_cancel_parser = dispatch_provider_sub.add_parser("cancel")
    dispatch_provider_cancel_parser.add_argument("--run-id", required=True)
    dispatch_provider_cancel_parser.add_argument("--task", default="")
    dispatch_provider_cancel_parser.add_argument("--reason", default="")
    add_request_id(dispatch_provider_cancel_parser)
    dispatch_provider_reconcile_parser = dispatch_provider_sub.add_parser("reconcile")
    dispatch_provider_reconcile_parser.add_argument("--run-id", required=True)
    add_request_id(dispatch_provider_reconcile_parser)
    dispatch_sub.add_parser("status")

    executor = sub.add_parser("executor")
    executor_sub = executor.add_subparsers(dest="executor_command", required=True)
    executor_allow = executor_sub.add_parser("allow-prefix")
    executor_allow_sub = executor_allow.add_subparsers(dest="executor_allow_command", required=True)
    executor_allow_add = executor_allow_sub.add_parser("add")
    executor_allow_add.add_argument("--prefix", required=True)
    executor_allow_add.add_argument("--reason", required=True)
    add_request_id(executor_allow_add)
    executor_allow_sub.add_parser("list")

    invariant = sub.add_parser("invariant")
    invariant_sub = invariant.add_subparsers(dest="invariant_command", required=True)
    invariant_sub.add_parser("validate")

    projection = sub.add_parser("projection")
    projection_sub = projection.add_subparsers(dest="projection_command", required=True)
    projection_sub.add_parser("rebuild")

    kernel = sub.add_parser("kernel")
    kernel_sub = kernel.add_subparsers(dest="kernel_command", required=True)
    kernel_sub.add_parser("doctor")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    root = Path(args.root).resolve()

    def semantic_args() -> dict[str, object]:
        return {key: value for key, value in vars(args).items() if key not in {"root", "request_id"}}

    def mutate(command: str, fn) -> None:
        print(run_idempotent(root, getattr(args, "request_id", None), command, semantic_args(), fn))

    try:
        if args.command == "init":
            if args.dry_run:
                print("DRY-RUN: would create .ai-team/state/harness.db, local harness views, and .codex/agents templates")
                return 0
            init_runtime(root)
            print("OK: project harness initialized")
        elif args.command == "status":
            print("\n".join(status_lines(root)))
        elif args.command == "doctor":
            issues = doctor(root)
            if issues:
                for issue in issues:
                    print(f"ERROR: {issue}")
                return 1
            print("OK: harness doctor passed")
        elif args.command == "validate":
            issues = validate_runtime(root, delivery=args.delivery)
            if issues:
                for issue in issues:
                    print(f"ERROR: {issue}")
                return 1
            print("OK: harness state is valid")
        elif args.command == "repair":
            plan = repair(root, dry_run=args.dry_run, clear_invariant=args.clear_invariant, confirm=args.confirm)
            if args.dry_run:
                print("DRY-RUN: repair plan")
                for item in plan:
                    print(f"- {item}")
                return 0
            if plan:
                for item in plan:
                    print(f"ERROR: {item}")
                return 1
            print("OK: repair complete")
        elif args.command == "migrate":
            report = migrate(root, args.from_version, args.to_version, dry_run=args.dry_run)
            if args.dry_run:
                print(f"DRY-RUN: would migrate {args.from_version}->{args.to_version}")
                if report:
                    for entity, count in sorted(report["imported"].items()):
                        print(f"- {entity}: {count}")
                return 0
            print(f"OK: migrated {args.from_version}->{args.to_version}")
        elif args.command == "phase":
            mutate("phase.transition", lambda: (transition_phase(root, args.phase, status=args.status, owner=args.owner), f"OK: phase={args.phase}")[1])
        elif args.command == "scope" and args.scope_command == "confirm":
            mutate("scope.confirm", lambda: (confirm_scope(root, args.by, args.summary), f"OK: scope confirmed by {args.by}")[1])
        elif args.command == "baseline" and args.baseline_command == "freeze":
            mutate("baseline.freeze", lambda: (freeze_baseline(root, args.id, args.summary, by=args.by), f"OK: baseline frozen {args.id}")[1])
        elif args.command == "baseline" and args.baseline_command == "diff":
            print("\n".join(baseline_diff(root, args.from_id, args.to)))
        elif args.command == "baseline" and args.baseline_command == "validate":
            issues = baseline_validate(root)
            if issues:
                for issue in issues:
                    print(f"ERROR: {issue}")
                return 1
            print("OK: baseline is current")
        elif args.command == "acceptance" and args.acceptance_command == "add":
            mutate("acceptance.add", lambda: (add_acceptance(root, args.id, args.criterion, args.priority, args.tool_link), f"OK: acceptance added {args.id}")[1])
        elif args.command == "requirement" and args.requirement_command == "add":
            mutate("requirement.add", lambda: (add_requirement(root, args.id, args.kind, args.body, priority=args.priority, status=args.status, tool_link=args.tool_link), f"OK: requirement added {args.id}")[1])
        elif args.command == "requirement" and args.requirement_command == "link":
            mutate("requirement.link", lambda: (link_requirement_acceptance(root, args.requirement, args.acceptance), f"OK: requirement linked {args.requirement}->{args.acceptance}")[1])
        elif args.command == "trace" and args.trace_command == "show":
            print("\n".join(trace_show(root, args.requirement)))
        elif args.command == "trace" and args.trace_command == "validate":
            issues = trace_validate(root)
            if issues:
                for issue in issues:
                    print(f"ERROR: {issue}")
                return 1
            print("OK: traceability is valid")
        elif args.command == "failure-mode" and args.failure_mode_command == "add":
            mutate(
                "failure-mode.add",
                lambda: (
                    add_failure_mode(
                        root,
                        args.id,
                        args.feature,
                        args.scenario,
                        args.trigger,
                        args.expected,
                        risk=args.risk,
                        status=args.status,
                        acceptance=args.acceptance,
                        recovery=args.recovery,
                        data_safety=args.data_safety,
                        accepted_by=args.accepted_by,
                        acceptance_reason=args.acceptance_reason,
                        acceptance_scope=args.acceptance_scope,
                        expires_at=args.expires_at,
                    ),
                    f"OK: failure mode added {args.id}",
                )[1],
            )
        elif args.command == "task" and args.task_command == "add":
            mutate(
                "task.add",
                lambda: (
                    add_task(
                        root,
                        args.id,
                        args.task,
                        owner=args.owner,
                        acceptance=args.acceptance,
                        failure_modes=", ".join(args.failure_mode),
                        depends_on=args.depends_on,
                        status=args.status,
                        evidence=args.evidence,
                        tool_link=args.tool_link,
                    ),
                    f"OK: task added {args.id}",
                )[1],
            )
        elif args.command == "task" and args.task_command == "update":
            mutate("task.update", lambda: (update_task(root, args.id, depends_on=args.depends_on, status=args.status), f"OK: task updated {args.id}")[1])
        elif args.command == "task" and args.task_command == "next":
            tasks = ready_tasks(root)
            if tasks:
                print("\n".join(tasks))
            else:
                print("NO_READY_TASKS")
        elif args.command == "task" and args.task_command == "claim":
            def claim_output() -> str:
                token, fence = claim_task(root, args.id, args.agent, args.expected_revision)
                return f"OK: claimed {args.id} token={token} fence={fence}"

            mutate("task.claim", claim_output)
        elif args.command == "task" and args.task_command == "heartbeat":
            mutate("task.heartbeat", lambda: (heartbeat_task(root, args.id, args.agent, args.lease_token, args.expected_revision, expected_fence=args.fence), f"OK: heartbeat {args.id}")[1])
        elif args.command == "task" and args.task_command == "recover-stale":
            mutate("task.recover-stale", lambda: f"OK: recovered {recover_stale_leases(root)} stale lease(s)")
        elif args.command == "task" and args.task_command == "start":
            mutate("task.start", lambda: (start_task(root, args.id, args.agent, lease_token=args.lease_token, expected_revision=args.expected_revision, expected_fence=args.fence), f"OK: task started {args.id}")[1])
        elif args.command == "task" and args.task_command == "submit":
            mutate("task.submit", lambda: (submit_task(root, args.id, args.evidence, agent=args.agent, lease_token=args.lease_token, expected_revision=args.expected_revision, expected_fence=args.fence, session_id=args.session_id), f"OK: task submitted {args.id}")[1])
        elif args.command == "task" and args.task_command == "complete":
            mutate("task.complete", lambda: (complete_task(root, args.id, args.evidence, agent=args.agent, lease_token=args.lease_token, expected_revision=args.expected_revision, expected_fence=args.fence, session_id=args.session_id), f"OK: task submitted {args.id}")[1])
        elif args.command == "task" and args.task_command == "review":
            def review_output() -> str:
                token, fence = review_task(root, args.id, args.agent, args.expected_revision, session_id=args.session_id)
                return f"OK: task review started {args.id} token={token} fence={fence}"

            mutate("task.review", review_output)
        elif args.command == "task" and args.task_command == "accept":
            mutate("task.accept", lambda: (accept_task(root, args.id, args.evidence, agent=args.agent, lease_token=args.lease_token, expected_revision=args.expected_revision, expected_fence=args.fence, session_id=args.session_id), f"OK: task accepted {args.id}")[1])
        elif args.command == "task" and args.task_command == "block":
            mutate("task.block", lambda: (block_task(root, args.id, args.reason, agent=args.agent, lease_token=args.lease_token, expected_revision=args.expected_revision, expected_fence=args.fence), f"OK: task blocked {args.id}")[1])
        elif args.command == "task" and args.task_command == "release":
            mutate("task.release", lambda: (release_task(root, args.id, args.agent, lease_token=args.lease_token, expected_revision=args.expected_revision, expected_fence=args.fence), f"OK: task released {args.id}")[1])
        elif args.command == "validation" and args.validation_command == "record":
            mutate(
                "validation.record",
                lambda: (
                    record_validation(
                        root,
                        args.surface,
                        args.findings,
                        args.result,
                        acceptance=args.acceptance,
                        commands=args.commands,
                        risk=args.risk,
                        failure_modes=", ".join(args.failure_mode),
                        tests=", ".join(args.test),
                        evidence=", ".join(args.evidence),
                        command=args.validation_command_text,
                        exit_code=args.exit_code,
                        stdout_sha256=args.stdout_sha256,
                        artifact_path=args.artifact_path,
                        target_id=args.target,
                        executed_count=args.executed_count,
                        allow_unlisted=args.allow_unlisted,
                        no_network=args.no_network,
                        sandbox_profile=args.sandbox_profile,
                        trust_anchor=args.trust_anchor,
                        trust_anchor_id=args.trust_anchor_id,
                        allow_unlisted_reason=args.reason,
                        code_identity=args.code_identity,
                    ),
                    "OK: validation recorded",
                )[1],
            )
        elif args.command == "test-target" and args.test_target_command == "add":
            mutate("test-target.add", lambda: (add_test_target(root, args.id, args.kind, args.command_template, args.description), f"OK: test target recorded {args.id}")[1])
        elif args.command == "test-target" and args.test_target_command == "link":
            mutate("test-target.link", lambda: (link_task_test_target(root, args.task, args.target), f"OK: test target linked {args.task}->{args.target}")[1])
        elif args.command == "test-target" and args.test_target_command == "list":
            print("\n".join(list_test_targets(root)))
        elif args.command == "decision" and args.decision_command == "record":
            mutate("decision.record", lambda: (record_decision(root, args.decision, args.reason), "OK: decision recorded")[1])
        elif args.command == "evidence" and args.evidence_command == "record":
            mutate(
                "evidence.record",
                lambda: (
                    record_evidence(
                        root,
                        args.id,
                        args.kind,
                        args.summary,
                        uri=args.uri,
                        artifact_hash=args.hash,
                        command=args.evidence_command_text,
                        exit_code=args.exit_code,
                        stdout_sha256=args.stdout_sha256,
                        artifact_path=args.artifact_path,
                        target_id=args.target,
                        executed_count=args.executed_count,
                        allow_unlisted=args.allow_unlisted,
                        no_network=args.no_network,
                        sandbox_profile=args.sandbox_profile,
                        trust_anchor=args.trust_anchor,
                        trust_anchor_id=args.trust_anchor_id,
                        allow_unlisted_reason=args.reason,
                        code_identity=args.code_identity,
                    ),
                    f"OK: evidence recorded {args.id}",
                )[1],
            )
        elif args.command == "test" and args.test_command == "record":
            mutate("test.record", lambda: (record_test(root, args.id, args.surface, args.test_command_text, args.result, evidence_id=args.evidence), f"OK: test recorded {args.id}")[1])
        elif args.command == "finding" and args.finding_command == "record":
            mutate("finding.record", lambda: (record_finding(root, args.id, args.surface, args.severity, args.status, args.summary, evidence_id=args.evidence), f"OK: finding recorded {args.id}")[1])
        elif args.command == "gate" and args.gate_command == "record":
            mutate(
                "gate.record",
                lambda: (
                    record_gate(
                        root,
                        args.reviewer_context,
                        args.result,
                        gate=args.gate,
                        commands=args.commands,
                        evidence=args.evidence,
                        blocking_findings=args.blocking_findings,
                        residual_risk=args.residual_risk,
                        findings=", ".join(args.finding),
                        reviewer_session_id=args.reviewer_session_id,
                        reviewer_attestation_id=args.reviewer_attestation_id,
                    ),
                    f"OK: quality gate recorded {args.gate}={args.result}",
                )[1],
            )
        elif args.command == "delivery" and args.delivery_command == "record":
            mutate(
                "delivery.record",
                lambda: (
                    record_delivery(
                        root,
                        args.scope,
                        acceptance=args.acceptance,
                        changed_files=args.changed_files,
                        validation=args.validation,
                        qa=args.qa,
                        failure_mode_coverage=args.failure_mode_coverage,
                        quality_gate=args.quality_gate,
                        data_config_notes=args.data_config_notes,
                        collaboration_links=args.collaboration_links,
                        known_gaps=args.known_gaps,
                        handoff=args.handoff,
                    ),
                    "OK: delivery recorded",
                )[1],
            )
        elif args.command == "adapter" and args.adapter_command == "record":
            mutate(
                "adapter.record",
                lambda: (
                    record_adapter(
                        root,
                        args.tool,
                        args.mode,
                        args.artifact,
                        args.external_id,
                        args.idempotency_key,
                        external_link=args.external_link,
                        evidence=args.evidence,
                        fallback=args.fallback,
                        confirmation_needed=args.confirmation_needed,
                    ),
                    f"OK: adapter recorded {args.tool}",
                )[1],
            )
        elif args.command == "adapter" and args.adapter_command == "plan":
            mutate("adapter.plan", lambda: f"OK: adapter action planned {adapter_plan(root, args.tool, args.mode, args.artifact, args.action, payload_json=args.payload_json, idempotency_key=args.idempotency_key)}")
        elif args.command == "adapter" and args.adapter_command == "draft":
            mutate("adapter.draft", lambda: (adapter_transition(root, args.id, "draft"), f"OK: adapter action draft {args.id}")[1])
        elif args.command == "adapter" and args.adapter_command == "confirm":
            mutate("adapter.confirm", lambda: (adapter_transition(root, args.id, "confirmed", confirmation=args.confirmation), f"OK: adapter action confirmed {args.id}")[1])
        elif args.command == "adapter" and args.adapter_command == "complete":
            mutate("adapter.complete", lambda: (adapter_transition(root, args.id, "completed", external_id=args.external_id, external_link=args.external_link), f"OK: adapter action completed {args.id}")[1])
        elif args.command == "adapter" and args.adapter_command == "ci-verify":
            mutate(
                "adapter.ci-verify",
                lambda: f"OK: ci verification recorded {record_ci_verification(root, args.provider, args.run_id, args.conclusion, args.commit_sha, external_link=args.external_link, origin=args.origin, verification_token=args.verification_token)}",
            )
        elif args.command == "adapter" and args.adapter_command == "external-session-verify":
            mutate(
                "adapter.external-session-verify",
                lambda: f"OK: external session verification recorded {record_external_session_verification(root, args.session_id, args.verifier, args.conclusion, args.commit_sha, external_link=args.external_link, origin=args.origin, verification_token=args.verification_token)}",
            )
        elif args.command == "adapter" and args.adapter_command == "reconcile":
            issues = adapter_reconcile(root)
            if issues:
                for issue in issues:
                    print(f"ERROR: {issue}")
                return 1
            print("OK: adapters reconciled")
        elif args.command == "risk" and args.risk_command == "sweep-expired":
            mutate("risk.sweep-expired", lambda: f"OK: swept {sweep_expired_risks(root)} expired risk acceptance(s)")
        elif args.command == "checkpoint" and args.checkpoint_command == "create":
            checkpoint_id = create_checkpoint(root, args.label)
            print(f"OK: checkpoint created {checkpoint_id}")
        elif args.command == "checkpoint" and args.checkpoint_command == "list":
            print("\n".join(list_checkpoints(root)))
        elif args.command == "checkpoint" and args.checkpoint_command == "export":
            export_checkpoint(root, Path(args.out))
            print(f"OK: checkpoint exported {args.out}")
        elif args.command == "checkpoint" and args.checkpoint_command == "import":
            issues = import_checkpoint(root, Path(args.file), apply=args.apply)
            if issues:
                for issue in issues:
                    print(f"ERROR: {issue}")
                return 1 if args.apply else 0
            print("OK: checkpoint import applied" if args.apply else "OK: checkpoint import dry-run passed")
        elif args.command == "event" and args.event_command == "export":
            export_events(root, Path(args.out))
            print(f"OK: events exported {args.out}")
        elif args.command == "event" and args.event_command == "validate":
            issues = validate_events(root)
            if issues:
                for issue in issues:
                    print(f"ERROR: {issue}")
                return 1
            print("OK: events are audit-compatible")
        elif args.command == "agent" and args.agent_command == "capability" and args.agent_capability_command == "add":
            mutate("agent.capability.add", lambda: (add_agent_capability(root, args.agent, args.capability), f"OK: agent capability added {args.agent}:{args.capability}")[1])
        elif args.command == "agents" and args.agents_command == "install":
            mutate("agents.install", lambda: f"OK: agents installed {install_agents(root, target_dir=args.dir, force=args.force, strict_no_overwrite=True)}")
        elif args.command == "session" and args.session_command == "attest":
            def attest_output() -> str:
                attestation_id = record_session_attestation(
                    root,
                    args.session_id,
                    args.agent,
                    args.role,
                    args.context_id,
                    provider_session_id=args.provider_session_id,
                    origin=args.origin,
                    verification_token=args.verification_token,
                )
                with connection(root) as conn:
                    row = conn.execute("select origin, token_status, trust_level from session_attestations where id = ?", (attestation_id,)).fetchone()
                return f"OK: session attested {args.session_id} attestation={attestation_id} origin={row['origin']} token_status={row['token_status']} trust={row['trust_level']}"

            mutate("session.attest", attest_output)
        elif args.command == "session" and args.session_command == "status":
            print("\n".join(session_status_lines(root, agent=args.agent)))
        elif args.command == "session" and args.session_command == "close":
            mutate("session.close", lambda: (close_agent_session(root, args.session_id), f"OK: session closed {args.session_id}")[1])
        elif args.command == "dispatch" and args.dispatch_command == "plan":
            mutate("dispatch.plan", lambda: f"OK: dispatch planned {dispatch_plan(root, args.scope)}")
        elif args.command == "dispatch" and args.dispatch_command == "export-csv":
            out_dir = Path(args.out_dir) if args.out_dir else None
            mutate("dispatch.export-csv", lambda: f"OK: dispatch csv exported {dispatch_export_csv(root, args.run_id, out_dir=(root / out_dir if out_dir and not out_dir.is_absolute() else out_dir), max_concurrency=args.max_concurrency, max_runtime_seconds=args.max_runtime_seconds)}")
        elif args.command == "dispatch" and args.dispatch_command == "import-csv":
            result_path = Path(args.result)
            mutate("dispatch.import-csv", lambda: f"OK: dispatch csv imported {dispatch_import_csv(root, args.run_id, result_path if result_path.is_absolute() else root / result_path)}")
        elif args.command == "dispatch" and args.dispatch_command == "verify-attempt":
            mutate("dispatch.verify-attempt", lambda: f"OK: dispatch attempt verified {dispatch_verify_attempt(root, args.run_id, args.task, runner=args.runner)}")
        elif args.command == "dispatch" and args.dispatch_command == "claim-next":
            task_id = dispatch_claim_next(root, args.agent)
            print(f"OK: dispatch claimed {task_id}")
        elif args.command == "dispatch" and args.dispatch_command == "run":
            mutate(
                "dispatch.run",
                lambda: f"OK: dispatch command evidence {dispatch_run(root, args.agent, args.dispatch_command_text, timeout=args.timeout, target_id=args.target, allow_unlisted=args.allow_unlisted, no_network=args.no_network, sandbox_profile=args.sandbox_profile, allow_unlisted_reason=args.reason, executed_count=args.executed_count, code_identity=args.code_identity, runner=args.runner, claim_files=args.claim_file)}",
            )
        elif args.command == "dispatch" and args.dispatch_command == "recover-stale":
            mutate("dispatch.recover-stale", lambda: f"OK: dispatch recovered {dispatch_recover_stale(root)} stale assignment(s)")
        elif args.command == "dispatch" and args.dispatch_command == "file-claim" and args.dispatch_file_claim_command == "add":
            mutate("dispatch.file-claim.add", lambda: f"OK: file claimed {dispatch_file_claim_add(root, args.task, args.agent, args.path)}")
        elif args.command == "dispatch" and args.dispatch_command == "file-claim" and args.dispatch_file_claim_command == "list":
            print("\n".join(dispatch_file_claim_list(root, task_id=args.task, agent=args.agent)))
        elif args.command == "dispatch" and args.dispatch_command == "file-claim" and args.dispatch_file_claim_command == "release":
            mutate("dispatch.file-claim.release", lambda: f"OK: file claims released {dispatch_file_claim_release(root, args.task, args.agent, path=args.path)}")
        elif args.command == "dispatch" and args.dispatch_command == "integrate":
            mutate("dispatch.integrate", lambda: f"OK: dispatch integrated {dispatch_integrate(root, args.run_id, target_branch=args.target_branch)}")
        elif args.command == "dispatch" and args.dispatch_command == "provider" and args.dispatch_provider_command == "start":
            mutate("dispatch.provider.start", lambda: f"OK: started {dispatch_provider_start(root, args.run_id, args.provider, max_concurrency=args.max_concurrency)} provider session(s)")
        elif args.command == "dispatch" and args.dispatch_command == "provider" and args.dispatch_provider_command == "status":
            print("\n".join(dispatch_provider_status(root, args.run_id)))
        elif args.command == "dispatch" and args.dispatch_command == "provider" and args.dispatch_provider_command == "collect":
            mutate("dispatch.provider.collect", lambda: f"OK: collected {dispatch_provider_collect(root, args.run_id)} provider report(s)")
        elif args.command == "dispatch" and args.dispatch_command == "provider" and args.dispatch_provider_command == "cancel":
            mutate("dispatch.provider.cancel", lambda: f"OK: cancelled {dispatch_provider_cancel(root, args.run_id, task_id=args.task, reason=args.reason)} provider session(s)")
        elif args.command == "dispatch" and args.dispatch_command == "provider" and args.dispatch_provider_command == "reconcile":
            mutate("dispatch.provider.reconcile", lambda: f"OK: reconciled {dispatch_provider_reconcile(root, args.run_id)} provider session(s)")
        elif args.command == "dispatch" and args.dispatch_command == "status":
            print("\n".join(dispatch_status(root)))
        elif args.command == "executor" and args.executor_command == "allow-prefix" and args.executor_allow_command == "add":
            mutate("executor.allow-prefix.add", lambda: (add_executor_prefix(root, args.prefix, args.reason), f"OK: executor prefix allowed {args.prefix}")[1])
        elif args.command == "executor" and args.executor_command == "allow-prefix" and args.executor_allow_command == "list":
            print("\n".join(list_executor_prefixes(root)))
        elif args.command == "invariant" and args.invariant_command == "validate":
            issues = invariant_validate(root)
            if issues:
                for issue in issues:
                    print(f"ERROR: {issue}")
                return 1
            print("OK: runtime invariants hold")
        elif args.command == "projection" and args.projection_command == "rebuild":
            projection_rebuild(root)
            print("OK: projections rebuilt")
        elif args.command == "kernel" and args.kernel_command == "doctor":
            issues = kernel_doctor(root)
            if issues:
                for issue in issues:
                    print(f"ERROR: {issue}")
                return 1
            print("OK: kernel doctor passed")
        else:
            parser.error("unknown command")
    except HarnessError as exc:
        print(f"ERROR: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

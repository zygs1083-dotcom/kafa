#!/usr/bin/env python3
"""Unified Codex Project Harness runtime CLI."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from harness_db import (
    HarnessError,
    accept_task,
    add_acceptance,
    add_failure_mode,
    add_task,
    block_task,
    claim_task,
    complete_task,
    doctor,
    init_runtime,
    migrate,
    ready_tasks,
    record_adapter,
    record_decision,
    record_delivery,
    record_evidence,
    record_finding,
    record_gate,
    record_test,
    record_validation,
    add_requirement,
    heartbeat_task,
    recover_stale_leases,
    release_task,
    repair,
    start_task,
    status_lines,
    submit_task,
    transition_phase,
    update_task,
    validate_runtime,
    review_task,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="Target project root")
    sub = parser.add_subparsers(dest="command", required=True)

    init_parser = sub.add_parser("init")
    init_parser.add_argument("--dry-run", action="store_true")
    sub.add_parser("status")
    sub.add_parser("doctor")
    validate_parser = sub.add_parser("validate")
    validate_parser.add_argument("--delivery", action="store_true")
    sub.add_parser("repair")

    migrate_parser = sub.add_parser("migrate")
    migrate_parser.add_argument("--from-version", required=True)
    migrate_parser.add_argument("--to-version", type=int, required=True)

    phase = sub.add_parser("phase")
    phase.add_argument("phase")
    phase.add_argument("--status")
    phase.add_argument("--owner")

    acceptance = sub.add_parser("acceptance")
    acceptance_sub = acceptance.add_subparsers(dest="acceptance_command", required=True)
    acceptance_add = acceptance_sub.add_parser("add")
    acceptance_add.add_argument("--id", required=True)
    acceptance_add.add_argument("--criterion", required=True)
    acceptance_add.add_argument("--priority", default="")
    acceptance_add.add_argument("--tool-link", default="")

    requirement = sub.add_parser("requirement")
    requirement_sub = requirement.add_subparsers(dest="requirement_command", required=True)
    requirement_add = requirement_sub.add_parser("add")
    requirement_add.add_argument("--id", required=True)
    requirement_add.add_argument("--kind", required=True, choices=["goal", "functional", "non-functional", "non-goal", "assumption", "open-question", "architecture"])
    requirement_add.add_argument("--body", required=True)
    requirement_add.add_argument("--priority", default="")
    requirement_add.add_argument("--status", default="active")
    requirement_add.add_argument("--tool-link", default="")

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

    task_update = task_sub.add_parser("update")
    task_update.add_argument("id")
    task_update.add_argument("--depends-on")
    task_update.add_argument("--status")

    task_sub.add_parser("next")

    task_claim = task_sub.add_parser("claim")
    task_claim.add_argument("id")
    task_claim.add_argument("--agent", required=True)
    task_claim.add_argument("--expected-revision", type=int, required=True)

    task_heartbeat = task_sub.add_parser("heartbeat")
    task_heartbeat.add_argument("id")
    task_heartbeat.add_argument("--agent", required=True)
    task_heartbeat.add_argument("--lease-token", required=True)
    task_heartbeat.add_argument("--expected-revision", type=int, required=True)

    task_sub.add_parser("recover-stale")

    task_start = task_sub.add_parser("start")
    task_start.add_argument("id")
    task_start.add_argument("--agent", required=True)
    task_start.add_argument("--lease-token", required=True)
    task_start.add_argument("--expected-revision", type=int, required=True)

    task_submit = task_sub.add_parser("submit")
    task_submit.add_argument("id")
    task_submit.add_argument("--agent", required=True)
    task_submit.add_argument("--lease-token", required=True)
    task_submit.add_argument("--expected-revision", type=int, required=True)
    task_submit.add_argument("--evidence", required=True)

    task_complete = task_sub.add_parser("complete")
    task_complete.add_argument("id")
    task_complete.add_argument("--agent", required=True)
    task_complete.add_argument("--lease-token", required=True)
    task_complete.add_argument("--expected-revision", type=int, required=True)
    task_complete.add_argument("--evidence", required=True)

    task_review = task_sub.add_parser("review")
    task_review.add_argument("id")
    task_review.add_argument("--agent", required=True)
    task_review.add_argument("--expected-revision", type=int, required=True)

    task_accept = task_sub.add_parser("accept")
    task_accept.add_argument("id")
    task_accept.add_argument("--agent", required=True)
    task_accept.add_argument("--lease-token", required=True)
    task_accept.add_argument("--expected-revision", type=int, required=True)
    task_accept.add_argument("--evidence", required=True)

    task_block = task_sub.add_parser("block")
    task_block.add_argument("id")
    task_block.add_argument("--agent", required=True)
    task_block.add_argument("--lease-token", required=True)
    task_block.add_argument("--expected-revision", type=int, required=True)
    task_block.add_argument("--reason", required=True)

    task_release = task_sub.add_parser("release")
    task_release.add_argument("id")
    task_release.add_argument("--agent", required=True)
    task_release.add_argument("--lease-token", required=True)
    task_release.add_argument("--expected-revision", type=int, required=True)

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

    decision = sub.add_parser("decision")
    decision_sub = decision.add_subparsers(dest="decision_command", required=True)
    decision_record = decision_sub.add_parser("record")
    decision_record.add_argument("--decision", required=True)
    decision_record.add_argument("--reason", required=True)

    evidence = sub.add_parser("evidence")
    evidence_sub = evidence.add_subparsers(dest="evidence_command", required=True)
    evidence_record = evidence_sub.add_parser("record")
    evidence_record.add_argument("--id", required=True)
    evidence_record.add_argument("--kind", required=True)
    evidence_record.add_argument("--summary", required=True)
    evidence_record.add_argument("--uri", default="")
    evidence_record.add_argument("--hash", default="")

    test = sub.add_parser("test")
    test_sub = test.add_subparsers(dest="test_command", required=True)
    test_record = test_sub.add_parser("record")
    test_record.add_argument("--id", required=True)
    test_record.add_argument("--surface", required=True)
    test_record.add_argument("--command", dest="test_command_text", default="")
    test_record.add_argument("--result", required=True, choices=["pass", "fail", "blocked", "partial"])
    test_record.add_argument("--evidence", default="")

    finding = sub.add_parser("finding")
    finding_sub = finding.add_subparsers(dest="finding_command", required=True)
    finding_record = finding_sub.add_parser("record")
    finding_record.add_argument("--id", required=True)
    finding_record.add_argument("--surface", required=True)
    finding_record.add_argument("--severity", required=True, choices=["low", "medium", "high", "critical"])
    finding_record.add_argument("--status", required=True, choices=["open", "resolved", "accepted", "false-positive"])
    finding_record.add_argument("--summary", required=True)
    finding_record.add_argument("--evidence", default="")

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

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    root = Path(args.root).resolve()

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
            repair(root)
            print("OK: repair complete")
        elif args.command == "migrate":
            migrate(root, args.from_version, args.to_version)
            print(f"OK: migrated {args.from_version}->{args.to_version}")
        elif args.command == "phase":
            transition_phase(root, args.phase, status=args.status, owner=args.owner)
            print(f"OK: phase={args.phase}")
        elif args.command == "acceptance" and args.acceptance_command == "add":
            add_acceptance(root, args.id, args.criterion, args.priority, args.tool_link)
            print(f"OK: acceptance added {args.id}")
        elif args.command == "requirement" and args.requirement_command == "add":
            add_requirement(root, args.id, args.kind, args.body, priority=args.priority, status=args.status, tool_link=args.tool_link)
            print(f"OK: requirement added {args.id}")
        elif args.command == "failure-mode" and args.failure_mode_command == "add":
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
            )
            print(f"OK: failure mode added {args.id}")
        elif args.command == "task" and args.task_command == "add":
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
            )
            print(f"OK: task added {args.id}")
        elif args.command == "task" and args.task_command == "update":
            update_task(root, args.id, depends_on=args.depends_on, status=args.status)
            print(f"OK: task updated {args.id}")
        elif args.command == "task" and args.task_command == "next":
            tasks = ready_tasks(root)
            if tasks:
                print("\n".join(tasks))
            else:
                print("NO_READY_TASKS")
        elif args.command == "task" and args.task_command == "claim":
            token = claim_task(root, args.id, args.agent, args.expected_revision)
            print(f"OK: claimed {args.id} token={token}")
        elif args.command == "task" and args.task_command == "heartbeat":
            heartbeat_task(root, args.id, args.agent, args.lease_token, args.expected_revision)
            print(f"OK: heartbeat {args.id}")
        elif args.command == "task" and args.task_command == "recover-stale":
            recovered = recover_stale_leases(root)
            print(f"OK: recovered {recovered} stale lease(s)")
        elif args.command == "task" and args.task_command == "start":
            start_task(root, args.id, args.agent, lease_token=args.lease_token, expected_revision=args.expected_revision)
            print(f"OK: task started {args.id}")
        elif args.command == "task" and args.task_command == "submit":
            submit_task(root, args.id, args.evidence, agent=args.agent, lease_token=args.lease_token, expected_revision=args.expected_revision)
            print(f"OK: task submitted {args.id}")
        elif args.command == "task" and args.task_command == "complete":
            complete_task(root, args.id, args.evidence, agent=args.agent, lease_token=args.lease_token, expected_revision=args.expected_revision)
            print(f"OK: task submitted {args.id}")
        elif args.command == "task" and args.task_command == "review":
            token = review_task(root, args.id, args.agent, args.expected_revision)
            print(f"OK: task review started {args.id} token={token}")
        elif args.command == "task" and args.task_command == "accept":
            accept_task(root, args.id, args.evidence, agent=args.agent, lease_token=args.lease_token, expected_revision=args.expected_revision)
            print(f"OK: task accepted {args.id}")
        elif args.command == "task" and args.task_command == "block":
            block_task(root, args.id, args.reason, agent=args.agent, lease_token=args.lease_token, expected_revision=args.expected_revision)
            print(f"OK: task blocked {args.id}")
        elif args.command == "task" and args.task_command == "release":
            release_task(root, args.id, args.agent, lease_token=args.lease_token, expected_revision=args.expected_revision)
            print(f"OK: task released {args.id}")
        elif args.command == "validation" and args.validation_command == "record":
            record_validation(root, args.surface, args.findings, args.result, acceptance=args.acceptance, commands=args.commands, risk=args.risk, failure_modes=", ".join(args.failure_mode))
            print("OK: validation recorded")
        elif args.command == "decision" and args.decision_command == "record":
            record_decision(root, args.decision, args.reason)
            print("OK: decision recorded")
        elif args.command == "evidence" and args.evidence_command == "record":
            record_evidence(root, args.id, args.kind, args.summary, uri=args.uri, artifact_hash=args.hash)
            print(f"OK: evidence recorded {args.id}")
        elif args.command == "test" and args.test_command == "record":
            record_test(root, args.id, args.surface, args.test_command_text, args.result, evidence_id=args.evidence)
            print(f"OK: test recorded {args.id}")
        elif args.command == "finding" and args.finding_command == "record":
            record_finding(root, args.id, args.surface, args.severity, args.status, args.summary, evidence_id=args.evidence)
            print(f"OK: finding recorded {args.id}")
        elif args.command == "gate" and args.gate_command == "record":
            record_gate(
                root,
                args.reviewer_context,
                args.result,
                gate=args.gate,
                commands=args.commands,
                evidence=args.evidence,
                blocking_findings=args.blocking_findings,
                residual_risk=args.residual_risk,
            )
            print(f"OK: quality gate recorded {args.gate}={args.result}")
        elif args.command == "delivery" and args.delivery_command == "record":
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
            )
            print("OK: delivery recorded")
        elif args.command == "adapter" and args.adapter_command == "record":
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
            )
            print(f"OK: adapter recorded {args.tool}")
        else:
            parser.error("unknown command")
    except HarnessError as exc:
        print(f"ERROR: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

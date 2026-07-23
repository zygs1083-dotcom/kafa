#!/usr/bin/env python3
"""Unified Codex Project Harness runtime CLI."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = Path(__file__).resolve().parent
for runtime_path in (SCRIPTS_ROOT, PLUGIN_ROOT):
    if str(runtime_path) not in sys.path:
        sys.path.insert(0, str(runtime_path))

from core.api import (
    HarnessError,
    accept_task,
    apply_delivery_plan,
    add_acceptance,
    add_failure_mode,
    add_requirement,
    add_task,
    add_test_target,
    baseline_diff,
    baseline_validate,
    block_task,
    cancel_task,
    confirm_baseline,
    cycle_audit,
    cycle_close,
    cycle_start,
    cycle_status,
    doctor,
    doctor_operator_report,
    enter_delivery_readiness,
    freeze_baseline,
    init_runtime,
    link_requirement_acceptance,
    link_task_test_target,
    list_tasks,
    list_test_targets,
    migrate,
    outcome_report,
    operator_error_report,
    operator_verbose_lines,
    record_decision,
    record_delivery,
    record_finding,
    record_gate,
    record_outcome_observation,
    record_validation,
    projection_rebuild,
    quickstart_minimal,
    quickstart_status,
    quickstart_status_lines,
    quickstart_operator_report,
    qualify_test_target,
    repair,
    start_task,
    status_lines,
    status_operator_report,
    submit_task,
    trace_show,
    trace_validate,
    validate_runtime,
    verified_patch,
    verify_run,
    runtime_initialized,
    uninitialized_lines,
)
from core.operator_output import render_concise, render_json, render_verbose
from core.errors import exception_text
from core.delivery_plan import DeliveryPlanError, load_delivery_plan
from core.schema_guard import (
    FAILURE_MODE_STATUS_VALUES,
    OUTCOME_KIND_VALUES,
    REQUIREMENT_STATUS_VALUES,
)


RETIRED_V2_COMMANDS = {"adapter", "connector", "dispatch", "agent", "agents", "session"}


def retired_v2_invocation(argv: list[str]) -> str:
    """Return a migration message for removed major-version commands."""

    tokens: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--root":
            index += 2
            continue
        if token.startswith("--root="):
            index += 1
            continue
        tokens.append(token)
        index += 1
    if not tokens:
        return ""
    if tokens[0] in RETIRED_V2_COMMANDS:
        return (
            f"{tokens[0]} runtime commands were removed in Kafa v2; "
            "use Native Codex/Apps directly and record only local verification facts"
        )
    if tokens[:2] == ["session", "attest"]:
        return (
            "session attest was removed in Kafa v2; host context ids are audit metadata, "
            "not cryptographic delivery trust"
        )
    return ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="Target project root")
    sub = parser.add_subparsers(dest="command", required=True)

    init_parser = sub.add_parser("init")
    init_parser.add_argument("--dry-run", action="store_true")
    status_parser = sub.add_parser("status")
    status_output = status_parser.add_mutually_exclusive_group()
    status_output.add_argument("--verbose", action="store_true")
    status_output.add_argument("--json", action="store_true")
    doctor_parser = sub.add_parser("doctor")
    doctor_output = doctor_parser.add_mutually_exclusive_group()
    doctor_output.add_argument("--verbose", action="store_true")
    doctor_output.add_argument("--json", action="store_true")

    quickstart = sub.add_parser("quickstart")
    quickstart_sub = quickstart.add_subparsers(dest="quickstart_command", required=True)
    quickstart_status_parser = quickstart_sub.add_parser("status")
    quickstart_status_output = quickstart_status_parser.add_mutually_exclusive_group()
    quickstart_status_output.add_argument("--verbose", action="store_true")
    quickstart_status_output.add_argument("--json", action="store_true")
    quickstart_minimal_parser = quickstart_sub.add_parser("minimal")
    quickstart_minimal_parser.add_argument("--id", required=True)
    quickstart_minimal_parser.add_argument("--goal", required=True)
    quickstart_minimal_parser.add_argument("--acceptance", required=True)
    quickstart_minimal_parser.add_argument("--task", required=True)
    quickstart_minimal_parser.add_argument("--test-command", required=True)
    quickstart_minimal_parser.add_argument("--execute", action="store_true")
    quickstart_plan_parser = quickstart_sub.add_parser("delivery-plan")
    quickstart_plan_parser.add_argument("--file", required=True)
    quickstart_plan_parser.add_argument("--dry-run", action="store_true")
    quickstart_plan_output = quickstart_plan_parser.add_mutually_exclusive_group()
    quickstart_plan_output.add_argument("--json", action="store_true")
    quickstart_plan_output.add_argument("--verbose", action="store_true")
    quickstart_verified_parser = quickstart_sub.add_parser("verified-patch")
    quickstart_verified_parser.add_argument("--id", required=True)
    quickstart_verified_output = quickstart_verified_parser.add_mutually_exclusive_group()
    quickstart_verified_output.add_argument("--json", action="store_true")
    quickstart_verified_output.add_argument("--verbose", action="store_true")

    cycle = sub.add_parser("cycle")
    cycle_sub = cycle.add_subparsers(dest="cycle_command", required=True)
    cycle_start_parser = cycle_sub.add_parser("start")
    cycle_start_parser.add_argument("--id", required=True)
    cycle_start_parser.add_argument("--name", required=True)
    cycle_start_parser.add_argument("--goal", required=True)
    cycle_start_parser.add_argument("--base-ref", default="")
    cycle_status_parser = cycle_sub.add_parser("status")
    cycle_status_parser.add_argument("--id", default="")
    cycle_status_parser.add_argument("--json", action="store_true")
    cycle_audit_parser = cycle_sub.add_parser("audit")
    cycle_audit_parser.add_argument("--id", required=True)
    cycle_audit_parser.add_argument("--json", action="store_true")
    cycle_close_parser = cycle_sub.add_parser("close")
    cycle_close_parser.add_argument("--status", required=True, choices=["delivered", "archived"])
    outcome_record_parser = cycle_sub.add_parser("outcome-record")
    outcome_record_parser.add_argument("--id", required=True)
    outcome_record_parser.add_argument("--kind", required=True, choices=OUTCOME_KIND_VALUES)
    outcome_record_parser.add_argument("--value", required=True, type=int)
    outcome_record_parser.add_argument("--details", required=True)
    outcome_record_parser.add_argument("--by", required=True)
    outcome_record_parser.add_argument("--observed-at", required=True)
    outcome_record_parser.add_argument("--cycle-id", default="")
    outcome_report_parser = cycle_sub.add_parser("outcome-report")
    outcome_report_parser.add_argument("--json", action="store_true")

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

    baseline = sub.add_parser("baseline")
    baseline_sub = baseline.add_subparsers(dest="baseline_command", required=True)
    baseline_freeze = baseline_sub.add_parser("freeze")
    baseline_freeze.add_argument("--id", required=True)
    baseline_freeze.add_argument("--summary", required=True)
    baseline_freeze.add_argument("--by", default="")
    baseline_confirm = baseline_sub.add_parser("confirm")
    baseline_confirm.add_argument("--id", required=True)
    baseline_confirm.add_argument("--summary", required=True)
    baseline_confirm.add_argument("--by", required=True)
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

    requirement = sub.add_parser("requirement")
    requirement_sub = requirement.add_subparsers(dest="requirement_command", required=True)
    requirement_add = requirement_sub.add_parser("add")
    requirement_add.add_argument("--id", required=True)
    requirement_add.add_argument("--kind", required=True, choices=["goal", "functional", "non-functional", "non-goal", "assumption", "open-question", "architecture"])
    requirement_add.add_argument("--body", required=True)
    requirement_add.add_argument("--priority", default="")
    requirement_add.add_argument(
        "--status",
        default="active",
        choices=REQUIREMENT_STATUS_VALUES,
    )
    requirement_link = requirement_sub.add_parser("link")
    requirement_link.add_argument("--requirement", required=True)
    requirement_link.add_argument("--acceptance", required=True)

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
    fm_add.add_argument(
        "--status",
        default="identified",
        choices=FAILURE_MODE_STATUS_VALUES,
    )
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

    task_sub.add_parser("list")

    task_start = task_sub.add_parser("start")
    task_start.add_argument("id")

    task_submit = task_sub.add_parser("submit")
    task_submit.add_argument("id")
    task_submit.add_argument("--context-id", default="")
    task_submit.add_argument("--evidence", required=True)

    task_accept = task_sub.add_parser("accept")
    task_accept.add_argument("id")
    task_accept.add_argument("--evidence", required=True)

    task_block = task_sub.add_parser("block")
    task_block.add_argument("id")
    task_block.add_argument("--reason", required=True)

    task_cancel = task_sub.add_parser("cancel")
    task_cancel.add_argument("id")
    task_cancel.add_argument("--reason", default="")

    validation = sub.add_parser("validation")
    validation_sub = validation.add_subparsers(dest="validation_command", required=True)
    validation_record = validation_sub.add_parser("record")
    validation_record.add_argument("--surface", required=True)
    validation_record.add_argument("--acceptance", default="")
    validation_record.add_argument("--findings", required=True)
    validation_record.add_argument("--result", required=True, choices=["pass", "fail", "blocked", "partial"])
    validation_record.add_argument("--failure-mode", action="append", default=[])
    validation_record.add_argument("--residual-risk", default="")

    test_target = sub.add_parser("test-target")
    test_target_sub = test_target.add_subparsers(dest="test_target_command", required=True)
    test_target_add = test_target_sub.add_parser("add")
    test_target_add.add_argument("--id", required=True)
    test_target_add.add_argument("--kind", required=True, choices=["unit", "integration", "lint", "build"])
    test_target_add.add_argument("--command-template", required=True)
    test_target_add.add_argument("--description", default="")
    test_target_add.add_argument("--stack-profile", default="python", choices=["python", "node", "go", "rust", "java", "browser-e2e", "data-integration"])
    test_target_add.add_argument("--container-image", default="")
    test_target_add.add_argument("--requires-sandbox", action="store_true")
    test_target_add.add_argument("--requires-no-network", action="store_true")
    test_target_add.add_argument("--result-format", default="regex", choices=["regex", "junit", "pytest-json", "jest-json", "go-json", "cargo-nextest-json", "playwright-json"])
    test_target_add.add_argument("--result-path", default="")
    test_target_link = test_target_sub.add_parser("link")
    test_target_link.add_argument("--task", required=True)
    test_target_link.add_argument("--target", required=True)
    test_target_qualify = test_target_sub.add_parser("qualify")
    test_target_qualify.add_argument("--id", required=True)
    test_target_qualify.add_argument("--target", required=True)
    test_target_qualify.add_argument("--acceptance", required=True)
    test_target_qualify.add_argument("--rationale", required=True)
    test_target_qualify.add_argument("--by", required=True)
    test_target_sub.add_parser("list")

    verify = sub.add_parser(
        "verify",
        help="run controller-owned immutable verification for a registered target",
    )
    verify_sub = verify.add_subparsers(dest="verify_command", required=True)
    verify_run_parser = verify_sub.add_parser(
        "run",
        description=(
            "Execute a registered target and record schema 31 provenance. "
            "Container images must already be local; execution uses the resolved "
            "immutable identity with --pull=never."
        ),
    )
    verify_run_parser.add_argument("--target", required=True)
    verify_run_parser.add_argument("--acceptance", default="")
    verify_run_parser.add_argument(
        "--failure-mode",
        action="append",
        default=[],
        help="failure mode linked to the same acceptance; medium coverage requires a structured result",
    )
    verify_run_parser.add_argument("--runner", choices=["local", "container"], default="local")
    verify_run_parser.add_argument(
        "--container-image",
        default="",
        help=(
            "requested already-local Docker/Podman image; Kafa resolves and runs "
            "its immutable identity without pulling"
        ),
    )

    decision = sub.add_parser("decision")
    decision_sub = decision.add_subparsers(dest="decision_command", required=True)
    decision_record = decision_sub.add_parser("record")
    decision_record.add_argument("--decision", required=True)
    decision_record.add_argument("--reason", required=True)

    finding = sub.add_parser("finding")
    finding_sub = finding.add_subparsers(dest="finding_command", required=True)
    finding_record = finding_sub.add_parser("record")
    finding_record.add_argument("--id", required=True)
    finding_record.add_argument("--surface", required=True)
    finding_record.add_argument("--severity", required=True, choices=["low", "medium", "high", "critical"])
    finding_record.add_argument("--status", required=True, choices=["open", "resolved", "accepted", "false-positive"])
    finding_record.add_argument("--summary", required=True)
    finding_record.add_argument("--waived-by", default="")
    finding_record.add_argument("--waiver-reason", default="")
    finding_record.add_argument("--waiver-scope", default="")
    finding_record.add_argument("--waived-revision", type=int)
    finding_record.add_argument("--waiver-expires-at", default="")

    gate = sub.add_parser("gate")
    gate_sub = gate.add_subparsers(dest="gate_command", required=True)
    gate_record = gate_sub.add_parser("record")
    gate_record.add_argument("--reviewer-context", required=True, choices=["fresh", "same-context-degraded"])
    gate_record.add_argument("--result", required=True, choices=["pass", "fail", "conditional", "blocked"])
    gate_record.add_argument("--gate", default="independent_qa")
    gate_record.add_argument("--blocking-findings", default="")
    gate_record.add_argument(
        "--residual-risk",
        default="",
        help="explicit residual-risk text; required for a passing same-context-degraded gate",
    )
    gate_record.add_argument("--finding", action="append", default=[])
    gate_record.add_argument("--reviewer-context-id", default="")
    gate_record.add_argument("--qualification", action="append", default=[])

    delivery = sub.add_parser("delivery")
    delivery_sub = delivery.add_subparsers(dest="delivery_command", required=True)
    delivery_sub.add_parser("ready")
    delivery_record = delivery_sub.add_parser("record")
    delivery_record.add_argument(
        "--scope",
        required=True,
        help="human scope/rationale; structured relations remain authoritative",
    )
    supplemental_help = (
        "legacy supplemental note only; does not affect authoritative "
        "relations, readiness, gate, or trust"
    )
    delivery_record.add_argument("--acceptance", default="", help=supplemental_help)
    delivery_record.add_argument("--changed-files", default="", help=supplemental_help)
    delivery_record.add_argument("--validation", default="", help=supplemental_help)
    delivery_record.add_argument("--qa", default="", help=supplemental_help)
    delivery_record.add_argument(
        "--failure-mode-coverage",
        default="",
        help=supplemental_help,
    )
    delivery_record.add_argument("--quality-gate", default="", help=supplemental_help)
    delivery_record.add_argument(
        "--data-config-notes",
        default="",
        help="human data/config exception note",
    )
    delivery_record.add_argument(
        "--known-gaps",
        default="",
        help="human known-gap note",
    )
    delivery_record.add_argument(
        "--handoff",
        default="",
        help="human handoff note",
    )

    projection = sub.add_parser("projection")
    projection_sub = projection.add_subparsers(dest="projection_command", required=True)
    projection_sub.add_parser("rebuild")

    return parser


def main() -> int:
    retired_message = retired_v2_invocation(sys.argv[1:])
    if retired_message:
        print(f"ERROR: {retired_message}")
        return 1
    parser = build_parser()
    args = parser.parse_args()
    root = Path(args.root).resolve()

    def mutate(_command: str, fn) -> None:
        print(fn())

    def emit_operator(report) -> None:
        if getattr(args, "json", False):
            sys.stdout.write(render_json(report))
        elif getattr(args, "verbose", False):
            sys.stdout.write(
                render_verbose(operator_verbose_lines(report))
            )
        else:
            sys.stdout.write(render_concise(report))

    try:
        needs_initialized = (
            args.command in {"validate", "verify"}
            or (
                args.command == "cycle"
                and args.cycle_command in {"status", "audit", "outcome-report"}
            )
        )
        if needs_initialized and not runtime_initialized(root):
            print("\n".join(uninitialized_lines(root)))
            return 1
        if args.command == "init":
            if args.dry_run:
                print("DRY-RUN: would create .ai-team/state/harness.db, local harness views, and .codex/agents templates")
                return 0
            init_runtime(root)
            print("OK: project harness initialized")
        elif args.command == "status":
            report = status_operator_report(root)
            emit_operator(report)
            if report.state in {"not-initialized", "recovery-required", "error"}:
                return 1
        elif args.command == "doctor":
            report = doctor_operator_report(root)
            emit_operator(report)
            if report.state != "healthy":
                return 1
        elif args.command == "cycle" and args.cycle_command == "start":
            mutate(
                "cycle.start",
                lambda: (
                    cycle_start(root, args.id, args.name, args.goal, base_ref=args.base_ref),
                    f"OK: cycle started {args.id}",
                )[1],
            )
        elif args.command == "cycle" and args.cycle_command == "status":
            row = cycle_status(root, args.id)
            if args.json:
                print(json.dumps(row, ensure_ascii=False, sort_keys=True))
            else:
                print(f"cycle={row['id']} status={row['status']} phase={row['phase']} candidate={row.get('candidate_sha', '')}")
        elif args.command == "cycle" and args.cycle_command == "audit":
            report = cycle_audit(root, args.id)
            if args.json:
                print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            else:
                print(
                    f"cycle={report['cycle']['id']} "
                    f"status={report['cycle']['status']} "
                    f"consistent={str(report['consistent']).lower()} "
                    f"facts_sha256={report['facts_sha256']}"
                )
                for blocker in report["blockers"]:
                    print(
                        f"[{blocker['code']}] {blocker['message']} "
                        f"entity={blocker['entity_type']}:{blocker['entity_id']}"
                    )
            if not report["consistent"]:
                return 1
        elif args.command == "cycle" and args.cycle_command == "close":
            mutate("cycle.close", lambda: (cycle_close(root, args.status), f"OK: cycle closed {args.status}")[1])
        elif args.command == "cycle" and args.cycle_command == "outcome-record":
            mutate(
                "cycle.outcome-record",
                lambda: (
                    record_outcome_observation(
                        root,
                        args.id,
                        args.kind,
                        args.value,
                        args.details,
                        args.by,
                        args.observed_at,
                        cycle_id=args.cycle_id,
                    ),
                    f"OK: outcome observation recorded {args.id}",
                )[1],
            )
        elif args.command == "cycle" and args.cycle_command == "outcome-report":
            report = outcome_report(root)
            if args.json:
                print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            else:
                print(
                    "outcome-report "
                    f"version={report['report_version']} "
                    f"cycle={report['cycle_id']} "
                    f"observations={report['observation_count']}"
                )
        elif args.command == "quickstart" and args.quickstart_command == "status":
            report = quickstart_operator_report(root)
            emit_operator(report)
            if report.state in {"recovery-required", "error"}:
                return 1
        elif args.command == "quickstart" and args.quickstart_command == "minimal":
            print(
                "\n".join(
                    quickstart_minimal(
                        root,
                        args.id,
                        args.goal,
                        args.acceptance,
                        args.task,
                        args.test_command,
                        execute=args.execute,
                    )
                )
            )
        elif args.command == "quickstart" and args.quickstart_command == "delivery-plan":
            try:
                plan = load_delivery_plan(Path(args.file).expanduser())
            except DeliveryPlanError as exc:
                raise HarnessError(str(exc)) from exc
            report = apply_delivery_plan(root, plan, dry_run=args.dry_run)
            if args.json:
                print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            else:
                prefix = "DRY-RUN" if args.dry_run else "OK"
                print(
                    f"{prefix}: delivery-plan {report['plan_id']} "
                    f"changed={str(report['changed']).lower()}"
                )
                if args.verbose:
                    for key, value in sorted(report["ids"].items()):
                        print(f"{key}: {value}")
                    for mutation in report["mutations"]:
                        print(f"mutation: {mutation}")
        elif args.command == "quickstart" and args.quickstart_command == "verified-patch":
            report = verified_patch(root, args.id)
            if args.json:
                print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            else:
                print(
                    "OK: verified-patch "
                    f"verification={report['verification_status']} "
                    f"task={report['task_status']} gate={report['gate_status']} "
                    f"delivery={report['delivery_status']}"
                )
                if args.verbose:
                    for key, value in sorted(report.items()):
                        print(f"{key}: {value}")
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
        elif args.command == "baseline" and args.baseline_command == "freeze":
            mutate("baseline.freeze", lambda: (freeze_baseline(root, args.id, args.summary, by=args.by), f"OK: baseline frozen {args.id}")[1])
        elif args.command == "baseline" and args.baseline_command == "confirm":
            mutate(
                "baseline.confirm",
                lambda: (
                    confirm_baseline(
                        root,
                        args.id,
                        args.summary,
                        by=args.by,
                    ),
                    f"OK: baseline confirmed {args.id}",
                )[1],
            )
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
            mutate("acceptance.add", lambda: (add_acceptance(root, args.id, args.criterion, args.priority), f"OK: acceptance added {args.id}")[1])
        elif args.command == "requirement" and args.requirement_command == "add":
            mutate("requirement.add", lambda: (add_requirement(root, args.id, args.kind, args.body, priority=args.priority, status=args.status), f"OK: requirement added {args.id}")[1])
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
                    ),
                    f"OK: task added {args.id}",
                )[1],
            )
        elif args.command == "task" and args.task_command == "list":
            lines = list_tasks(root)
            print("\n".join(lines) if lines else "NO_TASKS")
        elif args.command == "task" and args.task_command == "start":
            mutate(
                "task.start",
                lambda: (start_task(root, args.id), f"OK: task started {args.id}")[1],
            )
        elif args.command == "task" and args.task_command == "submit":
            mutate(
                "task.submit",
                lambda: (
                    submit_task(root, args.id, args.evidence, context_id=args.context_id),
                    f"OK: task submitted {args.id}",
                )[1],
            )
        elif args.command == "task" and args.task_command == "accept":
            mutate(
                "task.accept",
                lambda: (accept_task(root, args.id, args.evidence), f"OK: task accepted {args.id}")[1],
            )
        elif args.command == "task" and args.task_command == "block":
            mutate(
                "task.block",
                lambda: (block_task(root, args.id, args.reason), f"OK: task blocked {args.id}")[1],
            )
        elif args.command == "task" and args.task_command == "cancel":
            mutate(
                "task.cancel",
                lambda: (cancel_task(root, args.id, args.reason), f"OK: task cancelled {args.id}")[1],
            )
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
                        failure_modes=", ".join(args.failure_mode),
                        residual_risk=args.residual_risk,
                    ),
                    "OK: audit-only validation judgment recorded",
                )[1],
            )
            print(
                "NOTE: validation record stores judgment only; use verify run to create "
                "gate-eligible immutable execution evidence."
            )
        elif args.command == "test-target" and args.test_target_command == "add":
            mutate(
                "test-target.add",
                lambda: (
                    add_test_target(
                        root,
                        args.id,
                        args.kind,
                        args.command_template,
                        args.description,
                        stack_profile=args.stack_profile,
                        container_image=args.container_image,
                        requires_sandbox=args.requires_sandbox,
                        requires_no_network=args.requires_no_network,
                        result_format=args.result_format,
                        result_path=args.result_path,
                    ),
                    f"OK: test target recorded {args.id}",
                )[1],
            )
        elif args.command == "test-target" and args.test_target_command == "link":
            mutate("test-target.link", lambda: (link_task_test_target(root, args.task, args.target), f"OK: test target linked {args.task}->{args.target}")[1])
        elif args.command == "test-target" and args.test_target_command == "qualify":
            mutate(
                "test-target.qualify",
                lambda: (
                    qualify_test_target(
                        root,
                        args.id,
                        args.target,
                        args.acceptance,
                        args.rationale,
                        args.by,
                    ),
                    f"OK: test target qualification recorded {args.id}",
                )[1],
            )
        elif args.command == "test-target" and args.test_target_command == "list":
            print("\n".join(list_test_targets(root)))
        elif args.command == "verify" and args.verify_command == "run":
            execution_id, validation_id = verify_run(
                root,
                args.target,
                acceptance=args.acceptance,
                failure_modes=args.failure_mode,
                runner=args.runner,
                container_image=args.container_image,
            )
            print(
                f"OK: verification recorded execution={execution_id} "
                f"validation={validation_id}"
            )
        elif args.command == "decision" and args.decision_command == "record":
            mutate("decision.record", lambda: (record_decision(root, args.decision, args.reason), "OK: decision recorded")[1])
        elif args.command == "finding" and args.finding_command == "record":
            mutate("finding.record", lambda: (record_finding(
                root, args.id, args.surface, args.severity, args.status, args.summary,
                waived_by=args.waived_by,
                waiver_reason=args.waiver_reason, waiver_scope=args.waiver_scope,
                waived_revision=args.waived_revision, waiver_expires_at=args.waiver_expires_at,
            ), f"OK: finding recorded {args.id}")[1])
        elif args.command == "gate" and args.gate_command == "record":
            mutate(
                "gate.record",
                lambda: (
                    record_gate(
                        root,
                        args.reviewer_context,
                        args.result,
                        gate=args.gate,
                        blocking_findings=args.blocking_findings,
                        residual_risk=args.residual_risk,
                        findings=", ".join(args.finding),
                        reviewer_context_id=args.reviewer_context_id,
                        qualifications=args.qualification,
                    ),
                    f"OK: quality gate recorded {args.gate}={args.result}",
                )[1],
            )
        elif args.command == "delivery" and args.delivery_command == "ready":
            mutate(
                "delivery.ready",
                lambda: (
                    enter_delivery_readiness(root),
                    "OK: delivery readiness entered",
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
                        known_gaps=args.known_gaps,
                        handoff=args.handoff,
                    ),
                    "OK: delivery recorded",
                )[1],
            )
        elif args.command == "projection" and args.projection_command == "rebuild":
            projection_rebuild(root)
            print("OK: projections rebuilt")
        else:
            parser.error("unknown command")
    except HarnessError as exc:
        operator_command = (
            args.command in {"status", "doctor"}
            or (
                args.command == "quickstart"
                and args.quickstart_command == "status"
            )
        )
        if operator_command and getattr(args, "json", False):
            print(
                render_json(
                    operator_error_report(root, exc, allow_init=False)
                ),
                end="",
            )
        elif (
            args.command == "quickstart"
            and args.quickstart_command in {"delivery-plan", "verified-patch"}
            and getattr(args, "json", False)
        ):
            print(
                json.dumps(
                    {"ok": False, "error": exception_text(exc)},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        else:
            print(f"ERROR: {exception_text(exc)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

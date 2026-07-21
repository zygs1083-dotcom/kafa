#!/usr/bin/env python3
"""Run the fixed P0 false-delivery before/after regression benchmark."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "codex-project-harness"
SCRIPTS_ROOT = PLUGIN_ROOT / "scripts"
for path in (REPO_ROOT, PLUGIN_ROOT, SCRIPTS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from run_agent_e2e_eval import evaluation_source_identity  # noqa: E402


REPORT_VERSION = "kafa-outcome-benchmark-v1"
EVIDENCE_MODE = "regression-benchmark"
BASELINE_SOURCE = (
    "docs/audits/2026-07-20-delivery-integrity-hardening-baseline.md"
    "#p0-red-reproduction"
)
FIELD_METRIC_IDS = (
    "false_green_prevented_count",
    "escaped_defect_count",
    "rework_rate_per_delivery",
    "migration_recovery_success_rate",
    "time_to_verified_delivery_seconds",
    "qualification_coverage_rate",
)
SCENARIOS: tuple[dict[str, str], ...] = (
    {
        "id": "minimum-graph-empty",
        "title": "delivery without the minimum graph",
        "test_id": (
            "tests.test_delivery_integrity_p0_contracts."
            "MinimumDeliveryGraphRedTests.test_empty_graph_direct_api_fails_closed"
        ),
        "before_result": "false-delivery",
        "after_result": "fail-closed",
    },
    {
        "id": "cancelled-sole-task-coverage",
        "title": "cancelled task as sole acceptance coverage",
        "test_id": (
            "tests.test_delivery_integrity_p0_contracts."
            "CancelledTaskCoverageRedTests."
            "test_cancelled_sole_coverage_is_rejected_by_every_delivery_surface"
        ),
        "before_result": "false-delivery",
        "after_result": "fail-closed",
    },
    {
        "id": "unqualified-unrelated-target",
        "title": "unrelated target as acceptance evidence",
        "test_id": (
            "tests.test_delivery_integrity_p0_contracts."
            "QualifiedAcceptanceEvidenceRedTests."
            "test_existing_unrelated_target_cannot_claim_acceptance_without_qualification"
        ),
        "before_result": "false-delivery",
        "after_result": "fail-closed",
    },
    {
        "id": "direct-record-before-readiness",
        "title": "low-level delivery record before readiness",
        "test_id": (
            "tests.test_delivery_integrity_p0_contracts."
            "UnifiedPrerequisiteAndReadinessRedTests."
            "test_record_mode_requires_readiness_even_when_all_evidence_passes"
        ),
        "before_result": "false-delivery",
        "after_result": "fail-closed",
    },
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def run_scenario(scenario: Mapping[str, str]) -> dict[str, Any]:
    command = [
        sys.executable,
        "-B",
        "-W",
        "error::ResourceWarning",
        "-m",
        "unittest",
        str(scenario["test_id"]),
        "-v",
    ]
    started = time.perf_counter()
    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    duration = round(time.perf_counter() - started, 6)
    passed = result.returncode == 0
    return {
        "status": "passed" if passed else "failed",
        "result": scenario["after_result"] if passed else "not-proven",
        "command": command,
        "returncode": result.returncode,
        "duration_seconds": duration,
        "stdout_sha256": _sha256(result.stdout),
        "stderr_sha256": _sha256(result.stderr),
        "stdout_tail": result.stdout[-1000:],
        "stderr_tail": result.stderr[-2000:],
        "not_run_reason": "",
    }


def _not_run_result(reason: str) -> dict[str, Any]:
    return {
        "status": "not-run",
        "result": None,
        "command": [],
        "returncode": None,
        "duration_seconds": None,
        "stdout_sha256": None,
        "stderr_sha256": None,
        "stdout_tail": "",
        "stderr_tail": "",
        "not_run_reason": reason,
    }


def build_report(
    *,
    run_after: bool = True,
    scenario_runner: Callable[[Mapping[str, str]], dict[str, Any]] = run_scenario,
    generated_at: str | None = None,
    source_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    started_at = generated_at or _utc_now()
    scenarios: list[dict[str, Any]] = []
    for definition in SCENARIOS:
        after = (
            scenario_runner(definition)
            if run_after
            else _not_run_result("benchmark execution was explicitly disabled")
        )
        scenarios.append(
            {
                "id": definition["id"],
                "title": definition["title"],
                "test_id": definition["test_id"],
                "before": {
                    "status": "historical-reproduced",
                    "result": definition["before_result"],
                    "source": BASELINE_SOURCE,
                },
                "after": after,
            }
        )

    timestamp = generated_at or _utc_now()

    passed_count = sum(
        1 for scenario in scenarios if scenario["after"]["status"] == "passed"
    )
    failed_count = sum(
        1 for scenario in scenarios if scenario["after"]["status"] == "failed"
    )
    not_run_count = sum(
        1 for scenario in scenarios if scenario["after"]["status"] == "not-run"
    )
    inventory = [scenario["id"] for scenario in scenarios]
    fixed_inventory = [scenario["id"] for scenario in SCENARIOS]
    inventory_matches = inventory == fixed_inventory
    benchmark_status = (
        "passed"
        if passed_count == len(SCENARIOS) and inventory_matches
        else "not-run"
        if not_run_count == len(SCENARIOS) and inventory_matches
        else "failed"
    )
    field_reason = (
        "this source-repository regression benchmark did not observe an operator "
        "project or a completed field window; fixture results cannot substitute"
    )
    return {
        "report_version": REPORT_VERSION,
        "evidence_mode": EVIDENCE_MODE,
        "started_at": started_at,
        "generated_at": timestamp,
        "evaluation_source": dict(source_identity or evaluation_source_identity()),
        "benchmark_status": benchmark_status,
        "inventory": {
            "version": "kafa-p0-false-delivery-v1",
            "scenario_ids": fixed_inventory,
            "scenario_count": len(SCENARIOS),
            "before_after_inventory_matches": inventory_matches,
        },
        "numerator": {
            "definition": "fixed P0 scenarios that falsely delivered before hardening",
            "before_false_delivery_count": len(SCENARIOS),
            "after_fail_closed_count": passed_count,
        },
        "denominator": {
            "definition": "all scenarios in kafa-p0-false-delivery-v1",
            "value": len(SCENARIOS),
            "applicability": "required",
        },
        "window": {
            "kind": "fixed-before-after-regression",
            "before": "2026-07-20 historical reproduced red checkpoint",
            "after": timestamp if run_after else None,
            "complete": benchmark_status == "passed",
        },
        "missing_data_semantics": "not-run",
        "field_improvement_claimed": False,
        "summary": {
            "scenario_count": len(SCENARIOS),
            "passed_count": passed_count,
            "failed_count": failed_count,
            "not_run_count": not_run_count,
            "regression_closure_rate": (
                round(passed_count / len(SCENARIOS), 6)
                if benchmark_status != "not-run"
                else None
            ),
        },
        "scenarios": scenarios,
        "field_evidence": {
            "evidence_mode": "field",
            "status": "not-run",
            "reason": field_reason,
            "metrics": {
                metric_id: {
                    "status": "not-run",
                    "value": None,
                    "reason": field_reason,
                }
                for metric_id in FIELD_METRIC_IDS
            },
        },
    }


def validate_report(report: Mapping[str, Any]) -> list[str]:
    issues: list[str] = []
    if report.get("report_version") != REPORT_VERSION:
        issues.append("unexpected report_version")
    if report.get("evidence_mode") != EVIDENCE_MODE:
        issues.append("benchmark evidence_mode must be regression-benchmark")
    inventory = report.get("inventory")
    expected_ids = [scenario["id"] for scenario in SCENARIOS]
    if not isinstance(inventory, dict):
        issues.append("missing benchmark inventory")
    else:
        if inventory.get("scenario_ids") != expected_ids:
            issues.append("benchmark scenario inventory drift")
        if inventory.get("scenario_count") != len(expected_ids):
            issues.append("benchmark scenario count drift")
        if inventory.get("before_after_inventory_matches") is not True:
            issues.append("before/after benchmark inventory mismatch")
    scenarios = report.get("scenarios")
    if not isinstance(scenarios, list) or len(scenarios) != len(SCENARIOS):
        issues.append("benchmark scenarios are incomplete")
    else:
        for expected, actual in zip(SCENARIOS, scenarios):
            if actual.get("id") != expected["id"]:
                issues.append(f"scenario order/id drift: {expected['id']}")
                continue
            before = actual.get("before")
            after = actual.get("after")
            if not isinstance(before, dict) or before.get("result") != "false-delivery":
                issues.append(f"scenario before evidence is invalid: {expected['id']}")
            if not isinstance(after, dict) or after.get("status") not in {
                "passed",
                "failed",
                "not-run",
            }:
                issues.append(f"scenario after status is invalid: {expected['id']}")
            elif after.get("status") == "passed" and after.get("result") != "fail-closed":
                issues.append(f"passing scenario did not prove fail-closed: {expected['id']}")
            elif after.get("status") == "not-run" and not after.get("not_run_reason"):
                issues.append(f"not-run scenario lacks reason: {expected['id']}")
    if report.get("field_improvement_claimed") is not False:
        issues.append("regression benchmark must not claim field improvement")
    field = report.get("field_evidence")
    if not isinstance(field, dict) or field.get("status") != "not-run":
        issues.append("field evidence must remain explicitly not-run")
    elif set(field.get("metrics", {})) != set(FIELD_METRIC_IDS):
        issues.append("field metric not-run inventory is incomplete")
    return issues


def write_report(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the fixed P0 delivery-integrity outcome benchmark."
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--not-run",
        action="store_true",
        help="Persist an explicit not-run report without executing scenarios.",
    )
    args = parser.parse_args()

    report = build_report(run_after=not args.not_run)
    issues = validate_report(report)
    if issues:
        raise SystemExit("invalid outcome benchmark report: " + "; ".join(issues))
    write_report(args.out, report)
    print(
        "OK: delivery-integrity outcome benchmark "
        f"status={report['benchmark_status']} out={args.out}"
    )
    return 1 if report["benchmark_status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())

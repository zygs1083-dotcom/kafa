#!/usr/bin/env python3
"""Run the fixed P0 false-delivery before/after regression benchmark."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "codex-project-harness"
SCRIPTS_ROOT = PLUGIN_ROOT / "scripts"
for path in (REPO_ROOT, PLUGIN_ROOT, SCRIPTS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from run_agent_e2e_eval import evaluation_source_identity  # noqa: E402


REPORT_VERSION = "kafa-outcome-benchmark-v2"
LEGACY_REPORT_VERSION = "kafa-outcome-benchmark-v1"
EVIDENCE_MODE = "regression-benchmark"
BASELINE_SOURCE = (
    "docs/audits/2026-07-20-delivery-integrity-hardening-baseline.md"
    "#p0-red-reproduction"
)
LEGACY_FIELD_METRIC_IDS = (
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
        "field_metrics_status": "not-observed",
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
    }


def validate_report(report: Mapping[str, Any]) -> list[str]:
    issues: list[str] = []

    def _parse_timestamp(value: Any, field: str) -> datetime | None:
        if not isinstance(value, str) or not value:
            issues.append(f"{field} must be a timezone-aware timestamp")
            return None
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            issues.append(f"{field} must be a timezone-aware timestamp")
            return None
        if parsed.utcoffset() is None:
            issues.append(f"{field} must be a timezone-aware timestamp")
            return None
        return parsed.astimezone(timezone.utc)

    def _is_sha256(value: Any) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )

    def _matches_expected(value: Any, expected: Any) -> bool:
        if expected is None:
            return value is None
        if isinstance(expected, bool):
            return value is expected
        if type(expected) is int:
            return type(value) is int and value == expected
        if type(expected) is float:
            return type(value) is float and value == expected
        return value == expected

    def _check_value(
        container: Mapping[str, Any], field: str, expected: Any, label: str
    ) -> None:
        if not _matches_expected(container.get(field), expected):
            issues.append(f"{label} must equal scenario-derived value {expected!r}")

    report_version = report.get("report_version")
    if report_version not in {REPORT_VERSION, LEGACY_REPORT_VERSION}:
        issues.append("unexpected report_version")
    common_top_level = {
        "report_version",
        "evidence_mode",
        "started_at",
        "generated_at",
        "evaluation_source",
        "benchmark_status",
        "inventory",
        "numerator",
        "denominator",
        "window",
        "missing_data_semantics",
        "field_improvement_claimed",
        "summary",
        "scenarios",
    }
    expected_top_level = common_top_level | (
        {"field_evidence"}
        if report_version == LEGACY_REPORT_VERSION
        else {"field_metrics_status"}
    )
    if set(report) != expected_top_level:
        issues.append(
            "benchmark report keys mismatch: "
            f"actual={sorted(report)} expected={sorted(expected_top_level)}"
        )
    if report.get("evidence_mode") != EVIDENCE_MODE:
        issues.append("benchmark evidence_mode must be regression-benchmark")

    started_at = _parse_timestamp(report.get("started_at"), "started_at")
    generated_at = _parse_timestamp(report.get("generated_at"), "generated_at")
    if started_at is not None and generated_at is not None and started_at > generated_at:
        issues.append("started_at must not be later than generated_at")

    evaluation_source = report.get("evaluation_source")
    source_keys = {
        "generated_at",
        "git_head",
        "git_dirty",
        "workspace_sha256",
        "status_sha256",
        "status_entry_count",
        "source_scope",
    }
    if not isinstance(evaluation_source, Mapping) or set(evaluation_source) != source_keys:
        issues.append("benchmark evaluation_source shape is invalid")
    else:
        _parse_timestamp(
            evaluation_source.get("generated_at"),
            "evaluation_source.generated_at",
        )
        git_head = evaluation_source.get("git_head")
        if (
            not isinstance(git_head, str)
            or len(git_head) not in {40, 64}
            or git_head == "0" * len(git_head)
            or any(character not in "0123456789abcdef" for character in git_head)
        ):
            issues.append("benchmark evaluation_source git_head is invalid")
        for digest_field in ("workspace_sha256", "status_sha256"):
            digest = evaluation_source.get(digest_field)
            if not _is_sha256(digest) or digest == "0" * 64:
                issues.append(
                    f"benchmark evaluation_source {digest_field} is invalid"
                )
        dirty = evaluation_source.get("git_dirty")
        status_count = evaluation_source.get("status_entry_count")
        if not isinstance(dirty, bool):
            issues.append("benchmark evaluation_source git_dirty is invalid")
        if type(status_count) is not int or status_count < 0:
            issues.append("benchmark evaluation_source status_entry_count is invalid")
        elif isinstance(dirty, bool):
            if dirty is not (status_count > 0):
                issues.append(
                    "benchmark evaluation_source dirty/count state is inconsistent"
                )
            empty_status = hashlib.sha256(b"").hexdigest()
            if status_count == 0 and evaluation_source.get("status_sha256") != empty_status:
                issues.append(
                    "benchmark evaluation_source clean status digest is inconsistent"
                )
            if status_count > 0 and evaluation_source.get("status_sha256") == empty_status:
                issues.append(
                    "benchmark evaluation_source dirty status digest is inconsistent"
                )
        source_scope = evaluation_source.get("source_scope")
        if (
            not isinstance(source_scope, list)
            or not source_scope
            or any(not _valid_source_scope(value) for value in source_scope)
        ):
            issues.append("benchmark evaluation_source source_scope is invalid")
        elif len(source_scope) != len(set(source_scope)):
            issues.append("benchmark evaluation_source source_scope is invalid")

    inventory = report.get("inventory")
    expected_ids = [scenario["id"] for scenario in SCENARIOS]
    if not isinstance(inventory, Mapping):
        issues.append("missing benchmark inventory")
    else:
        if inventory.get("version") != "kafa-p0-false-delivery-v1":
            issues.append("unexpected benchmark inventory version")
        if inventory.get("scenario_ids") != expected_ids:
            issues.append("benchmark scenario inventory drift")
        if not _matches_expected(inventory.get("scenario_count"), len(expected_ids)):
            issues.append("benchmark scenario count drift")

    scenarios = report.get("scenarios")
    scenario_count = len(scenarios) if isinstance(scenarios, list) else 0
    passed_count = 0
    failed_count = 0
    not_run_count = 0
    before_false_delivery_count = 0
    after_fail_closed_count = 0
    actual_ids: list[Any] = []
    if not isinstance(scenarios, list) or scenario_count != len(SCENARIOS):
        issues.append("benchmark scenarios are incomplete")
    else:
        for expected, actual in zip(SCENARIOS, scenarios):
            if not isinstance(actual, Mapping):
                issues.append(f"scenario entry is invalid: {expected['id']}")
                actual_ids.append(None)
                continue
            actual_ids.append(actual.get("id"))
            if actual.get("id") != expected["id"]:
                issues.append(f"scenario order/id drift: {expected['id']}")
            if actual.get("title") != expected["title"]:
                issues.append(f"scenario title drift: {expected['id']}")
            if actual.get("test_id") != expected["test_id"]:
                issues.append(f"scenario test id drift: {expected['id']}")
            before = actual.get("before")
            after = actual.get("after")
            if not isinstance(before, Mapping):
                issues.append(f"scenario before evidence is invalid: {expected['id']}")
            else:
                if before.get("status") != "historical-reproduced":
                    issues.append(f"scenario before status is invalid: {expected['id']}")
                if before.get("result") != expected["before_result"]:
                    issues.append(f"scenario before evidence is invalid: {expected['id']}")
                else:
                    before_false_delivery_count += 1
                if before.get("source") != BASELINE_SOURCE:
                    issues.append(f"scenario before source is invalid: {expected['id']}")

            if not isinstance(after, Mapping):
                issues.append(f"scenario after status is invalid: {expected['id']}")
                continue

            status = after.get("status")
            if status not in {"passed", "failed", "not-run"}:
                issues.append(f"scenario after status is invalid: {expected['id']}")
                continue
            if status == "passed":
                passed_count += 1
                if after.get("result") != expected["after_result"]:
                    issues.append(
                        f"passing scenario did not prove fail-closed: {expected['id']}"
                    )
                else:
                    after_fail_closed_count += 1
            elif status == "failed":
                failed_count += 1
                if after.get("result") != "not-proven":
                    issues.append(f"failed scenario result is invalid: {expected['id']}")
            else:
                not_run_count += 1
                if after.get("result") is not None:
                    issues.append(f"not-run scenario result must be null: {expected['id']}")

            if status in {"passed", "failed"}:
                command = after.get("command")
                if (
                    not isinstance(command, list)
                    or not command
                    or not all(isinstance(part, str) and part for part in command)
                    or expected["test_id"] not in command
                ):
                    issues.append(f"executed scenario command is invalid: {expected['id']}")
                returncode = after.get("returncode")
                if status == "passed":
                    if type(returncode) is not int or returncode != 0:
                        issues.append(
                            f"passing scenario returncode must be zero: {expected['id']}"
                        )
                elif type(returncode) is not int or returncode == 0:
                    issues.append(
                        f"failed scenario returncode must be non-zero: {expected['id']}"
                    )
                duration = after.get("duration_seconds")
                if (
                    not isinstance(duration, (int, float))
                    or isinstance(duration, bool)
                    or not math.isfinite(duration)
                    or duration < 0
                ):
                    issues.append(f"executed scenario duration is invalid: {expected['id']}")
                for digest_field in ("stdout_sha256", "stderr_sha256"):
                    if not _is_sha256(after.get(digest_field)):
                        issues.append(
                            f"executed scenario {digest_field} is invalid: {expected['id']}"
                        )
                if after.get("not_run_reason") != "":
                    issues.append(
                        f"executed scenario must not have not-run reason: {expected['id']}"
                    )
            else:
                if after.get("command") != []:
                    issues.append(f"not-run scenario command must be empty: {expected['id']}")
                for field in (
                    "returncode",
                    "duration_seconds",
                    "stdout_sha256",
                    "stderr_sha256",
                ):
                    if after.get(field) is not None:
                        issues.append(
                            f"not-run scenario {field} must be null: {expected['id']}"
                        )
                reason = after.get("not_run_reason")
                if not isinstance(reason, str) or not reason.strip():
                    issues.append(f"not-run scenario lacks reason: {expected['id']}")

    inventory_matches = actual_ids == expected_ids
    if isinstance(inventory, Mapping):
        if inventory.get("before_after_inventory_matches") is not inventory_matches:
            issues.append("before/after benchmark inventory match flag is inconsistent")
        if not inventory_matches:
            issues.append("before/after benchmark inventory mismatch")

    benchmark_status = (
        "passed"
        if passed_count == len(SCENARIOS) and inventory_matches
        else "not-run"
        if not_run_count == len(SCENARIOS) and inventory_matches
        else "failed"
    )
    if report.get("benchmark_status") != benchmark_status:
        issues.append(
            f"benchmark_status must equal scenario-derived value {benchmark_status!r}"
        )

    numerator = report.get("numerator")
    if not isinstance(numerator, Mapping):
        issues.append("missing benchmark numerator")
    else:
        if numerator.get("definition") != (
            "fixed P0 scenarios that falsely delivered before hardening"
        ):
            issues.append("unexpected benchmark numerator definition")
        _check_value(
            numerator,
            "before_false_delivery_count",
            before_false_delivery_count,
            "numerator.before_false_delivery_count",
        )
        _check_value(
            numerator,
            "after_fail_closed_count",
            after_fail_closed_count,
            "numerator.after_fail_closed_count",
        )

    denominator = report.get("denominator")
    if not isinstance(denominator, Mapping):
        issues.append("missing benchmark denominator")
    else:
        if denominator.get("definition") != "all scenarios in kafa-p0-false-delivery-v1":
            issues.append("unexpected benchmark denominator definition")
        if denominator.get("applicability") != "required":
            issues.append("benchmark denominator must remain required")
        _check_value(denominator, "value", scenario_count, "denominator.value")

    closure_rate = (
        None
        if benchmark_status == "not-run"
        else round(passed_count / scenario_count, 6)
        if scenario_count
        else 0.0
    )
    summary = report.get("summary")
    if not isinstance(summary, Mapping):
        issues.append("missing benchmark summary")
    else:
        _check_value(summary, "scenario_count", scenario_count, "summary.scenario_count")
        _check_value(summary, "passed_count", passed_count, "summary.passed_count")
        _check_value(summary, "failed_count", failed_count, "summary.failed_count")
        _check_value(summary, "not_run_count", not_run_count, "summary.not_run_count")
        _check_value(
            summary,
            "regression_closure_rate",
            closure_rate,
            "summary.regression_closure_rate",
        )

    window = report.get("window")
    if not isinstance(window, Mapping):
        issues.append("missing benchmark window")
    else:
        if window.get("kind") != "fixed-before-after-regression":
            issues.append("unexpected benchmark window kind")
        if window.get("before") != "2026-07-20 historical reproduced red checkpoint":
            issues.append("unexpected benchmark before window")
        if window.get("complete") is not (benchmark_status == "passed"):
            issues.append("window.complete is inconsistent with scenario-derived status")
        after_value = window.get("after")
        if benchmark_status == "not-run":
            if after_value is not None:
                issues.append("not-run benchmark window.after must be null")
        else:
            after_at = _parse_timestamp(after_value, "window.after")
            if (
                after_at is not None
                and generated_at is not None
                and after_at != generated_at
            ):
                issues.append("window.after must equal generated_at")
            if after_at is not None and started_at is not None and after_at < started_at:
                issues.append("window.after must not be earlier than started_at")

    if report.get("missing_data_semantics") != "not-run":
        issues.append("missing data semantics must remain not-run")
    if report.get("field_improvement_claimed") is not False:
        issues.append("regression benchmark must not claim field improvement")
    if report_version == REPORT_VERSION:
        if report.get("field_metrics_status") != "not-observed":
            issues.append("field metrics status must remain not-observed")
        if "field_evidence" in report:
            issues.append("field metrics must not expand without an observed window")
    elif report_version == LEGACY_REPORT_VERSION:
        field = report.get("field_evidence")
        if "field_metrics_status" in report:
            issues.append("legacy report must not use v2 field metrics sentinel")
        if not isinstance(field, Mapping) or field.get("status") != "not-run":
            issues.append("legacy field evidence must remain explicitly not-run")
        else:
            if field.get("evidence_mode") != "field":
                issues.append("legacy field evidence mode is invalid")
            field_reason = field.get("reason")
            if not isinstance(field_reason, str) or not field_reason.strip():
                issues.append("legacy field evidence reason is missing")
            metrics = field.get("metrics")
            if not isinstance(metrics, Mapping) or set(metrics) != set(
                LEGACY_FIELD_METRIC_IDS
            ):
                issues.append("legacy field metric not-run inventory is incomplete")
            else:
                for metric_id in LEGACY_FIELD_METRIC_IDS:
                    metric = metrics.get(metric_id)
                    if not isinstance(metric, Mapping):
                        issues.append(f"legacy field metric is invalid: {metric_id}")
                        continue
                    if metric.get("status") != "not-run":
                        issues.append(f"legacy field metric status is invalid: {metric_id}")
                    if metric.get("value") is not None:
                        issues.append(f"legacy field metric value must be null: {metric_id}")
                    metric_reason = metric.get("reason")
                    if (
                        not isinstance(metric_reason, str)
                        or not metric_reason.strip()
                        or metric_reason != field_reason
                    ):
                        issues.append(f"legacy field metric reason is invalid: {metric_id}")
    return issues


def _valid_source_scope(value: Any) -> bool:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or "\x00" in value
        or value.startswith("/")
    ):
        return False
    normalized = value[:-1] if value.endswith("/") else value
    if not normalized or "//" in value:
        return False
    path = PurePosixPath(normalized)
    return (
        not path.is_absolute()
        and all(part not in {"", ".", ".."} for part in path.parts)
        and path.as_posix() == normalized
    )


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

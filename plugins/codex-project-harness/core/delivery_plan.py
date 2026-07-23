"""Closed version-1 delivery-plan model and deterministic identity helpers."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


MAX_PLAN_BYTES = 256 * 1024
MAX_PLAN_TEXT = 8 * 1024
_PLAN_KEYS = {
    "version",
    "id",
    "goal",
    "acceptance",
    "task",
    "test",
    "failure_mode",
}
_TEST_KEYS = {"kind", "command"}
_FAILURE_MODE_KEYS = {
    "feature",
    "scenario",
    "trigger",
    "expected",
    "risk",
    "recovery",
    "data_safety",
}
_RISK_VALUES = {"low", "medium", "high", "critical"}


class DeliveryPlanError(ValueError):
    """The delivery-plan document is not a closed version-1 value."""


@dataclass(frozen=True, slots=True)
class DeliveryPlanTest:
    kind: str
    command: str


@dataclass(frozen=True, slots=True)
class DeliveryPlanFailureMode:
    feature: str
    scenario: str
    trigger: str
    expected: str
    risk: str
    recovery: str
    data_safety: str


@dataclass(frozen=True, slots=True)
class DeliveryPlan:
    version: int
    id: str
    goal: str
    acceptance: str
    task: str
    test: DeliveryPlanTest
    failure_mode: DeliveryPlanFailureMode | None


def _closed_object(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DeliveryPlanError(f"{label} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise DeliveryPlanError(f"{label} keys must be strings")
    actual = set(value)
    if actual != expected:
        raise DeliveryPlanError(
            f"{label} keys mismatch: missing={sorted(expected - actual)} "
            f"extra={sorted(actual - expected)}"
        )
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise DeliveryPlanError(f"{label} must be a string")
    normalized = value.strip()
    if not normalized:
        raise DeliveryPlanError(f"{label} must be non-empty")
    if len(normalized.encode("utf-8")) > MAX_PLAN_TEXT:
        raise DeliveryPlanError(f"{label} exceeds {MAX_PLAN_TEXT} UTF-8 bytes")
    return normalized


def normalize_plan_id(value: Any) -> str:
    raw = _text(value, "delivery-plan id")
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-._").upper()
    if not normalized:
        raise DeliveryPlanError("delivery-plan id has no usable characters")
    if len(normalized.encode("utf-8")) > 128:
        raise DeliveryPlanError("delivery-plan id exceeds 128 UTF-8 bytes")
    return normalized


def parse_delivery_plan(value: Any) -> DeliveryPlan:
    # Callers may already hold the frozen model, but it is not a trusted bypass
    # around the closed version/type/normalization contract.
    if isinstance(value, DeliveryPlan):
        value = asdict(value)
    document = _closed_object(value, _PLAN_KEYS, "delivery-plan")
    version = document["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise DeliveryPlanError("delivery-plan version must be integer 1")

    test_value = _closed_object(document["test"], _TEST_KEYS, "delivery-plan test")
    test = DeliveryPlanTest(
        kind=_text(test_value["kind"], "delivery-plan test.kind"),
        command=_text(test_value["command"], "delivery-plan test.command"),
    )

    failure_value = document["failure_mode"]
    failure_mode: DeliveryPlanFailureMode | None
    if failure_value is None:
        failure_mode = None
    else:
        failure = _closed_object(
            failure_value,
            _FAILURE_MODE_KEYS,
            "delivery-plan failure_mode",
        )
        risk = _text(failure["risk"], "delivery-plan failure_mode.risk")
        if risk not in _RISK_VALUES:
            raise DeliveryPlanError(
                "delivery-plan failure_mode.risk must be low, medium, high, or critical"
            )
        failure_mode = DeliveryPlanFailureMode(
            feature=_text(
                failure["feature"], "delivery-plan failure_mode.feature"
            ),
            scenario=_text(
                failure["scenario"], "delivery-plan failure_mode.scenario"
            ),
            trigger=_text(
                failure["trigger"], "delivery-plan failure_mode.trigger"
            ),
            expected=_text(
                failure["expected"], "delivery-plan failure_mode.expected"
            ),
            risk=risk,
            recovery=_text(
                failure["recovery"], "delivery-plan failure_mode.recovery"
            ),
            data_safety=_text(
                failure["data_safety"], "delivery-plan failure_mode.data_safety"
            ),
        )

    return DeliveryPlan(
        version=1,
        id=normalize_plan_id(document["id"]),
        goal=_text(document["goal"], "delivery-plan goal"),
        acceptance=_text(document["acceptance"], "delivery-plan acceptance"),
        task=_text(document["task"], "delivery-plan task"),
        test=test,
        failure_mode=failure_mode,
    )


def load_delivery_plan(path: Path) -> DeliveryPlan:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise DeliveryPlanError(f"cannot read delivery-plan file: {exc}") from exc
    if len(payload) > MAX_PLAN_BYTES:
        raise DeliveryPlanError(
            f"delivery-plan file exceeds {MAX_PLAN_BYTES} bytes: {path}"
        )
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DeliveryPlanError("delivery-plan file must be UTF-8") from exc

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise DeliveryPlanError(f"duplicate delivery-plan key: {key}")
            result[key] = item
        return result

    try:
        value = json.loads(text, object_pairs_hook=reject_duplicates)
    except json.JSONDecodeError as exc:
        raise DeliveryPlanError(f"invalid delivery-plan JSON: {exc.msg}") from exc
    return parse_delivery_plan(value)


def logical_plan_digest(plan: DeliveryPlan) -> str:
    payload = json.dumps(
        asdict(plan),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def derive_plan_ids(plan_id: str, cycle_id: str) -> dict[str, str]:
    normalized = normalize_plan_id(plan_id)
    ids = {
        "requirement_id": f"{normalized}-REQ1",
        "acceptance_id": f"{normalized}-AC1",
        "task_id": f"{normalized}-T1",
    }
    global_stem = normalized
    if cycle_id != "CYCLE-current":
        cycle_token = re.sub(r"[^A-Za-z0-9._-]+", "-", cycle_id).strip(
            "-._"
        ).upper()
        global_stem = f"{normalized}-{cycle_token or 'CYCLE'}"
    ids["target_id"] = f"{global_stem}-UNIT"
    ids["qualification_id"] = f"{global_stem}-Q1"
    return ids


def plan_ids(plan: DeliveryPlan, cycle_id: str) -> dict[str, str]:
    ids = derive_plan_ids(plan.id, cycle_id)
    if plan.failure_mode is not None:
        ids = {
            **ids,
            "failure_mode_id": f"{plan.id}-FM1",
        }
    return ids


def planned_mutations(plan: DeliveryPlan) -> list[str]:
    mutations = [
        "requirement",
        "acceptance",
        "requirement_acceptance",
        "task",
        "task_acceptance",
        "test_target",
        "task_test_target",
        "acceptance_target_qualification",
        "project_revision",
        "audit_events",
        "projections",
    ]
    if plan.failure_mode is not None:
        mutations[3:3] = [
            "failure_mode",
            "failure_mode_acceptance",
            "task_failure_mode",
        ]
    return mutations

"""Write-time schema guard for runtime entities."""

from __future__ import annotations

class SchemaGuardError(ValueError):
    """Raised when an entity cannot be written safely."""


TASK_STATUSES = {
    "planned",
    "active",
    "submitted",
    "accepted",
    "blocked",
    "cancelled",
}
FAILURE_MODE_STATUSES = {"identified", "accepted", "exempt"}
VALIDATION_RESULTS = {"pass", "fail", "blocked", "partial"}
GATE_RESULTS = {"pass", "fail", "conditional", "blocked"}
GATE_CONTEXTS = {"fresh", "same-context-degraded"}
REQUIREMENT_KINDS = {"goal", "functional", "non-functional", "non-goal", "assumption", "open-question", "architecture"}
TEST_TARGET_KINDS = {"unit", "integration", "lint", "build"}
SANDBOX_STATUSES = {"", "available", "unavailable"}
STACK_PROFILES = {"python", "node", "go", "rust", "java", "browser-e2e", "data-integration"}
RESULT_FORMATS = {"regex", "junit", "pytest-json", "jest-json", "go-json", "cargo-nextest-json", "playwright-json"}


def require_text(label: str, value: str) -> None:
    if not str(value).strip():
        raise SchemaGuardError(f"{label} is required")


def require_choice(label: str, value: str, allowed: set[str]) -> None:
    if value not in allowed:
        raise SchemaGuardError(f"{label} must be one of {sorted(allowed)}: {value}")


def validate_requirement(requirement_id: str, kind: str, body: str, status: str) -> None:
    require_text("requirement id", requirement_id)
    require_choice("requirement kind", kind, REQUIREMENT_KINDS)
    require_text("requirement body", body)
    require_text("requirement status", status)


def validate_acceptance(acceptance_id: str, criterion: str) -> None:
    require_text("acceptance id", acceptance_id)
    require_text("acceptance criterion", criterion)


def validate_failure_mode(fm_id: str, risk: str, status: str) -> None:
    require_text("failure mode id", fm_id)
    require_choice("failure mode risk", risk, {"low", "medium", "high", "critical"})
    require_choice("failure mode status", status, FAILURE_MODE_STATUSES)


def validate_task(task_id: str, task: str, status: str) -> None:
    require_text("task id", task_id)
    require_text("task", task)
    require_choice("task status", status, TASK_STATUSES)


def validate_validation(surface: str, findings: str, result: str) -> None:
    require_text("validation surface", surface)
    require_text("validation findings", findings)
    require_choice("validation result", result, VALIDATION_RESULTS)


def validate_gate(reviewer_context: str, result: str, gate: str) -> None:
    require_text("quality gate", gate)
    require_choice("quality gate reviewer context", reviewer_context, GATE_CONTEXTS)
    require_choice("quality gate result", result, GATE_RESULTS)


def validate_delivery(scope: str) -> None:
    require_text("delivery scope", scope)


def validate_test_target(
    target_id: str,
    kind: str,
    command_template: str,
    stack_profile: str = "python",
    result_format: str = "regex",
) -> None:
    require_text("test target id", target_id)
    require_choice("test target kind", kind, TEST_TARGET_KINDS)
    require_text("test target command template", command_template)
    require_choice("test target stack profile", stack_profile, STACK_PROFILES)
    require_choice("test target result format", result_format, RESULT_FORMATS)

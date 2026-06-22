#!/usr/bin/env python3
"""Validate a project's local Codex Project Harness state."""

from __future__ import annotations

from pathlib import Path

from harness_lib import git_dirty, git_head_sha, read_state, split_markdown_row


REQUIRED_FILES = [
    ".ai-team/control/project-state.yaml",
    ".ai-team/control/tooling-map.md",
    ".ai-team/requirements/requirements.md",
    ".ai-team/requirements/acceptance.md",
    ".ai-team/requirements/failure-modes.md",
    ".ai-team/planning/task-board.md",
    "docs/harness/bootstrap.md",
    "docs/harness/validation.md",
    "docs/harness/quality-gates.md",
    "docs/harness/delivery.md",
]

VALID_PHASES = {
    "intake",
    "project_bootstrap",
    "requirement_baseline",
    "confirmation",
    "team_architecture",
    "planning",
    "implementation",
    "qa",
    "delivery_readiness",
    "retrospective",
    "archived",
}

HEADER_CELLS = {
    "id",
    "surface",
    "gate",
    "date",
    "artifact",
    "risk",
}

DELIVERY_PHASES = {"delivery_readiness", "retrospective"}
ACTIVE_TASK_STATUSES = {"draft", "ready", "planned", "in_progress", "needs_input", "blocked", "testing", "review", "done", "failed", "partial"}
IGNORED_TASK_STATUSES = {"cancelled", "skipped"}


def table_rows(path: Path) -> list[list[str]]:
    if not path.exists():
        return []
    rows: list[list[str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or "---" in stripped:
            continue
        cells = split_markdown_row(stripped)
        if cells and cells[0].lower() in HEADER_CELLS:
            continue
        rows.append(cells)
    return rows


def split_ids(value: str) -> set[str]:
    return {part.strip() for part in value.replace(";", ",").split(",") if part.strip()}


def main() -> int:
    root = Path.cwd()
    errors: list[str] = []
    warnings: list[str] = []

    for relpath in REQUIRED_FILES:
        if not (root / relpath).exists():
            errors.append(f"missing required harness file: {relpath}")

    state = read_state(root)
    phase = state.get("phase")
    if phase and phase not in VALID_PHASES:
        errors.append(f"invalid phase: {phase}")
    if not phase:
        errors.append("project state missing phase")

    acceptance_rows = table_rows(root / ".ai-team/requirements/acceptance.md")
    failure_mode_rows = table_rows(root / ".ai-team/requirements/failure-modes.md")
    task_rows = table_rows(root / ".ai-team/planning/task-board.md")
    validation_rows = table_rows(root / "docs/harness/validation.md")
    quality_gate_rows = table_rows(root / "docs/harness/quality-gates.md")

    if phase in {"planning", "implementation", "qa", "delivery_readiness", "retrospective"} and not task_rows:
        errors.append("phase requires task-board rows, but no tasks were found")
    if phase in {"implementation", "qa", "delivery_readiness", "retrospective"} and not acceptance_rows:
        warnings.append("implementation-phase work has no acceptance criteria rows")
    if phase in DELIVERY_PHASES and not validation_rows:
        errors.append("delivery requires validation evidence, but no validation rows were found")
    if phase in DELIVERY_PHASES and not quality_gate_rows:
        errors.append("delivery requires a quality gate record, but none was found")

    if phase in DELIVERY_PHASES:
        for row in validation_rows:
            while len(row) < 7:
                row.append("")
            surface, acceptance, tool_context, commands, findings, result, residual_risk = row[:7]
            if result != "pass":
                errors.append(f"delivery validation is not pass: {surface}={result or 'missing'}")
            if not commands:
                warnings.append(f"validation has no command evidence: {surface}")

        for row in failure_mode_rows:
            while len(row) < 10:
                row.append("")
            fm_id, feature, scenario, trigger, expected, recovery, data_safety, risk, test_mapping, status = row[:10]
            if risk in {"high", "critical"} and status not in {"covered", "accepted"}:
                errors.append(f"{risk} failure mode is not closed: {fm_id} status={status or 'missing'}")

        if quality_gate_rows:
            latest_gate = quality_gate_rows[-1]
            while len(latest_gate) < 8:
                latest_gate.append("")
            gate, commit, reviewer_context, result, blocking_findings, commands, evidence, residual_risk = latest_gate[:8]
            if result != "pass":
                errors.append(f"latest quality gate is not pass: {gate}={result or 'missing'}")
            if blocking_findings:
                errors.append(f"latest quality gate has blocking findings: {blocking_findings}")
            if not commit:
                errors.append("latest quality gate is missing reviewed commit")
            if not commands:
                warnings.append("latest quality gate has no command evidence")
            if not evidence:
                warnings.append("latest quality gate has no evidence summary")

            current_sha = git_head_sha(root)
            dirty = git_dirty(root)
            if current_sha:
                if dirty:
                    errors.append("git worktree is dirty after quality gate; record a new gate after committing changes")
                if commit != current_sha:
                    errors.append(f"latest quality gate commit does not match current HEAD: gate={commit} head={current_sha}")
                high_risk_open_or_present = any(
                    len(row) >= 9 and row[7] in {"high", "critical"} for row in failure_mode_rows
                )
                if high_risk_open_or_present and reviewer_context == "same-context-degraded":
                    errors.append("high/critical risk delivery requires fresh or external quality gate reviewer context")

    task_ids: set[str] = set()
    for row in task_rows:
        while len(row) < 9:
            row.append("")
        task_id, task, owner, status, acceptance, failure_modes, depends_on, tool_link, evidence = row[:9]
        if not task_id or not task:
            errors.append(f"task row missing id/task: {row}")
        if task_id in task_ids:
            errors.append(f"duplicate task id: {task_id}")
        task_ids.add(task_id)
        if not owner or owner == "unassigned":
            warnings.append(f"task has no concrete owner: {task_id}")
        if not acceptance:
            warnings.append(f"task has no acceptance mapping: {task_id}")
        if failure_mode_rows and not failure_modes:
            warnings.append(f"task has no failure-mode mapping: {task_id}")
        if phase in DELIVERY_PHASES and status in ACTIVE_TASK_STATUSES:
            errors.append(f"delivery task is not accepted: {task_id} status={status or 'missing'}")
        if status in {"done", "accepted"} and not evidence:
            warnings.append(f"completed task has no evidence: {task_id}")

    for row in task_rows:
        while len(row) < 9:
            row.append("")
        task_id, task, owner, status, acceptance, failure_modes, depends_on, tool_link, evidence = row[:9]
        for dependency in split_ids(depends_on):
            if dependency and dependency not in task_ids:
                errors.append(f"task depends on missing task: {task_id} -> {dependency}")

    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        for warning in warnings:
            print(f"WARN: {warning}")
        return 1

    for warning in warnings:
        print(f"WARN: {warning}")
    print("OK: harness state is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

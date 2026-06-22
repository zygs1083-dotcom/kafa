#!/usr/bin/env python3
"""Validate a project's local Codex Project Harness state."""

from __future__ import annotations

from pathlib import Path

from harness_lib import read_state


REQUIRED_FILES = [
    ".ai-team/control/project-state.yaml",
    ".ai-team/control/tooling-map.md",
    ".ai-team/requirements/requirements.md",
    ".ai-team/requirements/acceptance.md",
    ".ai-team/planning/task-board.md",
    "docs/harness/bootstrap.md",
    "docs/harness/validation.md",
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


def table_rows(path: Path) -> list[list[str]]:
    if not path.exists():
        return []
    rows: list[list[str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or "---" in stripped:
            continue
        cells = [cell.strip().replace("\\|", "|") for cell in stripped.strip("|").split("|")]
        if cells and cells[0].lower() in {"id", "surface"}:
            continue
        rows.append(cells)
    return rows


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
    task_rows = table_rows(root / ".ai-team/planning/task-board.md")
    validation_rows = table_rows(root / "docs/harness/validation.md")

    if phase in {"planning", "implementation", "qa", "delivery_readiness", "retrospective"} and not task_rows:
        errors.append("phase requires task-board rows, but no tasks were found")
    if phase in {"implementation", "qa", "delivery_readiness", "retrospective"} and not acceptance_rows:
        warnings.append("implementation-phase work has no acceptance criteria rows")
    if phase in {"delivery_readiness", "retrospective"} and not validation_rows:
        errors.append("delivery requires validation evidence, but no validation rows were found")

    for row in task_rows:
        while len(row) < 8:
            row.append("")
        task_id, task, owner, status, acceptance, depends_on, tool_link, evidence = row[:8]
        if not task_id or not task:
            errors.append(f"task row missing id/task: {row}")
        if not owner or owner == "unassigned":
            warnings.append(f"task has no concrete owner: {task_id}")
        if not acceptance:
            warnings.append(f"task has no acceptance mapping: {task_id}")
        if status in {"done", "qa", "delivery_ready"} and not evidence:
            warnings.append(f"completed task has no evidence: {task_id}")

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

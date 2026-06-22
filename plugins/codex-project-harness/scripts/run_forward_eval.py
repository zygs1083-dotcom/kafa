#!/usr/bin/env python3
"""Run executable forward-evaluation scenarios for Codex Project Harness."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
HARNESS = ROOT / "plugins" / "codex-project-harness" / "scripts" / "harness.py"
RESULT_PATH = ROOT / "docs" / "runtime" / "forward-eval-results.json"


def run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(HARNESS), "--root", str(root), *args],
        text=True,
        capture_output=True,
        check=False,
    )


def scenario_full_project() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        commands = [
            run(root, "init"),
            run(root, "phase", "project_bootstrap"),
            run(root, "phase", "requirement_baseline"),
            run(root, "acceptance", "add", "--id", "AC1", "--criterion", "Create tasks"),
            run(root, "failure-mode", "add", "--id", "FM1", "--feature", "Task creation", "--scenario", "Duplicate submit", "--trigger", "same form twice", "--expected", "one task", "--risk", "high", "--status", "covered", "--acceptance", "AC1"),
            run(root, "task", "add", "--id", "T1", "--task", "Implement task creation", "--acceptance", "AC1", "--failure-mode", "FM1"),
            run(root, "task", "start", "T1", "--agent", "developer"),
            run(root, "task", "complete", "T1", "--evidence", "unit test passed"),
            run(root, "validation", "record", "--surface", "Task creation", "--acceptance", "AC1", "--commands", "unit test", "--findings", "passed", "--result", "pass"),
            run(root, "gate", "record", "--reviewer-context", "fresh", "--result", "pass", "--commands", "unit test", "--evidence", "reviewed"),
            run(root, "delivery", "record", "--scope", "Task creation", "--acceptance", "AC1", "--validation", "unit test passed", "--qa", "gate passed", "--failure-mode-coverage", "FM1 covered", "--quality-gate", "pass"),
        ]
        ok = all(command.returncode == 0 for command in commands)
        files = [
            ".ai-team/state/harness.db",
            ".ai-team/planning/task-board.md",
            "docs/harness/validation.md",
            "docs/harness/delivery.md",
        ]
        ok = ok and all((root / file).exists() for file in files)
        return {"name": "full_project_runtime", "pass": ok, "commands": [command.returncode for command in commands]}


def scenario_tool_mapping() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        commands = [
            run(root, "init"),
            run(root, "adapter", "record", "--tool", "figma", "--mode", "read-only", "--artifact", "Design", "--external-id", "figma-frame-1", "--idempotency-key", "codex-project-harness:eval:design:figma-frame-1"),
            run(root, "adapter", "record", "--tool", "linear", "--mode", "draft-write", "--artifact", "Tasks", "--external-id", "LIN-1", "--idempotency-key", "codex-project-harness:eval:task:LIN-1"),
        ]
        tooling = (root / ".ai-team/control/tooling-map.md").read_text(encoding="utf-8")
        ok = all(command.returncode == 0 for command in commands) and "figma-frame-1" in tooling and "LIN-1" in tooling
        return {"name": "tool_mapping_runtime", "pass": ok, "commands": [command.returncode for command in commands]}


def main() -> int:
    results = [scenario_full_project(), scenario_tool_mapping()]
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    failed = [result for result in results if not result["pass"]]
    if failed:
        print(json.dumps(failed, ensure_ascii=False, indent=2))
        return 1
    print(f"OK: forward eval passed ({len(results)} scenarios)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

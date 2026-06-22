#!/usr/bin/env python3
"""Run executable runtime smoke scenarios for Codex Project Harness."""

from __future__ import annotations

import json
import hashlib
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
PLUGIN_ROOT = ROOT / "plugins" / "codex-project-harness"
SCRIPTS_ROOT = PLUGIN_ROOT / "scripts"
for path in [PLUGIN_ROOT, SCRIPTS_ROOT]:
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
HARNESS = ROOT / "plugins" / "codex-project-harness" / "scripts" / "harness.py"
RESULT_PATH = ROOT / "docs" / "runtime" / "runtime-smoke-results.json"
TEST_COMMAND = "python3 -c 'print(\"1 passed\")'"


def run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(HARNESS), "--root", str(root), *args],
        text=True,
        capture_output=True,
        check=False,
    )


def task_revision(root: Path, task_id: str) -> str:
    with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
        return str(conn.execute("select revision from tasks where id = ?", (task_id,)).fetchone()[0])


def token(stdout: str) -> str:
    return stdout.split("token=", 1)[1].strip()


def trusted_artifact(root: Path, suffix: str, content: str = "1 passed\n") -> tuple[str, str]:
    artifact = root / ".ai-team" / "runtime" / "smoke" / f"stdout-{suffix}.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(content, encoding="utf-8")
    return artifact.relative_to(root).as_posix(), hashlib.sha256(content.encode("utf-8")).hexdigest()


def scenario_full_project() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        commands = []
        commands.append(run(root, "init"))
        commands.append(run(root, "phase", "project_bootstrap"))
        commands.append(run(root, "phase", "requirement_baseline"))
        commands.append(run(root, "requirement", "add", "--id", "R1", "--kind", "functional", "--body", "User can create a task", "--priority", "must"))
        commands.append(run(root, "acceptance", "add", "--id", "AC1", "--criterion", "Create tasks"))
        commands.append(run(root, "requirement", "link", "--requirement", "R1", "--acceptance", "AC1"))
        commands.append(run(root, "phase", "confirmation"))
        commands.append(run(root, "failure-mode", "add", "--id", "FM1", "--feature", "Task creation", "--scenario", "Duplicate submit", "--trigger", "same form twice", "--expected", "one task", "--risk", "high", "--acceptance", "AC1"))
        commands.append(run(root, "task", "add", "--id", "T1", "--task", "Implement task creation", "--acceptance", "AC1", "--failure-mode", "FM1"))
        commands.append(run(root, "scope", "confirm", "--by", "project-manager", "--summary", "Task creation scope confirmed"))
        commands.append(run(root, "baseline", "freeze", "--id", "B1", "--summary", "Task creation baseline"))
        commands.append(run(root, "phase", "planning"))
        commands.append(run(root, "phase", "implementation"))
        claim = run(root, "task", "claim", "T1", "--agent", "developer", "--expected-revision", task_revision(root, "T1"))
        commands.append(claim)
        producer_token = token(claim.stdout) if claim.returncode == 0 else ""
        commands.append(run(root, "task", "start", "T1", "--agent", "developer", "--lease-token", producer_token, "--expected-revision", task_revision(root, "T1")))
        commands.append(run(root, "task", "submit", "T1", "--agent", "developer", "--lease-token", producer_token, "--expected-revision", task_revision(root, "T1"), "--evidence", "unit test passed"))
        commands.append(run(root, "phase", "qa"))
        review = run(root, "task", "review", "T1", "--agent", "qa-reviewer", "--expected-revision", task_revision(root, "T1"))
        commands.append(review)
        reviewer_token = token(review.stdout) if review.returncode == 0 else ""
        commands.append(run(root, "task", "accept", "T1", "--agent", "qa-reviewer", "--lease-token", reviewer_token, "--expected-revision", task_revision(root, "T1"), "--evidence", "reviewed"))
        commands.append(run(root, "test-target", "add", "--id", "TARGET1", "--kind", "unit", "--command-template", TEST_COMMAND, "--description", "Smoke unit target"))
        evidence_artifact, evidence_sha = trusted_artifact(root, "evidence")
        validation_artifact, validation_sha = trusted_artifact(root, "validation")
        commands.append(run(root, "evidence", "record", "--id", "EV1", "--kind", "command", "--summary", "unit test passed", "--command", TEST_COMMAND, "--exit-code", "0", "--stdout-sha256", evidence_sha, "--artifact-path", evidence_artifact, "--target", "TARGET1", "--executed-count", "1"))
        commands.append(run(root, "test", "record", "--id", "TEST1", "--surface", "Task creation", "--command", TEST_COMMAND, "--result", "pass", "--evidence", "EV1"))
        commands.append(run(root, "validation", "record", "--surface", "Task creation", "--acceptance", "AC1", "--commands", TEST_COMMAND, "--findings", "passed", "--result", "pass", "--failure-mode", "FM1", "--test", "TEST1", "--evidence", "EV1", "--command", TEST_COMMAND, "--exit-code", "0", "--stdout-sha256", validation_sha, "--artifact-path", validation_artifact, "--target", "TARGET1", "--executed-count", "1"))
        commands.append(run(root, "gate", "record", "--reviewer-context", "fresh", "--result", "pass", "--commands", "unit test", "--evidence", "reviewed"))
        commands.append(run(root, "phase", "delivery_readiness"))
        commands.append(run(root, "delivery", "record", "--scope", "Task creation", "--acceptance", "AC1", "--validation", "unit test passed", "--qa", "gate passed", "--failure-mode-coverage", "FM1 covered", "--quality-gate", "pass"))
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


def scenario_directed_invariant_benchmark() -> dict[str, object]:
    from core.invariant_checker import check_runtime_invariants

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        init = run(root, "init")
        with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
            conn.row_factory = sqlite3.Row
            now = "2026-01-01T00:00:00+00:00"
            conn.executemany(
                "insert into tasks (id, task, owner, status, updated_at) values (?, ?, 'developer', 'ready', ?)",
                [(f"B{i}", f"Benchmark {i}", now) for i in range(5000)],
            )
            conn.executemany(
                """
                insert into events (id, schema_version, type, source, target, payload_json, created_at)
                values (?, 11, 'benchmark_event', 'smoke', 'project', '{}', ?)
                """,
                [(f"bench-event-{i}", now) for i in range(5000)],
            )
            conn.commit()
            start = time.perf_counter()
            full_issues = check_runtime_invariants(conn, root)
            full_seconds = time.perf_counter() - start
            start = time.perf_counter()
            directed_issues = check_runtime_invariants(conn, root, scope=[("task", "B1")], full=False)
            directed_seconds = time.perf_counter() - start
        ratio = full_seconds / max(directed_seconds, 0.000001)
        ok = init.returncode == 0 and not full_issues and not directed_issues and ratio >= 10
        return {
            "name": "directed_invariant_benchmark",
            "pass": ok,
            "full_seconds": round(full_seconds, 6),
            "directed_seconds": round(directed_seconds, 6),
            "ratio": round(ratio, 2),
        }


def main() -> int:
    results = [scenario_full_project(), scenario_tool_mapping(), scenario_directed_invariant_benchmark()]
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    failed = [result for result in results if not result["pass"]]
    if failed:
        print(json.dumps(failed, ensure_ascii=False, indent=2))
        return 1
    print(f"OK: runtime smoke passed ({len(results)} scenarios)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

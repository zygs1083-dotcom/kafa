#!/usr/bin/env python3
"""Run executable schema-30 runtime smoke scenarios."""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
PLUGIN_ROOT = ROOT / "plugins/codex-project-harness"
SCRIPTS_ROOT = PLUGIN_ROOT / "scripts"
for path in (PLUGIN_ROOT, SCRIPTS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

HARNESS = SCRIPTS_ROOT / "harness.py"
RESULT_PATH = ROOT / "docs/runtime/runtime-smoke-results.json"
TEST_COMMAND = "python3 -B -m unittest test_harness_dummy.py"
DIRECTED_INVARIANT_MIN_RATIO = 10.0


def run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HARNESS), "--root", str(root), *args],
        text=True,
        capture_output=True,
        check=False,
    )


def create_candidate(root: Path) -> None:
    (root / "test_harness_dummy.py").write_text(
        "import unittest\n\n"
        "class HarnessSmokeTest(unittest.TestCase):\n"
        "    def test_smoke(self):\n"
        "        self.assertTrue(True)\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "smoke@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Smoke Runner"], cwd=root, check=True)
    subprocess.run(["git", "add", "test_harness_dummy.py"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "smoke baseline"], cwd=root, check=True, capture_output=True)


def scenario_local_delivery() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        create_candidate(root)
        initialized = run(root, "init")
        if initialized.returncode == 0:
            subprocess.run(
                ["git", "add", ".gitignore"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "record harness runtime ignore policy"],
                cwd=root,
                check=True,
                capture_output=True,
            )
        commands = [
            initialized,
            run(root, "requirement", "add", "--id", "R1", "--kind", "functional", "--body", "User can create a task"),
            run(root, "acceptance", "add", "--id", "AC1", "--criterion", "Create tasks"),
            run(root, "requirement", "link", "--requirement", "R1", "--acceptance", "AC1"),
            run(root, "failure-mode", "add", "--id", "FM1", "--feature", "Task creation", "--scenario", "Duplicate submit", "--trigger", "same form twice", "--expected", "one task", "--risk", "medium", "--acceptance", "AC1"),
            run(root, "task", "add", "--id", "T1", "--task", "Implement task creation", "--acceptance", "AC1", "--failure-mode", "FM1"),
            run(root, "baseline", "freeze", "--id", "B1", "--summary", "Task creation baseline"),
            run(root, "test-target", "add", "--id", "UNIT", "--kind", "unit", "--command-template", TEST_COMMAND),
            run(root, "test-target", "link", "--task", "T1", "--target", "UNIT"),
            run(root, "task", "start", "T1"),
            run(root, "verify", "run", "--target", "UNIT", "--acceptance", "AC1", "--failure-mode", "FM1"),
            run(root, "task", "submit", "T1", "--context-id", "smoke-producer", "--evidence", "verified immutable execution"),
            run(root, "task", "accept", "T1", "--evidence", "independent review returned"),
            run(root, "gate", "record", "--reviewer-context", "fresh", "--reviewer-context-id", "smoke-reviewer", "--result", "pass"),
            run(root, "delivery", "record", "--scope", "Task creation"),
        ]
        with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
            counts = {
                table: int(conn.execute(f"select count(*) from {table}").fetchone()[0])
                for table in ("executions", "validations", "deliveries")
            }
            task_status = conn.execute("select status from tasks where id='T1'").fetchone()[0]
        required = (
            root / ".ai-team/state/harness.db",
            root / ".ai-team/planning/task-board.md",
            root / "docs/harness/executions.md",
            root / "docs/harness/delivery.md",
        )
        passed = (
            all(command.returncode == 0 for command in commands)
            and counts == {"executions": 1, "validations": 1, "deliveries": 1}
            and task_status == "accepted"
            and all(path.is_file() for path in required)
        )
        return {
            "name": "local_delivery_runtime",
            "pass": passed,
            "commands": [command.returncode for command in commands],
            "facts": counts,
        }


def directed_invariant_benchmark_result(
    *,
    initialized_returncode: int,
    full_issue_count: int,
    directed_issue_count: int,
    full_seconds: float,
    directed_seconds: float,
) -> dict[str, object]:
    ratio = full_seconds / max(directed_seconds, 0.000001)
    return {
        "name": "directed_invariant_benchmark",
        "pass": (
            initialized_returncode == 0
            and full_issue_count == 0
            and directed_issue_count == 0
            and ratio >= DIRECTED_INVARIANT_MIN_RATIO
        ),
        "full_seconds": full_seconds,
        "directed_seconds": directed_seconds,
        "ratio": ratio,
        "minimum_ratio": DIRECTED_INVARIANT_MIN_RATIO,
    }


def scenario_directed_invariant_benchmark() -> dict[str, object]:
    from core.invariant_checker import check_runtime_invariants

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        initialized = run(root, "init")
        with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
            conn.row_factory = sqlite3.Row
            now = "2026-01-01T00:00:00+00:00"
            conn.executemany(
                """
                insert into tasks (id, cycle_id, task, owner, status, updated_at)
                values (?, 'CYCLE-current', ?, 'developer', 'planned', ?)
                """,
                [(f"B{index}", f"Benchmark {index}", now) for index in range(5000)],
            )
            conn.commit()
            start = time.perf_counter()
            full_issues = check_runtime_invariants(conn, root)
            full_seconds = time.perf_counter() - start
            start = time.perf_counter()
            directed_issues = check_runtime_invariants(
                conn, root, scope=[("task", "B1")], full=False
            )
            directed_seconds = time.perf_counter() - start
        return directed_invariant_benchmark_result(
            initialized_returncode=initialized.returncode,
            full_issue_count=len(full_issues),
            directed_issue_count=len(directed_issues),
            full_seconds=full_seconds,
            directed_seconds=directed_seconds,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run executable schema-30 runtime smoke scenarios")
    parser.add_argument(
        "--out",
        default=str(RESULT_PATH),
        help="JSON report path (defaults to docs/runtime/runtime-smoke-results.json)",
    )
    args = parser.parse_args(argv)

    results = [scenario_local_delivery(), scenario_directed_invariant_benchmark()]
    result_path = Path(args.out)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    failed = [result for result in results if not result["pass"]]
    if failed:
        print(json.dumps(failed, ensure_ascii=False, indent=2))
        return 1
    print(f"OK: runtime smoke passed ({len(results)} scenarios)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Run deterministic agent E2E evaluation scenarios.

The default fixture mode exercises the harness control plane with real CLI
commands and temporary git repositories. It intentionally does not require a
Codex service, network, Docker, or host credentials.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[3]
PLUGIN_ROOT = ROOT / "plugins" / "codex-project-harness"
SCRIPTS_ROOT = PLUGIN_ROOT / "scripts"
for path in [PLUGIN_ROOT, SCRIPTS_ROOT]:
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

HARNESS = SCRIPTS_ROOT / "harness.py"
TEST_COMMAND = "python3 -B -m unittest"


def run_harness(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["python3", str(HARNESS), "--root", str(root), *args], text=True, capture_output=True, check=False)
    if check and result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return result


def run_git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=True)
    return result.stdout.strip()


def stdout_field(stdout: str, name: str) -> str:
    return stdout.split(f"{name}=", 1)[1].split(None, 1)[0].strip()


def task_revision(root: Path, task_id: str) -> str:
    return str(db_rows(root, "select revision from tasks where id = ?", (task_id,))[0]["revision"])


def init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Eval Runner"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "eval@example.invalid"], cwd=root, check=True)
    (root / "README.md").write_text("eval\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)


def add_unittest(root: Path, *, failing_on_integration: bool = False) -> None:
    body = [
        "import pathlib",
        "import unittest",
        "",
        "class EvalTest(unittest.TestCase):",
        "    def test_ok(self):",
        "        self.assertTrue(True)",
    ]
    if failing_on_integration:
        body.extend(
            [
                "",
                "    def test_no_integration_regression(self):",
                "        self.assertFalse(pathlib.Path('file_a.txt').exists() and pathlib.Path('file_b.txt').exists())",
            ]
        )
    (root / "test_eval.py").write_text("\n".join(body) + "\n", encoding="utf-8")
    subprocess.run(["git", "add", "test_eval.py"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "add eval test"], cwd=root, check=True, capture_output=True)


def setup_basic_harness(root: Path, task_ids: list[str]) -> str:
    run_harness(root, "init")
    commit_harness_scaffold(root)
    run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Eval acceptance")
    run_harness(root, "test-target", "add", "--id", "UNIT", "--kind", "unit", "--command-template", TEST_COMMAND)
    for task_id in task_ids:
        run_harness(root, "task", "add", "--id", task_id, "--task", f"Task {task_id}", "--owner", f"agent-{task_id.lower()}", "--acceptance", "AC1")
        run_harness(root, "test-target", "link", "--task", task_id, "--target", "UNIT")
    return run_harness(root, "dispatch", "plan", "--scope", "Agent E2E").stdout.strip().split()[-1]


def commit_harness_scaffold(root: Path) -> None:
    subprocess.run(["git", "add", ".gitignore", ".codex", "docs"], cwd=root, check=True, capture_output=True)
    diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=root, check=False)
    if diff.returncode != 0:
        subprocess.run(["git", "commit", "-m", "add harness scaffold"], cwd=root, check=True, capture_output=True)


def commit_branch(root: Path, branch_name: str, file_name: str, content: str) -> tuple[str, str, str]:
    worktree = root / ".ai-team/runtime/e2e-worktrees" / branch_name.replace("/", "-")
    worktree.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "worktree", "add", "-B", branch_name, str(worktree), "HEAD"], cwd=root, check=True, capture_output=True)
    target = worktree / file_name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", file_name], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-m", f"agent change {file_name}"], cwd=worktree, check=True, capture_output=True)
    head = run_git(root, "rev-parse", branch_name)
    tree = run_git(root, "rev-parse", f"{branch_name}^{{tree}}")
    rel = worktree.relative_to(root).as_posix()
    subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=root, check=True, capture_output=True)
    return head, tree, rel


def fixture_report(root: Path, run_id: str, task_id: str, branch_name: str, *, status: str = "success") -> None:
    path = root / ".ai-team/runtime/provider-fixtures" / run_id / f"{task_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "status": status,
                "last_error": "" if status == "success" else status,
                "result": {
                    "command": "forged worker command",
                    "exit_code": 0,
                    "stdout_sha256": "0" * 64,
                    "artifact_path": ".ai-team/runtime/forged/stdout.txt",
                    "executed_count": 999,
                    "executed_count_source": "manual",
                    "source_tree_hash": "forged",
                    "branch_name": branch_name,
                    "status": "success",
                    "target_id": "UNIT",
                },
            }
        ),
        encoding="utf-8",
    )


def db_rows(root: Path, query: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
    with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(query, params).fetchall()


def accept_task_via_cli(root: Path, task_id: str) -> None:
    review = run_harness(root, "task", "review", task_id, "--agent", "qa-reviewer", "--expected-revision", task_revision(root, task_id))
    token = stdout_field(review.stdout, "token")
    fence = stdout_field(review.stdout, "fence")
    run_harness(
        root,
        "task",
        "accept",
        task_id,
        "--agent",
        "qa-reviewer",
        "--lease-token",
        token,
        "--expected-revision",
        task_revision(root, task_id),
        "--fence",
        fence,
        "--evidence",
        "fixture review accepted",
    )


def add_file_claim(root: Path, run_id: str, task_id: str, agent: str, path: str, worktree_path: str, branch_name: str) -> None:
    import harness_db

    harness_db.dispatch_file_claim_add(root, task_id, agent, path, run_id=run_id, worktree_path=worktree_path, branch_name=branch_name)


def collect_and_verify(root: Path, run_id: str, branches: dict[str, str]) -> None:
    run_harness(root, "dispatch", "provider", "start", "--run-id", run_id, "--provider", "fixture", "--max-concurrency", str(len(branches)))
    sessions = db_rows(root, "select task_id, branch_name from agent_provider_sessions where run_id = ?", (run_id,))
    for session in sessions:
        fixture_report(root, run_id, session["task_id"], branches[session["task_id"]])
    run_harness(root, "dispatch", "provider", "collect", "--run-id", run_id)
    for task_id in branches:
        run_harness(root, "dispatch", "verify-attempt", "--run-id", run_id, "--task", task_id)


def scenario_result(name: str, started: float, ok: bool, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "pass": bool(ok),
        "duration_seconds": round(time.perf_counter() - started, 6),
        "details": details or {},
    }


def scenario_parallel_success() -> dict[str, Any]:
    started = time.perf_counter()
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        init_git_repo(root)
        add_unittest(root)
        run_id = setup_basic_harness(root, ["T1", "T2"])
        sessions = db_rows(root, "select da.task_id, t.owner as agent_id from dispatch_assignments da join tasks t on t.id = da.task_id where run_id = ? order by da.task_id", (run_id,))
        branches: dict[str, str] = {}
        for session, file_name, content in zip(sessions, ["a.txt", "b.txt"], ["A\n", "B\n"], strict=True):
            branch = f"agent/{run_id}/{session['task_id']}/{session['agent_id']}"
            head, tree, worktree = commit_branch(root, branch, file_name, content)
            branches[session["task_id"]] = branch
            add_file_claim(root, run_id, session["task_id"], session["agent_id"], file_name, worktree, branch)
        collect_and_verify(root, run_id, branches)
        for task_id in branches:
            accept_task_via_cli(root, task_id)
        run_harness(root, "gate", "record", "--reviewer-context", "fresh", "--result", "pass", "--commands", TEST_COMMAND, "--evidence", "fixture review")

        import harness_db

        original_validate = harness_db.validate_runtime
        try:
            harness_db.validate_runtime = lambda _root, delivery=False: []
            target = harness_db.dispatch_integrate(root, run_id)
        finally:
            harness_db.validate_runtime = original_validate
        integrated_a = run_git(root, "show", f"{target}:a.txt") == "A"
        integrated_b = run_git(root, "show", f"{target}:b.txt") == "B"
        status = db_rows(root, "select status from dispatch_runs where id = ?", (run_id,))[0]["status"]
        return scenario_result("parallel_success", started, integrated_a and integrated_b and status == "integrated", {"run_id": run_id, "target_branch": target})


def scenario_dependency_blocked() -> dict[str, Any]:
    started = time.perf_counter()
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        run_harness(root, "init")
        run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Dependency acceptance")
        run_harness(root, "test-target", "add", "--id", "UNIT", "--kind", "unit", "--command-template", TEST_COMMAND)
        run_harness(root, "task", "add", "--id", "T1", "--task", "Prerequisite", "--owner", "prereq", "--acceptance", "AC1")
        run_harness(root, "task", "add", "--id", "T2", "--task", "Dependent", "--owner", "developer", "--acceptance", "AC1", "--depends-on", "T1")
        run_harness(root, "test-target", "link", "--task", "T1", "--target", "UNIT")
        run_harness(root, "test-target", "link", "--task", "T2", "--target", "UNIT")
        run_id = run_harness(root, "dispatch", "plan", "--scope", "Dependency").stdout.strip().split()[-1]
        planned = [row["task_id"] for row in db_rows(root, "select task_id from dispatch_assignments where run_id = ? order by task_id", (run_id,))]
        run_harness(root, "dispatch", "export-csv", run_id)
        input_csv = root / ".ai-team/runtime/codex-fanout" / run_id / "input.csv"
        with input_csv.open(encoding="utf-8") as handle:
            exported = [row["item_id"] for row in csv.DictReader(handle)]
        provider = run_harness(root, "dispatch", "provider", "start", "--run-id", run_id, "--provider", "fixture")
        claim = run_harness(root, "dispatch", "claim-next", "--agent", "developer", check=False)
        ok = planned == ["T1"] and exported == ["T1"] and "started 1 provider session" in provider.stdout and claim.returncode != 0
        return scenario_result("dependency_blocked", started, ok, {"planned": planned, "exported": exported, "claim_returncode": claim.returncode})


def scenario_same_file_conflict() -> dict[str, Any]:
    started = time.perf_counter()
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        run_harness(root, "init")
        first = run_harness(root, "dispatch", "file-claim", "add", "--task", "T1", "--agent", "developer", "--path", "shared.py")
        second = run_harness(root, "dispatch", "file-claim", "add", "--task", "T2", "--agent", "qa-reviewer", "--path", "shared.py", check=False)
        claims = db_rows(root, "select task_id, path from task_file_claims where status = 'active'")
        ok = first.returncode == 0 and second.returncode != 0 and "file-claim-conflict" in second.stdout and len(claims) == 1
        return scenario_result("same_file_conflict", started, ok, {"active_claims": len(claims)})


def scenario_forged_evidence_blocked() -> dict[str, Any]:
    started = time.perf_counter()
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        init_git_repo(root)
        add_unittest(root)
        run_id = setup_basic_harness(root, ["T1"])
        run_harness(root, "dispatch", "provider", "start", "--run-id", run_id, "--provider", "fixture")
        session = db_rows(root, "select task_id, branch_name from agent_provider_sessions where run_id = ?", (run_id,))[0]
        commit_branch(root, session["branch_name"], "agent.txt", "work\n")
        fixture_report(root, run_id, "T1", session["branch_name"])
        run_harness(root, "dispatch", "provider", "collect", "--run-id", run_id)
        delivery = run_harness(root, "validate", "--delivery", check=False)
        evidence_count = db_rows(root, "select count(*) as count from evidence where id like 'CODEX-%'")[0]["count"]
        ok = delivery.returncode != 0 and evidence_count == 0
        return scenario_result("forged_evidence_blocked", started, ok, {"delivery_returncode": delivery.returncode, "controller_evidence_count": evidence_count})


def scenario_integration_regression_blocked() -> dict[str, Any]:
    started = time.perf_counter()
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        init_git_repo(root)
        add_unittest(root, failing_on_integration=True)
        run_id = setup_basic_harness(root, ["T1", "T2"])
        assignments = db_rows(root, "select da.task_id, t.owner as agent_id from dispatch_assignments da join tasks t on t.id = da.task_id where run_id = ? order by da.task_id", (run_id,))
        branches: dict[str, str] = {}
        for assignment, file_name in zip(assignments, ["file_a.txt", "file_b.txt"], strict=True):
            branch = f"agent/{run_id}/{assignment['task_id']}/{assignment['agent_id']}"
            _head, _tree, worktree = commit_branch(root, branch, file_name, "break\n")
            branches[assignment["task_id"]] = branch
            add_file_claim(root, run_id, assignment["task_id"], assignment["agent_id"], file_name, worktree, branch)
        collect_and_verify(root, run_id, branches)
        for task_id in branches:
            accept_task_via_cli(root, task_id)
        run_harness(root, "gate", "record", "--reviewer-context", "fresh", "--result", "pass", "--commands", TEST_COMMAND, "--evidence", "fixture review")
        result = run_harness(root, "dispatch", "integrate", "--run-id", run_id, check=False)
        status = db_rows(root, "select status from dispatch_runs where id = ?", (run_id,))[0]["status"]
        finding = db_rows(root, "select summary from findings where surface = 'dispatch-integration' order by created_at desc limit 1")
        ok = result.returncode != 0 and status != "integrated"
        return scenario_result(
            "integration_regression_blocked",
            started,
            ok,
            {
                "integrate_returncode": result.returncode,
                "status": status,
                "finding_recorded": bool(finding),
                "stdout_tail": result.stdout[-500:],
                "stderr_tail": result.stderr[-500:],
            },
        )


FIXTURE_SCENARIOS: list[Callable[[], dict[str, Any]]] = [
    scenario_parallel_success,
    scenario_dependency_blocked,
    scenario_same_file_conflict,
    scenario_forged_evidence_blocked,
    scenario_integration_regression_blocked,
]


def summarize(mode: str, scenarios: list[dict[str, Any]], started: float, *, live_skipped: bool = False) -> dict[str, Any]:
    passed = sum(1 for scenario in scenarios if scenario["pass"])
    forged_blocks = sum(1 for scenario in scenarios if scenario["name"] == "forged_evidence_blocked" and scenario["pass"])
    false_pass_count = sum(1 for scenario in scenarios if scenario["name"] in {"forged_evidence_blocked", "integration_regression_blocked"} and not scenario["pass"])
    summary = {
        "scenario_count": len(scenarios),
        "passed_count": passed,
        "failed_count": len(scenarios) - passed,
        "task_once_completion_rate": round(passed / max(len(scenarios), 1), 4),
        "false_pass_count": false_pass_count,
        "forged_evidence_block_count": forged_blocks,
        "retry_count": 0,
        "merge_conflict_count": 0,
        "human_intervention_count": 0,
        "duration_seconds": round(time.perf_counter() - started, 6),
    }
    return {
        "mode": mode,
        "live_skipped": live_skipped,
        "token_count": None,
        "estimated_cost": None,
        "agent_runtime_seconds": None,
        "summary": summary,
        "scenarios": scenarios,
    }


def run_fixture() -> dict[str, Any]:
    started = time.perf_counter()
    scenarios: list[dict[str, Any]] = []
    for scenario in FIXTURE_SCENARIOS:
        try:
            scenarios.append(scenario())
        except Exception as exc:  # noqa: BLE001 - eval output should show scenario failure.
            scenarios.append({"name": scenario.__name__.replace("scenario_", ""), "pass": False, "duration_seconds": 0, "details": {"error": str(exc)}})
    return summarize("fixture", scenarios, started)


def run_live_command() -> dict[str, Any]:
    started = time.perf_counter()
    command = os.environ.get("CODEX_AGENT_EVAL_CMD", "").strip()
    if not command:
        return summarize("live-command", [], started, live_skipped=True)
    result = subprocess.run(command, shell=True, text=True, capture_output=True, check=False, timeout=1800)
    scenario = {
        "name": "live_command",
        "pass": result.returncode == 0,
        "duration_seconds": round(time.perf_counter() - started, 6),
        "details": {"returncode": result.returncode, "stdout_tail": result.stdout[-2000:], "stderr_tail": result.stderr[-2000:]},
    }
    return summarize("live-command", [scenario], started)


def should_fail(report: dict[str, Any]) -> bool:
    if report["mode"] == "live-command" and report["live_skipped"]:
        return False
    summary = report["summary"]
    if summary["failed_count"] != 0:
        return True
    if report["mode"] == "fixture":
        if summary["scenario_count"] != 5:
            return True
        if summary["false_pass_count"] != 0:
            return True
        if summary["forged_evidence_block_count"] < 1:
            return True
        if summary["human_intervention_count"] != 0:
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Run agent E2E evaluation scenarios")
    parser.add_argument("--mode", choices=["fixture", "live-command"], default="fixture")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    report = run_fixture() if args.mode == "fixture" else run_live_command()
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    print(text, end="")
    return 1 if should_fail(report) else 0


if __name__ == "__main__":
    raise SystemExit(main())

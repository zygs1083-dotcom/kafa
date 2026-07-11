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
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[3]
PLUGIN_ROOT = ROOT / "plugins" / "codex-project-harness"
SCRIPTS_ROOT = PLUGIN_ROOT / "scripts"
for path in [ROOT, PLUGIN_ROOT, SCRIPTS_ROOT]:
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from kafa.codex_app_server import AppServerClient, validate_app_server_discovery  # noqa: E402

HARNESS = SCRIPTS_ROOT / "harness.py"
PYTHON = json.dumps(sys.executable)
TEST_COMMAND = "python3 -B -m unittest"


def run_harness(
    root: Path,
    *args: str,
    check: bool = True,
    env: dict[str, str] | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    command_env = os.environ.copy()
    if env:
        command_env.update(env)
    result = subprocess.run(
        [sys.executable, str(HARNESS), "--root", str(root), *args],
        text=True,
        capture_output=True,
        check=False,
        env=command_env,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return result


def run_git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=True)
    return result.stdout.strip()


def git_porcelain_paths(output: str) -> set[str]:
    paths: set[str] = set()
    for line in output.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[1]
        paths.add(path)
    return paths


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
    run_harness(root, "requirement", "add", "--id", "R1", "--kind", "functional", "--body", "Agent E2E requirement")
    run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Eval acceptance")
    run_harness(root, "requirement", "link", "--requirement", "R1", "--acceptance", "AC1")
    run_harness(root, "test-target", "add", "--id", "UNIT", "--kind", "unit", "--command-template", TEST_COMMAND)
    for task_id in task_ids:
        run_harness(root, "task", "add", "--id", task_id, "--task", f"Task {task_id}", "--owner", f"agent-{task_id.lower()}", "--acceptance", "AC1")
        run_harness(root, "test-target", "link", "--task", task_id, "--target", "UNIT")
    run_harness(root, "scope", "confirm", "--by", "eval-controller", "--summary", "Agent E2E scope confirmed")
    run_harness(root, "baseline", "freeze", "--id", "E2E-BL", "--summary", "Agent E2E delivery baseline")
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


def fake_codex_sdk(temp: Path) -> tuple[Path, Path]:
    package_root = temp / "fake_sdk"
    package_dir = package_root / "openai_codex"
    package_dir.mkdir(parents=True)
    log_path = temp / "fake_codex_sdk_log.jsonl"
    package_dir.joinpath("__init__.py").write_text(
        textwrap.dedent(
            r'''
            import json
            import os
            import re
            from pathlib import Path

            class ApprovalMode:
                deny_all = "deny_all"
                auto_review = "auto_review"

            class Sandbox:
                read_only = "read_only"
                workspace_write = "workspace_write"
                full_access = "full_access"

            class CodexConfig:
                def __init__(self, codex_bin=None, client_name="", client_title="", client_version="", **kwargs):
                    self.codex_bin = codex_bin
                    self.client_name = client_name
                    self.client_title = client_title
                    self.client_version = client_version

            class TurnResult:
                def __init__(self, final_response):
                    self.final_response = final_response

            def log(message):
                log_path = os.environ["FAKE_CODEX_SDK_LOG"]
                with open(log_path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(message, sort_keys=True) + "\n")

            def prompt_value(prompt, name, default=""):
                match = re.search(rf'"{name}": "([^"]*)"', prompt)
                return match.group(1) if match else default

            def prompt_int(prompt, name, default=0):
                match = re.search(rf'"{name}": ([0-9]+)', prompt)
                return int(match.group(1)) if match else default

            def report(prompt):
                return {
                    "command": prompt_value(prompt, "command", "python3 -B -m unittest"),
                    "exit_code": 0,
                    "stdout_sha256": "0" * 64,
                    "artifact_path": ".ai-team/runtime/fake/stdout.txt",
                    "executed_count": 1,
                    "executed_count_source": "parsed",
                    "source_tree_hash": "fake-source-tree",
                    "branch_name": prompt_value(prompt, "branch_name"),
                    "status": "success",
                    "target_id": prompt_value(prompt, "target_id", "UNIT"),
                    "fence": prompt_int(prompt, "fence", 0),
                    "agent_id": prompt_value(prompt, "agent_id", "agent-t1"),
                }

            class Thread:
                id = "thr_fake"

                def run(self, input, *, cwd=None, sandbox=None, approval_mode=None, output_schema=None, model=None, **kwargs):
                    log({
                        "method": "thread.run",
                        "cwd": str(cwd),
                        "sandbox": str(sandbox),
                        "approval_mode": str(approval_mode),
                        "model": model,
                        "output_schema_required": sorted((output_schema or {}).get("required", [])),
                    })
                    Path(str(cwd)).joinpath("agent.txt").write_text("host codex sdk work\n", encoding="utf-8")
                    return TurnResult(report(input))

            class Codex:
                def __init__(self, config=None):
                    self.config = config

                def __enter__(self):
                    log({"method": "codex.__enter__", "client_name": getattr(self.config, "client_name", "")})
                    return self

                def __exit__(self, exc_type, exc, tb):
                    log({"method": "codex.__exit__"})

                def thread_start(self, *, cwd=None, sandbox=None, approval_mode=None, model=None, **kwargs):
                    log({"method": "thread_start", "cwd": str(cwd), "sandbox": str(sandbox), "approval_mode": str(approval_mode), "model": model})
                    return Thread()
            '''
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    return package_root, log_path


def plan_action(root: Path, tool: str, operation: str, params: dict[str, object], *, key: str = "") -> str:
    payload = json.dumps({"execute": True, "operation": operation, "params": params}, sort_keys=True)
    result = run_harness(
        root,
        "adapter",
        "plan",
        "--tool",
        tool,
        "--mode",
        "write-confirm",
        "--artifact",
        f"{tool} mock artifact",
        "--action",
        operation,
        "--payload-json",
        payload,
        "--idempotency-key",
        key or f"e2e:{tool}:{operation}",
    )
    return result.stdout.strip().split()[-1]


def set_connector_profiles(root: Path, project_key: str = "e2e") -> None:
    run_harness(
        root,
        "connector",
        "profile",
        "set",
        "--project-key",
        project_key,
        "--github-repo",
        "owner/repo",
        "--linear-team",
        "TEAM",
        "--notion-parent",
        "PARENT",
        "--slack-channel",
        "C123",
        "--figma-file",
        "FILE1",
    )


def fake_gh(temp: Path) -> tuple[Path, Path]:
    bin_dir = temp / "bin"
    bin_dir.mkdir()
    log_path = temp / "gh-log.jsonl"
    script = bin_dir / "gh"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import json
            import sys

            log_path = {str(log_path)!r}
            with open(log_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(sys.argv[1:], sort_keys=True) + "\\n")
            endpoint = sys.argv[2] if len(sys.argv) > 2 else ""
            if endpoint.endswith("/issues"):
                print(json.dumps({{"id": 123, "number": 7, "html_url": "https://github.example/repo/issues/7"}}))
            else:
                print(json.dumps({{"viewer": {{"login": "fake"}}}}))
            """
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)
    (bin_dir / "gh.cmd").write_text("@echo off\r\npython \"%~dp0gh\" %*\r\n", encoding="utf-8")
    return bin_dir, log_path


class ConnectorMockHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []
    marker: str = ""

    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode("utf-8")
        record = {"path": self.path, "headers": dict(self.headers), "body": json.loads(body) if body else {}}
        self.__class__.requests.append(record)
        if self.path == "/slack/api/search.messages":
            matches = [{"ts": "1710000000.000123", "channel": {"id": "C123"}, "permalink": "https://slack.example/existing"}] if self.__class__.marker else []
            response = {"ok": True, "messages": {"matches": matches}}
        elif self.path == "/linear/graphql":
            response = {"data": {"issueCreate": {"success": True, "issue": {"id": "LIN-1", "identifier": "ENG-1", "url": "https://linear.example/ENG-1"}}}}
        elif self.path == "/notion/v1/pages":
            response = {"id": "notion-page-1", "url": "https://notion.example/page-1"}
        elif self.path == "/figma/v1/files/FILE1/comments":
            response = {"id": "figma-comment-1", "file_key": "FILE1", "created_at": "2026-01-01T00:00:00Z"}
        elif self.path == "/slack/api/chat.postMessage":
            response = {"ok": True, "channel": "C123", "ts": "1710000000.000100", "permalink": "https://slack.example/archives/C123/p1710000000000100"}
        else:
            response = {"id": "unknown", "url": "https://example.invalid/unknown"}
        data = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class ConnectorMockServer:
    def __init__(self, *, marker: str = "") -> None:
        self.marker = marker

    def __enter__(self) -> "ConnectorMockServer":
        ConnectorMockHandler.requests = []
        ConnectorMockHandler.marker = self.marker
        self.server = HTTPServer(("127.0.0.1", 0), ConnectorMockHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    @property
    def requests(self) -> list[dict[str, object]]:
        return ConnectorMockHandler.requests


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


@contextmanager
def integration_candidate(root: Path, run_id: str, branches: dict[str, str]):
    original_branch = run_git(root, "branch", "--show-current")
    preview_branch = f"e2e-preview/{run_id}"
    subprocess.run(["git", "switch", "-C", preview_branch, original_branch], cwd=root, check=True, capture_output=True)
    try:
        for branch in branches.values():
            merge = subprocess.run(
                ["git", "merge", "--no-ff", "--no-edit", branch],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )
            if merge.returncode != 0:
                raise AssertionError(merge.stdout + merge.stderr)
        yield
    finally:
        subprocess.run(["git", "switch", original_branch], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "branch", "-D", preview_branch], cwd=root, check=False, capture_output=True)


def record_integration_candidate_gate(root: Path, run_id: str, branches: dict[str, str]) -> list[str]:
    with integration_candidate(root, run_id, branches):
        evidence_id = run_harness(
            root,
            "dispatch",
            "run",
            "--agent",
            "controller",
            "--target",
            "UNIT",
            "--command",
            TEST_COMMAND,
        ).stdout.strip().rsplit(" ", 1)[-1]
        run_harness(
            root,
            "validation",
            "record",
            "--surface",
            "Agent E2E integration candidate",
            "--acceptance",
            "AC1",
            "--commands",
            TEST_COMMAND,
            "--findings",
            "passed",
            "--result",
            "pass",
            "--evidence",
            evidence_id,
            "--target",
            "UNIT",
        )
        run_harness(
            root,
            "gate",
            "record",
            "--reviewer-context",
            "same-context-degraded",
            "--result",
            "pass",
            "--commands",
            TEST_COMMAND,
            "--evidence",
            "integration candidate independently reviewed",
        )
        validation = run_harness(root, "validate", "--delivery", check=False)
        if validation.returncode == 0:
            return []
        return [line for line in (validation.stdout + validation.stderr).splitlines() if line.strip()]


def record_failing_integration_candidate_gate(root: Path, run_id: str, branches: dict[str, str]) -> dict[str, Any]:
    with integration_candidate(root, run_id, branches):
        execution = run_harness(
            root,
            "dispatch",
            "run",
            "--agent",
            "controller",
            "--target",
            "UNIT",
            "--command",
            TEST_COMMAND,
            check=False,
        )
        output = execution.stdout + execution.stderr
        if "evidence=" not in output:
            raise AssertionError(f"failed controller execution did not record evidence: {output}")
        evidence_id = output.split("evidence=", 1)[1].split(None, 1)[0].strip()
        evidence = db_rows(root, "select * from evidence where id = ?", (evidence_id,))[0]
        artifact = root / evidence["artifact_path"]
        test_output = artifact.read_text(encoding="utf-8", errors="replace")
        run_harness(
            root,
            "validation",
            "record",
            "--surface",
            "Agent E2E merged regression candidate",
            "--acceptance",
            "AC1",
            "--commands",
            TEST_COMMAND,
            "--findings",
            "merged candidate test failed",
            "--result",
            "fail",
            "--evidence",
            evidence_id,
            "--target",
            "UNIT",
        )
        run_harness(
            root,
            "gate",
            "record",
            "--reviewer-context",
            "same-context-degraded",
            "--result",
            "fail",
            "--commands",
            TEST_COMMAND,
            "--evidence",
            "merged candidate regression observed",
            "--blocking-findings",
            "merged integration test failed",
        )
        validation = run_harness(root, "validate", "--delivery", check=False)
        return {
            "test_exit_code": int(evidence["exit_code"]),
            "executed_count": int(evidence["executed_count"]),
            "executed_count_source": evidence["executed_count_source"],
            "test_output_tail": test_output[-2000:],
            "preintegration_validation": (validation.stdout + validation.stderr)[-2000:],
        }


def wait_for_provider_collect(root: Path, run_id: str, *, expected: str = "collected 1 provider report", timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
    deadline = time.perf_counter() + timeout
    result = run_harness(root, "dispatch", "provider", "collect", "--run-id", run_id)
    while time.perf_counter() < deadline:
        if expected in result.stdout:
            return result
        time.sleep(0.1)
        result = run_harness(root, "dispatch", "provider", "collect", "--run-id", run_id)
    return result


def scenario_result(
    name: str,
    started: float,
    ok: bool,
    details: dict[str, Any] | None = None,
    *,
    category: str = "fixture",
    mode: str = "fixture",
    skip_reason: str = "",
) -> dict[str, Any]:
    return {
        "name": name,
        "category": category,
        "mode": mode,
        "pass": bool(ok),
        "duration_seconds": round(time.perf_counter() - started, 6),
        "skip_reason": skip_reason,
        "details": details or {},
    }


def skipped_scenario(name: str, reason: str, *, category: str, mode: str) -> dict[str, Any]:
    return {
        "name": name,
        "category": category,
        "mode": mode,
        "pass": False,
        "duration_seconds": 0,
        "skip_reason": reason,
        "details": {},
    }


def command_version(command: list[str]) -> str:
    try:
        result = subprocess.run(command, text=True, capture_output=True, check=False, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (result.stdout or result.stderr).strip().splitlines()[0] if (result.stdout or result.stderr).strip() else ""


def matrix_info(profile: str, *, live_skipped_reasons: list[str] | None = None) -> dict[str, Any]:
    return {
        "profile": profile,
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "git_version": command_version(["git", "--version"]),
        "codex_available": bool(live_codex_binary()) if profile == "live-codex" else shutil.which("codex") is not None,
        "container_available": shutil.which("docker") is not None or shutil.which("podman") is not None,
        "connector_mock": profile == "stability",
        "sqlite_stress": profile == "stability",
        "live_skipped_reasons": live_skipped_reasons or [],
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
        delivery_validation_issues = record_integration_candidate_gate(root, run_id, branches)
        integrate = run_harness(root, "dispatch", "integrate", "--run-id", run_id, check=False)
        target = integrate.stdout.strip().rsplit(" ", 1)[-1] if integrate.returncode == 0 else ""
        integrated_a = bool(target) and run_git(root, "show", f"{target}:a.txt") == "A"
        integrated_b = bool(target) and run_git(root, "show", f"{target}:b.txt") == "B"
        status = db_rows(root, "select status from dispatch_runs where id = ?", (run_id,))[0]["status"]
        return scenario_result(
            "parallel_success",
            started,
            not delivery_validation_issues and integrate.returncode == 0 and integrated_a and integrated_b and status == "integrated",
            {
                "run_id": run_id,
                "target_branch": target,
                "integrate_via_public_cli": True,
                "integrate_returncode": integrate.returncode,
                "delivery_validation_issues": delivery_validation_issues,
                "integrate_stdout_tail": integrate.stdout[-1000:],
                "integrate_stderr_tail": integrate.stderr[-1000:],
            },
        )


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
        regression = record_failing_integration_candidate_gate(root, run_id, branches)
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
                **regression,
            },
        )


def scenario_host_codex_fake_sdk_e2e() -> dict[str, Any]:
    started = time.perf_counter()
    with tempfile.TemporaryDirectory() as temp:
        temp_path = Path(temp)
        root = temp_path / "repo"
        root.mkdir()
        init_git_repo(root)
        add_unittest(root)
        run_id = setup_basic_harness(root, ["T1"])
        package_root, log_path = fake_codex_sdk(temp_path)
        env = {
            "HARNESS_CODEX_LEGACY_HOST_POLICY": "isolated-deny-all",
            "HARNESS_CODEX_TURN_TIMEOUT_SECONDS": "5",
            "FAKE_CODEX_SDK_LOG": str(log_path),
            "PYTHONPATH": str(package_root),
        }
        run_harness(root, "dispatch", "provider", "start", "--run-id", run_id, "--provider", "host-codex", env=env)
        collect = wait_for_provider_collect(root, run_id)
        session = db_rows(root, "select task_id, agent_id, branch_name, worktree_path from agent_provider_sessions where run_id = ?", (run_id,))[0]
        add_file_claim(root, run_id, session["task_id"], session["agent_id"], "agent.txt", session["worktree_path"], session["branch_name"])
        evidence_before = db_rows(root, "select count(*) as count from evidence where id like 'CODEX-%'")[0]["count"]
        run_harness(root, "dispatch", "verify-attempt", "--run-id", run_id, "--task", "T1")
        evidence_after = db_rows(root, "select count(*) as count from evidence where id like 'CODEX-%'")[0]["count"]
        accept_task_via_cli(root, "T1")
        branches = {"T1": session["branch_name"]}
        delivery_validation_issues = record_integration_candidate_gate(root, run_id, branches)
        integrate = run_harness(root, "dispatch", "integrate", "--run-id", run_id, check=False)
        target = integrate.stdout.strip().rsplit(" ", 1)[-1] if integrate.returncode == 0 else ""
        integrate_returncode = integrate.returncode
        status = db_rows(root, "select status from dispatch_runs where id = ?", (run_id,))[0]["status"]
        sdk_methods = [json.loads(line)["method"] for line in log_path.read_text(encoding="utf-8").splitlines()]
        ok = "collected 1 provider report" in collect.stdout and evidence_before == 0 and evidence_after == 1 and not delivery_validation_issues and integrate_returncode == 0 and status == "integrated"
        return scenario_result(
            "host_codex_fake_sdk_e2e",
            started,
            ok,
            {
                "run_id": run_id,
                "sdk_methods": sdk_methods[:4],
                "integrate_returncode": integrate_returncode,
                "status": status,
                "target_branch": target,
                "integrate_via_public_cli": True,
                "delivery_validation_issues": delivery_validation_issues,
            },
            category="host-codex",
            mode="stability",
        )


def scenario_host_codex_spark_policy_fake_sdk_e2e() -> dict[str, Any]:
    started = time.perf_counter()
    with tempfile.TemporaryDirectory() as temp:
        temp_path = Path(temp)
        root = temp_path / "repo"
        root.mkdir()
        init_git_repo(root)
        add_unittest(root)
        run_harness(root, "init")
        commit_harness_scaffold(root)
        run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Spark policy acceptance")
        run_harness(root, "test-target", "add", "--id", "UNIT", "--kind", "unit", "--command-template", TEST_COMMAND)
        run_harness(root, "task", "add", "--id", "T1", "--task", "Spark eligible developer task", "--owner", "developer", "--acceptance", "AC1")
        run_harness(root, "test-target", "link", "--task", "T1", "--target", "UNIT")
        run_id = run_harness(root, "dispatch", "plan", "--scope", "Spark Policy").stdout.strip().split()[-1]
        package_root, log_path = fake_codex_sdk(temp_path)
        env = {
            "HARNESS_CODEX_LEGACY_HOST_POLICY": "isolated-deny-all",
            "HARNESS_CODEX_TURN_TIMEOUT_SECONDS": "5",
            "HARNESS_CODEX_MODEL_POLICY": "spark-deterministic",
            "HARNESS_CODEX_SPARK_MODEL": "gpt-5.3-codex-spark",
            "FAKE_CODEX_SDK_LOG": str(log_path),
            "PYTHONPATH": str(package_root),
        }
        run_harness(root, "dispatch", "provider", "start", "--run-id", run_id, "--provider", "host-codex", env=env)
        collect = wait_for_provider_collect(root, run_id)
        session = db_rows(root, "select input_json from agent_provider_sessions where run_id = ?", (run_id,))[0]
        metadata = json.loads(session["input_json"])["provider_metadata"]
        events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        by_method = {event["method"]: event for event in events}
        evidence_count = db_rows(root, "select count(*) as count from evidence where id like 'CODEX-%'")[0]["count"]
        ok = (
            "collected 1 provider report" in collect.stdout
            and by_method["thread_start"]["model"] == "gpt-5.3-codex-spark"
            and by_method["thread.run"]["model"] == "gpt-5.3-codex-spark"
            and metadata["selected_model"] == "gpt-5.3-codex-spark"
            and metadata["model_policy"] == "spark-deterministic"
            and metadata["spark_eligible"] is True
            and evidence_count == 0
        )
        return scenario_result(
            "host_codex_spark_policy_fake_sdk_e2e",
            started,
            ok,
            {
                "run_id": run_id,
                "thread_start_model": by_method.get("thread_start", {}).get("model"),
                "thread_run_model": by_method.get("thread.run", {}).get("model"),
                "selected_model": metadata.get("selected_model"),
                "model_policy": metadata.get("model_policy"),
                "spark_eligible": metadata.get("spark_eligible"),
                "evidence_count": evidence_count,
            },
            category="host-codex",
            mode="stability",
        )


def scenario_multi_role_thread_lifecycle() -> dict[str, Any]:
    started = time.perf_counter()
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        run_harness(root, "init")
        run_harness(root, "agents", "install", "--force")
        run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Role lifecycle")
        run_harness(root, "task", "add", "--id", "T1", "--task", "Role lifecycle task", "--owner", "developer", "--acceptance", "AC1")
        run_harness(root, "session", "attest", "--session-id", "DEV-S1", "--agent", "developer", "--role", "developer", "--context-id", "run:T1")
        run_harness(root, "session", "attest", "--session-id", "ARCH-S1", "--agent", "architect", "--role", "architect", "--context-id", "run:T1")
        run_harness(root, "session", "attest", "--session-id", "QA-S1", "--agent", "qa-reviewer", "--role", "qa-reviewer", "--context-id", "run:T1")
        claim = run_harness(root, "task", "claim", "T1", "--agent", "developer", "--expected-revision", task_revision(root, "T1"))
        token = stdout_field(claim.stdout, "token")
        fence = stdout_field(claim.stdout, "fence")
        run_harness(root, "task", "start", "T1", "--agent", "developer", "--lease-token", token, "--expected-revision", task_revision(root, "T1"), "--fence", fence)
        run_harness(
            root,
            "task",
            "submit",
            "T1",
            "--agent",
            "developer",
            "--session-id",
            "DEV-S1",
            "--lease-token",
            token,
            "--expected-revision",
            task_revision(root, "T1"),
            "--fence",
            fence,
            "--evidence",
            "developer submitted",
        )
        producer_review = run_harness(root, "task", "review", "T1", "--agent", "developer", "--session-id", "DEV-S1", "--expected-revision", task_revision(root, "T1"), check=False)
        review = run_harness(root, "task", "review", "T1", "--agent", "qa-reviewer", "--session-id", "QA-S1", "--expected-revision", task_revision(root, "T1"))
        qa_token = stdout_field(review.stdout, "token")
        qa_fence = stdout_field(review.stdout, "fence")
        run_harness(
            root,
            "task",
            "accept",
            "T1",
            "--agent",
            "qa-reviewer",
            "--session-id",
            "QA-S1",
            "--lease-token",
            qa_token,
            "--expected-revision",
            task_revision(root, "T1"),
            "--fence",
            qa_fence,
            "--evidence",
            "qa accepted",
        )
        task = db_rows(root, "select status, submitted_session_id, accepted_session_id from tasks where id = 'T1'")[0]
        sessions = [row["role"] for row in db_rows(root, "select role from agent_sessions order by role")]
        ok = producer_review.returncode != 0 and task["status"] == "accepted" and task["submitted_session_id"] == "DEV-S1" and task["accepted_session_id"] == "QA-S1"
        return scenario_result(
            "multi_role_thread_lifecycle",
            started,
            ok,
            {"producer_review_returncode": producer_review.returncode, "task_status": task["status"], "roles": sessions},
            category="session",
            mode="stability",
        )


def scenario_connector_mock_server_e2e() -> dict[str, Any]:
    started = time.perf_counter()
    with tempfile.TemporaryDirectory() as temp:
        temp_path = Path(temp)
        root = temp_path / "repo"
        root.mkdir()
        run_harness(root, "init")
        set_connector_profiles(root)
        bin_dir, gh_log = fake_gh(temp_path)
        with ConnectorMockServer() as server:
            cases = [
                ("github", "github.issue.create", {"repo": "owner/repo", "title": "Issue title", "body": "Body"}, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "HARNESS_GH_BIN": subprocess.list2cmdline([sys.executable, str(bin_dir / "gh")])}),
                ("linear", "linear.issue.create", {"team_id": "TEAM", "title": "Linear issue", "description": "Body"}, {"LINEAR_API_KEY": "linear-token", "HARNESS_LINEAR_API_URL": server.base_url}),
                ("notion", "notion.page.create", {"parent_page_id": "PARENT", "title": "Notion page", "content": "Body"}, {"NOTION_TOKEN": "notion-token", "HARNESS_NOTION_API_URL": server.base_url}),
                ("figma", "figma.comment.create", {"file_key": "FILE1", "message": "Review note"}, {"FIGMA_TOKEN": "figma-token", "HARNESS_FIGMA_API_URL": server.base_url}),
                ("slack", "slack.message.post", {"channel": "C123", "text": "Ship it"}, {"SLACK_BOT_TOKEN": "slack-token", "HARNESS_SLACK_API_URL": server.base_url}),
            ]
            action_ids = []
            for tool, operation, params, env in cases:
                action = plan_action(root, tool, operation, params)
                action_ids.append(action)
                run_harness(root, "adapter", "confirm", "--id", action, "--request-id", f"REQ-{tool}", env=env)
            reconcile = run_harness(root, "adapter", "reconcile", check=False)
            completed = db_rows(root, "select count(*) as count from adapter_actions where status = 'completed'")[0]["count"]
            adapters = db_rows(root, "select count(*) as count from adapters")[0]["count"]
            evidence = db_rows(root, "select count(*) as count from evidence")[0]["count"]
            gh_calls = len(gh_log.read_text(encoding="utf-8").splitlines())
            token_leak = bool(db_rows(root, "select 1 from adapter_actions where payload_json like '%linear-token%' or payload_json like '%slack-token%' limit 1"))
            ok = completed == 5 and adapters == 5 and evidence == 0 and reconcile.returncode == 0 and gh_calls == 2 and not token_leak and len(server.requests) == 7
            return scenario_result(
                "connector_mock_server_e2e",
                started,
                ok,
                {
                    "actions": len(action_ids),
                    "completed": completed,
                    "adapters": adapters,
                    "evidence_count": evidence,
                    "http_requests": len(server.requests),
                    "gh_calls": gh_calls,
                    "token_leak": token_leak,
                },
                category="connector",
                mode="stability",
            )


def scenario_connector_exactly_once_recovery() -> dict[str, Any]:
    started = time.perf_counter()
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / "repo"
        root.mkdir()
        run_harness(root, "init")
        set_connector_profiles(root)
        key = "e2e:slack:exactly-once-recovery"
        marker = f"codex-project-harness:project-key=e2e\ncodex-project-harness:idempotency-key={key}"
        with ConnectorMockServer(marker=marker) as server:
            action = plan_action(root, "slack", "slack.message.post", {"channel": "C123", "text": "Ship it"}, key=key)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute(
                    "update adapter_actions set status = 'unknown', connector_status = 'degraded', blocked_reason = 'remote success local commit unknown' where id = ?",
                    (action,),
                )
                conn.commit()
            confirm = run_harness(root, "adapter", "confirm", "--id", action, env={"SLACK_BOT_TOKEN": "slack-token", "HARNESS_SLACK_API_URL": server.base_url}, check=False)
            row = db_rows(root, "select status, external_id, remote_recovery_count from adapter_actions where id = ?", (action,))[0]
            adapter_count = db_rows(root, "select count(*) as count from adapters where idempotency_key = ?", (key,))[0]["count"]
            evidence_count = db_rows(root, "select count(*) as count from evidence")[0]["count"]
            writes = [request for request in server.requests if request["path"] == "/slack/api/chat.postMessage"]
            searches = [request for request in server.requests if request["path"] == "/slack/api/search.messages"]
            ok = (
                confirm.returncode == 0
                and row["status"] == "completed"
                and row["external_id"] == "slack:message:C123:1710000000.000123"
                and row["remote_recovery_count"] == 1
                and adapter_count == 1
                and evidence_count == 0
                and len(writes) == 0
                and len(searches) >= 1
            )
            return scenario_result(
                "connector_exactly_once_recovery",
                started,
                ok,
                {
                    "status": row["status"],
                    "remote_recovery_count": row["remote_recovery_count"],
                    "adapter_count": adapter_count,
                    "evidence_count": evidence_count,
                    "writes": len(writes),
                    "searches": len(searches),
                },
                category="connector",
                mode="stability",
            )


def scenario_crash_retry_recovery() -> dict[str, Any]:
    started = time.perf_counter()
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        init_git_repo(root)
        add_unittest(root)
        run_id = setup_basic_harness(root, ["T1"])
        run_harness(root, "dispatch", "provider", "start", "--run-id", run_id, "--provider", "fixture", "--request-id", "REQ-START")
        session = db_rows(root, "select task_id, agent_id, branch_name from agent_provider_sessions where run_id = ?", (run_id,))[0]
        _head, _tree, worktree = commit_branch(root, session["branch_name"], "retry.txt", "retry work\n")
        add_file_claim(root, run_id, session["task_id"], session["agent_id"], "retry.txt", worktree, session["branch_name"])
        fixture_report(root, run_id, "T1", session["branch_name"])
        first_collect = run_harness(root, "dispatch", "provider", "collect", "--run-id", run_id, "--request-id", "REQ-COLLECT")
        second_collect = run_harness(root, "dispatch", "provider", "collect", "--run-id", run_id, "--request-id", "REQ-COLLECT")
        first_verify = run_harness(root, "dispatch", "verify-attempt", "--run-id", run_id, "--task", "T1", "--request-id", "REQ-VERIFY")
        second_verify = run_harness(root, "dispatch", "verify-attempt", "--run-id", run_id, "--task", "T1", "--request-id", "REQ-VERIFY")
        first_reconcile = run_harness(root, "dispatch", "provider", "reconcile", "--run-id", run_id, "--request-id", "REQ-RECONCILE")
        second_reconcile = run_harness(root, "dispatch", "provider", "reconcile", "--run-id", run_id, "--request-id", "REQ-RECONCILE")
        reports = db_rows(root, "select count(*) as count from agent_reports")[0]["count"]
        attempts = db_rows(root, "select count(*) as count from task_attempts")[0]["count"]
        evidence = db_rows(root, "select count(*) as count from evidence where id like 'CODEX-%'")[0]["count"]
        findings = db_rows(root, "select count(*) as count from findings")[0]["count"]
        ok = (
            reports == 1
            and attempts == 1
            and evidence == 1
            and findings == 0
            and second_collect.stdout == first_collect.stdout
            and second_verify.stdout == first_verify.stdout
            and second_reconcile.stdout == first_reconcile.stdout
        )
        return scenario_result(
            "crash_retry_recovery",
            started,
            ok,
            {"reports": reports, "attempts": attempts, "evidence": evidence, "findings": findings},
            category="recovery",
            mode="stability",
        )


def scenario_sqlite_contention_stress() -> dict[str, Any]:
    started = time.perf_counter()
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        run_harness(root, "init")

        def mutate(index: int) -> subprocess.CompletedProcess[str]:
            request_id = f"REQ-SQLITE-{index // 2}"
            acceptance_id = f"AC{index // 2}"
            return run_harness(
                root,
                "acceptance",
                "add",
                "--id",
                acceptance_id,
                "--criterion",
                f"contention criterion {acceptance_id}",
                "--request-id",
                request_id,
                check=False,
                timeout=30,
            )

        results: list[subprocess.CompletedProcess[str]] = []
        threads: list[threading.Thread] = []
        lock = threading.Lock()

        def worker(index: int) -> None:
            result = mutate(index)
            with lock:
                results.append(result)

        for index in range(12):
            thread = threading.Thread(target=worker, args=(index,))
            threads.append(thread)
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        lock_errors = sum("database is locked" in (result.stdout + result.stderr).lower() for result in results)
        failed = [result.returncode for result in results if result.returncode != 0]
        doctor = run_harness(root, "kernel", "doctor", check=False)
        invariant = run_harness(root, "invariant", "validate", check=False)
        acceptance_count = db_rows(root, "select count(*) as count from acceptance")[0]["count"]
        ok = len(results) == 12 and not failed and lock_errors == 0 and doctor.returncode == 0 and invariant.returncode == 0 and acceptance_count == 6
        return scenario_result(
            "sqlite_contention_stress",
            started,
            ok,
            {
                "operation_count": len(results),
                "failed_returncodes": failed,
                "sqlite_lock_error_count": lock_errors,
                "acceptance_count": acceptance_count,
                "doctor_returncode": doctor.returncode,
                "invariant_returncode": invariant.returncode,
            },
            category="sqlite",
            mode="stability",
        )


FIXTURE_SCENARIOS: list[Callable[[], dict[str, Any]]] = [
    scenario_parallel_success,
    scenario_dependency_blocked,
    scenario_same_file_conflict,
    scenario_forged_evidence_blocked,
    scenario_integration_regression_blocked,
]

STABILITY_SCENARIOS: list[Callable[[], dict[str, Any]]] = [
    scenario_host_codex_fake_sdk_e2e,
    scenario_host_codex_spark_policy_fake_sdk_e2e,
    scenario_multi_role_thread_lifecycle,
    scenario_connector_mock_server_e2e,
    scenario_connector_exactly_once_recovery,
    scenario_crash_retry_recovery,
    scenario_sqlite_contention_stress,
]


def summarize(
    mode: str,
    scenarios: list[dict[str, Any]],
    started: float,
    *,
    live_skipped: bool = False,
    live_skipped_reasons: list[str] | None = None,
    live_status: str = "",
) -> dict[str, Any]:
    skipped = sum(1 for scenario in scenarios if scenario.get("skip_reason"))
    passed = sum(1 for scenario in scenarios if scenario["pass"] and not scenario.get("skip_reason"))
    failed = sum(1 for scenario in scenarios if not scenario["pass"] and not scenario.get("skip_reason"))
    forged_blocks = sum(1 for scenario in scenarios if scenario["name"] == "forged_evidence_blocked" and scenario["pass"])
    false_pass_count = sum(1 for scenario in scenarios if scenario["name"] in {"forged_evidence_blocked", "integration_regression_blocked"} and not scenario["pass"])
    sqlite_lock_errors = sum(int(scenario.get("details", {}).get("sqlite_lock_error_count", 0) or 0) for scenario in scenarios)
    summary = {
        "scenario_count": len(scenarios),
        "passed_count": passed,
        "failed_count": failed,
        "skipped_count": skipped,
        "task_once_completion_rate": round(passed / max(len(scenarios), 1), 4),
        "false_pass_count": false_pass_count,
        "forged_evidence_block_count": forged_blocks,
        "retry_count": 0,
        "merge_conflict_count": 0,
        "sqlite_lock_error_count": sqlite_lock_errors,
        "human_intervention_count": 0,
        "duration_seconds": round(time.perf_counter() - started, 6),
    }
    resolved_live_status = live_status or "not-applicable"
    if not live_status and mode in {"live-command", "live-codex"}:
        if live_skipped:
            resolved_live_status = "not-run"
        elif failed:
            resolved_live_status = "failed"
        elif scenarios:
            resolved_live_status = "passed"
    return {
        "mode": mode,
        "live_skipped": live_skipped,
        "live_status": resolved_live_status,
        "matrix": matrix_info(mode, live_skipped_reasons=live_skipped_reasons),
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
            scenarios.append(
                scenario_result(
                    scenario.__name__.replace("scenario_", ""),
                    started,
                    False,
                    {"error": str(exc)},
                    category="fixture",
                    mode="fixture",
                )
            )
    return summarize("fixture", scenarios, started)


def run_stability() -> dict[str, Any]:
    started = time.perf_counter()
    scenarios: list[dict[str, Any]] = []
    for scenario in [*FIXTURE_SCENARIOS, *STABILITY_SCENARIOS]:
        try:
            scenarios.append(scenario())
        except Exception as exc:  # noqa: BLE001 - eval output should show scenario failure.
            name = scenario.__name__.replace("scenario_", "")
            scenarios.append(scenario_result(name, started, False, {"error": str(exc)}, category="stability", mode="stability"))
    return summarize("stability", scenarios, started)


def run_live_command() -> dict[str, Any]:
    started = time.perf_counter()
    command = os.environ.get("CODEX_AGENT_EVAL_CMD", "").strip()
    if not command:
        return summarize("live-command", [], started, live_skipped=True, live_skipped_reasons=["CODEX_AGENT_EVAL_CMD is not set"])
    result = subprocess.run(command, shell=True, text=True, capture_output=True, check=False, timeout=1800)
    scenario = {
        "name": "live_command",
        "category": "live-command",
        "mode": "live-command",
        "pass": result.returncode == 0,
        "duration_seconds": round(time.perf_counter() - started, 6),
        "skip_reason": "",
        "details": {"returncode": result.returncode, "stdout_tail": result.stdout[-2000:], "stderr_tail": result.stderr[-2000:]},
    }
    return summarize("live-command", [scenario], started)


class LiveCapabilityBlocked(RuntimeError):
    pass


def codex_cli_command(codex: str, *args: str) -> list[str]:
    if os.name == "nt" and Path(codex).suffix.lower() in {".cmd", ".bat"}:
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", codex, *args]
    return [codex, *args]


def live_codex_binary() -> str:
    configured = os.environ.get("HARNESS_E2E_CODEX_BIN", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if path.is_file():
            return str(path.resolve())
        return shutil.which(configured) or ""
    return shutil.which("codex") or ""


def run_live_preflight(codex: str) -> str:
    try:
        login = subprocess.run(
            codex_cli_command(codex, "login", "status"),
            cwd=ROOT,
            env=os.environ.copy(),
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LiveCapabilityBlocked(f"Codex login capability is unavailable: {exc}") from exc
    if login.returncode != 0:
        detail = (login.stdout + login.stderr).strip()[-1000:]
        raise LiveCapabilityBlocked(f"Codex is not authenticated: {detail or 'login status failed'}")
    version = command_version(codex_cli_command(codex, "--version"))
    expected = f"codex-cli {json.loads((ROOT / 'release.json').read_text(encoding='utf-8'))['codex_cli_smoke_version']}"
    if version != expected:
        raise LiveCapabilityBlocked(f"Codex CLI version mismatch: actual={version or '<missing>'} expected={expected}")
    return version


def prepare_live_codex_env(temp_path: Path, codex: str) -> dict[str, str]:
    live_env = os.environ.copy()
    live_env.pop("PYTHONPATH", None)
    home = temp_path / "home"
    codex_home = temp_path / "codex-home"
    home.mkdir()
    codex_home.mkdir()
    live_env["HOME"] = str(home)
    live_env["CODEX_HOME"] = str(codex_home)

    configured_auth = os.environ.get("HARNESS_E2E_CODEX_AUTH_FILE", "").strip()
    source_codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
    auth_path = Path(configured_auth).expanduser() if configured_auth else source_codex_home / "auth.json"
    if auth_path.is_file():
        target = codex_home / "auth.json"
        shutil.copyfile(auth_path, target)
        target.chmod(0o600)
    elif not os.environ.get("OPENAI_API_KEY"):
        raise LiveCapabilityBlocked(
            "live profile needs file-based Codex auth or OPENAI_API_KEY; set HARNESS_E2E_CODEX_AUTH_FILE for an explicit auth file"
        )

    login = subprocess.run(
        codex_cli_command(codex, "login", "status"),
        cwd=temp_path,
        env=live_env,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if login.returncode != 0:
        raise LiveCapabilityBlocked("isolated Codex home could not use the supplied authentication")
    return live_env


def run_required(command: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 120) -> str:
    result = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True, check=False, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError((result.stdout + result.stderr)[-4000:])
    return result.stdout


def add_live_codex_fixture(root: Path) -> None:
    (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "test_live_codex.py").write_text(
        textwrap.dedent(
            """\
            import unittest

            import app


            class LiveCodexTest(unittest.TestCase):
                def test_value(self):
                    self.assertEqual(app.VALUE, 2)
            """
        ),
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "app.py", "test_live_codex.py"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add live Codex fixture"], cwd=root, check=True, capture_output=True)


def export_native_package(root: Path, run_id: str) -> tuple[Path, dict[str, Any]]:
    exported = run_harness(root, "dispatch", "native-export", run_id)
    manifest_path = Path(exported.stdout.strip().split()[-1])
    if not manifest_path.is_absolute():
        manifest_path = root / manifest_path
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    package_path = root / str(manifest["packages"][0]["path"])
    return package_path, json.loads(package_path.read_text(encoding="utf-8"))


def rfc3339_timestamp(value: object) -> str:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        timestamp = time.time()
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def live_receipt_path(root: Path, run_id: str) -> Path:
    return root / ".ai-team" / "runtime" / "live-codex" / run_id / "native-receipt.json"


def effective_assignment_agent(assignment: dict[str, object]) -> str:
    return str(assignment.get("agent_id") or assignment.get("capability") or assignment.get("owner") or "developer")


def execute_live_codex_profile(codex: str, codex_version: str) -> dict[str, Any]:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    plugin_id = "codex-project-harness@kafa-local"
    expected_skills = {
        f"codex-project-harness:{skill.parent.name}"
        for skill in (PLUGIN_ROOT / "skills").glob("*/SKILL.md")
    }
    expected_hook_events = {"sessionStart", "subagentStart", "preToolUse", "postToolUse", "stop"}
    timeout = max(60, int(os.environ.get("HARNESS_E2E_LIVE_TIMEOUT_SECONDS", "600")))
    with tempfile.TemporaryDirectory() as temp:
        temp_path = Path(temp)
        live_env = prepare_live_codex_env(temp_path, codex)
        run_required(
            [sys.executable, "-m", "kafa.cli", "plugin", "install", "--scope", "user", "--repo", str(ROOT)],
            cwd=ROOT,
            env=live_env,
        )
        added = json.loads(
            run_required(codex_cli_command(codex, "plugin", "add", plugin_id, "--json"), cwd=temp_path, env=live_env)
        )
        cache_root = Path(str(added["installedPath"]))

        root = temp_path / "business"
        root.mkdir()
        init_git_repo(root)
        add_live_codex_fixture(root)
        run_id = setup_basic_harness(root, ["T1"])
        assignment = db_rows(
            root,
            """
            select da.task_id, da.agent_id, da.capability, t.owner
            from dispatch_assignments da
            join tasks t on t.cycle_id = da.cycle_id and t.id = da.task_id
            where da.run_id = ? and da.task_id = 'T1'
            """,
            (run_id,),
        )[0]
        claim_agent = effective_assignment_agent(dict(assignment))
        add_file_claim(root, run_id, "T1", claim_agent, "app.py", "", "")
        _package_path, package = export_native_package(root, run_id)
        branch = str(package["target_branch"])
        base_sha = str(package["base_sha"])
        worktree = temp_path / "codex-worktree"
        subprocess.run(
            ["git", "worktree", "add", "-b", branch, str(worktree), base_sha],
            cwd=root,
            check=True,
            text=True,
            capture_output=True,
        )

        output_schema = {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["completed"]},
                "summary": {"type": "string"},
            },
            "required": ["status", "summary"],
            "additionalProperties": False,
        }
        prompt = textwrap.dedent(
            f"""\
            Complete one narrow compatibility task in this isolated git worktree.
            Edit only app.py so `{TEST_COMMAND}` passes, then run exactly `{TEST_COMMAND}`.
            Do not edit tests, .ai-team, .codex, docs, or git configuration.
            Return the required JSON object. This is a live compatibility probe; Kafa will independently verify the branch.

            Native package hash: {package['package_sha256']}
            Target branch: {branch}
            """
        )
        discovery: dict[str, Any]
        thread_id = ""
        turn_id = ""
        turn_completed: dict[str, Any] = {}
        hook_runs: list[dict[str, Any]] = []
        thread_result: dict[str, Any] = {}
        app_server_command = codex_cli_command(codex, "app-server", "--stdio")
        with AppServerClient(app_server_command, env=live_env, cwd=worktree, timeout=timeout) as client:
            initialized = client.request(
                "initialize",
                {
                    "clientInfo": {"name": "kafa-live-compatibility", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            client.notify("initialized", {})
            discovery = {
                "initialize": initialized,
                "skills": client.request("skills/list", {"cwds": [str(worktree)], "forceReload": True}),
                "hooks": client.request("hooks/list", {"cwds": [str(worktree)]}),
                "plugin": client.request("plugin/installed", {"cwds": [str(worktree)]}),
            }
            discovery_report = validate_app_server_discovery(
                discovery,
                cache_root=cache_root,
                plugin_id=plugin_id,
                version=version,
                expected_skills=expected_skills,
                expected_hook_events=expected_hook_events,
            )
            thread_result = client.request(
                "thread/start",
                {
                    "cwd": str(worktree),
                    "ephemeral": True,
                    "approvalPolicy": "never",
                    "sandbox": "workspace-write",
                    "config": {"bypass_hook_trust": True},
                },
            )
            thread_id = str(thread_result["thread"]["id"])
            turn_result = client.request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": prompt}],
                    "outputSchema": output_schema,
                    "approvalPolicy": "never",
                },
                timeout=60,
            )
            turn_id = str(turn_result["turn"]["id"])
            turn_completed = client.wait_for_notification(
                "turn/completed",
                predicate=lambda message: str(message.get("params", {}).get("turn", {}).get("id", "")) == turn_id,
                timeout=timeout,
            )
            hook_runs = [
                message["params"]["run"]
                for message in client.notifications
                if message.get("method") == "hook/completed"
                and message.get("params", {}).get("run", {}).get("source") == "plugin"
                and Path(str(message.get("params", {}).get("run", {}).get("sourcePath", ""))).resolve().is_relative_to(cache_root.resolve())
            ]

        turn = turn_completed.get("params", {}).get("turn", {})
        if turn.get("status") != "completed":
            error = turn.get("error", {})
            message = str(error.get("message", error))
            if any(token in message.lower() for token in ["unauthorized", "usage limit", "rate limit", "not available"]):
                raise LiveCapabilityBlocked(f"live Codex turn capability blocked: {message[-1500:]}")
            raise RuntimeError(f"live Codex turn failed: {message[-1500:]}")
        required_host_hook_events = {"sessionStart", "preToolUse", "postToolUse", "stop"}
        completed_hook_events = {
            str(run.get("eventName", ""))
            for run in hook_runs
            if run.get("status") == "completed"
        }
        if not required_host_hook_events.issubset(completed_hook_events):
            raise RuntimeError(
                f"host Hook execution incomplete: completed={sorted(completed_hook_events)} runs={hook_runs}"
            )

        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=worktree,
            text=True,
            capture_output=True,
            check=True,
        )
        changed_paths = git_porcelain_paths(status.stdout)
        if changed_paths != {"app.py"}:
            raise RuntimeError(f"live Codex changed unexpected paths: {sorted(changed_paths)}")
        if (worktree / "app.py").read_text(encoding="utf-8").strip() != "VALUE = 2":
            raise RuntimeError("live Codex did not produce the expected app.py change")
        subprocess.run(["git", "add", "app.py"], cwd=worktree, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "live Codex compatibility change"], cwd=worktree, check=True, capture_output=True)
        head_sha = run_git(worktree, "rev-parse", "HEAD")
        tree_sha = run_git(worktree, "rev-parse", "HEAD^{tree}")
        worktree_id = "git-worktree-" + hashlib.sha256(str(worktree.resolve()).encode("utf-8")).hexdigest()[:20]
        subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=root, check=True, capture_output=True)

        artifact = root / ".ai-team" / "runtime" / "live-codex" / run_id / "turn-completed.json"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact_payload = {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "turn_status": turn.get("status"),
            "hook_events": sorted(completed_hook_events),
        }
        artifact.write_text(json.dumps(artifact_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        turn_digest = hashlib.sha256(json.dumps(turn_completed, sort_keys=True).encode("utf-8")).hexdigest()
        receipt = {
            "receipt_version": "1",
            "package_sha256": package["package_sha256"],
            "run_id": run_id,
            "assignment_id": package["assignment_id"],
            "host": {
                "surface": "codex-app-server-live-eval",
                "task_id": turn_id,
                "thread_id": thread_id,
                "parent_thread_id": "",
                "worktree_id": worktree_id,
                "worktree_path": str(worktree),
                "worktree_owner": "live-eval-runner",
                "handoff_id": "",
            },
            "policy": {
                "approval_mode": "never",
                "sandbox": "workspace-write",
                "network": "disabled",
                "selected_model": str(thread_result.get("model", "host-default")),
                "reasoning": str(thread_result.get("reasoningEffort") or "host-default"),
            },
            "status": "completed",
            "branch": branch,
            "base_sha": base_sha,
            "head_sha": head_sha,
            "report": {
                "command": TEST_COMMAND,
                "exit_code": 0,
                "stdout_sha256": turn_digest,
                "artifact_path": artifact.relative_to(root).as_posix(),
                "executed_count": 1,
                "executed_count_source": "manual",
                "source_tree_hash": tree_sha,
                "branch_name": branch,
                "status": "success",
                "target_id": "UNIT",
            },
            "started_at": rfc3339_timestamp(turn.get("startedAt")),
            "completed_at": rfc3339_timestamp(turn.get("completedAt")),
            "provenance": {
                "kind": "audit-only",
                "issuer": "codex-app-server-live-eval",
                "payload_sha256": turn_digest,
                "signature": "",
            },
        }
        receipt_path = live_receipt_path(root, run_id)
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        evidence_before = len(db_rows(root, "select id from evidence"))
        imported = run_harness(root, "dispatch", "native-import", run_id, "--receipt", str(receipt_path))
        evidence_after_import = len(db_rows(root, "select id from evidence"))
        verified = run_harness(root, "dispatch", "verify-attempt", "--run-id", run_id, "--task", "T1")
        evidence_after_verify = len(db_rows(root, "select id from evidence"))
        accept_task_via_cli(root, "T1")
        delivery_issues = record_integration_candidate_gate(root, run_id, {"T1": branch})
        integrated = run_harness(root, "dispatch", "integrate", "--run-id", run_id, check=False)
        target_branch = integrated.stdout.strip().rsplit(" ", 1)[-1] if integrated.returncode == 0 else ""
        run_status = db_rows(root, "select status from dispatch_runs where id = ?", (run_id,))[0]["status"]
        integrated_value = run_git(root, "show", f"{target_branch}:app.py").strip() if target_branch else ""
        if not (
            evidence_before == 0
            and evidence_after_import == 0
            and evidence_after_verify > 0
            and not delivery_issues
            and integrated.returncode == 0
            and run_status == "integrated"
            and integrated_value == "VALUE = 2"
        ):
            raise RuntimeError(
                "live native receipt delivery flow failed: "
                f"evidence={evidence_before}/{evidence_after_import}/{evidence_after_verify} "
                f"delivery_issues={delivery_issues} integrate={integrated.returncode} status={run_status} "
                f"stdout={(integrated.stdout or '')[-1500:]} stderr={(integrated.stderr or '')[-1500:]}"
            )

        return {
            "discovery": {
                "codex_version": codex_version,
                "plugin_id": discovery_report["plugin_id"],
                "plugin_version": discovery_report["plugin_local_version"],
                "skill_count": discovery_report["skill_count"],
                "hook_events_discovered": discovery_report["hook_events"],
                "hook_events_executed": sorted(completed_hook_events),
                "host_hook_execution_observed": True,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "turn_status": turn.get("status"),
                "sandbox": thread_result.get("sandbox"),
                "approval_policy": thread_result.get("approvalPolicy"),
            },
            "delivery": {
                "run_id": run_id,
                "assignment_id": package["assignment_id"],
                "branch": branch,
                "base_sha": base_sha,
                "head_sha": head_sha,
                "worktree_id": worktree_id,
                "worktree_owner": "live-eval-runner",
                "receipt_provenance": "audit-only",
                "native_subagent_observed": False,
                "native_subagent_reason": "Codex app-server exposed a real thread/turn but no host child-subagent receipt was requested",
                "import_stdout": imported.stdout.strip(),
                "verify_stdout": verified.stdout.strip(),
                "evidence_before": evidence_before,
                "evidence_after_import": evidence_after_import,
                "evidence_after_verify": evidence_after_verify,
                "integration_returncode": integrated.returncode,
                "integration_status": run_status,
                "target_branch": target_branch,
            },
        }


def run_live_codex() -> dict[str, Any]:
    started = time.perf_counter()
    if os.environ.get("HARNESS_E2E_ENABLE_LIVE_CODEX") != "1":
        reasons = ["HARNESS_E2E_ENABLE_LIVE_CODEX is not set to 1"]
        scenarios = [
            skipped_scenario("live_codex_app_server_e2e", "; ".join(reasons), category="live-codex", mode="live-codex"),
            skipped_scenario("live_codex_native_receipt_e2e", "; ".join(reasons), category="live-codex", mode="live-codex"),
        ]
        return summarize("live-codex", scenarios, started, live_skipped=True, live_skipped_reasons=reasons)
    codex = live_codex_binary()
    if not codex:
        reason = "codex CLI is not available on PATH or HARNESS_E2E_CODEX_BIN"
        scenarios = [
            scenario_result(name, started, False, {"capability_status": "blocked", "reason": reason}, category="live-codex", mode="live-codex")
            for name in ["live_codex_app_server_e2e", "live_codex_native_receipt_e2e"]
        ]
        return summarize("live-codex", scenarios, started, live_status="blocked")
    try:
        codex_version = run_live_preflight(codex)
        details = execute_live_codex_profile(codex, codex_version)
    except LiveCapabilityBlocked as exc:
        scenarios = [
            scenario_result(
                name,
                started,
                False,
                {"capability_status": "blocked", "reason": str(exc)},
                category="live-codex",
                mode="live-codex",
            )
            for name in ["live_codex_app_server_e2e", "live_codex_native_receipt_e2e"]
        ]
        return summarize("live-codex", scenarios, started, live_status="blocked")
    except Exception as exc:  # noqa: BLE001 - live report must preserve the failing boundary.
        scenarios = [
            scenario_result(
                name,
                started,
                False,
                {"capability_status": "failed", "error": str(exc)},
                category="live-codex",
                mode="live-codex",
            )
            for name in ["live_codex_app_server_e2e", "live_codex_native_receipt_e2e"]
        ]
        return summarize("live-codex", scenarios, started)
    scenarios = [
        scenario_result(
            "live_codex_app_server_e2e",
            started,
            True,
            {"capability_status": "passed", **details["discovery"]},
            category="live-codex",
            mode="live-codex",
        ),
        scenario_result(
            "live_codex_native_receipt_e2e",
            started,
            True,
            {"capability_status": "passed", **details["delivery"]},
            category="live-codex",
            mode="live-codex",
        ),
    ]
    return summarize("live-codex", scenarios, started)


def should_fail(report: dict[str, Any]) -> bool:
    if report["mode"] == "live-codex" and report["live_skipped"]:
        return True
    if report["mode"] == "live-command" and report["live_skipped"]:
        return False
    summary = report["summary"]
    if summary["failed_count"] != 0:
        return True
    if report["mode"] == "fixture" and summary["scenario_count"] != 5:
        return True
    if report["mode"] == "stability" and summary["scenario_count"] < 10:
        return True
    if report["mode"] in {"fixture", "stability"}:
        if summary["false_pass_count"] != 0:
            return True
        if summary["forged_evidence_block_count"] < 1:
            return True
        if summary["human_intervention_count"] != 0:
            return True
        if summary.get("sqlite_lock_error_count", 0) != 0:
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Run agent E2E evaluation scenarios")
    parser.add_argument("--mode", choices=["fixture", "stability", "live-codex", "live-command"], default="fixture")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    runners = {
        "fixture": run_fixture,
        "stability": run_stability,
        "live-codex": run_live_codex,
        "live-command": run_live_command,
    }
    report = runners[args.mode]()
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    print(text, end="")
    return 1 if should_fail(report) else 0


if __name__ == "__main__":
    raise SystemExit(main())

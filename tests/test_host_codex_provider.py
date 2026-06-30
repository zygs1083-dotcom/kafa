from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import textwrap
import time
import unittest
from contextlib import closing
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "plugins/codex-project-harness/scripts/harness.py"
TEST_COMMAND = "python3 -m unittest"


def run_harness(root: Path, *args: str, env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    command_env = os.environ.copy()
    if env:
        command_env.update(env)
    result = subprocess.run(["python3", str(HARNESS), "--root", str(root), *args], text=True, capture_output=True, check=False, env=command_env)
    if check and result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return result


def git_repo(root: Path) -> None:
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
    (root / "test_sample.py").write_text(
        "import unittest\n\n"
        "class SampleTest(unittest.TestCase):\n"
        "    def test_ok(self):\n"
        "        self.assertTrue(True)\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "test_sample.py"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)


def bootstrap(root: Path) -> str:
    run_harness(root, "init")
    run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
    run_harness(root, "test-target", "add", "--id", "UNIT", "--kind", "unit", "--command-template", TEST_COMMAND)
    run_harness(root, "task", "add", "--id", "T1", "--task", "Example", "--owner", "developer", "--acceptance", "AC1")
    run_harness(root, "test-target", "link", "--task", "T1", "--target", "UNIT")
    return run_harness(root, "dispatch", "plan", "--scope", "Host Codex").stdout.strip().split()[-1]


def db_one(root: Path, query: str, params: tuple[object, ...] = ()) -> sqlite3.Row:
    with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(query, params).fetchone()


def db_all(root: Path, query: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
    with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(query, params).fetchall()


def wait_for_collect(root: Path, run_id: str, *, expected: str, timeout: float = 20.0) -> subprocess.CompletedProcess[str]:
    deadline = time.monotonic() + timeout
    last = run_harness(root, "dispatch", "provider", "collect", "--run-id", run_id)
    while time.monotonic() < deadline:
        if expected in last.stdout:
            return last
        time.sleep(0.1)
        last = run_harness(root, "dispatch", "provider", "collect", "--run-id", run_id)
    return last


def wait_for_session_status(root: Path, run_id: str, status: str, *, timeout: float = 60.0) -> sqlite3.Row:
    deadline = time.monotonic() + timeout
    row = db_one(root, "select status, last_error from agent_provider_sessions where run_id = ?", (run_id,))
    while time.monotonic() < deadline:
        if row["status"] == status:
            return row
        run_harness(root, "dispatch", "provider", "collect", "--run-id", run_id, check=False)
        time.sleep(0.1)
        row = db_one(root, "select status, last_error from agent_provider_sessions where run_id = ?", (run_id,))
    return row


def wait_for_sdk_events(log_path: Path, *, timeout: float = 5.0) -> list[dict[str, object]]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if log_path.exists() and "thread.run" in log_path.read_text(encoding="utf-8"):
            return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        time.sleep(0.05)
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]


def fake_sdk_package(temp: Path, *, import_error: bool = False) -> tuple[Path, Path]:
    package_root = temp / "fake_sdk"
    package_dir = package_root / "openai_codex"
    package_dir.mkdir(parents=True)
    log_path = temp / "fake_codex_sdk_log.jsonl"
    if import_error:
        package_dir.joinpath("__init__.py").write_text("raise ImportError('fake sdk missing')\n", encoding="utf-8")
        return package_root, log_path
    package_dir.joinpath("__init__.py").write_text(
        textwrap.dedent(
            r'''
            import json
            import os
            import re
            import time
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

            def value_name(value):
                return getattr(value, "value", value)

            def log(message):
                path = os.environ["FAKE_CODEX_SDK_LOG"]
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(message, sort_keys=True) + "\n")

            def prompt_value(prompt, name, default=""):
                match = re.search(rf'"{name}": "([^"]*)"', prompt)
                return match.group(1) if match else default

            def report_from_prompt(prompt):
                mode = os.environ.get("FAKE_CODEX_MODE", "success")
                expected = {
                    "command": prompt_value(prompt, "command", "python3 -m unittest"),
                    "exit_code": 0,
                    "stdout_sha256": "0" * 64,
                    "artifact_path": ".ai-team/runtime/fake/stdout.txt",
                    "executed_count": 1,
                    "executed_count_source": "parsed",
                    "source_tree_hash": "fake-source-tree",
                    "branch_name": prompt_value(prompt, "branch_name"),
                    "status": "success",
                    "target_id": prompt_value(prompt, "target_id", "UNIT"),
                    "fence": int(prompt_value(prompt, "fence", "0")),
                    "agent_id": prompt_value(prompt, "agent_id", "developer"),
                }
                if mode == "manual":
                    expected["executed_count_source"] = "manual"
                if mode == "branch-mismatch":
                    expected["branch_name"] = "agent/wrong/branch"
                return expected

            class Thread:
                id = "thr_fake"

                def run(self, input, *, cwd=None, sandbox=None, approval_mode=None, output_schema=None, model=None, **kwargs):
                    mode = os.environ.get("FAKE_CODEX_MODE", "success")
                    delay_seconds = float(os.environ.get("FAKE_CODEX_DELAY_SECONDS", "0"))
                    prompt = input
                    if delay_seconds:
                        time.sleep(delay_seconds)
                    log({
                        "method": "thread.run",
                        "cwd": str(cwd),
                        "sandbox": value_name(sandbox),
                        "approval_mode": value_name(approval_mode),
                        "model": model,
                        "output_schema_required": sorted((output_schema or {}).get("required", [])),
                    })
                    if mode == "exception":
                        raise RuntimeError("fake sdk boom")
                    if mode != "no-write":
                        Path(str(cwd)).joinpath("agent.txt").write_text("agent work from fake sdk\n", encoding="utf-8")
                    if mode == "invalid-json":
                        return TurnResult("not a json result")
                    return TurnResult(report_from_prompt(prompt))

            class Codex:
                def __init__(self, config=None):
                    self.config = config

                def __enter__(self):
                    log({
                        "method": "codex.__enter__",
                        "codex_bin": getattr(self.config, "codex_bin", None),
                        "client_name": getattr(self.config, "client_name", ""),
                        "client_version": getattr(self.config, "client_version", ""),
                    })
                    return self

                def __exit__(self, exc_type, exc, tb):
                    log({"method": "codex.__exit__"})

                def thread_start(self, *, cwd=None, sandbox=None, approval_mode=None, model=None, **kwargs):
                    log({
                        "method": "thread_start",
                        "cwd": str(cwd),
                        "sandbox": value_name(sandbox),
                        "approval_mode": value_name(approval_mode),
                        "model": model,
                    })
                    return Thread()
            '''
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    return package_root, log_path


class HostCodexProviderTest(unittest.TestCase):
    def host_env(self, package_root: Path, log_path: Path, *, mode: str = "success", timeout: str = "5") -> dict[str, str]:
        return {
            "HARNESS_CODEX_TURN_TIMEOUT_SECONDS": timeout,
            "FAKE_CODEX_SDK_LOG": str(log_path),
            "FAKE_CODEX_MODE": mode,
            "PYTHONPATH": str(package_root),
        }

    def test_host_codex_start_returns_before_turn_completion_and_releases_db_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            root = temp_path / "repo"
            root.mkdir()
            git_repo(root)
            run_id = bootstrap(root)
            package_root, log_path = fake_sdk_package(temp_path)
            env = self.host_env(package_root, log_path)
            env["FAKE_CODEX_DELAY_SECONDS"] = "2"

            started_at = time.monotonic()
            started = run_harness(root, "dispatch", "provider", "start", "--run-id", run_id, "--provider", "host-codex", env=env)
            duration = time.monotonic() - started_at
            other_write = run_harness(root, "acceptance", "add", "--id", "AC-LOCK", "--criterion", "DB writes are not blocked")
            early_collect = run_harness(root, "dispatch", "provider", "collect", "--run-id", run_id)

            self.assertLess(duration, 1.0)
            self.assertIn("started 1 provider session", started.stdout)
            self.assertIn("OK: acceptance added", other_write.stdout)
            self.assertIn("collected 0 provider report", early_collect.stdout)
            session = db_one(root, "select * from agent_provider_sessions where run_id = ?", (run_id,))
            metadata = json.loads(session["input_json"])["provider_metadata"]
            self.assertEqual(session["provider"], "host-codex")
            self.assertEqual(session["status"], "running")
            self.assertTrue(session["worktree_path"])
            self.assertTrue(session["provider_session_id"].startswith("host-codex:"))
            self.assertTrue(metadata["worker_pid"])
            self.assertTrue(metadata["report_path"].endswith("/T1.json"))
            run_harness(root, "dispatch", "provider", "cancel", "--run-id", run_id, "--reason", "test cleanup")

    def test_host_codex_start_honors_max_concurrency_without_waiting_for_first_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            root = temp_path / "repo"
            root.mkdir()
            git_repo(root)
            run_harness(root, "init")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
            run_harness(root, "test-target", "add", "--id", "UNIT", "--kind", "unit", "--command-template", TEST_COMMAND)
            for task_id in ["T1", "T2"]:
                run_harness(root, "task", "add", "--id", task_id, "--task", f"Example {task_id}", "--owner", "developer", "--acceptance", "AC1")
                run_harness(root, "test-target", "link", "--task", task_id, "--target", "UNIT")
            run_id = run_harness(root, "dispatch", "plan", "--scope", "Host Codex").stdout.strip().split()[-1]
            package_root, log_path = fake_sdk_package(temp_path)
            env = self.host_env(package_root, log_path)
            env["FAKE_CODEX_DELAY_SECONDS"] = "2"

            started_at = time.monotonic()
            started = run_harness(root, "dispatch", "provider", "start", "--run-id", run_id, "--provider", "host-codex", "--max-concurrency", "2", env=env)
            duration = time.monotonic() - started_at

            sessions = db_all(root, "select task_id, status, provider_session_id from agent_provider_sessions where run_id = ? order by task_id", (run_id,))
            self.assertLess(duration, 1.5)
            self.assertIn("started 2 provider session", started.stdout)
            self.assertEqual([row["task_id"] for row in sessions], ["T1", "T2"])
            self.assertEqual([row["status"] for row in sessions], ["running", "running"])
            self.assertTrue(all(row["provider_session_id"].startswith("host-codex:") for row in sessions))
            run_harness(root, "dispatch", "provider", "cancel", "--run-id", run_id, "--reason", "test cleanup")

    def test_host_codex_start_records_worker_metadata_and_sdk_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            root = temp_path / "repo"
            root.mkdir()
            git_repo(root)
            run_id = bootstrap(root)
            package_root, log_path = fake_sdk_package(temp_path)

            started = run_harness(root, "dispatch", "provider", "start", "--run-id", run_id, "--provider", "host-codex", env=self.host_env(package_root, log_path))

            self.assertIn("started 1 provider session", started.stdout)
            session = db_one(root, "select * from agent_provider_sessions where run_id = ?", (run_id,))
            metadata = json.loads(session["input_json"])["provider_metadata"]
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and (not log_path.exists() or "thread.run" not in log_path.read_text(encoding="utf-8")):
                time.sleep(0.05)
            events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
            by_method = {event["method"]: event for event in events}
            worktree_path = root / session["worktree_path"]
            worktree_row = db_one(root, "select * from dispatch_worktrees where run_id = ? and task_id = 'T1'", (run_id,))
            self.assertEqual(session["provider"], "host-codex")
            self.assertEqual(session["status"], "running")
            self.assertEqual(session["worktree_path"], worktree_row["worktree_path"])
            self.assertTrue(worktree_path.exists())
            self.assertTrue(session["provider_session_id"].startswith("host-codex:"))
            self.assertTrue(metadata["worker_pid"])
            self.assertEqual(metadata["sdk"], "openai-codex")
            self.assertTrue(metadata["report_path"].endswith("/T1.json"))
            self.assertEqual(by_method["thread_start"]["cwd"], str(worktree_path.resolve()))
            self.assertEqual(by_method["thread_start"]["sandbox"], "workspace_write")
            self.assertEqual(by_method["thread_start"]["approval_mode"], "deny_all")
            self.assertEqual(by_method["thread.run"]["cwd"], str(worktree_path.resolve()))
            self.assertEqual(by_method["thread.run"]["sandbox"], "workspace_write")
            self.assertEqual(by_method["thread.run"]["approval_mode"], "deny_all")
            self.assertIn("branch_name", by_method["thread.run"]["output_schema_required"])
            self.assertIn("fence", by_method["thread.run"]["output_schema_required"])
            run_harness(root, "dispatch", "provider", "cancel", "--run-id", run_id, "--reason", "test cleanup")

    def test_host_codex_model_override_wins_over_spark_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            root = temp_path / "repo"
            root.mkdir()
            git_repo(root)
            run_id = bootstrap(root)
            package_root, log_path = fake_sdk_package(temp_path)
            env = self.host_env(package_root, log_path)
            env["HARNESS_CODEX_MODEL_POLICY"] = "spark-deterministic"
            env["HARNESS_CODEX_MODEL"] = "gpt-custom-main"

            run_harness(root, "dispatch", "provider", "start", "--run-id", run_id, "--provider", "host-codex", env=env)

            events = wait_for_sdk_events(log_path)
            by_method = {event["method"]: event for event in events}
            session = db_one(root, "select * from agent_provider_sessions where run_id = ?", (run_id,))
            metadata = json.loads(session["input_json"])["provider_metadata"]
            self.assertEqual(by_method["thread_start"]["model"], "gpt-custom-main")
            self.assertEqual(by_method["thread.run"]["model"], "gpt-custom-main")
            self.assertEqual(metadata["selected_model"], "gpt-custom-main")
            self.assertEqual(metadata["model_policy"], "spark-deterministic")
            self.assertEqual(metadata["model_selection_reason"], "HARNESS_CODEX_MODEL override")
            self.assertTrue(metadata["spark_eligible"])
            run_harness(root, "dispatch", "provider", "cancel", "--run-id", run_id, "--reason", "test cleanup")

    def test_host_codex_spark_policy_selects_spark_for_eligible_developer_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            root = temp_path / "repo"
            root.mkdir()
            git_repo(root)
            run_id = bootstrap(root)
            package_root, log_path = fake_sdk_package(temp_path)
            env = self.host_env(package_root, log_path)
            env["HARNESS_CODEX_MODEL_POLICY"] = "spark-deterministic"

            run_harness(root, "dispatch", "provider", "start", "--run-id", run_id, "--provider", "host-codex", env=env)

            events = wait_for_sdk_events(log_path)
            by_method = {event["method"]: event for event in events}
            session = db_one(root, "select * from agent_provider_sessions where run_id = ?", (run_id,))
            metadata = json.loads(session["input_json"])["provider_metadata"]
            self.assertEqual(by_method["thread_start"]["model"], "gpt-5.3-codex-spark")
            self.assertEqual(by_method["thread.run"]["model"], "gpt-5.3-codex-spark")
            self.assertEqual(metadata["selected_model"], "gpt-5.3-codex-spark")
            self.assertEqual(metadata["model_policy"], "spark-deterministic")
            self.assertTrue(metadata["spark_eligible"])
            self.assertIn("spark eligible", metadata["model_selection_reason"])
            run_harness(root, "dispatch", "provider", "cancel", "--run-id", run_id, "--reason", "test cleanup")

    def test_host_codex_spark_policy_keeps_default_for_ineligible_tasks(self) -> None:
        cases = [
            ("architect role", {"owner": "architect"}),
            ("qa reviewer role", {"owner": "qa-reviewer"}),
            ("missing target", {"link_target": False}),
            ("non gateable target", {"command_template": "echo hello"}),
            ("sandbox target", {"requires_sandbox": True}),
            ("no network target", {"requires_no_network": True}),
            ("high risk", {"risk": "high"}),
            ("critical risk", {"risk": "critical"}),
        ]
        for _name, options in cases:
            with self.subTest(_name):
                with tempfile.TemporaryDirectory() as temp:
                    temp_path = Path(temp)
                    root = temp_path / "repo"
                    root.mkdir()
                    git_repo(root)
                    run_harness(root, "init")
                    run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
                    target_args = ["test-target", "add", "--id", "UNIT", "--kind", "unit", "--command-template", str(options.get("command_template", TEST_COMMAND))]
                    if options.get("requires_sandbox"):
                        target_args.append("--requires-sandbox")
                    if options.get("requires_no_network"):
                        target_args.append("--requires-no-network")
                    run_harness(root, *target_args)
                    if options.get("risk"):
                        run_harness(root, "failure-mode", "add", "--id", "FM1", "--feature", "Example", "--scenario", "Risk", "--trigger", "change", "--expected", "safe", "--risk", str(options["risk"]), "--acceptance", "AC1")
                    task_args = ["task", "add", "--id", "T1", "--task", "Example", "--owner", str(options.get("owner", "developer")), "--acceptance", "AC1"]
                    if options.get("risk"):
                        task_args.extend(["--failure-mode", "FM1"])
                    run_harness(root, *task_args)
                    if options.get("link_target", True):
                        run_harness(root, "test-target", "link", "--task", "T1", "--target", "UNIT")
                    run_id = run_harness(root, "dispatch", "plan", "--scope", "Host Codex").stdout.strip().split()[-1]
                    package_root, log_path = fake_sdk_package(temp_path)
                    env = self.host_env(package_root, log_path)
                    env["HARNESS_CODEX_MODEL_POLICY"] = "spark-deterministic"

                    run_harness(root, "dispatch", "provider", "start", "--run-id", run_id, "--provider", "host-codex", env=env)

                    events = wait_for_sdk_events(log_path)
                    by_method = {event["method"]: event for event in events}
                    session = db_one(root, "select * from agent_provider_sessions where run_id = ?", (run_id,))
                    metadata = json.loads(session["input_json"])["provider_metadata"]
                    self.assertIsNone(by_method["thread_start"]["model"])
                    self.assertIsNone(by_method["thread.run"]["model"])
                    self.assertEqual(metadata["selected_model"], "")
                    self.assertEqual(metadata["model_policy"], "spark-deterministic")
                    self.assertFalse(metadata["spark_eligible"])
                    self.assertIn("spark ineligible", metadata["model_selection_reason"])
                    run_harness(root, "dispatch", "provider", "cancel", "--run-id", run_id, "--reason", "test cleanup")

    def test_host_codex_worker_commits_in_isolated_worktree_and_controller_verify_creates_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            root = temp_path / "repo"
            root.mkdir()
            git_repo(root)
            main_branch = subprocess.run(["git", "branch", "--show-current"], cwd=root, text=True, capture_output=True, check=True).stdout.strip()
            (root / "main-only.txt").write_text("dirty main worktree\n", encoding="utf-8")
            run_id = bootstrap(root)
            package_root, log_path = fake_sdk_package(temp_path)
            run_harness(root, "dispatch", "provider", "start", "--run-id", run_id, "--provider", "host-codex", env=self.host_env(package_root, log_path))

            collected = wait_for_collect(root, run_id, expected="collected 1 provider report")

            self.assertIn("collected 1 provider report", collected.stdout)
            session = db_one(root, "select branch_name, worktree_path from agent_provider_sessions where run_id = ?", (run_id,))
            branch = session["branch_name"]
            head = subprocess.run(["git", "rev-parse", branch], cwd=root, text=True, capture_output=True, check=True).stdout.strip()
            committed_content = subprocess.run(["git", "show", f"{branch}:agent.txt"], cwd=root, text=True, capture_output=True, check=True).stdout
            current_branch = subprocess.run(["git", "branch", "--show-current"], cwd=root, text=True, capture_output=True, check=True).stdout.strip()
            attempt = db_one(root, "select status, head_commit_sha, provider_session_id from task_attempts where run_id = ?", (run_id,))
            evidence_before = db_one(root, "select count(*) as count from evidence where id like 'CODEX-%'")["count"]
            self.assertEqual(current_branch, main_branch)
            self.assertTrue((root / "main-only.txt").exists())
            self.assertEqual(committed_content, "agent work from fake sdk\n")
            self.assertEqual(attempt["status"], "reported")
            self.assertEqual(attempt["head_commit_sha"], head)
            self.assertTrue(attempt["provider_session_id"].startswith("host-codex:"))
            self.assertEqual(evidence_before, 0)

            verified = run_harness(root, "dispatch", "verify-attempt", "--run-id", run_id, "--task", "T1")

            self.assertIn("OK: dispatch attempt verified", verified.stdout)
            evidence = db_one(root, "select executed_count_source, verified_by from evidence where id like 'CODEX-%'")
            task_status = db_one(root, "select status from tasks where id = 'T1'")["status"]
            self.assertEqual(evidence["executed_count_source"], "parsed")
            self.assertEqual(evidence["verified_by"], "controller-local")
            self.assertEqual(task_status, "submitted")

    def test_host_codex_rejects_manual_executed_count_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            root = temp_path / "repo"
            root.mkdir()
            git_repo(root)
            run_id = bootstrap(root)
            package_root, log_path = fake_sdk_package(temp_path)
            run_harness(root, "dispatch", "provider", "start", "--run-id", run_id, "--provider", "host-codex", env=self.host_env(package_root, log_path, mode="manual"))

            collected = wait_for_collect(root, run_id, expected="collected 0 provider report")

            self.assertIn("collected 0 provider report", collected.stdout)
            session = wait_for_session_status(root, run_id, "verification_failed")
            evidence_count = db_one(root, "select count(*) as count from evidence where id like 'CODEX-%'")["count"]
            finding = db_one(root, "select summary from findings where surface = 'dispatch-integration' order by created_at desc limit 1")
            self.assertEqual(session["status"], "verification_failed")
            self.assertIn("executed_count_source is not parsed", session["last_error"])
            self.assertEqual(evidence_count, 0)
            self.assertIn("executed_count_source is not parsed", finding["summary"])

    def test_host_codex_spawn_failure_does_not_claim_assignment_or_create_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            root = temp_path / "repo"
            root.mkdir()
            git_repo(root)
            run_id = bootstrap(root)
            package_root, log_path = fake_sdk_package(temp_path, import_error=True)

            started = run_harness(
                root,
                "dispatch",
                "provider",
                "start",
                "--run-id",
                run_id,
                "--provider",
                "host-codex",
                env=self.host_env(package_root, log_path, timeout="1"),
            )
            collected = wait_for_collect(root, run_id, expected="collected 0 provider report")

            self.assertIn("started 1 provider session", started.stdout)
            self.assertIn("collected 0 provider report", collected.stdout)
            session = db_one(root, "select status, last_error from agent_provider_sessions where run_id = ?", (run_id,))
            assignment = db_one(root, "select status, provider_session_id from dispatch_assignments where run_id = ?", (run_id,))
            evidence_count = db_one(root, "select count(*) as count from evidence where id like 'CODEX-%'")["count"]
            self.assertEqual(session["status"], "verification_failed")
            self.assertIn("fake sdk missing", session["last_error"])
            self.assertEqual(assignment["status"], "verification_failed")
            self.assertTrue(assignment["provider_session_id"].startswith("host-codex:"))
            self.assertEqual(evidence_count, 0)

    def test_host_codex_invalid_final_json_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            root = temp_path / "repo"
            root.mkdir()
            git_repo(root)
            run_id = bootstrap(root)
            package_root, log_path = fake_sdk_package(temp_path)

            started = run_harness(root, "dispatch", "provider", "start", "--run-id", run_id, "--provider", "host-codex", env=self.host_env(package_root, log_path, mode="invalid-json"))
            collected = wait_for_collect(root, run_id, expected="collected 0 provider report")

            self.assertIn("started 1 provider session", started.stdout)
            self.assertIn("collected 0 provider report", collected.stdout)
            session = db_one(root, "select status, last_error from agent_provider_sessions where run_id = ?", (run_id,))
            branch = db_one(root, "select branch_name from agent_provider_sessions where run_id = ?", (run_id,))["branch_name"]
            extra_commits = subprocess.run(["git", "rev-list", "--count", f"master..{branch}"], cwd=root, text=True, capture_output=True, check=True).stdout.strip()
            evidence_count = db_one(root, "select count(*) as count from evidence where id like 'CODEX-%'")["count"]
            self.assertEqual(session["status"], "verification_failed")
            self.assertIn("missing final JSON object", session["last_error"])
            self.assertEqual(extra_commits, "0")
            self.assertEqual(evidence_count, 0)

    def test_host_codex_branch_mismatch_is_rejected_on_collect(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            root = temp_path / "repo"
            root.mkdir()
            git_repo(root)
            run_id = bootstrap(root)
            package_root, log_path = fake_sdk_package(temp_path)
            run_harness(root, "dispatch", "provider", "start", "--run-id", run_id, "--provider", "host-codex", env=self.host_env(package_root, log_path, mode="branch-mismatch"))

            collected = wait_for_collect(root, run_id, expected="collected 0 provider report")

            self.assertIn("collected 0 provider report", collected.stdout)
            session = wait_for_session_status(root, run_id, "verification_failed")
            self.assertEqual(session["status"], "verification_failed")
            self.assertIn("branch differs from export", session["last_error"])

    def test_host_codex_cancelled_session_late_report_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            root = temp_path / "repo"
            root.mkdir()
            git_repo(root)
            run_id = bootstrap(root)
            package_root, log_path = fake_sdk_package(temp_path)
            run_harness(root, "dispatch", "provider", "start", "--run-id", run_id, "--provider", "host-codex", env=self.host_env(package_root, log_path))
            run_harness(root, "dispatch", "provider", "cancel", "--run-id", run_id, "--task", "T1", "--reason", "operator stop")

            collected = run_harness(root, "dispatch", "provider", "collect", "--run-id", run_id)

            reports = db_one(root, "select count(*) as count from agent_reports where run_id = ?", (run_id,))["count"]
            attempts = db_one(root, "select count(*) as count from task_attempts where run_id = ?", (run_id,))["count"]
            self.assertIn("collected 0 provider report", collected.stdout)
            self.assertEqual(reports, 0)
            self.assertEqual(attempts, 0)

    def test_host_codex_cancel_terminates_running_worker_and_cleans_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            root = temp_path / "repo"
            root.mkdir()
            git_repo(root)
            run_id = bootstrap(root)
            package_root, log_path = fake_sdk_package(temp_path)
            env = self.host_env(package_root, log_path)
            env["FAKE_CODEX_DELAY_SECONDS"] = "5"
            run_harness(root, "dispatch", "provider", "start", "--run-id", run_id, "--provider", "host-codex", env=env)
            session_before = db_one(root, "select worktree_path from agent_provider_sessions where run_id = ?", (run_id,))
            worktree_path = root / session_before["worktree_path"]
            self.assertTrue(worktree_path.exists())

            cancelled = run_harness(root, "dispatch", "provider", "cancel", "--run-id", run_id, "--task", "T1", "--reason", "operator stop")

            collected = run_harness(root, "dispatch", "provider", "collect", "--run-id", run_id)
            assignment = db_one(root, "select status, provider_session_id from dispatch_assignments where run_id = ?", (run_id,))
            worktree = db_one(root, "select status, cleaned_at from dispatch_worktrees where run_id = ?", (run_id,))
            reports = db_one(root, "select count(*) as count from agent_reports where run_id = ?", (run_id,))["count"]
            attempts = db_one(root, "select count(*) as count from task_attempts where run_id = ?", (run_id,))["count"]
            self.assertIn("cancelled 1 provider session", cancelled.stdout)
            self.assertIn("collected 0 provider report", collected.stdout)
            self.assertEqual(assignment["status"], "planned")
            self.assertEqual(assignment["provider_session_id"], "")
            self.assertFalse(worktree_path.exists())
            self.assertEqual(worktree["status"], "cleaned")
            self.assertTrue(worktree["cleaned_at"])
            self.assertEqual(reports, 0)
            self.assertEqual(attempts, 0)


if __name__ == "__main__":
    unittest.main()

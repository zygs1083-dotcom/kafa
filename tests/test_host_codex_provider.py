from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "plugins/codex-project-harness/scripts/harness.py"
AGENT_PROVIDER = REPO_ROOT / "plugins/codex-project-harness/core/agent_provider.py"
TEST_COMMAND = "python3 -m unittest"
PLUGIN_ROOT = REPO_ROOT / "plugins/codex-project-harness"
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))
SCRIPTS_ROOT = PLUGIN_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))
from core import agent_provider as agent_provider_core
import harness_db as harness_db_core


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


def pid_is_alive(pid: int) -> bool:
    return agent_provider_core._process_alive(pid)


def wait_for_pid_exit(pid: int, *, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_is_alive(pid):
            return True
        time.sleep(0.05)
    return not pid_is_alive(pid)


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
            import subprocess
            import sys
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
                    child_pid_file = os.environ.get("FAKE_CODEX_CHILD_PID_FILE", "")
                    if child_pid_file:
                        late_child_file = os.environ.get("FAKE_CODEX_CHILD_SPAWN_ON_TERM_FILE", "")
                        if late_child_file:
                            child_code = (
                                "import os,signal,subprocess,sys,time; "
                                "handler=lambda *_: (lambda p: open(sys.argv[1],'w').write(str(p.pid)))("
                                "subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'], start_new_session=True)); "
                                "signal.signal(signal.SIGTERM, handler); time.sleep(60)"
                            )
                            child = subprocess.Popen([sys.executable, "-c", child_code, late_child_file], start_new_session=True)
                        else:
                            child = subprocess.Popen(
                                [sys.executable, "-c", "import time; time.sleep(60)"],
                                start_new_session=os.environ.get("FAKE_CODEX_CHILD_DETACHED", "") == "1",
                            )
                        Path(child_pid_file).write_text(str(child.pid), encoding="utf-8")
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
            "HARNESS_CODEX_LEGACY_HOST_POLICY": "isolated-deny-all",
            "HARNESS_CODEX_TURN_TIMEOUT_SECONDS": timeout,
            "FAKE_CODEX_SDK_LOG": str(log_path),
            "FAKE_CODEX_MODE": mode,
            "PYTHONPATH": str(package_root),
        }

    def test_host_codex_requires_explicit_legacy_permission_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            root = temp_path / "repo"
            root.mkdir()
            git_repo(root)
            run_id = bootstrap(root)
            package_root, log_path = fake_sdk_package(temp_path)
            env = self.host_env(package_root, log_path)
            env["HARNESS_CODEX_LEGACY_HOST_POLICY"] = ""

            started = run_harness(root, "dispatch", "provider", "start", "--run-id", run_id, "--provider", "host-codex", env=env)

            session = db_one(root, "select status, last_error from agent_provider_sessions where run_id = ?", (run_id,))
            evidence_count = db_one(root, "select count(*) as count from evidence")["count"]
            self.assertIn("started 0 provider session", started.stdout)
            self.assertEqual(session["status"], "spawn_failed")
            self.assertIn("requires explicit HARNESS_CODEX_LEGACY_HOST_POLICY=isolated-deny-all", session["last_error"])
            self.assertEqual(evidence_count, 0)
            self.assertFalse(log_path.exists())

    def test_host_codex_worker_rejects_tampered_job_without_legacy_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            root = temp_path / "repo"
            root.mkdir()
            worktree = root / "worktree"
            worktree.mkdir()
            package_root, log_path = fake_sdk_package(temp_path)
            report_path = root / "report.json"
            job_path = root / "job.json"
            job_path.write_text(
                json.dumps(
                    {
                        "request": {
                            "root": str(root),
                            "run_id": "RUN1",
                            "task_id": "T1",
                            "agent_id": "developer",
                            "branch_name": "agent/T1",
                            "fence": 1,
                            "target_id": "UNIT",
                            "command_template": TEST_COMMAND,
                            "instruction": "Do not run",
                            "input_json": {},
                            "worktree_path": "worktree",
                        },
                        "legacy_host_policy": "",
                        "report_path": str(report_path),
                        "timeout": 1,
                    }
                ),
                encoding="utf-8",
            )
            env = self.host_env(package_root, log_path)

            result = subprocess.run(
                ["python3", str(AGENT_PROVIDER), "host-codex-worker", str(job_path)],
                text=True,
                capture_output=True,
                check=False,
                env={**os.environ, **env},
            )

            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(result.returncode, 1)
            self.assertEqual(report["status"], "failed")
            self.assertIn("requires matching job and environment", report["last_error"])
            self.assertFalse(log_path.exists())

            job = json.loads(job_path.read_text(encoding="utf-8"))
            job["legacy_host_policy"] = "isolated-deny-all"
            job_path.write_text(json.dumps(job), encoding="utf-8")
            report_path.unlink()
            env["HARNESS_CODEX_LEGACY_HOST_POLICY"] = ""
            environment_bypass = subprocess.run(
                ["python3", str(AGENT_PROVIDER), "host-codex-worker", str(job_path)],
                text=True,
                capture_output=True,
                check=False,
                env={**os.environ, **env},
            )
            environment_report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(environment_bypass.returncode, 1)
            self.assertEqual(environment_report["status"], "failed")
            self.assertIn("requires matching job and environment", environment_report["last_error"])
            self.assertFalse(log_path.exists())

            direct_call = subprocess.run(
                [
                    "python3",
                    "-c",
                    (
                        "import sys; from pathlib import Path; "
                        "from core.agent_provider import AgentJobRequest, HostCodexProvider; "
                        "root=Path(sys.argv[1]); "
                        "request=AgentJobRequest(root=root, run_id='RUN1', task_id='T1', agent_id='developer', "
                        "branch_name='agent/T1', fence=1, target_id='UNIT', command_template='test', "
                        "instruction='Do not run', input_json={}, worktree_path='worktree'); "
                        "HostCodexProvider()._run_sdk_turn(request, codex_bin='', model='')"
                    ),
                    str(root),
                ],
                text=True,
                capture_output=True,
                check=False,
                env={
                    **os.environ,
                    **env,
                    "PYTHONPATH": os.pathsep.join([str(REPO_ROOT / "plugins/codex-project-harness"), str(package_root)]),
                },
            )
            self.assertNotEqual(direct_call.returncode, 0)
            self.assertIn("SDK execution requires explicit", direct_call.stderr)
            self.assertFalse(log_path.exists())

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
            self.assertTrue(metadata["watchdog_pid"])
            self.assertTrue(metadata["deadline_epoch"])
            self.assertEqual(Path(metadata["report_path_absolute"]).name, "T1.json")
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
            env["HARNESS_CODEX_SPARK_MODEL"] = "gpt-5.3-codex-spark"

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

    def test_host_codex_spark_policy_requires_explicit_legacy_model_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            root = temp_path / "repo"
            root.mkdir()
            git_repo(root)
            run_id = bootstrap(root)
            package_root, log_path = fake_sdk_package(temp_path)
            env = self.host_env(package_root, log_path)
            env["HARNESS_CODEX_MODEL_POLICY"] = "spark-deterministic"
            env["HARNESS_CODEX_SPARK_MODEL"] = ""

            started = run_harness(
                root,
                "dispatch",
                "provider",
                "start",
                "--run-id",
                run_id,
                "--provider",
                "host-codex",
                env=env,
            )

            session = db_one(root, "select status, last_error from agent_provider_sessions where run_id = ?", (run_id,))
            assignment = db_one(root, "select status from dispatch_assignments where run_id = ?", (run_id,))
            evidence_count = db_one(root, "select count(*) as count from evidence where id like 'CODEX-%'")["count"]
            self.assertIn("started 0 provider session", started.stdout)
            self.assertEqual(session["status"], "spawn_failed")
            self.assertIn("requires explicit HARNESS_CODEX_SPARK_MODEL", session["last_error"])
            self.assertEqual(assignment["status"], "planned")
            self.assertEqual(evidence_count, 0)
            self.assertFalse(log_path.exists())

    def test_host_codex_spark_policy_does_not_hide_missing_model_on_ineligible_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            root = temp_path / "repo"
            root.mkdir()
            git_repo(root)
            run_harness(root, "init")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Architecture")
            run_harness(root, "test-target", "add", "--id", "UNIT", "--kind", "unit", "--command-template", TEST_COMMAND)
            run_harness(root, "task", "add", "--id", "T1", "--task", "Architecture", "--owner", "architect", "--acceptance", "AC1")
            run_harness(root, "test-target", "link", "--task", "T1", "--target", "UNIT")
            run_id = run_harness(root, "dispatch", "plan", "--scope", "Host Codex").stdout.strip().split()[-1]
            package_root, log_path = fake_sdk_package(temp_path)
            env = self.host_env(package_root, log_path)
            env["HARNESS_CODEX_MODEL_POLICY"] = "spark-deterministic"
            env["HARNESS_CODEX_SPARK_MODEL"] = ""

            run_harness(root, "dispatch", "provider", "start", "--run-id", run_id, "--provider", "host-codex", env=env)

            session = db_one(root, "select status, last_error from agent_provider_sessions where run_id = ?", (run_id,))
            self.assertEqual(session["status"], "spawn_failed")
            self.assertIn("requires explicit HARNESS_CODEX_SPARK_MODEL", session["last_error"])
            self.assertFalse(log_path.exists())

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
                    env["HARNESS_CODEX_SPARK_MODEL"] = "gpt-5.3-codex-spark"

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
            self.assertIn("install kafa[host-codex]", session["last_error"])
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

    def test_host_codex_cancel_terminates_known_tree_but_keeps_assignment_failed_closed(self) -> None:
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
            session = db_one(root, "select status, last_error from agent_provider_sessions where run_id = ?", (run_id,))
            assignment = db_one(root, "select status, provider_session_id from dispatch_assignments where run_id = ?", (run_id,))
            worktree = db_one(root, "select status, cleaned_at from dispatch_worktrees where run_id = ?", (run_id,))
            reports = db_one(root, "select count(*) as count from agent_reports where run_id = ?", (run_id,))["count"]
            attempts = db_one(root, "select count(*) as count from task_attempts where run_id = ?", (run_id,))["count"]
            self.assertIn("cancelled 0 provider session", cancelled.stdout)
            self.assertIn("collected 0 provider report", collected.stdout)
            self.assertEqual(session["status"], "verification_failed")
            self.assertIn("cannot be independently confirmed", session["last_error"])
            self.assertEqual(assignment["status"], "verification_failed")
            self.assertTrue(assignment["provider_session_id"])
            self.assertTrue(worktree_path.exists())
            self.assertEqual(worktree["status"], "active")
            self.assertFalse(worktree["cleaned_at"])
            self.assertEqual(reports, 0)
            self.assertEqual(attempts, 0)

    def test_host_codex_watchdog_times_out_without_collect_polling(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            root = temp_path / "repo"
            root.mkdir()
            git_repo(root)
            run_id = bootstrap(root)
            package_root, log_path = fake_sdk_package(temp_path)
            env = self.host_env(package_root, log_path, timeout="0.2")
            env["FAKE_CODEX_DELAY_SECONDS"] = "5"
            run_harness(root, "dispatch", "provider", "start", "--run-id", run_id, "--provider", "host-codex", env=env)
            session = db_one(root, "select input_json from agent_provider_sessions where run_id = ?", (run_id,))
            worker_pid = int(json.loads(session["input_json"])["provider_metadata"]["worker_pid"])
            report_path = root / ".ai-team/runtime/host-codex" / run_id / "T1.json"

            deadline = time.monotonic() + 3
            report = json.loads(report_path.read_text(encoding="utf-8"))
            while time.monotonic() < deadline and report["status"] == "running":
                time.sleep(0.05)
                report = json.loads(report_path.read_text(encoding="utf-8"))

            self.assertEqual(report["status"], "failed")
            self.assertIn("turn timeout", report["last_error"])
            self.assertTrue(wait_for_pid_exit(worker_pid))
            collected = run_harness(root, "dispatch", "provider", "collect", "--run-id", run_id)
            session_after = db_one(root, "select status, last_error from agent_provider_sessions where run_id = ?", (run_id,))
            evidence_count = db_one(root, "select count(*) as count from evidence")["count"]
            self.assertIn("collected 0 provider report", collected.stdout)
            self.assertEqual(session_after["status"], "verification_failed")
            self.assertEqual(evidence_count, 0)

    def test_host_codex_collect_detects_worker_exit_without_terminal_report(self) -> None:
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
            session = db_one(root, "select input_json from agent_provider_sessions where run_id = ?", (run_id,))
            worker_pid = int(json.loads(session["input_json"])["provider_metadata"]["worker_pid"])
            os.kill(worker_pid, signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM)
            self.assertTrue(wait_for_pid_exit(worker_pid))

            collected = run_harness(root, "dispatch", "provider", "collect", "--run-id", run_id)

            session_after = db_one(root, "select status, last_error from agent_provider_sessions where run_id = ?", (run_id,))
            evidence_count = db_one(root, "select count(*) as count from evidence")["count"]
            self.assertIn("collected 0 provider report", collected.stdout)
            self.assertEqual(session_after["status"], "verification_failed")
            self.assertIn("worker exited without terminal report", session_after["last_error"])
            self.assertEqual(evidence_count, 0)

    def test_host_codex_cancel_terminates_sdk_descendant_process(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            root = temp_path / "repo"
            root.mkdir()
            git_repo(root)
            run_id = bootstrap(root)
            package_root, log_path = fake_sdk_package(temp_path)
            child_pid_file = temp_path / "child.pid"
            late_child_pid_file = temp_path / "late-child.pid"
            env = self.host_env(package_root, log_path)
            env["FAKE_CODEX_DELAY_SECONDS"] = "5"
            env["FAKE_CODEX_CHILD_PID_FILE"] = str(child_pid_file)
            env["FAKE_CODEX_CHILD_DETACHED"] = "1"
            env["FAKE_CODEX_CHILD_SPAWN_ON_TERM_FILE"] = str(late_child_pid_file)
            run_harness(root, "dispatch", "provider", "start", "--run-id", run_id, "--provider", "host-codex", env=env)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not child_pid_file.exists():
                time.sleep(0.05)
            self.assertTrue(child_pid_file.exists())
            child_pid = int(child_pid_file.read_text(encoding="utf-8"))
            self.addCleanup(lambda: os.kill(child_pid, signal.SIGKILL) if pid_is_alive(child_pid) else None)

            cancelled = run_harness(root, "dispatch", "provider", "cancel", "--run-id", run_id, "--task", "T1", "--reason", "operator stop")

            self.assertIn("cancelled 0 provider session", cancelled.stdout)
            self.assertTrue(wait_for_pid_exit(child_pid))
            self.assertFalse(late_child_pid_file.exists())
            session = db_one(root, "select status from agent_provider_sessions where run_id = ?", (run_id,))
            self.assertEqual(session["status"], "verification_failed")

    def test_host_codex_late_worker_cannot_overwrite_cancelled_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            root = temp_path / "repo"
            root.mkdir()
            git_repo(root)
            run_id = bootstrap(root)
            package_root, log_path = fake_sdk_package(temp_path)
            env = self.host_env(package_root, log_path)
            env["FAKE_CODEX_DELAY_SECONDS"] = "0.3"
            run_harness(root, "dispatch", "provider", "start", "--run-id", run_id, "--provider", "host-codex", env=env)

            run_harness(root, "dispatch", "provider", "cancel", "--run-id", run_id, "--task", "T1", "--reason", "operator stop")
            time.sleep(0.5)

            report_path = root / ".ai-team/runtime/host-codex" / run_id / "T1.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            evidence_count = db_one(root, "select count(*) as count from evidence")["count"]
            self.assertEqual(report["status"], "failed")
            self.assertEqual(evidence_count, 0)

    def test_host_codex_watchdog_records_unconfirmed_termination_as_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            report_path = Path(temp) / "report.json"
            report_path.write_text(
                json.dumps({"status": "running", "last_error": "", "result_json": "", "metadata": {}}),
                encoding="utf-8",
            )

            with patch.object(agent_provider_core, "_process_alive", return_value=True), patch.object(
                agent_provider_core,
                "_terminate_process_tree",
                return_value=False,
            ):
                result = agent_provider_core._host_codex_watchdog(report_path, 12345, 0)

            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(result, 1)
            self.assertEqual(report["status"], "failed")
            self.assertIn("termination unconfirmed", report["last_error"])

    def test_host_codex_cancel_records_unconfirmed_termination_as_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            report_path = Path(temp) / "report.json"
            metadata = {
                "worker_pid": 12345,
                "worker_pgid": 12345,
                "watchdog_pid": 12346,
                "report_path_absolute": str(report_path),
            }
            report_path.write_text(
                json.dumps({"status": "running", "last_error": "", "result_json": "", "metadata": metadata}),
                encoding="utf-8",
            )
            handle = agent_provider_core.AgentJobHandle("host-codex", "session", "job", "running", json.dumps(metadata))

            with patch.object(agent_provider_core, "_terminate_process_tree", return_value=False):
                result = agent_provider_core.HostCodexProvider().cancel(handle, "operator stop")

            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(result.status, "cancel_failed")
            self.assertEqual(report["status"], "failed")
            self.assertIn("termination not confirmed", report["last_error"])

    def test_host_codex_collect_enforces_deadline_when_watchdog_dies(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            root = temp_path / "repo"
            root.mkdir()
            git_repo(root)
            run_id = bootstrap(root)
            package_root, log_path = fake_sdk_package(temp_path)
            env = self.host_env(package_root, log_path, timeout="0.2")
            env["FAKE_CODEX_DELAY_SECONDS"] = "5"
            run_harness(root, "dispatch", "provider", "start", "--run-id", run_id, "--provider", "host-codex", env=env)
            session = db_one(root, "select input_json from agent_provider_sessions where run_id = ?", (run_id,))
            metadata = json.loads(session["input_json"])["provider_metadata"]
            watchdog_pid = int(metadata["watchdog_pid"])
            worker_pid = int(metadata["worker_pid"])
            os.kill(watchdog_pid, signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM)
            self.assertTrue(wait_for_pid_exit(watchdog_pid))
            time.sleep(0.25)

            run_harness(root, "dispatch", "provider", "collect", "--run-id", run_id)

            session_after = db_one(root, "select status from agent_provider_sessions where run_id = ?", (run_id,))
            self.assertEqual(session_after["status"], "verification_failed")
            self.assertTrue(wait_for_pid_exit(worker_pid))

    def test_host_codex_collect_cannot_renew_session_cancelled_during_poll(self) -> None:
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

            def cancel_then_return_none(*_args, **_kwargs):
                harness_db_core.dispatch_provider_cancel(root, run_id, task_id="T1", reason="race cancel")
                return None

            with patch.object(agent_provider_core.HostCodexProvider, "collect", side_effect=cancel_then_return_none):
                collected = harness_db_core.dispatch_provider_collect(root, run_id)

            session = db_one(root, "select status from agent_provider_sessions where run_id = ?", (run_id,))
            assignment = db_one(root, "select status, provider_session_id from dispatch_assignments where run_id = ?", (run_id,))
            self.assertEqual(collected, 0)
            self.assertEqual(session["status"], "verification_failed")
            self.assertEqual(assignment["status"], "verification_failed")
            self.assertTrue(assignment["provider_session_id"])

    def test_host_codex_verify_cannot_commit_evidence_after_concurrent_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            root = temp_path / "repo"
            root.mkdir()
            git_repo(root)
            sentinel = temp_path / "verify.started"
            (root / "verify_gate.py").write_text(
                "import time, unittest\n"
                f"open({str(sentinel)!r}, 'w').write('started')\n"
                "time.sleep(0.5)\n"
                "class GateTest(unittest.TestCase):\n"
                "    def test_ok(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "verify_gate.py"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "add verification gate"], cwd=root, text=True, capture_output=True, check=True)
            run_harness(root, "init")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
            run_harness(root, "test-target", "add", "--id", "UNIT", "--kind", "unit", "--command-template", "python3 -m unittest verify_gate.py")
            run_harness(root, "task", "add", "--id", "T1", "--task", "Example", "--owner", "developer", "--acceptance", "AC1")
            run_harness(root, "test-target", "link", "--task", "T1", "--target", "UNIT")
            run_id = run_harness(root, "dispatch", "plan", "--scope", "Host Codex").stdout.strip().split()[-1]
            package_root, log_path = fake_sdk_package(temp_path)
            run_harness(root, "dispatch", "provider", "start", "--run-id", run_id, "--provider", "host-codex", env=self.host_env(package_root, log_path))
            collected = wait_for_collect(root, run_id, expected="collected 1 provider report")
            if "collected 1 provider report" not in collected.stdout:
                failed_session = db_one(root, "select status, last_error from agent_provider_sessions where run_id = ?", (run_id,))
                self.fail(f"provider report was not collected: {dict(failed_session)}")
            verify = subprocess.Popen(
                ["python3", str(HARNESS), "--root", str(root), "dispatch", "verify-attempt", "--run-id", run_id, "--task", "T1"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=os.environ.copy(),
            )
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not sentinel.exists():
                time.sleep(0.05)
            if not sentinel.exists():
                stdout, stderr = verify.communicate(timeout=10)
                self.fail(f"verification command did not start target:\n{stdout}\n{stderr}")
            run_harness(root, "dispatch", "provider", "cancel", "--run-id", run_id, "--task", "T1", "--reason", "race cancel")
            stdout, stderr = verify.communicate(timeout=10)

            session = db_one(root, "select status from agent_provider_sessions where run_id = ?", (run_id,))
            evidence_count = db_one(root, "select count(*) as count from evidence")["count"]
            self.assertNotEqual(verify.returncode, 0, stdout + stderr)
            self.assertEqual(session["status"], "verification_failed")
            self.assertEqual(evidence_count, 0)
            self.assertIn("provider-session-stale", stdout + stderr)

    def test_host_codex_reconcile_requires_confirmed_termination_and_refreshes_run(self) -> None:
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
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute("update agent_provider_sessions set lease_expires_at = '2000-01-01T00:00:00+00:00' where run_id = ?", (run_id,))
                conn.commit()

            reconciled = harness_db_core.dispatch_provider_reconcile(root, run_id)

            session = db_one(root, "select status from agent_provider_sessions where run_id = ?", (run_id,))
            assignment = db_one(root, "select status from dispatch_assignments where run_id = ?", (run_id,))
            dispatch_run = db_one(root, "select status from dispatch_runs where id = ?", (run_id,))
            self.assertEqual(reconciled, 1)
            self.assertEqual(session["status"], "verification_failed")
            self.assertEqual(assignment["status"], "verification_failed")
            self.assertEqual(dispatch_run["status"], "verification_failed")

            root = temp_path / "repo-unconfirmed"
            root.mkdir()
            git_repo(root)
            run_id = bootstrap(root)
            second_sdk = temp_path / "second-sdk"
            package_root, log_path = fake_sdk_package(second_sdk)
            env = self.host_env(package_root, log_path)
            env["FAKE_CODEX_DELAY_SECONDS"] = "5"
            run_harness(root, "dispatch", "provider", "start", "--run-id", run_id, "--provider", "host-codex", env=env)
            session = db_one(root, "select input_json from agent_provider_sessions where run_id = ?", (run_id,))
            metadata = json.loads(session["input_json"])["provider_metadata"]
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute("update agent_provider_sessions set lease_expires_at = '2000-01-01T00:00:00+00:00' where run_id = ?", (run_id,))
                conn.commit()
            failed_cancel = agent_provider_core.AgentJobHandle("host-codex", "session", "job", "cancel_failed", "not confirmed")
            with patch.object(agent_provider_core.HostCodexProvider, "cancel", return_value=failed_cancel):
                harness_db_core.dispatch_provider_reconcile(root, run_id)
            agent_provider_core._terminate_process_tree(int(metadata["worker_pid"]), expected_pgid=int(metadata["worker_pgid"]))
            agent_provider_core._terminate_process_tree(int(metadata["watchdog_pid"]), expected_pgid=int(metadata["watchdog_pid"]))

            failed_session = db_one(root, "select status from agent_provider_sessions where run_id = ?", (run_id,))
            failed_assignment = db_one(root, "select status from dispatch_assignments where run_id = ?", (run_id,))
            failed_run = db_one(root, "select status from dispatch_runs where id = ?", (run_id,))
            self.assertEqual(failed_session["status"], "verification_failed")
            self.assertEqual(failed_assignment["status"], "verification_failed")
            self.assertEqual(failed_run["status"], "verification_failed")


if __name__ == "__main__":
    unittest.main()

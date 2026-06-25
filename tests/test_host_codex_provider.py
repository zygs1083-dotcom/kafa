from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import textwrap
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


def create_branch(root: Path, branch_name: str) -> str:
    subprocess.run(["git", "switch", "-c", branch_name], cwd=root, check=True, capture_output=True)
    (root / "agent.txt").write_text("agent work\n", encoding="utf-8")
    subprocess.run(["git", "add", "agent.txt"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "agent work"], cwd=root, check=True, capture_output=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=True).stdout.strip()
    subprocess.run(["git", "switch", "master"], cwd=root, check=True, capture_output=True)
    return head


def db_one(root: Path, query: str, params: tuple[object, ...] = ()) -> sqlite3.Row:
    with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(query, params).fetchone()


def db_all(root: Path, query: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
    with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(query, params).fetchall()


def fake_app_server(temp: Path) -> tuple[Path, Path]:
    script = temp / "fake_codex_app_server.py"
    log_path = temp / "fake_codex_log.jsonl"
    script.write_text(
        textwrap.dedent(
            r'''
            import json
            import os
            import re
            import sys
            import time

            mode = os.environ.get("FAKE_CODEX_MODE", "success")
            log_path = os.environ["FAKE_CODEX_LOG"]

            def log(message):
                with open(log_path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(message, sort_keys=True) + "\n")

            def send(message):
                sys.stdout.write(json.dumps(message, sort_keys=True) + "\n")
                sys.stdout.flush()

            def prompt_value(prompt, name, default=""):
                match = re.search(rf'"{name}": "([^"]*)"', prompt)
                return match.group(1) if match else default

            def report_from_prompt(prompt):
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
                }
                if mode == "manual":
                    expected["executed_count_source"] = "manual"
                if mode == "branch-mismatch":
                    expected["branch_name"] = "agent/wrong/branch"
                return expected

            if mode == "sleep":
                time.sleep(10)

            for line in sys.stdin:
                message = json.loads(line)
                log(message)
                method = message.get("method")
                if method == "initialize":
                    send({"id": message["id"], "result": {"serverInfo": {"name": "fake-codex"}}})
                elif method == "initialized":
                    continue
                elif method == "thread/start":
                    if mode == "rpc-error":
                        send({"id": message["id"], "error": {"code": 123, "message": "fake rpc boom"}})
                    else:
                        send({"id": message["id"], "result": {"thread": {"id": "thr_fake"}}})
                elif method == "turn/start":
                    prompt = message["params"]["input"][0]["text"]
                    send({"id": message["id"], "result": {"turn": {"id": "turn_fake"}}})
                    send({"method": "turn/started", "params": {"turn": {"id": "turn_fake"}}})
                    if mode == "invalid-json":
                        text = "not a json result"
                    else:
                        text = json.dumps(report_from_prompt(prompt), sort_keys=True)
                    send({"method": "item/agentMessage/delta", "params": {"delta": text}})
                    send({"method": "turn/completed", "params": {"turn": {"id": "turn_fake", "status": "completed"}}})
            '''
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    return script, log_path


class HostCodexProviderTest(unittest.TestCase):
    def host_env(self, script: Path, log_path: Path, *, mode: str = "success", timeout: str = "5") -> dict[str, str]:
        return {
            "HARNESS_CODEX_APP_SERVER_CMD": f"python3 {script}",
            "HARNESS_CODEX_TURN_TIMEOUT_SECONDS": timeout,
            "FAKE_CODEX_LOG": str(log_path),
            "FAKE_CODEX_MODE": mode,
        }

    def test_host_codex_start_records_thread_turn_metadata_and_rpc_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            root = temp_path / "repo"
            root.mkdir()
            git_repo(root)
            run_id = bootstrap(root)
            script, log_path = fake_app_server(temp_path)

            started = run_harness(root, "dispatch", "provider", "start", "--run-id", run_id, "--provider", "host-codex", env=self.host_env(script, log_path))

            self.assertIn("started 1 provider session", started.stdout)
            session = db_one(root, "select * from agent_provider_sessions where run_id = ?", (run_id,))
            metadata = json.loads(session["input_json"])["provider_metadata"]
            events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(session["provider"], "host-codex")
            self.assertEqual(session["status"], "running")
            self.assertEqual(session["provider_session_id"], "host-codex:thr_fake")
            self.assertEqual(session["provider_job_id"], "turn_fake")
            self.assertEqual(metadata["thread_id"], "thr_fake")
            self.assertEqual(metadata["turn_id"], "turn_fake")
            self.assertEqual([event["method"] for event in events[:4]], ["initialize", "initialized", "thread/start", "turn/start"])
            self.assertEqual(events[0]["params"]["clientInfo"]["name"], "codex_project_harness")
            self.assertIn("Expected report shape", events[3]["params"]["input"][0]["text"])

    def test_host_codex_collects_raw_report_and_controller_verify_creates_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            root = temp_path / "repo"
            root.mkdir()
            git_repo(root)
            run_id = bootstrap(root)
            script, log_path = fake_app_server(temp_path)
            run_harness(root, "dispatch", "provider", "start", "--run-id", run_id, "--provider", "host-codex", env=self.host_env(script, log_path))
            branch = db_one(root, "select branch_name from agent_provider_sessions where run_id = ?", (run_id,))["branch_name"]
            head = create_branch(root, branch)

            collected = run_harness(root, "dispatch", "provider", "collect", "--run-id", run_id)

            self.assertIn("collected 1 provider report", collected.stdout)
            attempt = db_one(root, "select status, head_commit_sha, provider_session_id from task_attempts where run_id = ?", (run_id,))
            evidence_before = db_one(root, "select count(*) as count from evidence where id like 'CODEX-%'")["count"]
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
            script, log_path = fake_app_server(temp_path)
            run_harness(root, "dispatch", "provider", "start", "--run-id", run_id, "--provider", "host-codex", env=self.host_env(script, log_path, mode="manual"))
            branch = db_one(root, "select branch_name from agent_provider_sessions where run_id = ?", (run_id,))["branch_name"]
            create_branch(root, branch)

            collected = run_harness(root, "dispatch", "provider", "collect", "--run-id", run_id)

            self.assertIn("collected 0 provider report", collected.stdout)
            session = db_one(root, "select status, last_error from agent_provider_sessions where run_id = ?", (run_id,))
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
            missing = temp_path / "missing_app_server.py"

            started = run_harness(
                root,
                "dispatch",
                "provider",
                "start",
                "--run-id",
                run_id,
                "--provider",
                "host-codex",
                env={"HARNESS_CODEX_APP_SERVER_CMD": f"python3 {missing}", "HARNESS_CODEX_TURN_TIMEOUT_SECONDS": "1"},
            )

            self.assertIn("started 0 provider session", started.stdout)
            session = db_one(root, "select status, last_error from agent_provider_sessions where run_id = ?", (run_id,))
            assignment = db_one(root, "select status, provider_session_id from dispatch_assignments where run_id = ?", (run_id,))
            evidence_count = db_one(root, "select count(*) as count from evidence where id like 'CODEX-%'")["count"]
            self.assertEqual(session["status"], "spawn_failed")
            self.assertIn("No such file", session["last_error"])
            self.assertEqual(assignment["status"], "planned")
            self.assertEqual(assignment["provider_session_id"], "")
            self.assertEqual(evidence_count, 0)

    def test_host_codex_invalid_final_json_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            root = temp_path / "repo"
            root.mkdir()
            git_repo(root)
            run_id = bootstrap(root)
            script, log_path = fake_app_server(temp_path)

            started = run_harness(root, "dispatch", "provider", "start", "--run-id", run_id, "--provider", "host-codex", env=self.host_env(script, log_path, mode="invalid-json"))

            self.assertIn("started 0 provider session", started.stdout)
            session = db_one(root, "select status, last_error from agent_provider_sessions where run_id = ?", (run_id,))
            evidence_count = db_one(root, "select count(*) as count from evidence where id like 'CODEX-%'")["count"]
            self.assertEqual(session["status"], "spawn_failed")
            self.assertIn("missing final JSON object", session["last_error"])
            self.assertEqual(evidence_count, 0)

    def test_host_codex_branch_mismatch_is_rejected_on_collect(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            root = temp_path / "repo"
            root.mkdir()
            git_repo(root)
            run_id = bootstrap(root)
            script, log_path = fake_app_server(temp_path)
            run_harness(root, "dispatch", "provider", "start", "--run-id", run_id, "--provider", "host-codex", env=self.host_env(script, log_path, mode="branch-mismatch"))
            branch = db_one(root, "select branch_name from agent_provider_sessions where run_id = ?", (run_id,))["branch_name"]
            create_branch(root, branch)

            collected = run_harness(root, "dispatch", "provider", "collect", "--run-id", run_id)

            self.assertIn("collected 0 provider report", collected.stdout)
            session = db_one(root, "select status, last_error from agent_provider_sessions where run_id = ?", (run_id,))
            self.assertEqual(session["status"], "verification_failed")
            self.assertIn("branch differs from export", session["last_error"])

    def test_host_codex_cancelled_session_late_report_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            root = temp_path / "repo"
            root.mkdir()
            git_repo(root)
            run_id = bootstrap(root)
            script, log_path = fake_app_server(temp_path)
            run_harness(root, "dispatch", "provider", "start", "--run-id", run_id, "--provider", "host-codex", env=self.host_env(script, log_path))
            run_harness(root, "dispatch", "provider", "cancel", "--run-id", run_id, "--task", "T1", "--reason", "operator stop")

            collected = run_harness(root, "dispatch", "provider", "collect", "--run-id", run_id)

            reports = db_one(root, "select count(*) as count from agent_reports where run_id = ?", (run_id,))["count"]
            attempts = db_one(root, "select count(*) as count from task_attempts where run_id = ?", (run_id,))["count"]
            self.assertIn("collected 0 provider report", collected.stdout)
            self.assertEqual(reports, 0)
            self.assertEqual(attempts, 0)


if __name__ == "__main__":
    unittest.main()

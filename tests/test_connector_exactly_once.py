from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "plugins/codex-project-harness/scripts/harness.py"


def run_harness(root: Path, *args: str, env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    command_env = os.environ.copy()
    command_env["HARNESS_CONNECTOR_RETRY_SLEEP"] = "0"
    if env:
        command_env.update(env)
    result = subprocess.run(["python3", str(HARNESS), "--root", str(root), *args], text=True, capture_output=True, check=False, env=command_env)
    if check and result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return result


def action_id(stdout: str) -> str:
    return stdout.strip().split()[-1]


def db_one(root: Path, query: str, params: tuple[object, ...] = ()) -> sqlite3.Row:
    with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(query, params).fetchone()


def db_all(root: Path, query: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
    with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(query, params).fetchall()


def plan_slack_action(root: Path, *, key: str = "exactly-once:slack") -> str:
    payload = json.dumps({"execute": True, "operation": "slack.message.post", "params": {"channel": "C123", "text": "Ship it"}}, sort_keys=True)
    result = run_harness(
        root,
        "adapter",
        "plan",
        "--tool",
        "slack",
        "--mode",
        "write-confirm",
        "--artifact",
        "Slack update",
        "--action",
        "slack.message.post",
        "--payload-json",
        payload,
        "--idempotency-key",
        key,
    )
    return action_id(result.stdout)


class ExactlyOnceHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []
    counts: dict[str, int] = {}
    mode: str = "normal"
    marker: str = ""
    lock = threading.Lock()

    @classmethod
    def reset(cls, *, mode: str = "normal", marker: str = "") -> None:
        cls.requests = []
        cls.counts = {}
        cls.mode = mode
        cls.marker = marker

    def _record(self, body: object) -> int:
        with self.__class__.lock:
            self.__class__.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
            self.__class__.counts[self.path] = self.__class__.counts.get(self.path, 0) + 1
            return self.__class__.counts[self.path]

    def _json_response(self, status: int, payload: dict[str, object]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        body_text = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode("utf-8")
        body = json.loads(body_text) if body_text else {}
        count = self._record(body)
        if self.path == "/slack/api/search.messages":
            matches = [{"ts": "1710000000.000123", "channel": {"id": "C123"}, "permalink": "https://slack.example/existing"}] if self.__class__.marker else []
            self._json_response(200, {"ok": True, "messages": {"matches": matches}})
            return
        if self.path == "/slack/api/chat.postMessage":
            if self.__class__.mode == "slow-write":
                time.sleep(0.5)
            if self.__class__.mode == "crash-after-write" and count == 1:
                self.__class__.marker = str(body.get("text", ""))
                self.close_connection = True
                return
            self.__class__.marker = str(body.get("text", ""))
            self._json_response(200, {"ok": True, "channel": "C123", "ts": "1710000000.000200", "permalink": "https://slack.example/created"})
            return
        self._json_response(404, {"error": "not_found"})

    def log_message(self, _format: str, *_args: object) -> None:
        return


class FakeSlackServer:
    def __init__(self, *, mode: str = "normal", marker: str = "") -> None:
        self.mode = mode
        self.marker = marker

    def __enter__(self) -> "FakeSlackServer":
        ExactlyOnceHandler.reset(mode=self.mode, marker=self.marker)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ExactlyOnceHandler)
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
        return ExactlyOnceHandler.requests


class ConnectorExactlyOnceTest(unittest.TestCase):
    def test_concurrent_confirm_claims_action_once_before_remote_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp, FakeSlackServer(mode="slow-write") as server:
            root = Path(temp)
            run_harness(root, "init")
            action = plan_slack_action(root)
            env = os.environ.copy()
            env.update({"SLACK_BOT_TOKEN": "token", "HARNESS_SLACK_API_URL": server.base_url, "HARNESS_CONNECTOR_RETRY_SLEEP": "0"})
            command = ["python3", str(HARNESS), "--root", str(root), "adapter", "confirm", "--id", action]

            first = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
            time.sleep(0.05)
            second = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
            first_out, first_err = first.communicate(timeout=10)
            second_out, second_err = second.communicate(timeout=10)

            writes = [request for request in server.requests if request["path"] == "/slack/api/chat.postMessage"]
            row = db_one(root, "select status, external_id, execution_fence from adapter_actions where id = ?", (action,))
            self.assertEqual(len(writes), 1, (first.returncode, first_out, first_err, second.returncode, second_out, second_err, server.requests))
            self.assertEqual(row["status"], "completed")
            self.assertEqual(row["external_id"], "slack:message:C123:1710000000.000200")
            self.assertEqual(row["execution_fence"], 1)

    def test_unknown_action_recovers_existing_remote_marker_without_duplicate_write(self) -> None:
        key = "exactly-once:recovery"
        marker = f"codex-project-harness:idempotency-key={key}"
        with tempfile.TemporaryDirectory() as temp, FakeSlackServer(marker=marker) as server:
            root = Path(temp)
            run_harness(root, "init")
            action = plan_slack_action(root, key=key)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute("update adapter_actions set status = 'unknown', connector_status = 'degraded', blocked_reason = 'local commit lost' where id = ?", (action,))
                conn.commit()

            run_harness(root, "adapter", "confirm", "--id", action, env={"SLACK_BOT_TOKEN": "token", "HARNESS_SLACK_API_URL": server.base_url})

            writes = [request for request in server.requests if request["path"] == "/slack/api/chat.postMessage"]
            searches = [request for request in server.requests if request["path"] == "/slack/api/search.messages"]
            row = db_one(root, "select status, external_id, remote_recovery_count from adapter_actions where id = ?", (action,))
            adapter = db_one(root, "select external_id from adapters where idempotency_key = ?", (key,))
            self.assertGreaterEqual(len(searches), 1)
            self.assertEqual(writes, [])
            self.assertEqual(row["status"], "completed")
            self.assertEqual(row["external_id"], "slack:message:C123:1710000000.000123")
            self.assertEqual(adapter["external_id"], row["external_id"])
            self.assertEqual(row["remote_recovery_count"], 1)

    def test_ambiguous_remote_failure_marks_unknown_and_next_confirm_recovers_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp, FakeSlackServer(mode="crash-after-write") as server:
            root = Path(temp)
            run_harness(root, "init")
            action = plan_slack_action(root, key="exactly-once:ambiguous")

            first = run_harness(root, "adapter", "confirm", "--id", action, env={"SLACK_BOT_TOKEN": "token", "HARNESS_SLACK_API_URL": server.base_url}, check=False)

            row = db_one(root, "select status, connector_status, external_id from adapter_actions where id = ?", (action,))
            self.assertNotEqual(first.returncode, 0)
            self.assertEqual(row["status"], "unknown")
            self.assertEqual(row["external_id"], "")

            run_harness(root, "adapter", "confirm", "--id", action, env={"SLACK_BOT_TOKEN": "token", "HARNESS_SLACK_API_URL": server.base_url})

            writes = [request for request in server.requests if request["path"] == "/slack/api/chat.postMessage"]
            row = db_one(root, "select status, external_id, remote_recovery_count from adapter_actions where id = ?", (action,))
            self.assertEqual(len(writes), 1)
            self.assertEqual(row["status"], "completed")
            self.assertEqual(row["external_id"], "slack:message:C123:1710000000.000123")
            self.assertEqual(row["remote_recovery_count"], 1)

    def test_connector_results_and_advisory_fallbacks_are_not_delivery_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp, FakeSlackServer() as server:
            root = Path(temp)
            run_harness(root, "init")
            action = plan_slack_action(root, key="exactly-once:not-evidence")

            run_harness(root, "adapter", "confirm", "--id", action, env={"SLACK_BOT_TOKEN": "token", "HARNESS_SLACK_API_URL": server.base_url})

            evidence_count = db_one(root, "select count(*) as count from evidence")["count"]
            delivery = run_harness(root, "validate", "--delivery", check=False)
            self.assertEqual(evidence_count, 0)
            self.assertNotEqual(delivery.returncode, 0)

    def test_same_request_id_retry_does_not_duplicate_recovery_or_write(self) -> None:
        key = "exactly-once:request-id"
        marker = f"codex-project-harness:idempotency-key={key}"
        with tempfile.TemporaryDirectory() as temp, FakeSlackServer(marker=marker) as server:
            root = Path(temp)
            run_harness(root, "init")
            action = plan_slack_action(root, key=key)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute("update adapter_actions set status = 'unknown', connector_status = 'degraded', blocked_reason = 'retry recovery' where id = ?", (action,))
                conn.commit()

            first = run_harness(root, "adapter", "confirm", "--id", action, "--request-id", "REQ-EXACTLY-ONCE", env={"SLACK_BOT_TOKEN": "token", "HARNESS_SLACK_API_URL": server.base_url})
            second = run_harness(root, "adapter", "confirm", "--id", action, "--request-id", "REQ-EXACTLY-ONCE", env={"SLACK_BOT_TOKEN": "token", "HARNESS_SLACK_API_URL": server.base_url})

            writes = [request for request in server.requests if request["path"] == "/slack/api/chat.postMessage"]
            row = db_one(root, "select status, remote_recovery_count from adapter_actions where id = ?", (action,))
            logs = db_all(root, "select * from command_log where request_id = 'REQ-EXACTLY-ONCE'")
            self.assertEqual(second.stdout, first.stdout)
            self.assertEqual(writes, [])
            self.assertEqual(row["status"], "completed")
            self.assertEqual(row["remote_recovery_count"], 1)
            self.assertEqual(len(logs), 1)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from contextlib import closing
from http.server import BaseHTTPRequestHandler, HTTPServer
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


def set_slack_profile(root: Path, *, project_key: str = "project-a", channel: str = "C123") -> None:
    run_harness(root, "connector", "profile", "set", "--project-key", project_key, "--slack-channel", channel)


def plan_slack(root: Path, *, channel: str = "C123", mode: str = "write-confirm", project_key: str = "project-a", override: bool = False) -> str:
    payload = {"execute": True, "operation": "slack.message.post", "params": {"channel": channel, "text": "Ship it"}}
    if override:
        payload["scope_override"] = True
    result = run_harness(
        root,
        "adapter",
        "plan",
        "--tool",
        "slack",
        "--mode",
        mode,
        "--artifact",
        "Slack update",
        "--action",
        "post",
        "--payload-json",
        json.dumps(payload, sort_keys=True),
        "--idempotency-key",
        f"namespace:{project_key}:slack",
    )
    return action_id(result.stdout)


class SlackNamespaceHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []
    marker_text: str = ""

    @classmethod
    def reset(cls, marker_text: str = "") -> None:
        cls.requests = []
        cls.marker_text = marker_text

    def _json(self, payload: dict[str, object]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        body_text = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode("utf-8")
        body = json.loads(body_text) if body_text else {}
        self.__class__.requests.append({"path": self.path, "body": body, "headers": dict(self.headers)})
        if self.path == "/slack/api/search.messages":
            matches = []
            query = str(body.get("query", ""))
            if self.__class__.marker_text and all(part in self.__class__.marker_text for part in query.splitlines() if part):
                matches = [{"ts": "1710000000.000123", "channel": {"id": "C123"}, "permalink": "https://slack.example/existing"}]
            self._json({"ok": True, "messages": {"matches": matches}})
            return
        if self.path == "/slack/api/chat.postMessage":
            self.__class__.marker_text = str(body.get("text", ""))
            self._json({"ok": True, "channel": body.get("channel", ""), "ts": "1710000000.000200", "permalink": "https://slack.example/created"})
            return
        self._json({"ok": False, "error": "unknown"})

    def log_message(self, _format: str, *_args: object) -> None:
        return


class FakeSlackServer:
    def __init__(self, marker_text: str = "") -> None:
        self.marker_text = marker_text

    def __enter__(self) -> "FakeSlackServer":
        SlackNamespaceHandler.reset(self.marker_text)
        self.server = HTTPServer(("127.0.0.1", 0), SlackNamespaceHandler)
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
        return SlackNamespaceHandler.requests


class ConnectorNamespaceIsolationTest(unittest.TestCase):
    def test_profile_status_reports_project_key_and_unbound_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")

            result = run_harness(root, "connector", "profile", "status", "--json")

            data = json.loads(result.stdout)
            self.assertTrue(data["project_key"])
            self.assertEqual(data["profiles"]["slack"]["status"], "unbound")

    def test_execute_write_without_profile_fails_before_remote_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp, FakeSlackServer() as server:
            root = Path(temp)
            run_harness(root, "init")
            action = plan_slack(root)

            result = run_harness(root, "adapter", "confirm", "--id", action, env={"SLACK_BOT_TOKEN": "token", "HARNESS_SLACK_API_URL": server.base_url}, check=False)

            row = db_one(root, "select status from adapter_actions where id = ?", (action,))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("connector profile missing", result.stdout)
            self.assertEqual(row["status"], "planned")
            self.assertEqual(server.requests, [])

    def test_matching_profile_writes_double_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp, FakeSlackServer() as server:
            root = Path(temp)
            run_harness(root, "init")
            set_slack_profile(root, project_key="project-a", channel="C123")
            action = plan_slack(root, project_key="project-a")

            run_harness(root, "adapter", "confirm", "--id", action, env={"SLACK_BOT_TOKEN": "token", "HARNESS_SLACK_API_URL": server.base_url})

            writes = [request for request in server.requests if request["path"] == "/slack/api/chat.postMessage"]
            text = str(writes[0]["body"]["text"])
            self.assertIn("codex-project-harness:project-key=project-a", text)
            self.assertIn("codex-project-harness:idempotency-key=namespace:project-a:slack", text)

    def test_profile_mismatch_fails_closed_and_audits(self) -> None:
        with tempfile.TemporaryDirectory() as temp, FakeSlackServer() as server:
            root = Path(temp)
            run_harness(root, "init")
            set_slack_profile(root, project_key="project-a", channel="C123")
            action = plan_slack(root, channel="C999")

            result = run_harness(root, "adapter", "confirm", "--id", action, env={"SLACK_BOT_TOKEN": "token", "HARNESS_SLACK_API_URL": server.base_url}, check=False)

            findings = db_one(root, "select count(*) as count from findings where id like 'connector-scope-override:%'")["count"]
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("connector scope mismatch", result.stdout)
            self.assertEqual(server.requests, [])
            self.assertEqual(findings, 1)

    def test_scope_override_only_allows_write_confirm(self) -> None:
        with tempfile.TemporaryDirectory() as temp, FakeSlackServer() as server:
            root = Path(temp)
            run_harness(root, "init")
            set_slack_profile(root, project_key="project-a", channel="C123")
            auto = plan_slack(root, channel="C999", mode="write-auto", override=True)
            confirm = plan_slack(root, channel="C999", mode="write-confirm", override=True, project_key="project-a-override")

            auto_result = run_harness(root, "adapter", "confirm", "--id", auto, env={"SLACK_BOT_TOKEN": "token", "HARNESS_SLACK_API_URL": server.base_url}, check=False)
            confirm_result = run_harness(root, "adapter", "confirm", "--id", confirm, env={"SLACK_BOT_TOKEN": "token", "HARNESS_SLACK_API_URL": server.base_url})

            self.assertNotEqual(auto_result.returncode, 0)
            self.assertIn("scope_override requires write-confirm", auto_result.stdout)
            self.assertIn("OK: adapter action confirmed", confirm_result.stdout)

    def test_recovery_requires_matching_project_marker(self) -> None:
        wrong_marker = "\n".join([
            "codex-project-harness:project-key=project-b",
            "codex-project-harness:idempotency-key=namespace:project-a:slack",
        ])
        with tempfile.TemporaryDirectory() as temp, FakeSlackServer(marker_text=wrong_marker) as server:
            root = Path(temp)
            run_harness(root, "init")
            set_slack_profile(root, project_key="project-a", channel="C123")
            action = plan_slack(root, project_key="project-a")
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute("update adapter_actions set status = 'unknown' where id = ?", (action,))
                conn.commit()

            run_harness(root, "adapter", "confirm", "--id", action, env={"SLACK_BOT_TOKEN": "token", "HARNESS_SLACK_API_URL": server.base_url})

            writes = [request for request in server.requests if request["path"] == "/slack/api/chat.postMessage"]
            row = db_one(root, "select status, remote_recovery_count from adapter_actions where id = ?", (action,))
            self.assertEqual(len(writes), 1)
            self.assertEqual(row["status"], "completed")
            self.assertEqual(row["remote_recovery_count"], 0)


if __name__ == "__main__":
    unittest.main()

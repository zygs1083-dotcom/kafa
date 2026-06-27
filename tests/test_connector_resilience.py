from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import textwrap
import threading
import unittest
from contextlib import closing
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "plugins/codex-project-harness/scripts/harness.py"


def run_harness(root: Path, *args: str, env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    command_env = os.environ.copy()
    if env:
        command_env.update(env)
    command_env["HARNESS_CONNECTOR_RETRY_SLEEP"] = "0"
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


def plan_action(root: Path, tool: str, mode: str, operation: str, params: dict[str, object], *, key: str = "") -> str:
    payload = json.dumps({"execute": True, "operation": operation, "params": params}, sort_keys=True)
    result = run_harness(
        root,
        "adapter",
        "plan",
        "--tool",
        tool,
        "--mode",
        mode,
        "--artifact",
        f"{tool} artifact",
        "--action",
        operation,
        "--payload-json",
        payload,
        "--idempotency-key",
        key or f"connector-resilience:{tool}:{operation}",
    )
    return action_id(result.stdout)


class SequencedHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []
    counts: dict[str, int] = {}
    mode: str = "retry-success"
    marker: str = ""

    def _record(self, body: object) -> None:
        record = {"path": self.path, "headers": dict(self.headers), "body": body}
        self.__class__.requests.append(record)
        self.__class__.counts[self.path] = self.__class__.counts.get(self.path, 0) + 1

    def _json_response(self, status: int, payload: dict[str, object], headers: dict[str, str] | None = None) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        self._record({})
        if self.path == "/notion/v1/users/me":
            self._json_response(200, {"object": "user", "id": "notion-user-1"})
            return
        if self.path == "/figma/v1/me":
            self._json_response(200, {"id": "figma-user-1", "email": "user@example.test"})
            return
        if self.path == "/figma/v1/files/FILE1":
            self._json_response(200, {"name": "Fixture File", "key": "FILE1"})
            return
        if self.path == "/figma/v1/files/FILE1/comments":
            comments = [{"id": "figma-existing", "message": self.__class__.marker, "file_key": "FILE1"}] if self.__class__.marker else []
            self._json_response(200, {"comments": comments})
            return
        self._json_response(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        body_text = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode("utf-8")
        body = json.loads(body_text) if body_text else {}
        self._record(body)
        count = self.__class__.counts[self.path]
        if self.__class__.mode == "retry-success" and self.path == "/slack/api/chat.postMessage" and count == 1:
            self._json_response(429, {"ok": False, "error": "rate_limited"}, {"Retry-After": "1"})
            return
        if self.__class__.mode == "always-429" and self.path == "/slack/api/chat.postMessage":
            self._json_response(429, {"ok": False, "error": "rate_limited"}, {"Retry-After": "1"})
            return
        if self.path == "/slack/api/chat.postMessage":
            self._json_response(200, {"ok": True, "channel": "C123", "ts": "1710000000.000200", "permalink": "https://slack.example/archives/C123/p1710000000000200"})
            return
        if self.path == "/slack/api/search.messages":
            matches = [{"ts": "1710000000.000123", "channel": {"id": "C123"}, "permalink": "https://slack.example/existing"}] if self.__class__.marker else []
            self._json_response(200, {"ok": True, "messages": {"matches": matches}})
            return
        if self.path == "/linear/graphql":
            query = str(body.get("query", ""))
            if "Viewer" in query:
                self._json_response(200, {"data": {"viewer": {"id": "linear-user-1", "name": "Linear User"}}})
            elif "IssueCreate" in query:
                self._json_response(200, {"data": {"issueCreate": {"success": True, "issue": {"id": "lin-id-1", "identifier": "ENG-1", "url": "https://linear.example/ENG-1"}}}})
            else:
                self._json_response(200, {"data": {"search": {"nodes": [{"id": "lin-existing", "identifier": "ENG-9", "url": "https://linear.example/ENG-9"}]}}})
            return
        if self.path == "/notion/v1/search":
            if self.__class__.marker:
                self._json_response(200, {"results": [{"id": "notion-existing", "url": "https://notion.example/existing"}]})
            else:
                self._json_response(200, {"results": []})
            return
        if self.path == "/notion/v1/pages":
            self._json_response(200, {"id": "notion-new", "url": "https://notion.example/new"})
            return
        if self.path == "/figma/v1/files/FILE1/comments":
            if self.__class__.mode == "figma-comments-existing":
                self._json_response(200, {"comments": [{"id": "figma-existing", "message": self.__class__.marker, "file_key": "FILE1"}]})
            else:
                self._json_response(200, {"id": "figma-new", "file_key": "FILE1"})
            return
        self._json_response(404, {"error": "not_found"})

    def log_message(self, _format: str, *_args: object) -> None:
        return


class FakeHttpServer:
    def __init__(self, *, mode: str = "retry-success", marker: str = "") -> None:
        self.mode = mode
        self.marker = marker

    def __enter__(self) -> "FakeHttpServer":
        SequencedHandler.requests = []
        SequencedHandler.counts = {}
        SequencedHandler.mode = self.mode
        SequencedHandler.marker = self.marker
        self.server = HTTPServer(("127.0.0.1", 0), SequencedHandler)
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
        return SequencedHandler.requests


def fake_gh(temp: Path, *, first_rate_limited: bool = False, existing_marker: bool = False, always_rate_limited: bool = False) -> tuple[Path, Path]:
    bin_dir = temp / "bin"
    bin_dir.mkdir()
    log_path = temp / "gh-log.jsonl"
    script = bin_dir / "gh"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import json
            import pathlib
            import sys

            log_path = pathlib.Path({str(log_path)!r})
            args = sys.argv[1:]
            calls = log_path.read_text(encoding="utf-8").splitlines() if log_path.exists() else []
            log_path.write_text("\\n".join(calls + [json.dumps(args, sort_keys=True)]) + "\\n", encoding="utf-8")
            endpoint = args[1] if len(args) > 1 else ""
            if endpoint == "user":
                print(json.dumps({{"login": "fixture-user"}}))
                raise SystemExit(0)
            if endpoint == "search/issues" and {str(bool(existing_marker))}:
                print(json.dumps({{"items": [{{"number": 42, "html_url": "https://github.example/repo/issues/42"}}]}}))
                raise SystemExit(0)
            if endpoint == "search/issues":
                print(json.dumps({{"items": []}}))
                raise SystemExit(0)
            write_attempts = len([line for line in calls if "repos/owner/repo/issues" in line])
            if ({str(bool(first_rate_limited))} and endpoint == "repos/owner/repo/issues" and write_attempts == 0) or ({str(bool(always_rate_limited))} and endpoint == "repos/owner/repo/issues"):
                print("HTTP 403: API rate limit exceeded; retry-after: 1; x-ratelimit-remaining: 0; x-ratelimit-reset: 1893456000", file=sys.stderr)
                raise SystemExit(1)
            if endpoint == "repos/owner/repo/issues":
                print(json.dumps({{"id": 123, "number": 7, "html_url": "https://github.example/repo/issues/7"}}))
                raise SystemExit(0)
            print(json.dumps({{"ok": True}}))
            """
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)
    (bin_dir / "gh.cmd").write_text("@echo off\r\npython \"%~dp0gh\" %*\r\n", encoding="utf-8")
    return bin_dir, log_path


class ConnectorResilienceTest(unittest.TestCase):
    def test_http_retry_after_records_budget_and_completes_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp, FakeHttpServer(mode="retry-success") as server:
            root = Path(temp) / "repo"
            root.mkdir()
            run_harness(root, "init")
            action = plan_action(root, "slack", "write-confirm", "slack.message.post", {"channel": "C123", "text": "Ship it"})

            run_harness(root, "adapter", "confirm", "--id", action, env={"SLACK_BOT_TOKEN": "token", "HARNESS_SLACK_API_URL": server.base_url})

            row = db_one(root, "select status, external_id, attempt_count, connector_status from adapter_actions where id = ?", (action,))
            budget = db_one(root, "select status, retry_after_at, last_status_code from connector_budgets where tool = 'slack' and operation = 'slack.message.post'")
            self.assertEqual(row["status"], "completed")
            self.assertEqual(row["external_id"], "slack:message:C123:1710000000.000200")
            self.assertEqual(row["attempt_count"], 3)
            self.assertEqual(row["connector_status"], "available")
            self.assertEqual(budget["status"], "available")
            self.assertEqual(budget["last_status_code"], 200)
            self.assertTrue(budget["retry_after_at"])
            self.assertEqual(len([request for request in server.requests if request["path"] == "/slack/api/chat.postMessage"]), 2)

    def test_retry_budget_exhaustion_blocks_action_without_adapter_record(self) -> None:
        with tempfile.TemporaryDirectory() as temp, FakeHttpServer(mode="always-429") as server:
            root = Path(temp) / "repo"
            root.mkdir()
            run_harness(root, "init")
            action = plan_action(root, "slack", "write-confirm", "slack.message.post", {"channel": "C123", "text": "Nope"})

            result = run_harness(root, "adapter", "confirm", "--id", action, env={"SLACK_BOT_TOKEN": "token", "HARNESS_SLACK_API_URL": server.base_url}, check=False)

            row = db_one(root, "select status, connector_status, blocked_reason, attempt_count from adapter_actions where id = ?", (action,))
            adapter_count = db_one(root, "select count(*) as count from adapters")["count"]
            budget = db_one(root, "select status, last_status_code, last_error from connector_budgets where tool = 'slack' and operation = 'slack.message.post'")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("connector execution failed", result.stdout)
            self.assertEqual(row["status"], "blocked")
            self.assertEqual(row["connector_status"], "blocked")
            self.assertIn("rate", row["blocked_reason"])
            self.assertEqual(row["attempt_count"], 4)
            self.assertEqual(adapter_count, 0)
            self.assertEqual(budget["status"], "blocked")
            self.assertEqual(budget["last_status_code"], 429)
            self.assertIn("429", budget["last_error"])

    def test_notion_and_figma_probe_use_real_probe_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as temp, FakeHttpServer() as server:
            root = Path(temp)
            run_harness(root, "init")
            notion = plan_action(root, "notion", "read-only", "notion.probe", {}, key="probe:notion")
            figma = plan_action(root, "figma", "read-only", "figma.probe", {"file_key": "FILE1"}, key="probe:figma")

            run_harness(root, "adapter", "confirm", "--id", notion, env={"NOTION_TOKEN": "notion-token", "HARNESS_NOTION_API_URL": server.base_url})
            run_harness(root, "adapter", "confirm", "--id", figma, env={"FIGMA_TOKEN": "figma-token", "HARNESS_FIGMA_API_URL": server.base_url})

            paths = [request["path"] for request in server.requests]
            notion_row = db_one(root, "select status, external_id from adapter_actions where id = ?", (notion,))
            figma_row = db_one(root, "select status, external_id from adapter_actions where id = ?", (figma,))
            self.assertIn("/notion/v1/users/me", paths)
            self.assertIn("/figma/v1/files/FILE1", paths)
            self.assertEqual(notion_row["external_id"], "notion:probe:notion-user-1")
            self.assertEqual(figma_row["external_id"], "figma:probe:FILE1")

    def test_notion_payload_limits_fail_closed_before_external_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp, FakeHttpServer() as server:
            root = Path(temp)
            run_harness(root, "init")
            children = [{} for _ in range(1000)]
            action = plan_action(root, "notion", "write-confirm", "notion.page.create", {"parent_page_id": "PARENT", "title": "Huge", "children": children})

            result = run_harness(root, "adapter", "confirm", "--id", action, env={"NOTION_TOKEN": "notion-token", "HARNESS_NOTION_API_URL": server.base_url}, check=False)

            row = db_one(root, "select status, connector_status, blocked_reason from adapter_actions where id = ?", (action,))
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(row["status"], "blocked")
            self.assertEqual(row["connector_status"], "blocked")
            self.assertIn("Notion payload", row["blocked_reason"])
            self.assertEqual(server.requests, [])

    def test_marker_search_reuses_existing_github_issue_without_duplicate_create(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            root.mkdir()
            run_harness(root, "init")
            bin_dir, log_path = fake_gh(Path(temp), existing_marker=True)
            action = plan_action(root, "github", "write-confirm", "github.issue.create", {"repo": "owner/repo", "title": "Issue title", "body": "Body"})

            run_harness(root, "adapter", "confirm", "--id", action, env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"})

            row = db_one(root, "select status, external_id, external_link, attempt_count from adapter_actions where id = ?", (action,))
            calls = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
            endpoints = [call[1] for call in calls if len(call) > 1]
            self.assertEqual(row["status"], "completed")
            self.assertEqual(row["external_id"], "github:issue:42")
            self.assertEqual(row["external_link"], "https://github.example/repo/issues/42")
            self.assertEqual(row["attempt_count"], 1)
            self.assertIn("search/issues", endpoints)
            self.assertNotIn("repos/owner/repo/issues", endpoints)

    def test_marker_search_reuses_existing_http_connector_objects_without_duplicate_create(self) -> None:
        key = "reuse-http-marker"
        marker = f"codex-project-harness:idempotency-key={key}"
        cases = [
            ("linear", "linear.issue.create", {"team_id": "TEAM", "title": "Linear issue", "description": "Body"}, "LINEAR_API_KEY", "HARNESS_LINEAR_API_URL", "linear:issue:ENG-9"),
            ("notion", "notion.page.create", {"parent_page_id": "PARENT", "title": "Notion page", "content": "Body"}, "NOTION_TOKEN", "HARNESS_NOTION_API_URL", "notion:page:notion-existing"),
            ("figma", "figma.comment.create", {"file_key": "FILE1", "message": "Review note"}, "FIGMA_TOKEN", "HARNESS_FIGMA_API_URL", "figma:comment:figma-existing"),
            ("slack", "slack.message.post", {"channel": "C123", "text": "Ship it"}, "SLACK_BOT_TOKEN", "HARNESS_SLACK_API_URL", "slack:message:C123:1710000000.000123"),
        ]
        with tempfile.TemporaryDirectory() as temp, FakeHttpServer(marker=marker) as server:
            root = Path(temp)
            run_harness(root, "init")
            for index, (tool, operation, params, token_env, url_env, expected_external_id) in enumerate(cases):
                action = plan_action(root, tool, "write-confirm", operation, params, key=key)
                run_harness(root, "adapter", "confirm", "--id", action, env={token_env: f"token-{index}", url_env: server.base_url})
                row = db_one(root, "select status, external_id from adapter_actions where id = ?", (action,))
                self.assertEqual(row["status"], "completed")
                self.assertEqual(row["external_id"], expected_external_id)

            request_bodies = [json.dumps(request["body"]) for request in server.requests]
            request_paths = [str(request["path"]) for request in server.requests]
            self.assertFalse(any("IssueCreate" in body for body in request_bodies))
            self.assertNotIn("/notion/v1/pages", request_paths)
            self.assertNotIn("/slack/api/chat.postMessage", request_paths)
            self.assertNotIn("/figma/v1/files/FILE1/comments", [path for request, path in zip(server.requests, request_paths) if request["body"]])

    def test_same_request_id_retry_does_not_duplicate_budget_or_findings(self) -> None:
        with tempfile.TemporaryDirectory() as temp, FakeHttpServer(mode="always-429") as server:
            root = Path(temp)
            run_harness(root, "init")
            action = plan_action(root, "slack", "write-confirm", "slack.message.post", {"channel": "C123", "text": "Retry idempotently"})
            env = {"SLACK_BOT_TOKEN": "token", "HARNESS_SLACK_API_URL": server.base_url}

            first = run_harness(root, "adapter", "confirm", "--id", action, "--request-id", "REQ-CONN-BLOCK", env=env, check=False)
            second = run_harness(root, "adapter", "confirm", "--id", action, "--request-id", "REQ-CONN-BLOCK", env=env, check=False)

            rows = db_all(root, "select * from connector_budgets where tool = 'slack' and operation = 'slack.message.post'")
            findings = db_all(root, "select * from findings where surface = 'connector'")
            self.assertNotEqual(first.returncode, 0)
            self.assertEqual(second.stdout, first.stdout)
            self.assertEqual(len(rows), 1)
            self.assertEqual(len(findings), 1)


if __name__ == "__main__":
    unittest.main()

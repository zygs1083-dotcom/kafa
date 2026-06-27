from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
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


def plan_action(root: Path, tool: str, mode: str, operation: str, params: dict[str, object]) -> str:
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
        f"connector-test:{tool}:{operation}",
    )
    return action_id(result.stdout)


def fake_gh(temp: Path, *, fail: bool = False) -> tuple[Path, Path]:
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
            if {str(bool(fail))}:
                print("fake gh failure", file=sys.stderr)
                sys.exit(2)
            endpoint = sys.argv[2] if len(sys.argv) > 2 else ""
            if endpoint.endswith("/issues"):
                print(json.dumps({{"id": 123, "number": 7, "html_url": "https://github.example/repo/issues/7"}}))
            elif "/issues/" in endpoint and endpoint.endswith("/comments"):
                print(json.dumps({{"id": 456, "html_url": "https://github.example/repo/issues/7#issuecomment-456"}}))
            elif endpoint.endswith("/pulls"):
                print(json.dumps({{"id": 789, "number": 3, "html_url": "https://github.example/repo/pull/3"}}))
            else:
                print(json.dumps({{"viewer": {{"login": "fake"}}}}))
            """
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)
    (bin_dir / "gh.cmd").write_text("@echo off\r\npython \"%~dp0gh\" %*\r\n", encoding="utf-8")
    return bin_dir, log_path


class CaptureHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []

    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode("utf-8")
        record = {"path": self.path, "headers": dict(self.headers), "body": json.loads(body) if body else {}}
        self.__class__.requests.append(record)
        if self.path == "/slack/api/chat.postMessage":
            response = {"ok": True, "channel": "C123", "ts": "1710000000.000100", "permalink": "https://slack.example/archives/C123/p1710000000000100"}
        elif self.path == "/linear/graphql":
            response = {"data": {"issueCreate": {"success": True, "issue": {"id": "LIN-1", "identifier": "ENG-1", "url": "https://linear.example/ENG-1"}}}}
        elif self.path == "/notion/v1/pages":
            response = {"id": "notion-page-1", "url": "https://notion.example/page-1"}
        elif self.path == "/notion/v1/pages/notion-page-1":
            response = {"id": "notion-page-1", "url": "https://notion.example/page-1"}
        elif self.path == "/figma/v1/files/FILE1/comments":
            response = {"id": "figma-comment-1", "file_key": "FILE1", "created_at": "2026-01-01T00:00:00Z"}
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


class FakeHttpServer:
    def __enter__(self) -> "FakeHttpServer":
        CaptureHandler.requests = []
        self.server = HTTPServer(("127.0.0.1", 0), CaptureHandler)
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
        return CaptureHandler.requests


class RealConnectorAdaptersTest(unittest.TestCase):
    def test_github_issue_create_executes_once_and_records_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            root.mkdir()
            run_harness(root, "init")
            bin_dir, log_path = fake_gh(Path(temp))
            action = plan_action(root, "github", "write-confirm", "github.issue.create", {"repo": "owner/repo", "title": "Issue title", "body": "Body"})
            env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "HARNESS_GH_BIN": f"{sys.executable} {bin_dir / 'gh'}"}

            first = run_harness(root, "adapter", "confirm", "--id", action, "--request-id", "REQ-GH-1", env=env)
            second = run_harness(root, "adapter", "confirm", "--id", action, "--request-id", "REQ-GH-1", env=env)

            row = db_one(root, "select status, external_id, external_link from adapter_actions where id = ?", (action,))
            adapter = db_one(root, "select external_id, external_link from adapters where idempotency_key = 'connector-test:github:github.issue.create'")
            calls = log_path.read_text(encoding="utf-8").splitlines()
            self.assertIn("OK: adapter action confirmed", first.stdout)
            self.assertEqual(second.stdout, first.stdout)
            self.assertEqual(row["status"], "completed")
            self.assertEqual(row["external_id"], "github:issue:7")
            self.assertEqual(adapter["external_link"], "https://github.example/repo/issues/7")
            self.assertEqual(len(calls), 2)

    def test_github_failure_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            root.mkdir()
            run_harness(root, "init")
            bin_dir, _log_path = fake_gh(Path(temp), fail=True)
            action = plan_action(root, "github", "write-confirm", "github.issue.create", {"repo": "owner/repo", "title": "Issue title"})

            result = run_harness(
                root,
                "adapter",
                "confirm",
                "--id",
                action,
                env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "HARNESS_GH_BIN": f"{sys.executable} {bin_dir / 'gh'}"},
                check=False,
            )

            row = db_one(root, "select status, external_id from adapter_actions where id = ?", (action,))
            adapter_count = db_one(root, "select count(*) as count from adapters")["count"]
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("connector execution failed", result.stdout)
            self.assertEqual(row["status"], "blocked")
            self.assertEqual(row["external_id"], "")
            self.assertEqual(adapter_count, 0)

    def test_http_connectors_execute_with_tokens_and_markers(self) -> None:
        cases = [
            ("linear", "linear.issue.create", {"team_id": "TEAM", "title": "Linear issue", "description": "Body"}, "LINEAR_API_KEY", "HARNESS_LINEAR_API_URL", "/linear/graphql", "linear:issue:ENG-1"),
            ("notion", "notion.page.create", {"parent_page_id": "PARENT", "title": "Notion page", "content": "Body"}, "NOTION_TOKEN", "HARNESS_NOTION_API_URL", "/notion/v1/pages", "notion:page:notion-page-1"),
            ("figma", "figma.comment.create", {"file_key": "FILE1", "message": "Review note"}, "FIGMA_TOKEN", "HARNESS_FIGMA_API_URL", "/figma/v1/files/FILE1/comments", "figma:comment:figma-comment-1"),
            ("slack", "slack.message.post", {"channel": "C123", "text": "Ship it"}, "SLACK_BOT_TOKEN", "HARNESS_SLACK_API_URL", "/slack/api/chat.postMessage", "slack:message:C123:1710000000.000100"),
        ]
        with FakeHttpServer() as server:
            for index, (tool, operation, params, token_env, url_env, path, expected_external_id) in enumerate(cases):
                with tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    run_harness(root, "init")
                    action = plan_action(root, tool, "write-confirm", operation, params)
                    env = {token_env: f"token-{index}", url_env: server.base_url}

                    run_harness(root, "adapter", "confirm", "--id", action, env=env)

                    row = db_one(root, "select status, external_id from adapter_actions where id = ?", (action,))
                    self.assertEqual(row["status"], "completed")
                    self.assertEqual(row["external_id"], expected_external_id)
                    self.assertEqual(server.requests[-1]["path"], path)
                    self.assertIn("codex-project-harness:idempotency-key=", json.dumps(server.requests[-1]["body"]))
                    self.assertIn(f"token-{index}", json.dumps(server.requests[-1]["headers"]))

    def test_connector_validation_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            draft = plan_action(root, "github", "draft-write", "github.issue.create", {"repo": "owner/repo", "title": "Issue title"})
            read_only = plan_action(root, "github", "read-only", "github.issue.create", {"repo": "owner/repo", "title": "Issue title"})
            mismatch = plan_action(root, "github", "write-confirm", "slack.message.post", {"channel": "C123", "text": "Nope"})
            unknown = plan_action(root, "github", "write-confirm", "github.unknown", {})
            missing_token = plan_action(root, "slack", "write-confirm", "slack.message.post", {"channel": "C123", "text": "No token"})

            for action in [draft, read_only, mismatch, unknown, missing_token]:
                result = run_harness(root, "adapter", "confirm", "--id", action, check=False)
                self.assertNotEqual(result.returncode, 0)

    def test_legacy_manual_confirm_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            result = run_harness(root, "adapter", "plan", "--tool", "github", "--mode", "write-confirm", "--artifact", "Issue", "--action", "manual")
            action = action_id(result.stdout)

            run_harness(root, "adapter", "confirm", "--id", action)

            row = db_one(root, "select status, external_id from adapter_actions where id = ?", (action,))
            adapter_count = db_one(root, "select count(*) as count from adapters")["count"]
            self.assertEqual(row["status"], "confirmed")
            self.assertEqual(row["external_id"], "")
            self.assertEqual(adapter_count, 0)


if __name__ == "__main__":
    unittest.main()

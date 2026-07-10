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
    result = subprocess.run(
        ["python3", str(HARNESS), "--root", str(root), *args],
        text=True,
        capture_output=True,
        check=False,
        env=command_env,
    )
    if check and result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return result


def db_status(root: Path, action_id: str) -> str:
    with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
        return str(conn.execute("select status from adapter_actions where id = ?", (action_id,)).fetchone()[0])


def plan_linear_action(root: Path, operation: str, issue_id: str, *, key: str) -> str:
    params: dict[str, object] = {"issue_id": issue_id}
    if operation == "linear.issue.comment":
        params["body"] = "Scoped comment"
    else:
        params["title"] = "Scoped update"
    payload = json.dumps({"execute": True, "operation": operation, "params": params}, sort_keys=True)
    result = run_harness(
        root,
        "adapter",
        "plan",
        "--tool",
        "linear",
        "--mode",
        "write-confirm",
        "--artifact",
        operation,
        "--action",
        operation,
        "--payload-json",
        payload,
        "--idempotency-key",
        key,
    )
    return result.stdout.strip().split()[-1]


class LinearScopeHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []

    def do_POST(self) -> None:  # noqa: N802
        body_text = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode("utf-8")
        body = json.loads(body_text) if body_text else {}
        self.__class__.requests.append(body)
        query = str(body.get("query", ""))
        variables = body.get("variables", {})
        if "IssueScope" in query:
            issue_id = str(variables.get("id", "")) if isinstance(variables, dict) else ""
            issue = {
                "id": issue_id,
                "team": {"id": "TEAM-A" if issue_id != "ISSUE-B" else "TEAM-B"},
                "project": {"id": "PROJECT-A" if issue_id != "ISSUE-B" else "PROJECT-B"},
            }
            if issue_id == "ISSUE-MISSING":
                issue = None
            response = {"data": {"issue": issue}}
        elif "query Search" in query:
            response = {"data": {"search": {"nodes": []}}}
        elif "CommentCreate" in query:
            response = {"data": {"commentCreate": {"success": True, "comment": {"id": "COMMENT-1", "url": "https://linear.example/comment/1"}}}}
        elif "IssueUpdate" in query:
            response = {"data": {"issueUpdate": {"success": True, "issue": {"id": "ISSUE-A", "identifier": "A-1", "url": "https://linear.example/A-1"}}}}
        else:
            response = {"errors": [{"message": "unexpected query"}]}
        encoded = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class FakeLinearServer:
    def __enter__(self) -> "FakeLinearServer":
        LinearScopeHandler.requests = []
        self.server = HTTPServer(("127.0.0.1", 0), LinearScopeHandler)
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
        return LinearScopeHandler.requests


class LinearScopeIsolationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.env: dict[str, str] = {}

    def initialize(self, root: Path, server: FakeLinearServer) -> None:
        run_harness(root, "init")
        run_harness(
            root,
            "connector",
            "profile",
            "set",
            "--project-key",
            "project-a",
            "--linear-team",
            "TEAM-A",
            "--linear-project",
            "PROJECT-A",
        )
        self.env = {"LINEAR_API_KEY": "token", "HARNESS_LINEAR_API_URL": server.base_url}

    def test_cross_project_comment_and_update_make_zero_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as temp, FakeLinearServer() as server:
            root = Path(temp)
            self.initialize(root, server)
            comment = plan_linear_action(root, "linear.issue.comment", "ISSUE-B", key="linear-scope:comment")
            update = plan_linear_action(root, "linear.issue.update", "ISSUE-B", key="linear-scope:update")

            comment_result = run_harness(root, "adapter", "confirm", "--id", comment, env=self.env, check=False)
            update_result = run_harness(root, "adapter", "confirm", "--id", update, env=self.env, check=False)
            comment_status = db_status(root, comment)
            update_status = db_status(root, update)

        queries = [str(request.get("query", "")) for request in server.requests]
        self.assertNotEqual(comment_result.returncode, 0)
        self.assertNotEqual(update_result.returncode, 0)
        self.assertIn("connector scope mismatch", comment_result.stdout + comment_result.stderr)
        self.assertIn("connector scope mismatch", update_result.stdout + update_result.stderr)
        self.assertFalse(any("mutation" in query for query in queries))
        self.assertEqual(comment_status, "planned")
        self.assertEqual(update_status, "planned")

    def test_matching_issue_scope_allows_comment_after_metadata_check(self) -> None:
        with tempfile.TemporaryDirectory() as temp, FakeLinearServer() as server:
            root = Path(temp)
            self.initialize(root, server)
            action = plan_linear_action(root, "linear.issue.comment", "ISSUE-A", key="linear-scope:matching")

            result = run_harness(root, "adapter", "confirm", "--id", action, env=self.env)
            status = db_status(root, action)

        queries = [str(request.get("query", "")) for request in server.requests]
        self.assertIn("OK: adapter action confirmed", result.stdout)
        self.assertEqual(status, "completed")
        self.assertTrue(any("IssueScope" in query for query in queries))
        self.assertEqual(sum("CommentCreate" in query for query in queries), 1)

    def test_unconfirmed_issue_metadata_fails_closed_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp, FakeLinearServer() as server:
            root = Path(temp)
            self.initialize(root, server)
            action = plan_linear_action(root, "linear.issue.update", "ISSUE-MISSING", key="linear-scope:missing")

            result = run_harness(root, "adapter", "confirm", "--id", action, env=self.env, check=False)
            status = db_status(root, action)

        queries = [str(request.get("query", "")) for request in server.requests]
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("scope could not be confirmed", result.stdout + result.stderr)
        self.assertFalse(any("IssueUpdate" in query for query in queries))
        self.assertEqual(status, "planned")

    def test_unknown_recovery_checks_issue_scope_before_marker_search(self) -> None:
        with tempfile.TemporaryDirectory() as temp, FakeLinearServer() as server:
            root = Path(temp)
            self.initialize(root, server)
            action = plan_linear_action(root, "linear.issue.comment", "ISSUE-B", key="linear-scope:recovery")
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute("update adapter_actions set status = 'unknown' where id = ?", (action,))
                conn.commit()

            result = run_harness(root, "adapter", "reconcile", env=self.env, check=False)
            status = db_status(root, action)

        queries = [str(request.get("query", "")) for request in server.requests]
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("connector scope mismatch", result.stdout + result.stderr)
        self.assertFalse(any("query Search" in query for query in queries))
        self.assertFalse(any("CommentCreate" in query for query in queries))
        self.assertEqual(status, "unknown")


if __name__ == "__main__":
    unittest.main()

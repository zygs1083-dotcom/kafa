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


def db_action(root: Path, action_id: str) -> sqlite3.Row:
    with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("select * from adapter_actions where id = ?", (action_id,)).fetchone()


def plan_notion_page(root: Path, key: str) -> str:
    payload = {
        "execute": True,
        "operation": "notion.page.create",
        "params": {
            "parent_page_id": "PARENT",
            "title": "Custom page",
            "children": [
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": [{"type": "text", "text": {"content": "Caller-owned content"}}]},
                }
            ],
        },
    }
    result = run_harness(
        root,
        "adapter",
        "plan",
        "--tool",
        "notion",
        "--mode",
        "write-confirm",
        "--artifact",
        "Notion page",
        "--action",
        "notion.page.create",
        "--payload-json",
        json.dumps(payload, sort_keys=True),
        "--idempotency-key",
        key,
    )
    return result.stdout.strip().split()[-1]


class NotionAmbiguousHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []
    stored_page: dict[str, object] | None = None
    disconnect_after_create = False

    @classmethod
    def reset(cls, *, disconnect_after_create: bool) -> None:
        cls.requests = []
        cls.stored_page = None
        cls.disconnect_after_create = disconnect_after_create

    def do_POST(self) -> None:  # noqa: N802
        body_text = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode("utf-8")
        body = json.loads(body_text) if body_text else {}
        self.__class__.requests.append({"path": self.path, "body": body})
        if self.path == "/notion/v1/pages":
            self.__class__.stored_page = body
            if self.__class__.disconnect_after_create:
                self.__class__.disconnect_after_create = False
                self.connection.shutdown(2)
                self.connection.close()
                return
            self._json({"id": "PAGE-1", "url": "https://notion.example/PAGE-1"})
            return
        if self.path == "/notion/v1/search":
            query = str(body.get("query", ""))
            page = self.__class__.stored_page
            title = ""
            if isinstance(page, dict):
                title_items = page.get("properties", {}).get("title", {}).get("title", [])
                if isinstance(title_items, list) and title_items:
                    title = str(title_items[0].get("text", {}).get("content", ""))
            results = [{"id": "PAGE-1", "url": "https://notion.example/PAGE-1"}] if query and query in title else []
            self._json({"results": results})
            return
        self._json({"error": "unexpected"}, status=404)

    def _json(self, payload: dict[str, object], *, status: int = 200) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class FakeNotionServer:
    def __init__(self, *, disconnect_after_create: bool = False) -> None:
        self.disconnect_after_create = disconnect_after_create

    def __enter__(self) -> "FakeNotionServer":
        NotionAmbiguousHandler.reset(disconnect_after_create=self.disconnect_after_create)
        self.server = HTTPServer(("127.0.0.1", 0), NotionAmbiguousHandler)
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
        return NotionAmbiguousHandler.requests


class NotionAmbiguousRecoveryTest(unittest.TestCase):
    def initialize(self, root: Path, server: FakeNotionServer) -> dict[str, str]:
        run_harness(root, "init")
        run_harness(root, "connector", "profile", "set", "--project-key", "project-a", "--notion-parent", "PARENT")
        return {"NOTION_TOKEN": "token", "HARNESS_NOTION_API_URL": server.base_url}

    def test_custom_children_ambiguous_success_recovers_without_second_create(self) -> None:
        with tempfile.TemporaryDirectory() as temp, FakeNotionServer(disconnect_after_create=True) as server:
            root = Path(temp)
            env = self.initialize(root, server)
            action = plan_notion_page(root, "notion-ambiguous:recover")

            first = run_harness(root, "adapter", "confirm", "--id", action, env=env, check=False)
            unknown = db_action(root, action)
            second = run_harness(root, "adapter", "confirm", "--id", action, env=env)
            completed = db_action(root, action)

        creates = [request for request in server.requests if request["path"] == "/notion/v1/pages"]
        stored = NotionAmbiguousHandler.stored_page or {}
        title = stored.get("properties", {}).get("title", {}).get("title", [])[0].get("text", {}).get("content", "")
        children_text = json.dumps(stored.get("children", []), ensure_ascii=False)
        self.assertNotEqual(first.returncode, 0)
        self.assertEqual(unknown["status"], "unknown")
        self.assertIn("OK: adapter action confirmed", second.stdout)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(len(creates), 1)
        self.assertIn("codex-project-harness:project-key=project-a", title)
        self.assertIn("codex-project-harness:idempotency-key=notion-ambiguous:recover", title)
        self.assertIn("codex-project-harness:idempotency-key=notion-ambiguous:recover", children_text)

    def test_unknown_marker_miss_remains_unknown_without_create(self) -> None:
        with tempfile.TemporaryDirectory() as temp, FakeNotionServer() as server:
            root = Path(temp)
            env = self.initialize(root, server)
            action = plan_notion_page(root, "notion-ambiguous:miss")
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute("update adapter_actions set status = 'unknown', blocked_reason = 'ambiguous outcome' where id = ?", (action,))
                conn.commit()

            result = run_harness(root, "adapter", "confirm", "--id", action, env=env, check=False)
            row = db_action(root, action)

        creates = [request for request in server.requests if request["path"] == "/notion/v1/pages"]
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("remains unknown", result.stdout + result.stderr)
        self.assertEqual(row["status"], "unknown")
        self.assertEqual(creates, [])


if __name__ == "__main__":
    unittest.main()

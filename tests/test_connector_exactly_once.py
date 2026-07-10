from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "plugins/codex-project-harness/scripts/harness.py"
PLUGIN_ROOT = REPO_ROOT / "plugins/codex-project-harness"
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))


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


def plan_slack_action(root: Path, *, key: str = "exactly-once:slack", text: str = "Ship it", payload_json: str = "") -> str:
    payload = payload_json or json.dumps(
        {"execute": True, "operation": "slack.message.post", "params": {"channel": "C123", "text": text}},
        sort_keys=True,
    )
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


def set_slack_profile(root: Path) -> None:
    run_harness(root, "connector", "profile", "set", "--project-key", "exactly-once", "--slack-channel", "C123")


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
    def test_valid_json_cannot_collide_with_invalid_legacy_payload_hash(self) -> None:
        from core.schema_guard import adapter_action_payload_hash

        invalid_hash = adapter_action_payload_hash("slack", "write-confirm", "Slack update", "slack.message.post", "not-json")
        valid_hash = adapter_action_payload_hash(
            "slack",
            "write-confirm",
            "Slack update",
            "slack.message.post",
            '{"invalid_legacy_payload":"not-json"}',
        )

        self.assertNotEqual(valid_hash, invalid_hash)

    def test_same_idempotency_key_and_semantic_payload_reuses_action_without_mutation(self) -> None:
        key = "exactly-once:payload-same"
        canonical = json.dumps(
            {"execute": True, "operation": "slack.message.post", "params": {"channel": "C123", "text": "Ship it"}},
            sort_keys=True,
        )
        reordered = '{"params":{"text":"Ship it","channel":"C123"},"operation":"slack.message.post","execute":true}'
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            first = plan_slack_action(root, key=key, payload_json=canonical)
            second = plan_slack_action(root, key=key, payload_json=reordered)
            row = db_one(
                root,
                "select id, payload_json, payload_hash, status from adapter_actions where tool = 'slack' and idempotency_key = ?",
                (key,),
            )
            events = db_one(
                root,
                "select count(*) as count from events where type = 'adapter_action_planned' and idempotency_key = ?",
                (key,),
            )["count"]

        self.assertEqual(second, first)
        self.assertEqual(row["payload_json"], canonical)
        self.assertEqual(len(row["payload_hash"]), 64)
        self.assertEqual(row["status"], "planned")
        self.assertEqual(events, 1)

    def test_request_id_uses_canonical_payload_semantics(self) -> None:
        key = "exactly-once:canonical-request"
        request_id = "REQ-CANONICAL-PAYLOAD"
        first_payload = '{"execute":true,"operation":"slack.message.post","params":{"channel":"C123","text":"Ship it"}}'
        second_payload = '{"params":{"text":"Ship it","channel":"C123"},"operation":"slack.message.post","execute":true}'
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            first = run_harness(
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
                first_payload,
                "--idempotency-key",
                key,
                "--request-id",
                request_id,
            )
            second = run_harness(
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
                second_payload,
                "--idempotency-key",
                key,
                "--request-id",
                request_id,
            )

        self.assertEqual(second.stdout, first.stdout)

    def test_same_idempotency_key_with_different_payload_conflicts(self) -> None:
        key = "exactly-once:payload-conflict"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            action = plan_slack_action(root, key=key, text="Ship it")
            before = db_one(root, "select * from adapter_actions where id = ?", (action,))
            conflict = run_harness(
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
                json.dumps(
                    {"execute": True, "operation": "slack.message.post", "params": {"channel": "C123", "text": "Different"}},
                    sort_keys=True,
                ),
                "--idempotency-key",
                key,
                check=False,
            )
            after = db_one(root, "select * from adapter_actions where id = ?", (action,))

        self.assertNotEqual(conflict.returncode, 0)
        self.assertIn("idempotency-conflict", conflict.stdout + conflict.stderr)
        self.assertEqual(dict(after), dict(before))

    def test_completed_action_is_immutable_under_replan(self) -> None:
        key = "exactly-once:completed-immutable"
        with tempfile.TemporaryDirectory() as temp, FakeSlackServer() as server:
            root = Path(temp)
            run_harness(root, "init")
            set_slack_profile(root)
            action = plan_slack_action(root, key=key)
            run_harness(
                root,
                "adapter",
                "confirm",
                "--id",
                action,
                env={"SLACK_BOT_TOKEN": "token", "HARNESS_SLACK_API_URL": server.base_url},
            )
            before = db_one(root, "select * from adapter_actions where id = ?", (action,))
            conflict = run_harness(
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
                json.dumps(
                    {"execute": True, "operation": "slack.message.post", "params": {"channel": "C123", "text": "Overwrite"}},
                    sort_keys=True,
                ),
                "--idempotency-key",
                key,
                check=False,
            )
            after = db_one(root, "select * from adapter_actions where id = ?", (action,))

        self.assertNotEqual(conflict.returncode, 0)
        self.assertIn("idempotency-conflict", conflict.stdout + conflict.stderr)
        self.assertEqual(after["status"], "completed")
        self.assertEqual(after["external_id"], before["external_id"])
        self.assertEqual(dict(after), dict(before))

    def test_existing_schema29_action_backfills_missing_payload_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            action = plan_slack_action(root, key="exactly-once:legacy-hash")
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute("update adapter_actions set payload_hash = '' where id = ?", (action,))
                conn.commit()

            retried = plan_slack_action(root, key="exactly-once:legacy-hash")
            row = db_one(root, "select payload_hash from adapter_actions where id = ?", (action,))

        self.assertEqual(retried, action)
        self.assertEqual(len(row["payload_hash"]), 64)

    def test_structural_upgrade_backfills_hash_only_when_column_was_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            action = plan_slack_action(root, key="exactly-once:structural-upgrade")
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute("alter table adapter_actions drop column payload_hash")
                conn.commit()

            run_harness(root, "connector", "profile", "status", "--json")
            row = db_one(root, "select payload_hash from adapter_actions where id = ?", (action,))

        self.assertEqual(len(row["payload_hash"]), 64)

    def test_payload_hash_tampering_blocks_remote_execution_and_invariant(self) -> None:
        with tempfile.TemporaryDirectory() as temp, FakeSlackServer() as server:
            root = Path(temp)
            run_harness(root, "init")
            set_slack_profile(root)
            action = plan_slack_action(root, key="exactly-once:tamper")
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute(
                    "update adapter_actions set payload_json = ? where id = ?",
                    (
                        json.dumps(
                            {
                                "execute": True,
                                "operation": "slack.message.post",
                                "params": {"channel": "C123", "text": "Tampered"},
                            },
                            sort_keys=True,
                        ),
                        action,
                    ),
                )
                conn.commit()

            confirmed = run_harness(
                root,
                "adapter",
                "confirm",
                "--id",
                action,
                env={"SLACK_BOT_TOKEN": "token", "HARNESS_SLACK_API_URL": server.base_url},
                check=False,
            )
            invariant = run_harness(root, "invariant", "validate", check=False)
            row = db_one(root, "select status from adapter_actions where id = ?", (action,))

        self.assertNotEqual(confirmed.returncode, 0)
        self.assertIn("payload hash mismatch", confirmed.stdout + confirmed.stderr)
        self.assertEqual(server.requests, [])
        self.assertEqual(row["status"], "planned")
        self.assertNotEqual(invariant.returncode, 0)
        self.assertIn("payload hash mismatch", invariant.stdout + invariant.stderr)

    def test_blank_payload_hash_is_not_self_healed_before_remote_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temp, FakeSlackServer() as server:
            root = Path(temp)
            run_harness(root, "init")
            set_slack_profile(root)
            action = plan_slack_action(root, key="exactly-once:blank-hash-tamper")
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute(
                    "update adapter_actions set payload_json = ?, payload_hash = '' where id = ?",
                    (
                        json.dumps(
                            {
                                "execute": True,
                                "operation": "slack.message.post",
                                "params": {"channel": "C123", "text": "Tampered and blanked"},
                            },
                            sort_keys=True,
                        ),
                        action,
                    ),
                )
                conn.commit()

            confirmed = run_harness(
                root,
                "adapter",
                "confirm",
                "--id",
                action,
                env={"SLACK_BOT_TOKEN": "token", "HARNESS_SLACK_API_URL": server.base_url},
                check=False,
            )
            row = db_one(root, "select status, payload_hash from adapter_actions where id = ?", (action,))

        self.assertNotEqual(confirmed.returncode, 0)
        self.assertIn("payload hash mismatch", confirmed.stdout + confirmed.stderr)
        self.assertEqual(server.requests, [])
        self.assertEqual(row["status"], "planned")
        self.assertEqual(row["payload_hash"], "")

    def test_completed_action_cannot_be_reopened_or_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            action = plan_slack_action(root, key="exactly-once:completed-transition")
            run_harness(root, "adapter", "complete", "--id", action, "--external-id", "MSG-1", "--external-link", "https://example.invalid/MSG-1")

            reopened = run_harness(root, "adapter", "draft", "--id", action, check=False)
            rewritten = run_harness(
                root,
                "adapter",
                "complete",
                "--id",
                action,
                "--external-id",
                "MSG-2",
                "--external-link",
                "https://example.invalid/MSG-2",
                check=False,
            )
            row = db_one(root, "select status, external_id, external_link from adapter_actions where id = ?", (action,))

        self.assertNotEqual(reopened.returncode, 0)
        self.assertIn("immutable after completion", reopened.stdout + reopened.stderr)
        self.assertNotEqual(rewritten.returncode, 0)
        self.assertIn("immutable after completion", rewritten.stdout + rewritten.stderr)
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["external_id"], "MSG-1")
        self.assertEqual(row["external_link"], "https://example.invalid/MSG-1")

    def test_reconcile_rejects_completed_action_with_tampered_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            action = plan_slack_action(root, key="exactly-once:completed-reconcile")
            run_harness(root, "adapter", "complete", "--id", action, "--external-id", "MSG-1")
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute("update adapter_actions set payload_json = '{\"tampered\":true}' where id = ?", (action,))
                conn.commit()

            reconciled = run_harness(root, "adapter", "reconcile", check=False)

        self.assertNotEqual(reconciled.returncode, 0)
        self.assertIn("payload hash mismatch", reconciled.stdout + reconciled.stderr)

    def test_concurrent_confirm_claims_action_once_before_remote_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp, FakeSlackServer(mode="slow-write") as server:
            root = Path(temp)
            run_harness(root, "init")
            set_slack_profile(root)
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
        marker = f"codex-project-harness:project-key=exactly-once\ncodex-project-harness:idempotency-key={key}"
        with tempfile.TemporaryDirectory() as temp, FakeSlackServer(marker=marker) as server:
            root = Path(temp)
            run_harness(root, "init")
            set_slack_profile(root)
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
            set_slack_profile(root)
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
            set_slack_profile(root)
            action = plan_slack_action(root, key="exactly-once:not-evidence")

            run_harness(root, "adapter", "confirm", "--id", action, env={"SLACK_BOT_TOKEN": "token", "HARNESS_SLACK_API_URL": server.base_url})

            evidence_count = db_one(root, "select count(*) as count from evidence")["count"]
            delivery = run_harness(root, "validate", "--delivery", check=False)
            self.assertEqual(evidence_count, 0)
            self.assertNotEqual(delivery.returncode, 0)

    def test_same_request_id_retry_does_not_duplicate_recovery_or_write(self) -> None:
        key = "exactly-once:request-id"
        marker = f"codex-project-harness:project-key=exactly-once\ncodex-project-harness:idempotency-key={key}"
        with tempfile.TemporaryDirectory() as temp, FakeSlackServer(marker=marker) as server:
            root = Path(temp)
            run_harness(root, "init")
            set_slack_profile(root)
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

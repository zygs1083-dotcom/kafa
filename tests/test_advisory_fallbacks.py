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


def db_rows(root: Path, query: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
    with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(query, params).fetchall()


def db_one(root: Path, query: str, params: tuple[object, ...] = ()) -> sqlite3.Row:
    rows = db_rows(root, query, params)
    if not rows:
        raise AssertionError(f"missing row for query: {query}")
    return rows[0]


def plan_action(root: Path, tool: str, operation: str, params: dict[str, object]) -> str:
    payload = json.dumps({"execute": True, "operation": operation, "params": params}, sort_keys=True)
    result = run_harness(
        root,
        "adapter",
        "plan",
        "--tool",
        tool,
        "--mode",
        "write-confirm",
        "--artifact",
        f"{tool} artifact",
        "--action",
        operation,
        "--payload-json",
        payload,
        "--idempotency-key",
        f"advisory-fallback:{tool}:{operation}",
    )
    return action_id(result.stdout)


def fake_rate_limited_gh(temp: Path) -> Path:
    bin_dir = temp / "bin"
    bin_dir.mkdir()
    script = bin_dir / "gh"
    script.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import sys
            print("HTTP 403: API rate limit exceeded; retry-after: 1; x-ratelimit-remaining: 0", file=sys.stderr)
            raise SystemExit(1)
            """
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)
    return bin_dir


class AdvisoryFallbacksTest(unittest.TestCase):
    def test_blocked_connectors_generate_advisory_fallback_rows_and_artifacts(self) -> None:
        cases = [
            ("github", "github.issue.create", {"repo": "owner/repo", "title": "Issue", "body": "Body"}, "GitHub draft"),
            ("linear", "linear.issue.create", {"team_id": "TEAM", "title": "Linear issue", "description": "Body"}, "Linear task fallback"),
            ("notion", "notion.page.create", {"parent_page_id": "PARENT", "title": "Notion page", "content": "Body"}, "Notion document fallback"),
            ("figma", "figma.comment.create", {"file_key": "FILE1", "message": "Review note"}, "Product Design fallback"),
            ("slack", "slack.message.post", {"channel": "C123", "text": "Ship it"}, "Slack handoff fallback"),
        ]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            root.mkdir()
            run_harness(root, "init")
            gh_bin = fake_rate_limited_gh(Path(temp))
            for tool, operation, params, expected_kind in cases:
                action = plan_action(root, tool, operation, params)
                env = {"PATH": f"{gh_bin}:{os.environ['PATH']}"} if tool == "github" else {}

                result = run_harness(root, "adapter", "confirm", "--id", action, env=env, check=False)

                self.assertNotEqual(result.returncode, 0)
                action_row = db_one(root, "select status, connector_status from adapter_actions where id = ?", (action,))
                fallback = db_one(root, "select * from advisory_fallbacks where action_id = ?", (action,))
                artifact = root / fallback["artifact_path"]
                text = artifact.read_text(encoding="utf-8")
                self.assertEqual(action_row["status"], "blocked")
                self.assertEqual(action_row["connector_status"], "blocked")
                self.assertEqual(fallback["tool"], tool)
                self.assertEqual(fallback["operation"], operation)
                self.assertEqual(fallback["fallback_kind"], expected_kind)
                self.assertEqual(fallback["delivery_eligible"], 0)
                self.assertEqual(fallback["status"], "generated")
                self.assertTrue(artifact.exists())
                self.assertIn("Not delivery evidence", text)
                self.assertIn(fallback["official_capability"], text)
                self.assertIn("Copy-ready draft", text)
                self.assertNotIn("HARNESS_CONNECTOR_KEY", text)
                self.assertNotIn("token", text.lower())

            projection = (root / ".ai-team/control/advisory-fallbacks.md").read_text(encoding="utf-8")
            self.assertIn("github.issue.create", projection)
            self.assertIn("slack.message.post", projection)
            self.assertEqual(db_one(root, "select count(*) as count from evidence")["count"], 0)
            self.assertEqual(db_one(root, "select count(*) as count from validations")["count"], 0)

    def test_request_id_retry_does_not_duplicate_fallback_budget_or_finding(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            root.mkdir()
            run_harness(root, "init")
            action = plan_action(root, "slack", "slack.message.post", {"channel": "C123", "text": "Send me"})

            first = run_harness(root, "adapter", "confirm", "--id", action, "--request-id", "REQ-ADVISORY-1", check=False)
            second = run_harness(root, "adapter", "confirm", "--id", action, "--request-id", "REQ-ADVISORY-1", check=False)
            conflict = run_harness(root, "adapter", "confirm", "--id", action, "--request-id", "REQ-ADVISORY-1", "--confirmation", "changed", check=False)

            self.assertNotEqual(first.returncode, 0)
            self.assertEqual(second.stdout, first.stdout)
            self.assertIn("idempotency-conflict", conflict.stdout)
            self.assertEqual(db_one(root, "select count(*) as count from advisory_fallbacks where action_id = ?", (action,))["count"], 1)
            self.assertEqual(db_one(root, "select count(*) as count from connector_budgets where tool = 'slack'")["count"], 1)
            self.assertEqual(db_one(root, "select count(*) as count from findings where surface = 'connector'")["count"], 1)

    def test_advisory_fallback_does_not_satisfy_delivery_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            root.mkdir()
            run_harness(root, "init")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Works")
            action = plan_action(root, "notion", "notion.page.create", {"parent_page_id": "PARENT", "title": "Spec", "content": "Body"})
            blocked = run_harness(root, "adapter", "confirm", "--id", action, check=False)
            delivery = run_harness(root, "validate", "--delivery", check=False)

            self.assertNotEqual(blocked.returncode, 0)
            self.assertNotEqual(delivery.returncode, 0)
            self.assertIn("delivery requires validation evidence", delivery.stdout)
            self.assertEqual(db_one(root, "select count(*) as count from advisory_fallbacks")["count"], 1)
            self.assertEqual(db_one(root, "select count(*) as count from evidence")["count"], 0)


if __name__ == "__main__":
    unittest.main()

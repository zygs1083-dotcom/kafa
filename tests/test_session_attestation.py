from __future__ import annotations

import os
import hashlib
import hmac
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "plugins" / "codex-project-harness" / "scripts" / "harness.py"


def run_harness(root: Path, *args: str, check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    result = subprocess.run(
        ["python3", str(HARNESS), "--root", str(root), *args],
        text=True,
        capture_output=True,
        check=False,
        env=merged_env,
    )
    if check and result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return result


def stdout_field(stdout: str, name: str) -> str:
    return stdout.split(f"{name}=", 1)[1].split(None, 1)[0].strip()


def task_revision(root: Path, task_id: str) -> int:
    with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
        return int(conn.execute("select revision from tasks where id = ?", (task_id,)).fetchone()[0])


def bootstrap_task(root: Path) -> None:
    run_harness(root, "init")
    run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
    run_harness(root, "task", "add", "--id", "T1", "--task", "Example", "--owner", "developer", "--acceptance", "AC1")


def claim(root: Path, agent: str = "developer") -> tuple[str, str]:
    result = run_harness(root, "task", "claim", "T1", "--agent", agent, "--expected-revision", str(task_revision(root, "T1")))
    return stdout_field(result.stdout, "token"), stdout_field(result.stdout, "fence")


def review(root: Path, session_id: str | None = None) -> tuple[str, str]:
    args = ["task", "review", "T1", "--agent", "qa-reviewer", "--expected-revision", str(task_revision(root, "T1"))]
    if session_id:
        args.extend(["--session-id", session_id])
    result = run_harness(root, *args)
    return stdout_field(result.stdout, "token"), stdout_field(result.stdout, "fence")


class SessionAttestationTest(unittest.TestCase):
    def test_connector_without_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")

            result = run_harness(
                root,
                "session",
                "attest",
                "--session-id",
                "S-review",
                "--agent",
                "qa-reviewer",
                "--role",
                "qa-reviewer",
                "--context-id",
                "ctx-review",
                "--origin",
                "connector",
                "--verification-token",
                "arbitrary",
                check=False,
                env={"HARNESS_CONNECTOR_KEY": ""},
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("connector verifier key unavailable", result.stdout)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                count = conn.execute("select count(*) from session_attestations").fetchone()[0]
            self.assertEqual(count, 0)

    def test_connector_with_key_verifies_external_session_hmac(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")

            payload = "agent-session:S-review:qa-reviewer:qa-reviewer:ctx-review"
            token = hmac.new(b"test-secret", payload.encode("utf-8"), hashlib.sha256).hexdigest()
            result = run_harness(
                root,
                "session",
                "attest",
                "--session-id",
                "S-review",
                "--agent",
                "qa-reviewer",
                "--role",
                "qa-reviewer",
                "--context-id",
                "ctx-review",
                "--origin",
                "connector",
                "--verification-token",
                token,
                env={"HARNESS_CONNECTOR_KEY": "test-secret"},
            )

            self.assertIn("trust=connector", result.stdout)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                row = conn.execute("select origin, token_status, trust_level, length(verification_token) from session_attestations").fetchone()
            self.assertEqual(row, ("connector", "hmac-valid", "connector", 64))

    def test_connector_hmac_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")

            result = run_harness(
                root,
                "session",
                "attest",
                "--session-id",
                "S-review",
                "--agent",
                "qa-reviewer",
                "--role",
                "qa-reviewer",
                "--context-id",
                "ctx-review",
                "--origin",
                "connector",
                "--verification-token",
                "bad-token",
                check=False,
                env={"HARNESS_CONNECTOR_KEY": "test-secret"},
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("verification_token does not match connector HMAC", result.stdout)

    def test_submit_and_accept_require_independent_sessions_when_provided(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bootstrap_task(root)
            run_harness(root, "session", "attest", "--session-id", "S-shared", "--agent", "developer", "--role", "developer", "--context-id", "ctx-dev")
            run_harness(root, "session", "attest", "--session-id", "S-qa", "--agent", "qa-reviewer", "--role", "qa-reviewer", "--context-id", "ctx-qa")
            token, fence = claim(root)
            run_harness(root, "task", "start", "T1", "--agent", "developer", "--lease-token", token, "--expected-revision", str(task_revision(root, "T1")), "--fence", fence)
            run_harness(root, "task", "submit", "T1", "--agent", "developer", "--lease-token", token, "--expected-revision", str(task_revision(root, "T1")), "--fence", fence, "--evidence", "done", "--session-id", "S-shared")
            run_harness(root, "session", "attest", "--session-id", "S-shared", "--agent", "qa-reviewer", "--role", "qa-reviewer", "--context-id", "ctx-dev")
            bad_review = run_harness(root, "task", "review", "T1", "--agent", "qa-reviewer", "--expected-revision", str(task_revision(root, "T1")), "--session-id", "S-shared", check=False)

            self.assertNotEqual(bad_review.returncode, 0)
            self.assertIn("review-session-not-independent", bad_review.stdout)
            good_token, good_fence = review(root, "S-qa")
            run_harness(root, "task", "accept", "T1", "--agent", "qa-reviewer", "--lease-token", good_token, "--expected-revision", str(task_revision(root, "T1")), "--fence", good_fence, "--evidence", "reviewed", "--session-id", "S-qa")
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                task = conn.execute("select status, submitted_session_id, accepted_session_id from tasks where id = 'T1'").fetchone()
            self.assertEqual(task, ("accepted", "S-shared", "S-qa"))


if __name__ == "__main__":
    unittest.main()

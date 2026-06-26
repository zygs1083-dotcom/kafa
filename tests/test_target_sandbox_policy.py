from __future__ import annotations

import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "plugins/codex-project-harness/scripts/harness.py"
PLUGIN_ROOT = REPO_ROOT / "plugins/codex-project-harness"
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(PLUGIN_ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(PLUGIN_ROOT))
if str(SCRIPTS) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(SCRIPTS))

import harness_db  # noqa: E402


def run_harness(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["python3", str(HARNESS), "--root", str(root), *args], text=True, capture_output=True, check=False)
    if check and result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return result


def git_repo(root: Path) -> None:
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
    (root / "test_sample.py").write_text(
        "import unittest\n\nclass Sample(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "test_sample.py"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)


def make_reported_attempt(root: Path, *, extra_target_args: list[str] | None = None) -> str:
    run_harness(root, "init")
    run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "sandbox")
    run_harness(root, "task", "add", "--id", "T1", "--task", "sandbox", "--acceptance", "AC1")
    run_harness(
        root,
        "test-target",
        "add",
        "--id",
        "UNIT",
        "--kind",
        "unit",
        "--command-template",
        "python3 -m unittest test_sample.py",
        *(extra_target_args or []),
    )
    run_harness(root, "test-target", "link", "--task", "T1", "--target", "UNIT")
    run_id = run_harness(root, "dispatch", "plan", "--scope", "sandbox").stdout.strip().split()[-1]
    branch = f"agent/{run_id}/T1/developer"
    subprocess.run(["git", "branch", branch, "HEAD"], cwd=root, check=True, capture_output=True)
    head = subprocess.run(["git", "rev-parse", branch], cwd=root, text=True, capture_output=True, check=True).stdout.strip()
    tree = subprocess.run(["git", "rev-parse", f"{branch}^{{tree}}"], cwd=root, text=True, capture_output=True, check=True).stdout.strip()
    with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
        conn.execute("update dispatch_assignments set agent_id = 'developer', status = 'reported' where run_id = ? and task_id = 'T1'", (run_id,))
        conn.execute(
            """
            insert into task_attempts
            (id, run_id, task_id, agent_id, fence, base_commit_sha, head_commit_sha, tree_sha,
             branch_name, target_id, status, provider_session_id, agent_session_id, report_id, evidence_id, started_at, finished_at)
            values ('ATTEMPT1', ?, 'T1', 'developer', 0, ?, ?, ?, ?, 'UNIT', 'reported', '', '', '', '', 'now', '')
            """,
            (run_id, head, head, tree, branch),
        )
        conn.execute(
            """
            insert into dispatch_worktrees
            (id, run_id, task_id, agent_id, branch_name, worktree_path, status, created_at, cleaned_at)
            values ('WT1', ?, 'T1', 'developer', ?, '', 'active', 'now', '')
            """,
            (run_id, branch),
        )
        conn.commit()
    return run_id


class TargetSandboxPolicyTest(unittest.TestCase):
    def test_test_target_add_records_policy_and_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")

            run_harness(
                root,
                "test-target",
                "add",
                "--id",
                "NODE",
                "--kind",
                "unit",
                "--command-template",
                "npm test",
                "--stack-profile",
                "node",
                "--container-image",
                "node:22-slim",
                "--requires-sandbox",
                "--requires-no-network",
                "--result-format",
                "jest-json",
                "--result-path",
                "jest.json",
            )

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                row = conn.execute(
                    """
                    select stack_profile, container_image, requires_sandbox, requires_no_network,
                           result_format, result_path
                    from test_targets where id = 'NODE'
                    """
                ).fetchone()
            self.assertEqual(tuple(row), ("node", "node:22-slim", 1, 1, "jest-json", "jest.json"))

    def test_container_image_precedence_includes_target_and_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "test-target", "add", "--id", "NODE", "--kind", "unit", "--command-template", "npm test", "--stack-profile", "node", "--container-image", "node:custom")
            control = root / ".ai-team/control/container-image.txt"
            control.write_text("python:control\n", encoding="utf-8")

            self.assertEqual(harness_db.resolve_container_image(root, "cli:image", target_id="NODE"), "cli:image")
            self.assertEqual(harness_db.resolve_container_image(root, "", target_id="NODE"), "node:custom")
            run_harness(root, "test-target", "add", "--id", "NODE2", "--kind", "unit", "--command-template", "npm test", "--stack-profile", "node")
            self.assertEqual(harness_db.resolve_container_image(root, "", target_id="NODE2"), "python:control")
            control.write_text("\n", encoding="utf-8")
            self.assertEqual(harness_db.resolve_container_image(root, "", target_id="NODE2"), "node:22-bookworm-slim")

    def test_requires_sandbox_target_rejects_local_verify(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            git_repo(root)
            run_id = make_reported_attempt(root, extra_target_args=["--requires-sandbox", "--requires-no-network"])

            result = run_harness(root, "dispatch", "verify-attempt", "--run-id", run_id, "--task", "T1", check=False)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("target requires sandbox", result.stderr + result.stdout)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                self.assertEqual(conn.execute("select count(*) from evidence").fetchone()[0], 0)

    def test_requires_sandbox_target_fails_closed_when_container_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            git_repo(root)
            run_id = make_reported_attempt(root, extra_target_args=["--requires-sandbox", "--requires-no-network"])

            with mock.patch("core.agent_runner.shutil.which", return_value=None):
                with self.assertRaises(harness_db.HarnessError) as ctx:
                    harness_db.dispatch_verify_attempt(root, run_id, "T1", runner="container")

            self.assertIn("sandbox-unavailable", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

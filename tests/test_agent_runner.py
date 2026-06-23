import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "plugins/codex-project-harness/scripts/harness.py"


def run_harness(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["python3", str(HARNESS), "--root", str(root), *args], text=True, capture_output=True, check=False)
    if check and result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return result


def git_repo(root: Path) -> None:
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
    (root / "README.md").write_text("root\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)


def bootstrap_dispatch(root: Path) -> str:
    run_harness(root, "init")
    run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
    run_harness(root, "task", "add", "--id", "T1", "--task", "Example", "--acceptance", "AC1")
    result = run_harness(root, "dispatch", "plan", "--scope", "Example")
    return result.stdout.strip().split()[-1]


def add_test_target(root: Path, target_id: str, command: str) -> None:
    run_harness(root, "test-target", "add", "--id", target_id, "--kind", "unit", "--command-template", command)


def latest_agent_branch(root: Path) -> str:
    with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
        row = conn.execute("select branch_name from dispatch_worktrees order by created_at desc limit 1").fetchone()
    return row[0] if row else ""


def branch_contains_path(root: Path, branch: str, path: str) -> bool:
    if not branch:
        return False
    result = subprocess.run(["git", "cat-file", "-e", f"{branch}:{path}"], cwd=root, text=True, capture_output=True, check=False)
    return result.returncode == 0


class AgentRunnerTest(unittest.TestCase):
    def test_null_runner_default_dispatch_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bootstrap_dispatch(root)

            result = run_harness(
                root,
                "dispatch",
                "run",
                "--agent",
                "developer",
                "--command",
                "python3 -c \"print('Ran 1 tests')\"",
                "--allow-unlisted",
                "--reason",
                "test",
            )

            self.assertIn("OK: dispatch command evidence", result.stdout)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                row = conn.execute("select command, executed_count, executed_count_source from evidence order by created_at desc limit 1").fetchone()
            self.assertEqual(row[1], 1)
            self.assertEqual(row[2], "parsed")

    def test_local_process_runner_uses_worktree_and_root_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            git_repo(root)
            bootstrap_dispatch(root)

            result = run_harness(
                root,
                "dispatch",
                "run",
                "--runner",
                "local-process",
                "--agent",
                "developer",
                "--claim-file",
                "agent-a.txt",
                "--command",
                "python3 -c \"from pathlib import Path; Path('agent-a.txt').write_text('A\\\\n'); print('Ran 1 tests')\"",
                "--allow-unlisted",
                "--reason",
                "test",
            )

            self.assertIn("OK: dispatch command evidence", result.stdout)
            self.assertFalse((root / "agent-a.txt").exists())
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                evidence = conn.execute("select artifact_path, executed_count_source from evidence order by created_at desc limit 1").fetchone()
                worktree = conn.execute("select worktree_path, branch_name from dispatch_worktrees where status = 'active'").fetchone()
            self.assertEqual(evidence[1], "parsed")
            self.assertTrue((root / evidence[0]).exists())
            self.assertTrue((root / worktree[0] / "agent-a.txt").exists())
            log = subprocess.run(["git", "log", "--oneline", worktree[1], "-1"], cwd=root, text=True, capture_output=True, check=True)
            self.assertIn("Agent developer task T1", log.stdout)

    def test_local_process_runner_rejects_branch_changes_outside_active_claims(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            git_repo(root)
            run_id = bootstrap_dispatch(root)
            command = (
                "python3 -c \"from pathlib import Path; import subprocess; "
                "Path('claimed.txt').write_text('claimed\\\\n'); "
                "Path('unclaimed.txt').write_text('unclaimed\\\\n'); "
                "subprocess.run(['git', 'add', '.'], check=True); "
                "subprocess.run(['git', '-c', 'user.name=Agent', '-c', 'user.email=agent@example.invalid', 'commit', '-m', 'agent raw commit'], check=True); "
                "print('Ran 1 tests')\""
            )
            add_test_target(root, "UNIT", command)

            result = run_harness(
                root,
                "dispatch",
                "run",
                "--runner",
                "local-process",
                "--agent",
                "developer",
                "--target",
                "UNIT",
                "--claim-file",
                "claimed.txt",
                "--command",
                command,
                check=False,
            )

            branch = latest_agent_branch(root)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                statuses = conn.execute(
                    """
                    select dr.status, da.status
                    from dispatch_runs dr
                    join dispatch_assignments da on da.run_id = dr.id
                    where dr.id = ?
                    """,
                    (run_id,),
                ).fetchone()
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(statuses, ("failed", "failed"))
            self.assertFalse(branch_contains_path(root, branch, "unclaimed.txt"))

    def test_local_process_runner_with_empty_claims_rejects_code_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            git_repo(root)
            run_id = bootstrap_dispatch(root)
            command = "python3 -c \"from pathlib import Path; Path('created-without-claim.txt').write_text('change\\\\n'); print('Ran 1 tests')\""
            add_test_target(root, "UNIT", command)

            result = run_harness(
                root,
                "dispatch",
                "run",
                "--runner",
                "local-process",
                "--agent",
                "developer",
                "--target",
                "UNIT",
                "--command",
                command,
                check=False,
            )

            branch = latest_agent_branch(root)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                statuses = conn.execute(
                    """
                    select dr.status, da.status
                    from dispatch_runs dr
                    join dispatch_assignments da on da.run_id = dr.id
                    where dr.id = ?
                    """,
                    (run_id,),
                ).fetchone()
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(statuses, ("failed", "failed"))
            self.assertFalse(branch_contains_path(root, branch, "created-without-claim.txt"))

    def test_local_process_runner_request_id_replays_without_duplicate_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            git_repo(root)
            bootstrap_dispatch(root)
            args = [
                "dispatch",
                "run",
                "--runner",
                "local-process",
                "--agent",
                "developer",
                "--claim-file",
                "agent-a.txt",
                "--command",
                "python3 -c \"from pathlib import Path; Path('agent-a.txt').write_text('A\\\\n'); print('Ran 1 tests')\"",
                "--allow-unlisted",
                "--reason",
                "test",
                "--request-id",
                "REQ-runner",
            ]

            first = run_harness(root, *args)
            second = run_harness(root, *args)

            self.assertEqual(first.stdout, second.stdout)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                evidence_count = conn.execute("select count(*) from evidence where kind = 'command'").fetchone()[0]
                log_count = conn.execute("select count(*) from command_log where request_id = 'REQ-runner'").fetchone()[0]
            self.assertEqual(evidence_count, 1)
            self.assertEqual(log_count, 1)


if __name__ == "__main__":
    unittest.main()

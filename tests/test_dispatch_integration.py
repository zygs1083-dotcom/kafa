import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "plugins/codex-project-harness/scripts/harness.py"
SCRIPTS = REPO_ROOT / "plugins/codex-project-harness/scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

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
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "base.txt"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)


def commit_branch(root: Path, run_id: str, task_id: str, agent: str, file_name: str, content: str) -> tuple[str, str]:
    branch = f"agent/{run_id}/{task_id}/{agent}"
    worktree = root / ".ai-team/runtime/worktrees" / run_id / task_id / agent
    worktree.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "worktree", "add", "-B", branch, str(worktree), "HEAD"], cwd=root, check=True, capture_output=True)
    target = worktree / file_name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", file_name], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-m", f"{task_id}"], cwd=worktree, check=True, capture_output=True)
    return branch, worktree.relative_to(root).as_posix()


def branch_head_and_tree(root: Path, branch: str) -> tuple[str, str]:
    head = subprocess.run(["git", "rev-parse", branch], cwd=root, text=True, capture_output=True, check=True).stdout.strip()
    tree = subprocess.run(["git", "rev-parse", f"{branch}^{{tree}}"], cwd=root, text=True, capture_output=True, check=True).stdout.strip()
    return head, tree


def branch_changed_files(root: Path, branch: str) -> list[str]:
    return subprocess.run(["git", "diff", "--name-only", f"HEAD..{branch}"], cwd=root, text=True, capture_output=True, check=True).stdout.splitlines()


def record_run_and_worktrees(root: Path, run_id: str, rows: list[tuple[str, str, str, str]]) -> None:
    with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
        conn.execute("insert into dispatch_runs (id, scope, status, created_at, updated_at) values (?, 'scope', 'planned', 'now', 'now')", (run_id,))
        for task_id, agent, branch, worktree in rows:
            head, tree = branch_head_and_tree(root, branch)
            conn.execute("insert or ignore into tasks (id, task, owner, status, updated_at) values (?, ?, ?, 'submitted', 'now')", (task_id, task_id, agent))
            conn.execute(
                "insert into dispatch_assignments (run_id, task_id, agent_id, status, evidence, updated_at) values (?, ?, ?, 'completed', 'EV', 'now')",
                (run_id, task_id, agent),
            )
            conn.execute(
                """
                insert into task_attempts
                (id, run_id, task_id, agent_id, fence, base_commit_sha, head_commit_sha, tree_sha,
                 branch_name, target_id, status, provider_session_id, agent_session_id, report_id, evidence_id, started_at, finished_at)
                values (?, ?, ?, ?, 0, 'HEAD', ?, ?, ?, 'UNIT', 'verified', '', '', '', 'EV', 'now', 'now')
                """,
                (f"ATTEMPT-{task_id}", run_id, task_id, agent, head, tree, branch),
            )
            conn.execute(
                """
                insert into dispatch_worktrees
                (id, run_id, task_id, agent_id, branch_name, worktree_path, status, created_at, cleaned_at)
                values (?, ?, ?, ?, ?, ?, 'active', 'now', '')
                """,
                (f"{task_id}-{agent}", run_id, task_id, agent, branch, worktree),
            )
            for path in branch_changed_files(root, branch):
                conn.execute(
                    """
                    insert into task_file_claims
                    (id, run_id, task_id, agent_id, path, worktree_path, branch_name, status, created_at, released_at)
                    values (?, ?, ?, ?, ?, ?, ?, 'active', 'now', '')
                    """,
                    (f"CLAIM-{task_id}-{path}", run_id, task_id, agent, path, worktree, branch),
                )
        conn.commit()


class DispatchIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._validate_runtime = harness_db.validate_runtime

    def tearDown(self) -> None:
        harness_db.validate_runtime = self._validate_runtime

    def test_integrate_merges_agent_branches_and_cleans_worktrees(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            git_repo(root)
            run_harness(root, "init")
            run_id = "RUN1"
            a = commit_branch(root, run_id, "T1", "developer", "a.txt", "A\n")
            b = commit_branch(root, run_id, "T2", "qa-reviewer", "b.txt", "B\n")
            record_run_and_worktrees(root, run_id, [("T1", "developer", *a), ("T2", "qa-reviewer", *b)])
            harness_db.validate_runtime = lambda _root, delivery=False: []

            target = harness_db.dispatch_integrate(root, run_id)

            self.assertEqual(target, "integration/RUN1")
            show_a = subprocess.run(["git", "show", f"{target}:a.txt"], cwd=root, text=True, capture_output=True, check=True)
            show_b = subprocess.run(["git", "show", f"{target}:b.txt"], cwd=root, text=True, capture_output=True, check=True)
            self.assertEqual(show_a.stdout, "A\n")
            self.assertEqual(show_b.stdout, "B\n")
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                status = conn.execute("select status from dispatch_runs where id = ?", (run_id,)).fetchone()[0]
                cleaned = conn.execute("select count(*) from dispatch_worktrees where status = 'cleaned'").fetchone()[0]
            self.assertEqual(status, "integrated")
            self.assertEqual(cleaned, 2)

    def test_integrate_uses_isolated_worktree_and_preserves_dirty_main_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            git_repo(root)
            run_harness(root, "init")
            run_id = "RUN4"
            a = commit_branch(root, run_id, "T1", "developer", "base.txt", "agent\n")
            record_run_and_worktrees(root, run_id, [("T1", "developer", *a)])
            harness_db.validate_runtime = lambda _root, delivery=False: []
            original_branch = subprocess.run(["git", "branch", "--show-current"], cwd=root, text=True, capture_output=True, check=True).stdout.strip()
            root_literal = root.as_posix().replace("'", "'\"'\"'")
            checkout_log = root / "root-checkout.log"
            hook = root / ".git/hooks/post-checkout"
            hook.write_text(
                f"#!/bin/sh\nROOT='{root_literal}'\nif [ \"$PWD\" = \"$ROOT\" ]; then echo root-checkout >> \"$ROOT/root-checkout.log\"; fi\n",
                encoding="utf-8",
            )
            hook.chmod(0o755)
            (root / "base.txt").write_text("user draft\n", encoding="utf-8")

            target = harness_db.dispatch_integrate(root, run_id)

            current_branch = subprocess.run(["git", "branch", "--show-current"], cwd=root, text=True, capture_output=True, check=True).stdout.strip()
            integrated_file = subprocess.run(["git", "show", f"{target}:base.txt"], cwd=root, text=True, capture_output=True, check=True)
            dirty_status = subprocess.run(["git", "status", "--short", "--", "base.txt"], cwd=root, text=True, capture_output=True, check=True)
            self.assertEqual(target, "integration/RUN4")
            self.assertEqual(current_branch, original_branch)
            self.assertEqual((root / "base.txt").read_text(encoding="utf-8"), "user draft\n")
            self.assertEqual(integrated_file.stdout, "agent\n")
            self.assertTrue(dirty_status.stdout.startswith(" M "))
            self.assertFalse(checkout_log.exists())

    def test_integrate_records_finding_on_merge_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            git_repo(root)
            run_harness(root, "init")
            run_id = "RUN2"
            a = commit_branch(root, run_id, "T1", "developer", "conflict", "A\n")
            b = commit_branch(root, run_id, "T2", "qa-reviewer", "conflict/child.txt", "B\n")
            record_run_and_worktrees(root, run_id, [("T1", "developer", *a), ("T2", "qa-reviewer", *b)])

            with self.assertRaises(harness_db.HarnessError):
                harness_db.dispatch_integrate(root, run_id)

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                status = conn.execute("select status from dispatch_runs where id = ?", (run_id,)).fetchone()[0]
                finding = conn.execute("select summary from findings where surface = 'dispatch-integration'").fetchone()[0]
            self.assertEqual(status, "integration_conflict")
            self.assertIn("merge conflict", finding)

    def test_integrate_records_verification_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            git_repo(root)
            run_harness(root, "init")
            run_id = "RUN3"
            a = commit_branch(root, run_id, "T1", "developer", "a.txt", "A\n")
            record_run_and_worktrees(root, run_id, [("T1", "developer", *a)])
            harness_db.validate_runtime = lambda _root, delivery=False: ["delivery gate failed"]

            with self.assertRaises(harness_db.HarnessError):
                harness_db.dispatch_integrate(root, run_id)

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                status = conn.execute("select status from dispatch_runs where id = ?", (run_id,)).fetchone()[0]
                finding = conn.execute("select summary from findings where surface = 'dispatch-integration'").fetchone()[0]
            self.assertEqual(status, "verification_failed")
            self.assertIn("delivery validation failed", finding)

    def test_integrate_rejects_unverified_branch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            git_repo(root)
            run_harness(root, "init")
            run_id = "RUN5"
            a = commit_branch(root, run_id, "T1", "developer", "a.txt", "A\n")
            record_run_and_worktrees(root, run_id, [("T1", "developer", *a)])
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute("update task_attempts set status = 'reported', evidence_id = '' where run_id = ? and task_id = 'T1'", (run_id,))
                conn.commit()

            with self.assertRaisesRegex(harness_db.HarnessError, "integration-unverified-branch"):
                harness_db.dispatch_integrate(root, run_id)

    def test_integrate_rejects_branch_drift_after_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            git_repo(root)
            run_harness(root, "init")
            run_id = "RUN6"
            branch, worktree = commit_branch(root, run_id, "T1", "developer", "a.txt", "A\n")
            record_run_and_worktrees(root, run_id, [("T1", "developer", branch, worktree)])
            worktree_path = root / worktree
            (worktree_path / "a.txt").write_text("drift\n", encoding="utf-8")
            subprocess.run(["git", "add", "a.txt"], cwd=worktree_path, check=True)
            subprocess.run(["git", "commit", "-m", "drift"], cwd=worktree_path, check=True, capture_output=True)

            with self.assertRaisesRegex(harness_db.HarnessError, "integration-branch-drift"):
                harness_db.dispatch_integrate(root, run_id)

    def test_integrate_rechecks_file_claim_subset(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            git_repo(root)
            run_harness(root, "init")
            run_id = "RUN7"
            a = commit_branch(root, run_id, "T1", "developer", "a.txt", "A\n")
            record_run_and_worktrees(root, run_id, [("T1", "developer", *a)])
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute("delete from task_file_claims where run_id = ? and path = 'a.txt'", (run_id,))
                conn.commit()

            with self.assertRaisesRegex(harness_db.HarnessError, "file-claim-violation"):
                harness_db.dispatch_integrate(root, run_id)


if __name__ == "__main__":
    unittest.main()

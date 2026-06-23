import json
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


def add_passing_unittest(root: Path) -> None:
    (root / "test_sample.py").write_text(
        "import unittest\n\n"
        "class SampleTest(unittest.TestCase):\n"
        "    def test_ok(self):\n"
        "        self.assertTrue(True)\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "test_sample.py"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "add test"], cwd=root, check=True, capture_output=True)


def bootstrap_provider_project(root: Path) -> str:
    run_harness(root, "init")
    run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
    run_harness(root, "test-target", "add", "--id", "UNIT", "--kind", "unit", "--command-template", "python3 -m unittest")
    run_harness(root, "task", "add", "--id", "T1", "--task", "Example", "--owner", "developer", "--acceptance", "AC1")
    run_harness(root, "test-target", "link", "--task", "T1", "--target", "UNIT")
    return run_harness(root, "dispatch", "plan", "--scope", "Provider").stdout.strip().split()[-1]


def create_branch(root: Path, branch_name: str) -> str:
    subprocess.run(["git", "switch", "-c", branch_name], cwd=root, check=True, capture_output=True)
    (root / "agent.txt").write_text("agent work\n", encoding="utf-8")
    subprocess.run(["git", "add", "agent.txt"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "agent work"], cwd=root, check=True, capture_output=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=True).stdout.strip()
    subprocess.run(["git", "switch", "master"], cwd=root, check=True, capture_output=True)
    return head


def write_fixture_report(root: Path, run_id: str, task_id: str, branch_name: str, *, status: str = "success") -> Path:
    path = root / ".ai-team/runtime/provider-fixtures" / run_id / f"{task_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "status": status,
                "last_error": "",
                "result": {
                    "command": "forged command",
                    "exit_code": 0,
                    "stdout_sha256": "0" * 64,
                    "artifact_path": ".ai-team/runtime/forged/stdout.txt",
                    "executed_count": 999,
                    "executed_count_source": "manual",
                    "source_tree_hash": "forged",
                    "branch_name": branch_name,
                    "status": "success",
                    "target_id": "UNIT",
                },
            }
        ),
        encoding="utf-8",
    )
    return path


class AgentProviderLifecycleTest(unittest.TestCase):
    def test_provider_start_uses_ready_queue_and_records_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
            run_harness(root, "task", "add", "--id", "T1", "--task", "Prerequisite", "--owner", "developer", "--acceptance", "AC1")
            run_harness(root, "task", "add", "--id", "T2", "--task", "Dependent", "--owner", "developer", "--acceptance", "AC1", "--depends-on", "T1")
            run_id = "RUN-provider-ready"
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute("insert into dispatch_runs (id, scope, status, created_at, updated_at) values (?, 'scope', 'planned', 'now', 'now')", (run_id,))
                conn.execute("insert into dispatch_assignments (run_id, task_id, capability, status, updated_at) values (?, 'T1', 'developer', 'planned', 'now')", (run_id,))
                conn.execute("insert into dispatch_assignments (run_id, task_id, capability, status, updated_at) values (?, 'T2', 'developer', 'planned', 'now')", (run_id,))
                conn.commit()

            started = run_harness(root, "dispatch", "provider", "start", "--run-id", run_id, "--provider", "fixture")

            self.assertIn("started 1 provider session", started.stdout)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                sessions = conn.execute("select task_id, provider, status, fence from agent_provider_sessions order by task_id").fetchall()
                assignments = conn.execute("select task_id, status, provider_session_id from dispatch_assignments where run_id = ? order by task_id", (run_id,)).fetchall()
            self.assertEqual(sessions, [("T1", "fixture", "running", 0)])
            self.assertTrue(assignments[0][2])
            self.assertEqual(assignments[0][:2], ("T1", "claimed"))
            self.assertEqual(assignments[1], ("T2", "planned", ""))

    def test_provider_collect_records_raw_report_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            git_repo(root)
            add_passing_unittest(root)
            run_id = bootstrap_provider_project(root)
            run_harness(root, "dispatch", "provider", "start", "--run-id", run_id, "--provider", "fixture")
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                branch = conn.execute("select branch_name from agent_provider_sessions where run_id = ? and task_id = 'T1'", (run_id,)).fetchone()[0]
            head = create_branch(root, branch)
            write_fixture_report(root, run_id, "T1", branch)

            collected = run_harness(root, "dispatch", "provider", "collect", "--run-id", run_id)

            self.assertIn("collected 1 provider report", collected.stdout)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                report_count = conn.execute("select count(*) from agent_reports where run_id = ?", (run_id,)).fetchone()[0]
                attempt = conn.execute("select status, head_commit_sha, provider_session_id from task_attempts where run_id = ? and task_id = 'T1'", (run_id,)).fetchone()
                evidence_count = conn.execute("select count(*) from evidence where id like 'CODEX-%'").fetchone()[0]
                assignment = conn.execute("select status from dispatch_assignments where run_id = ? and task_id = 'T1'", (run_id,)).fetchone()[0]
            self.assertEqual(report_count, 1)
            self.assertEqual(attempt[0], "reported")
            self.assertEqual(attempt[1], head)
            self.assertTrue(attempt[2])
            self.assertEqual(evidence_count, 0)
            self.assertEqual(assignment, "reported")

    def test_provider_attempt_is_verified_by_controller(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            git_repo(root)
            add_passing_unittest(root)
            run_id = bootstrap_provider_project(root)
            run_harness(root, "dispatch", "provider", "start", "--run-id", run_id, "--provider", "fixture")
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                branch = conn.execute("select branch_name from agent_provider_sessions where run_id = ? and task_id = 'T1'", (run_id,)).fetchone()[0]
            create_branch(root, branch)
            write_fixture_report(root, run_id, "T1", branch)
            run_harness(root, "dispatch", "provider", "collect", "--run-id", run_id)

            verified = run_harness(root, "dispatch", "verify-attempt", "--run-id", run_id, "--task", "T1")

            self.assertIn("OK: dispatch attempt verified", verified.stdout)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                evidence = conn.execute("select executed_count_source, verified_by from evidence where id like 'CODEX-%'").fetchone()
                task = conn.execute("select status from tasks where id = 'T1'").fetchone()[0]
            self.assertEqual(evidence, ("parsed", "controller-local"))
            self.assertEqual(task, "submitted")


if __name__ == "__main__":
    unittest.main()

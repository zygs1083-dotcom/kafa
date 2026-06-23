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


def bootstrap(root: Path) -> str:
    run_harness(root, "init")
    run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
    run_harness(root, "task", "add", "--id", "T1", "--task", "Example", "--owner", "developer", "--acceptance", "AC1")
    return run_harness(root, "dispatch", "plan", "--scope", "Provider").stdout.strip().split()[-1]


class AgentProviderReconcileTest(unittest.TestCase):
    def test_provider_cancel_replans_assignment_without_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_id = bootstrap(root)
            run_harness(root, "dispatch", "provider", "start", "--run-id", run_id, "--provider", "fixture")

            cancelled = run_harness(root, "dispatch", "provider", "cancel", "--run-id", run_id, "--task", "T1", "--reason", "operator stop")

            self.assertIn("cancelled 1 provider session", cancelled.stdout)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                session = conn.execute("select status, last_error from agent_provider_sessions where run_id = ?", (run_id,)).fetchone()
                assignment = conn.execute("select status, agent_id, provider_session_id from dispatch_assignments where run_id = ?", (run_id,)).fetchone()
                evidence_count = conn.execute("select count(*) from evidence where id like 'CODEX-%'").fetchone()[0]
            self.assertEqual(session, ("cancelled", "operator stop"))
            self.assertEqual(assignment, ("planned", "", ""))
            self.assertEqual(evidence_count, 0)

    def test_provider_reconcile_only_recovers_expired_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_id = bootstrap(root)
            run_harness(root, "dispatch", "provider", "start", "--run-id", run_id, "--provider", "fixture")

            fresh = run_harness(root, "dispatch", "provider", "reconcile", "--run-id", run_id)

            self.assertIn("reconciled 0 provider session", fresh.stdout)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute(
                    "update agent_provider_sessions set lease_expires_at = '2000-01-01T00:00:00+00:00' where run_id = ?",
                    (run_id,),
                )
                conn.commit()

            expired = run_harness(root, "dispatch", "provider", "reconcile", "--run-id", run_id)

            self.assertIn("reconciled 1 provider session", expired.stdout)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                session = conn.execute("select status from agent_provider_sessions where run_id = ?", (run_id,)).fetchone()[0]
                assignment = conn.execute("select status, provider_session_id from dispatch_assignments where run_id = ?", (run_id,)).fetchone()
            self.assertEqual(session, "timed_out")
            self.assertEqual(assignment, ("planned", ""))

    def test_late_report_from_cancelled_session_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_id = bootstrap(root)
            run_harness(root, "dispatch", "provider", "start", "--run-id", run_id, "--provider", "fixture")
            run_harness(root, "dispatch", "provider", "cancel", "--run-id", run_id, "--task", "T1")

            collected = run_harness(root, "dispatch", "provider", "collect", "--run-id", run_id)

            self.assertIn("collected 0 provider report", collected.stdout)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                reports = conn.execute("select count(*) from agent_reports where run_id = ?", (run_id,)).fetchone()[0]
                attempts = conn.execute("select count(*) from task_attempts where run_id = ?", (run_id,)).fetchone()[0]
            self.assertEqual(reports, 0)
            self.assertEqual(attempts, 0)


if __name__ == "__main__":
    unittest.main()

import csv
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


def bootstrap_dependency_project(root: Path) -> None:
    run_harness(root, "init")
    run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
    run_harness(root, "task", "add", "--id", "T1", "--task", "Prerequisite", "--owner", "prereq-capability", "--acceptance", "AC1")
    run_harness(root, "task", "add", "--id", "T2", "--task", "Dependent", "--owner", "developer", "--acceptance", "AC1", "--depends-on", "T1")


def dispatch_assignment_task_ids(root: Path, run_id: str) -> list[str]:
    with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
        return [row[0] for row in conn.execute("select task_id from dispatch_assignments where run_id = ? order by task_id", (run_id,))]


def insert_dispatch_run(root: Path, run_id: str) -> None:
    with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
        conn.execute(
            "insert into dispatch_runs (id, scope, status, created_at, updated_at) values (?, 'scope', 'planned', 'now', 'now')",
            (run_id,),
        )
        conn.execute(
            "insert into dispatch_assignments (run_id, task_id, capability, status, updated_at) values (?, 'T1', 'prereq-capability', 'planned', 'now')",
            (run_id,),
        )
        conn.execute(
            "insert into dispatch_assignments (run_id, task_id, capability, status, updated_at) values (?, 'T2', 'developer', 'planned', 'now')",
            (run_id,),
        )
        conn.commit()


def exported_task_ids(root: Path, run_id: str) -> list[str]:
    input_csv = root / ".ai-team/runtime/codex-fanout" / run_id / "input.csv"
    with input_csv.open(encoding="utf-8") as handle:
        return [row["item_id"] for row in csv.DictReader(handle)]


class DispatchSchedulingTest(unittest.TestCase):
    def test_dispatch_plan_uses_ready_queue_for_dependency_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bootstrap_dependency_project(root)

            planned = run_harness(root, "dispatch", "plan", "--scope", "Dependency scheduling")
            run_id = planned.stdout.strip().split()[-1]

            self.assertEqual(dispatch_assignment_task_ids(root, run_id), ["T1"])

    def test_dispatch_export_csv_uses_ready_queue_for_dependency_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bootstrap_dependency_project(root)
            run_harness(root, "test-target", "add", "--id", "UNIT", "--kind", "unit", "--command-template", "python3 -m unittest")
            run_id = "RUN-export-ready-queue"
            insert_dispatch_run(root, run_id)

            run_harness(root, "dispatch", "export-csv", run_id)

            self.assertEqual(exported_task_ids(root, run_id), ["T1"])

    def test_dispatch_claim_next_uses_ready_queue_for_dependency_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bootstrap_dependency_project(root)
            run_harness(root, "dispatch", "plan", "--scope", "Dependency scheduling")

            claimed = run_harness(root, "dispatch", "claim-next", "--agent", "developer", check=False)

            self.assertNotEqual(claimed.returncode, 0)
            self.assertIn("no dispatch assignment for agent: developer", claimed.stdout)

    def test_dispatch_recover_stale_keeps_unexpired_claimed_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
            run_harness(root, "task", "add", "--id", "T1", "--task", "Claimed work", "--owner", "developer", "--acceptance", "AC1")
            run_id = run_harness(root, "dispatch", "plan", "--scope", "Lease recovery").stdout.strip().split()[-1]
            run_harness(root, "dispatch", "claim-next", "--agent", "developer")

            recovered = run_harness(root, "dispatch", "recover-stale")

            self.assertIn("recovered 0 stale", recovered.stdout)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                row = conn.execute("select agent_id, status, claimed_at from dispatch_assignments where run_id = ? and task_id = 'T1'", (run_id,)).fetchone()
            self.assertEqual(row[0], "developer")
            self.assertEqual(row[1], "claimed")
            self.assertTrue(row[2])

    def test_dispatch_recover_stale_recovers_expired_claimed_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
            run_harness(root, "task", "add", "--id", "T1", "--task", "Expired work", "--owner", "developer", "--acceptance", "AC1")
            run_id = run_harness(root, "dispatch", "plan", "--scope", "Lease recovery").stdout.strip().split()[-1]
            run_harness(root, "dispatch", "claim-next", "--agent", "developer")
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                columns = {row[1] for row in conn.execute("pragma table_info(dispatch_assignments)")}
                self.assertIn("lease_expires_at", columns)
                conn.execute(
                    """
                    update dispatch_assignments
                    set claimed_at = '2000-01-01T00:00:00+00:00',
                        lease_expires_at = '2000-01-01T00:00:00+00:00'
                    where run_id = ? and task_id = 'T1'
                    """,
                    (run_id,),
                )
                conn.commit()

            recovered = run_harness(root, "dispatch", "recover-stale")

            self.assertIn("recovered 1 stale", recovered.stdout)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                row = conn.execute("select agent_id, status, claimed_at, lease_expires_at from dispatch_assignments where run_id = ? and task_id = 'T1'", (run_id,)).fetchone()
            self.assertEqual(row, ("", "planned", None, None))


if __name__ == "__main__":
    unittest.main()

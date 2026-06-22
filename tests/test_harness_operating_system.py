from __future__ import annotations

import sqlite3
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "plugins" / "codex-project-harness" / "scripts" / "harness.py"


def run_harness(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(HARNESS), "--root", str(root), *args],
        text=True,
        capture_output=True,
        check=check,
    )


class HarnessOperatingSystemTest(unittest.TestCase):
    def test_init_creates_sqlite_state_and_installs_agents(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            dry_run = run_harness(root, "init", "--dry-run")
            result = run_harness(root, "init")

            db = root / ".ai-team/state/harness.db"
            self.assertIn("DRY-RUN", dry_run.stdout)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(db.exists())
            self.assertTrue((root / ".codex/agents/developer.toml").exists())
            with sqlite3.connect(db) as conn:
                project = conn.execute("select schema_version, runtime_version from project").fetchone()
                tables = {
                    row[0]
                    for row in conn.execute("select name from sqlite_master where type='table'").fetchall()
                }
            self.assertEqual(project[0], 2)
            self.assertIn("tasks", tables)
            self.assertIn("events", tables)

    def test_legacy_init_also_creates_sqlite_state(self) -> None:
        legacy_init = REPO_ROOT / "plugins" / "codex-project-harness" / "scripts" / "init_project_harness.py"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = subprocess.run(["python3", str(legacy_init)], cwd=root, text=True, capture_output=True, check=False)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((root / ".ai-team/state/harness.db").exists())

    def test_phase_transition_graph_rejects_illegal_jump(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")

            result = run_harness(root, "phase", "delivery_readiness", check=False)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("illegal phase transition", result.stdout)

    def test_scheduler_ready_queue_respects_dependencies_and_cycle_detection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
            run_harness(root, "task", "add", "--id", "T1", "--task", "First", "--acceptance", "AC1")
            duplicate = run_harness(root, "task", "add", "--id", "T1", "--task", "Duplicate", "--acceptance", "AC1", check=False)
            run_harness(root, "task", "add", "--id", "T2", "--task", "Second", "--acceptance", "AC1", "--depends-on", "T1")

            next_before = run_harness(root, "task", "next")
            premature_start = run_harness(root, "task", "start", "T2", "--agent", "developer", check=False)
            run_harness(root, "task", "start", "T1", "--agent", "developer")
            run_harness(root, "task", "complete", "T1", "--evidence", "done")
            next_after = run_harness(root, "task", "next")
            cycle = run_harness(root, "task", "update", "T1", "--depends-on", "T2", check=False)

            self.assertNotEqual(duplicate.returncode, 0)
            self.assertIn("duplicate task id", duplicate.stdout)
            self.assertIn("T1", next_before.stdout)
            self.assertNotIn("T2", next_before.stdout)
            self.assertIn("T2", next_after.stdout)
            self.assertNotEqual(cycle.returncode, 0)
            self.assertIn("cycle", cycle.stdout)
            self.assertNotEqual(premature_start.returncode, 0)
            self.assertIn("dependencies are not accepted", premature_start.stdout)

    def test_task_claim_uses_lease_and_expected_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
            run_harness(root, "task", "add", "--id", "T1", "--task", "First", "--acceptance", "AC1")

            claim = run_harness(root, "task", "claim", "T1", "--agent", "developer", "--expected-revision", "1")
            stale = run_harness(root, "task", "claim", "T1", "--agent", "qa-reviewer", "--expected-revision", "1", check=False)
            run_harness(root, "task", "release", "T1", "--agent", "developer")
            fresh = run_harness(root, "task", "claim", "T1", "--agent", "qa-reviewer", "--expected-revision", "3")

            self.assertIn("claimed", claim.stdout)
            self.assertNotEqual(stale.returncode, 0)
            self.assertIn("revision mismatch", stale.stdout)
            self.assertIn("claimed", fresh.stdout)

    def test_doctor_repair_migrate_and_adapter_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            doctor_before = run_harness(root, "doctor", check=False)
            repair_result = run_harness(root, "repair")
            run_harness(root, "migrate", "--from-version", "1", "--to-version", "2")
            run_harness(
                root,
                "adapter",
                "record",
                "--tool",
                "github",
                "--mode",
                "read-only",
                "--artifact",
                "Tasks",
                "--external-id",
                "issue-1",
                "--idempotency-key",
                "codex-project-harness:test:task:T1",
            )
            run_harness(
                root,
                "delivery",
                "record",
                "--scope",
                "Example delivery",
                "--quality-gate",
                "independent_qa pass",
                "--failure-mode-coverage",
                "FM1 covered",
            )
            doctor_after = run_harness(root, "doctor")

            self.assertNotEqual(doctor_before.returncode, 0)
            self.assertIn("OK: repair complete", repair_result.stdout)
            self.assertIn("OK: harness doctor passed", doctor_after.stdout)
            with sqlite3.connect(root / ".ai-team/state/harness.db") as conn:
                adapter = conn.execute("select tool, mode, idempotency_key from adapters").fetchone()
                delivery = conn.execute("select scope, quality_gate from deliveries").fetchone()
                latest_event = conn.execute("select sequence from events order by sequence desc limit 1").fetchone()
            self.assertEqual(adapter, ("github", "read-only", "codex-project-harness:test:task:T1"))
            self.assertEqual(delivery, ("Example delivery", "independent_qa pass"))
            self.assertGreaterEqual(latest_event[0], 1)

    def test_concurrent_adapter_writes_do_not_lose_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")

            def write_adapter(index: int) -> int:
                result = run_harness(
                    root,
                    "adapter",
                    "record",
                    "--tool",
                    "github",
                    "--mode",
                    "read-only",
                    "--artifact",
                    f"Task-{index}",
                    "--external-id",
                    f"issue-{index}",
                    "--idempotency-key",
                    f"codex-project-harness:test:task:T{index}",
                    check=False,
                )
                return result.returncode

            with ThreadPoolExecutor(max_workers=5) as executor:
                codes = list(executor.map(write_adapter, range(10)))

            with sqlite3.connect(root / ".ai-team/state/harness.db") as conn:
                adapter_count = conn.execute("select count(*) from adapters").fetchone()[0]
                event_count = conn.execute("select count(*) from events where type = 'adapter_recorded'").fetchone()[0]

            self.assertEqual(codes, [0] * 10)
            self.assertEqual(adapter_count, 10)
            self.assertEqual(event_count, 10)

    def test_unified_validate_and_delivery_phase_are_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "phase", "project_bootstrap")
            run_harness(root, "phase", "requirement_baseline")
            run_harness(root, "phase", "confirmation")
            run_harness(root, "phase", "planning")
            run_harness(root, "phase", "implementation")
            run_harness(root, "phase", "qa")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
            run_harness(root, "failure-mode", "add", "--id", "FM1", "--feature", "Example", "--scenario", "Risk", "--trigger", "bad input", "--expected", "safe", "--risk", "critical", "--acceptance", "AC1")
            run_harness(root, "task", "add", "--id", "T1", "--task", "Example", "--acceptance", "AC1", "--failure-mode", "FM1")
            run_harness(root, "task", "complete", "T1", "--evidence", "done")
            run_harness(root, "validation", "record", "--surface", "Example", "--acceptance", "AC1", "--commands", "test", "--findings", "failed", "--result", "fail")
            run_harness(root, "gate", "record", "--reviewer-context", "fresh", "--result", "fail", "--commands", "test", "--evidence", "review")

            validate = run_harness(root, "validate", "--delivery", check=False)
            transition = run_harness(root, "phase", "delivery_readiness", check=False)

            self.assertNotEqual(validate.returncode, 0)
            self.assertIn("validation is not pass", validate.stdout)
            self.assertIn("critical failure mode is not closed", validate.stdout)
            self.assertIn("latest quality gate is not pass", validate.stdout)
            self.assertNotEqual(transition.returncode, 0)
            self.assertIn("delivery readiness blocked", transition.stdout)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "plugins" / "codex-project-harness" / "scripts" / "harness.py"
DEFAULT_TEST_COMMAND = "python3 -B -m unittest test_harness_dummy.py"


def run_harness(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(HARNESS), "--root", str(root), *args],
        text=True,
        capture_output=True,
        check=check,
    )


def db_path(root: Path) -> Path:
    return root / ".ai-team/state/harness.db"


def rows(root: Path, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
    with closing(sqlite3.connect(db_path(root))) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, params).fetchall()


def one(root: Path, sql: str, params: tuple[object, ...] = ()) -> sqlite3.Row:
    result = rows(root, sql, params)
    if not result:
        raise AssertionError(f"no row for query: {sql}")
    return result[0]


def token_from_stdout(stdout: str) -> str:
    return stdout.split("token=", 1)[1].split(None, 1)[0].strip()


def task_revision(root: Path, task_id: str) -> int:
    return int(one(root, "select revision from tasks where id = ?", (task_id,))["revision"])


def ensure_dummy_unittest(root: Path, *, marker: str = "ok") -> None:
    (root / "test_harness_dummy.py").write_text(
        "import unittest\n\n"
        "class HarnessDummyTest(unittest.TestCase):\n"
        "    def test_dummy(self):\n"
        f"        self.assertEqual({marker!r}, {marker!r})\n",
        encoding="utf-8",
    )


def claim_start_submit_accept(root: Path, task_id: str = "T1") -> None:
    claim = run_harness(root, "task", "claim", task_id, "--agent", "developer", "--expected-revision", str(task_revision(root, task_id)))
    producer_token = token_from_stdout(claim.stdout)
    run_harness(
        root,
        "task",
        "start",
        task_id,
        "--agent",
        "developer",
        "--lease-token",
        producer_token,
        "--expected-revision",
        str(task_revision(root, task_id)),
    )
    run_harness(
        root,
        "task",
        "submit",
        task_id,
        "--agent",
        "developer",
        "--lease-token",
        producer_token,
        "--expected-revision",
        str(task_revision(root, task_id)),
        "--evidence",
        "done",
    )
    review = run_harness(root, "task", "review", task_id, "--agent", "qa-reviewer", "--expected-revision", str(task_revision(root, task_id)))
    reviewer_token = token_from_stdout(review.stdout)
    run_harness(
        root,
        "task",
        "accept",
        task_id,
        "--agent",
        "qa-reviewer",
        "--lease-token",
        reviewer_token,
        "--expected-revision",
        str(task_revision(root, task_id)),
        "--evidence",
        "reviewed",
    )


def prepare_delivery_project(root: Path) -> None:
    run_harness(root, "init")
    run_harness(root, "requirement", "add", "--id", "R1", "--kind", "functional", "--body", "Example")
    run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example acceptance")
    run_harness(root, "requirement", "link", "--requirement", "R1", "--acceptance", "AC1")
    run_harness(root, "task", "add", "--id", "T1", "--task", "Example task", "--acceptance", "AC1")
    claim_start_submit_accept(root)
    run_harness(root, "scope", "confirm", "--by", "project-manager", "--summary", "confirmed")
    run_harness(root, "baseline", "freeze", "--id", "B1", "--summary", "baseline")


def record_pass_validation(root: Path, *, suffix: str = "1", acceptance: str = "AC1") -> None:
    ensure_dummy_unittest(root, marker=suffix)
    target_id = f"TARGET{suffix}"
    test_id = f"TEST{suffix}"
    run_harness(root, "test-target", "add", "--id", target_id, "--kind", "unit", "--command-template", DEFAULT_TEST_COMMAND)
    evidence = run_harness(
        root,
        "dispatch",
        "run",
        "--agent",
        "developer",
        "--target",
        target_id,
        "--command",
        DEFAULT_TEST_COMMAND,
        "--code-identity",
        "content-hash",
    ).stdout.strip().rsplit(" ", 1)[-1]
    run_harness(root, "test", "record", "--id", test_id, "--surface", "Example", "--command", DEFAULT_TEST_COMMAND, "--result", "pass", "--evidence", evidence)
    run_harness(
        root,
        "validation",
        "record",
        "--surface",
        "Example",
        "--acceptance",
        acceptance,
        "--commands",
        DEFAULT_TEST_COMMAND,
        "--findings",
        "passed",
        "--result",
        "pass",
        "--test",
        test_id,
        "--evidence",
        evidence,
        "--target",
        target_id,
        "--code-identity",
        "content-hash",
    )


def record_fail_validation(root: Path, *, acceptance: str = "AC1") -> None:
    run_harness(
        root,
        "validation",
        "record",
        "--surface",
        "Example",
        "--acceptance",
        acceptance,
        "--commands",
        DEFAULT_TEST_COMMAND,
        "--findings",
        "failed",
        "--result",
        "fail",
        "--code-identity",
        "content-hash",
    )


def record_gate(root: Path, result: str = "pass") -> None:
    run_harness(root, "gate", "record", "--reviewer-context", "fresh", "--result", result, "--commands", DEFAULT_TEST_COMMAND, "--evidence", "reviewed")


def move_to_delivery_readiness(root: Path) -> subprocess.CompletedProcess[str]:
    for phase in ["project_bootstrap", "requirement_baseline", "confirmation", "planning", "implementation", "qa"]:
        run_harness(root, "phase", phase)
    return run_harness(root, "phase", "delivery_readiness", check=False)


class DeliveryCyclesTest(unittest.TestCase):
    def test_schema26_init_creates_active_current_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            run_harness(root, "init")

            project = one(root, "select schema_version, current_cycle_id, phase from project where id = 1")
            cycle = one(root, "select id, status, phase, name, goal from delivery_cycles where id = ?", ("CYCLE-current",))
        self.assertEqual(project["schema_version"], 27)
        self.assertEqual(project["current_cycle_id"], "CYCLE-current")
        self.assertEqual(project["phase"], "intake")
        self.assertEqual(cycle["status"], "active")
        self.assertEqual(cycle["phase"], "intake")
        self.assertTrue(cycle["name"])
        self.assertTrue(cycle["goal"])

    def test_cycle_start_requires_closed_current_cycle_and_resets_phase(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")

            premature = run_harness(root, "cycle", "start", "--id", "CYCLE-next", "--name", "Next", "--goal", "Iterate", check=False)
            run_harness(root, "cycle", "close", "--status", "archived")
            started = run_harness(root, "cycle", "start", "--id", "CYCLE-next", "--name", "Next", "--goal", "Iterate")
            status = json.loads(run_harness(root, "cycle", "status", "--json").stdout)
            project = one(root, "select current_cycle_id, phase from project where id = 1")

        self.assertNotEqual(premature.returncode, 0)
        self.assertIn("current cycle is not closed", premature.stdout + premature.stderr)
        self.assertIn("OK: cycle started CYCLE-next", started.stdout)
        self.assertEqual(status["id"], "CYCLE-next")
        self.assertEqual(status["status"], "active")
        self.assertEqual(status["phase"], "intake")
        self.assertEqual(project["current_cycle_id"], "CYCLE-next")
        self.assertEqual(project["phase"], "intake")

    def test_delivery_record_closes_current_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_delivery_project(root)
            record_pass_validation(root)
            record_gate(root)
            ready = move_to_delivery_readiness(root)

            run_harness(root, "delivery", "record", "--scope", "release", "--acceptance", "AC1")
            cycle = one(root, "select status, closed_at, candidate_sha from delivery_cycles where id = 'CYCLE-current'")

        self.assertEqual(ready.returncode, 0, ready.stdout + ready.stderr)
        self.assertEqual(cycle["status"], "delivered")
        self.assertTrue(cycle["closed_at"])
        self.assertTrue(cycle["candidate_sha"])

    def test_candidate_change_requires_current_candidate_validation_and_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_delivery_project(root)
            record_pass_validation(root, suffix="old")
            record_gate(root)
            (root / "new_candidate.txt").write_text("changed\n", encoding="utf-8")

            ready = move_to_delivery_readiness(root)

        self.assertNotEqual(ready.returncode, 0)
        output = ready.stdout + ready.stderr
        self.assertIn("acceptance has no passing validation for current candidate", output)
        self.assertIn("delivery requires a quality gate record for current candidate", output)

    def test_current_candidate_fail_validation_supersedes_old_pass_and_blocks_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_delivery_project(root)
            record_pass_validation(root)
            record_fail_validation(root)
            record_gate(root)

            ready = move_to_delivery_readiness(root)
            validation_rows = rows(root, "select result, validation_status, superseded_by from validations order by result")

        self.assertNotEqual(ready.returncode, 0)
        self.assertIn("validation is not pass: Example=fail", ready.stdout + ready.stderr)
        by_result = {row["result"]: row for row in validation_rows}
        self.assertEqual(by_result["pass"]["validation_status"], "superseded")
        self.assertTrue(by_result["pass"]["superseded_by"])
        self.assertEqual(by_result["fail"]["validation_status"], "active")

    def test_legacy_cycle_validation_and_invalidation_are_audit_only_after_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_delivery_project(root)
            record_fail_validation(root)
            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.execute(
                    """
                    insert into invalidations
                    (id, source_type, source_id, target_type, target_id, reason, resolved_at, created_at)
                    values ('legacy-stale', 'acceptance', 'AC1', 'validation', 'legacy', 'old stale record', null, '2026-01-01T00:00:00Z')
                    """
                )
                for table in ["requirements", "acceptance", "tasks", "validations", "quality_gates", "deliveries", "invalidations", "dispatch_runs"]:
                    conn.execute(f"update {table} set cycle_id = ''")
                conn.execute("delete from delivery_cycles")
                conn.execute("update project set schema_version = 24")
                conn.commit()

            run_harness(root, "migrate", "--from-version", "24", "--to-version", "27")
            legacy_count = one(root, "select count(*) as count from validations where cycle_id = 'CYCLE-legacy'")["count"]
            run_harness(root, "requirement", "add", "--id", "R2", "--kind", "functional", "--body", "Fresh")
            run_harness(root, "acceptance", "add", "--id", "AC2", "--criterion", "Fresh acceptance")
            run_harness(root, "requirement", "link", "--requirement", "R2", "--acceptance", "AC2")
            run_harness(root, "task", "add", "--id", "T2", "--task", "Fresh task", "--acceptance", "AC2")
            claim_start_submit_accept(root, "T2")
            run_harness(root, "baseline", "freeze", "--id", "B2", "--summary", "fresh baseline")
            record_pass_validation(root, suffix="fresh", acceptance="AC2")
            record_gate(root)
            ready = move_to_delivery_readiness(root)

        self.assertEqual(legacy_count, 1)
        self.assertEqual(ready.returncode, 0, ready.stdout + ready.stderr)
        self.assertNotIn("old stale record", ready.stdout + ready.stderr)

    def test_current_cycle_invalidation_still_blocks_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_delivery_project(root)
            record_pass_validation(root)
            record_gate(root)
            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.execute(
                    """
                    insert into invalidations
                    (id, cycle_id, source_type, source_id, target_type, target_id, reason, resolved_at, created_at)
                    values ('current-stale', 'CYCLE-current', 'acceptance', 'AC1', 'validation', 'current', 'current stale record', null, '2026-01-01T00:00:00Z')
                    """
                )
                conn.commit()

            ready = move_to_delivery_readiness(root)

        self.assertNotEqual(ready.returncode, 0)
        self.assertIn("current stale record", ready.stdout + ready.stderr)


if __name__ == "__main__":
    unittest.main()

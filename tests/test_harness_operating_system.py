from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
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


def task_revision(root: Path, task_id: str) -> int:
    with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
        return int(conn.execute("select revision from tasks where id = ?", (task_id,)).fetchone()[0])


def token_from_stdout(stdout: str) -> str:
    marker = "token="
    return stdout.split(marker, 1)[1].strip()


def claim_start_submit(root: Path, task_id: str, *, agent: str = "developer") -> None:
    claim = run_harness(root, "task", "claim", task_id, "--agent", agent, "--expected-revision", str(task_revision(root, task_id)))
    token = token_from_stdout(claim.stdout)
    run_harness(
        root,
        "task",
        "start",
        task_id,
        "--agent",
        agent,
        "--lease-token",
        token,
        "--expected-revision",
        str(task_revision(root, task_id)),
    )
    run_harness(
        root,
        "task",
        "submit",
        task_id,
        "--agent",
        agent,
        "--lease-token",
        token,
        "--expected-revision",
        str(task_revision(root, task_id)),
        "--evidence",
        "done",
    )


def review_accept(root: Path, task_id: str, *, agent: str = "qa-reviewer") -> None:
    review = run_harness(root, "task", "review", task_id, "--agent", agent, "--expected-revision", str(task_revision(root, task_id)))
    token = token_from_stdout(review.stdout)
    run_harness(
        root,
        "task",
        "accept",
        task_id,
        "--agent",
        agent,
        "--lease-token",
        token,
        "--expected-revision",
        str(task_revision(root, task_id)),
        "--evidence",
        "reviewed",
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
            with closing(sqlite3.connect(db)) as conn:
                project = conn.execute("select schema_version, runtime_version from project").fetchone()
                tables = {
                    row[0]
                    for row in conn.execute("select name from sqlite_master where type='table'").fetchall()
                }
            self.assertEqual(project[0], 7)
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
            claim_start_submit(root, "T1")
            review_accept(root, "T1")
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
            self.assertIn("required", premature_start.stderr)

    def test_task_claim_uses_lease_and_expected_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
            run_harness(root, "task", "add", "--id", "T1", "--task", "First", "--acceptance", "AC1")
            run_harness(root, "task", "add", "--id", "T2", "--task", "Second", "--acceptance", "AC1")

            claim = run_harness(root, "task", "claim", "T1", "--agent", "developer", "--expected-revision", "1")
            second_claim = run_harness(root, "task", "claim", "T2", "--agent", "developer", "--expected-revision", "1", check=False)
            stale = run_harness(root, "task", "claim", "T1", "--agent", "qa-reviewer", "--expected-revision", "1", check=False)
            token = token_from_stdout(claim.stdout)
            run_harness(root, "task", "release", "T1", "--agent", "developer", "--lease-token", token, "--expected-revision", str(task_revision(root, "T1")))
            fresh = run_harness(root, "task", "claim", "T1", "--agent", "qa-reviewer", "--expected-revision", "3")

            self.assertIn("claimed", claim.stdout)
            self.assertNotEqual(second_claim.returncode, 0)
            self.assertIn("agent already leased", second_claim.stdout)
            self.assertNotEqual(stale.returncode, 0)
            self.assertIn("revision mismatch", stale.stdout)
            self.assertIn("claimed", fresh.stdout)

    def test_task_acceptance_requires_complete_flow_and_releases_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
            add_accepted = run_harness(root, "task", "add", "--id", "T0", "--task", "Shortcut", "--acceptance", "AC1", "--status", "accepted", check=False)
            run_harness(root, "task", "add", "--id", "T1", "--task", "Real flow", "--acceptance", "AC1")
            update_accepted = run_harness(root, "task", "update", "T1", "--status", "accepted", check=False)
            complete_early = run_harness(root, "task", "complete", "T1", "--evidence", "done", check=False)
            claim_start_submit(root, "T1")
            producer_review = run_harness(root, "task", "review", "T1", "--agent", "developer", "--expected-revision", str(task_revision(root, "T1")), check=False)
            review_accept(root, "T1")

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                task = conn.execute("select status, lease_agent from tasks where id = 'T1'").fetchone()
                agent = conn.execute("select status, lease_task_id from agents where id = 'developer'").fetchone()

            self.assertNotEqual(add_accepted.returncode, 0)
            self.assertIn("new tasks cannot be created as accepted", add_accepted.stdout)
            self.assertNotEqual(update_accepted.returncode, 0)
            self.assertIn("task acceptance must use task complete", update_accepted.stdout)
            self.assertNotEqual(complete_early.returncode, 0)
            self.assertIn("required", complete_early.stderr)
            self.assertNotEqual(producer_review.returncode, 0)
            self.assertIn("producer cannot review own task", producer_review.stdout)
            self.assertEqual(task, ("accepted", None))
            self.assertEqual(agent, ("available", ""))

    def test_task_start_creates_agent_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
            run_harness(root, "task", "add", "--id", "T1", "--task", "First", "--acceptance", "AC1")
            run_harness(root, "task", "add", "--id", "T2", "--task", "Second", "--acceptance", "AC1")

            claim = run_harness(root, "task", "claim", "T1", "--agent", "developer", "--expected-revision", "1")
            token = token_from_stdout(claim.stdout)
            run_harness(root, "task", "start", "T1", "--agent", "developer", "--lease-token", token, "--expected-revision", str(task_revision(root, "T1")))
            second_claim = run_harness(root, "task", "claim", "T2", "--agent", "developer", "--expected-revision", "1", check=False)
            run_harness(root, "task", "submit", "T1", "--agent", "developer", "--lease-token", token, "--expected-revision", str(task_revision(root, "T1")), "--evidence", "done")

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                agent = conn.execute("select status, lease_task_id from agents where id = 'developer'").fetchone()

            self.assertNotEqual(second_claim.returncode, 0)
            self.assertIn("agent already leased", second_claim.stdout)
            self.assertEqual(agent, ("available", ""))

    def test_lease_heartbeat_and_stale_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
            run_harness(root, "task", "add", "--id", "T1", "--task", "Lease flow", "--acceptance", "AC1")

            claim = run_harness(root, "task", "claim", "T1", "--agent", "developer", "--expected-revision", "1")
            token = token_from_stdout(claim.stdout)
            heartbeat = run_harness(root, "task", "heartbeat", "T1", "--agent", "developer", "--lease-token", token, "--expected-revision", str(task_revision(root, "T1")))
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                heartbeat_at = conn.execute("select lease_heartbeat_at from tasks where id = 'T1'").fetchone()[0]
                conn.execute("update tasks set lease_expires_at = '2000-01-01T00:00:00+00:00' where id = 'T1'")
                conn.commit()

            expired_start = run_harness(root, "task", "start", "T1", "--agent", "developer", "--lease-token", token, "--expected-revision", str(task_revision(root, "T1")), check=False)
            recover = run_harness(root, "task", "recover-stale")
            fresh_claim = run_harness(root, "task", "claim", "T1", "--agent", "qa-reviewer", "--expected-revision", str(task_revision(root, "T1")))

            self.assertEqual(heartbeat.returncode, 0, heartbeat.stdout + heartbeat.stderr)
            self.assertTrue(heartbeat_at)
            self.assertNotEqual(expired_start.returncode, 0)
            self.assertIn("lease expired", expired_start.stdout)
            self.assertIn("recovered 1 stale lease", recover.stdout)
            self.assertIn("claimed", fresh_claim.stdout)

    def test_doctor_repair_migrate_and_adapter_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            doctor_before = run_harness(root, "doctor", check=False)
            repair_result = run_harness(root, "repair")
            run_harness(root, "migrate", "--from-version", "6", "--to-version", "7")
            run_harness(root, "validation", "record", "--surface", "Smoke", "--commands", "true", "--findings", "passed", "--result", "pass")
            run_harness(root, "gate", "record", "--reviewer-context", "fresh", "--result", "pass", "--commands", "true", "--evidence", "reviewed")
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
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                adapter = conn.execute("select tool, mode, idempotency_key from adapters").fetchone()
                delivery = conn.execute("select scope, quality_gate from deliveries").fetchone()
                latest_event = conn.execute("select sequence from events order by sequence desc limit 1").fetchone()
            self.assertEqual(adapter, ("github", "read-only", "codex-project-harness:test:task:T1"))
            self.assertEqual(delivery, ("Example delivery", "independent_qa pass"))
            self.assertGreaterEqual(latest_event[0], 1)

    def test_doctor_validates_runtime_schema_enums_and_event_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute(
                    """
                    insert into adapters
                    (id, tool, mode, artifact, idempotency_key, updated_at)
                    values ('bad-adapter', 'github', 'teleport', 'Tasks', 'bad', 'now')
                    """
                )
                conn.execute(
                    """
                    insert into events
                    (id, schema_version, type, source, target, payload_json, created_at)
                    values ('bad-event', 5, 'bad_event', 'test', 'project', '{bad json', 'now')
                    """
                )
                conn.commit()

            result = run_harness(root, "doctor", check=False)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("invalid adapter mode", result.stdout)
            self.assertIn("invalid event payload_json", result.stdout)

    def test_delivery_record_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")

            blocked = run_harness(root, "delivery", "record", "--scope", "Premature delivery", check=False)

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                delivery_count = conn.execute("select count(*) from deliveries").fetchone()[0]

            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("delivery record blocked", blocked.stdout)
            self.assertEqual(delivery_count, 0)

    def test_phase_prerequisites_block_empty_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "phase", "project_bootstrap")
            run_harness(root, "phase", "requirement_baseline")
            run_harness(root, "requirement", "add", "--id", "R1", "--kind", "functional", "--body", "Example")

            no_acceptance = run_harness(root, "phase", "confirmation", check=False)
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
            run_harness(root, "phase", "confirmation")
            no_tasks = run_harness(root, "phase", "planning")
            no_task_implementation = run_harness(root, "phase", "implementation", check=False)
            run_harness(root, "task", "add", "--id", "T1", "--task", "Example", "--acceptance", "AC1")
            run_harness(root, "phase", "implementation")
            qa_before_submit = run_harness(root, "phase", "qa", check=False)
            claim_start_submit(root, "T1")
            qa_after_submit = run_harness(root, "phase", "qa")

            self.assertNotEqual(no_acceptance.returncode, 0)
            self.assertIn("requires at least one acceptance criterion", no_acceptance.stdout)
            self.assertEqual(no_tasks.returncode, 0, no_tasks.stdout + no_tasks.stderr)
            self.assertNotEqual(no_task_implementation.returncode, 0)
            self.assertIn("requires at least one task", no_task_implementation.stdout)
            self.assertNotEqual(qa_before_submit.returncode, 0)
            self.assertIn("submitted or accepted", qa_before_submit.stdout)
            self.assertEqual(qa_after_submit.returncode, 0, qa_after_submit.stdout + qa_after_submit.stderr)

    def test_requirement_baseline_is_structured_and_required_for_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "phase", "project_bootstrap")
            run_harness(root, "phase", "requirement_baseline")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")

            blocked = run_harness(root, "phase", "confirmation", check=False)
            added = run_harness(root, "requirement", "add", "--id", "R1", "--kind", "functional", "--body", "Users can create an item", "--priority", "must")
            allowed = run_harness(root, "phase", "confirmation")

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                requirement = conn.execute("select kind, body from requirements where id = 'R1'").fetchone()
            rendered = (root / ".ai-team/requirements/requirements.md").read_text(encoding="utf-8")

            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("requires at least one requirement baseline record", blocked.stdout)
            self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
            self.assertEqual(allowed.returncode, 0, allowed.stdout + allowed.stderr)
            self.assertEqual(requirement, ("functional", "Users can create an item"))
            self.assertIn("Users can create an item", rendered)

    def test_acceptance_change_invalidates_downstream_validation_and_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "requirement", "add", "--id", "R1", "--kind", "functional", "--body", "Example")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Original acceptance")
            run_harness(root, "requirement", "link", "--requirement", "R1", "--acceptance", "AC1")
            run_harness(root, "failure-mode", "add", "--id", "FM1", "--feature", "Example", "--scenario", "Risk", "--trigger", "bad input", "--expected", "safe", "--risk", "critical", "--acceptance", "AC1")
            run_harness(root, "task", "add", "--id", "T1", "--task", "Example", "--acceptance", "AC1", "--failure-mode", "FM1")
            claim_start_submit(root, "T1")
            review_accept(root, "T1")
            run_harness(root, "validation", "record", "--surface", "Example", "--acceptance", "AC1", "--failure-mode", "FM1", "--commands", "test", "--findings", "passed", "--result", "pass")
            run_harness(root, "gate", "record", "--reviewer-context", "fresh", "--result", "pass", "--commands", "test", "--evidence", "review")
            before = run_harness(root, "validate", "--delivery")

            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Changed acceptance")
            stale = run_harness(root, "validate", "--delivery", check=False)
            run_harness(root, "validation", "record", "--surface", "Example", "--acceptance", "AC1", "--failure-mode", "FM1", "--commands", "test", "--findings", "passed again", "--result", "pass")
            run_harness(root, "gate", "record", "--reviewer-context", "fresh", "--result", "pass", "--commands", "test", "--evidence", "review")
            after = run_harness(root, "validate", "--delivery")

            self.assertEqual(before.returncode, 0, before.stdout + before.stderr)
            self.assertNotEqual(stale.returncode, 0)
            self.assertIn("stale runtime artifact", stale.stdout)
            self.assertEqual(after.returncode, 0, after.stdout + after.stderr)

    def test_risk_acceptance_requires_scope_and_blocks_when_expired(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "requirement", "add", "--id", "R1", "--kind", "functional", "--body", "Example")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
            run_harness(root, "requirement", "link", "--requirement", "R1", "--acceptance", "AC1")
            missing_scope = run_harness(
                root,
                "failure-mode",
                "add",
                "--id",
                "FM1",
                "--feature",
                "Example",
                "--scenario",
                "Risk",
                "--trigger",
                "bad input",
                "--expected",
                "safe",
                "--risk",
                "critical",
                "--status",
                "accepted",
                "--accepted-by",
                "owner",
                "--acceptance-reason",
                "temporary",
                "--expires-at",
                "2099-01-01T00:00:00+00:00",
                check=False,
            )
            run_harness(
                root,
                "failure-mode",
                "add",
                "--id",
                "FM1",
                "--feature",
                "Example",
                "--scenario",
                "Risk",
                "--trigger",
                "bad input",
                "--expected",
                "safe",
                "--risk",
                "critical",
                "--status",
                "accepted",
                "--accepted-by",
                "owner",
                "--acceptance-reason",
                "temporary",
                "--acceptance-scope",
                "test only",
                "--expires-at",
                "2000-01-01T00:00:00+00:00",
                "--acceptance",
                "AC1",
            )
            run_harness(root, "task", "add", "--id", "T1", "--task", "Example", "--acceptance", "AC1", "--failure-mode", "FM1")
            claim_start_submit(root, "T1")
            review_accept(root, "T1")
            run_harness(root, "validation", "record", "--surface", "Example", "--acceptance", "AC1", "--commands", "test", "--findings", "passed", "--result", "pass")
            run_harness(root, "gate", "record", "--reviewer-context", "fresh", "--result", "pass", "--commands", "test", "--evidence", "review")
            validate = run_harness(root, "validate", "--delivery", check=False)

            self.assertNotEqual(missing_scope.returncode, 0)
            self.assertIn("acceptance-scope", missing_scope.stdout)
            self.assertNotEqual(validate.returncode, 0)
            self.assertIn("failure mode risk acceptance expired", validate.stdout)

    def test_failure_mode_coverage_is_derived_from_passing_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")

            manual_covered = run_harness(
                root,
                "failure-mode",
                "add",
                "--id",
                "FM1",
                "--feature",
                "Example",
                "--scenario",
                "Risk",
                "--trigger",
                "bad input",
                "--expected",
                "safe",
                "--risk",
                "critical",
                "--status",
                "covered",
                "--acceptance",
                "AC1",
                check=False,
            )
            run_harness(root, "failure-mode", "add", "--id", "FM1", "--feature", "Example", "--scenario", "Risk", "--trigger", "bad input", "--expected", "safe", "--risk", "critical", "--acceptance", "AC1")
            run_harness(root, "validation", "record", "--surface", "Example", "--acceptance", "AC1", "--failure-mode", "FM1", "--commands", "test", "--findings", "passed", "--result", "pass")

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                status = conn.execute("select status from failure_modes where id = 'FM1'").fetchone()[0]
            rendered = (root / ".ai-team/requirements/failure-modes.md").read_text(encoding="utf-8")

            self.assertNotEqual(manual_covered.returncode, 0)
            self.assertIn("invalid choice", manual_covered.stderr)
            self.assertEqual(status, "identified")
            self.assertIn("Derived Coverage", rendered)
            self.assertIn("covered", rendered)

    def test_traceability_link_is_required_for_delivery_when_requirements_exist(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "requirement", "add", "--id", "R1", "--kind", "functional", "--body", "Example")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
            run_harness(root, "task", "add", "--id", "T1", "--task", "Example", "--acceptance", "AC1")
            claim_start_submit(root, "T1")
            review_accept(root, "T1")
            run_harness(root, "validation", "record", "--surface", "Example", "--acceptance", "AC1", "--commands", "test", "--findings", "passed", "--result", "pass")
            run_harness(root, "gate", "record", "--reviewer-context", "fresh", "--result", "pass", "--commands", "test", "--evidence", "review")

            blocked = run_harness(root, "validate", "--delivery", check=False)
            run_harness(root, "requirement", "link", "--requirement", "R1", "--acceptance", "AC1")
            allowed = run_harness(root, "trace", "validate")
            trace_view = (root / ".ai-team/requirements/traceability.md").read_text(encoding="utf-8")

            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("requirement has no acceptance link", blocked.stdout)
            self.assertEqual(allowed.returncode, 0, allowed.stdout + allowed.stderr)
            self.assertIn("R1", trace_view)
            self.assertIn("AC1", trace_view)

    def test_schema_contract_checks_db_row_types(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute(
                    """
                    insert into quality_gates
                    (id, gate, reviewed_commit, reviewer_context, result, project_revision, created_at)
                    values ('bad-gate', 'qa', 'HEAD', 'fresh', 'pass', 'oops', 'now')
                    """
                )
                conn.commit()

            doctor = run_harness(root, "doctor", check=False)

            self.assertNotEqual(doctor.returncode, 0)
            self.assertIn("schema contract failed", doctor.stdout)
            self.assertIn("quality_gates.bad-gate.project_revision", doctor.stdout)

    def test_migration_and_repair_dry_run_do_not_write_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".ai-team/requirements").mkdir(parents=True)
            (root / ".ai-team/requirements/acceptance.md").write_text(
                "# Acceptance Criteria\n\n| ID | Criterion | Priority | Tool Link | Status |\n| --- | --- | --- | --- | --- |\n| AC1 | Imported acceptance | must | | active |\n",
                encoding="utf-8",
            )

            migrate = run_harness(root, "migrate", "--from-version", "markdown-v1", "--to-version", "7", "--dry-run")
            repair_plan = run_harness(root, "repair", "--dry-run")

            self.assertEqual(migrate.returncode, 0, migrate.stdout + migrate.stderr)
            self.assertEqual(repair_plan.returncode, 0, repair_plan.stdout + repair_plan.stderr)
            self.assertIn("DRY-RUN", migrate.stdout)
            self.assertIn("DRY-RUN", repair_plan.stdout)
            self.assertFalse((root / ".ai-team/state/harness.db").exists())

    def test_doctor_fails_when_runtime_state_is_tracked_by_git(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init"], cwd=root, text=True, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, text=True, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=root, text=True, capture_output=True, check=True)
            run_harness(root, "init")
            subprocess.run(["git", "add", "-f", ".ai-team/state/harness.db"], cwd=root, text=True, capture_output=True, check=True)

            doctor = run_harness(root, "doctor", check=False)

            self.assertNotEqual(doctor.returncode, 0)
            self.assertIn("runtime state is tracked by git", doctor.stdout)

    def test_task_events_include_audit_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
            run_harness(root, "task", "add", "--id", "T1", "--task", "Example", "--acceptance", "AC1")

            run_harness(root, "task", "claim", "T1", "--agent", "developer", "--expected-revision", "1")

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                payload_json = conn.execute("select payload_json from events where type = 'task_claimed'").fetchone()[0]
            data = json.loads(payload_json)

            self.assertEqual(data["entity_type"], "task")
            self.assertEqual(data["entity_id"], "T1")
            self.assertEqual(data["previous_status"], "ready")
            self.assertEqual(data["new_status"], "claimed")
            self.assertEqual(data["before"]["status"], "ready")
            self.assertEqual(data["after"]["status"], "claimed")
            self.assertTrue(data["correlation_id"])

    def test_delivery_blocks_when_validation_code_snapshot_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init"], cwd=root, text=True, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, text=True, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=root, text=True, capture_output=True, check=True)
            (root / "app.txt").write_text("v1\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.txt"], cwd=root, text=True, capture_output=True, check=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=root, text=True, capture_output=True, check=True)
            run_harness(root, "init")
            run_harness(root, "requirement", "add", "--id", "R1", "--kind", "functional", "--body", "Example")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
            run_harness(root, "requirement", "link", "--requirement", "R1", "--acceptance", "AC1")
            run_harness(root, "failure-mode", "add", "--id", "FM1", "--feature", "Example", "--scenario", "Risk", "--trigger", "bad input", "--expected", "safe", "--risk", "critical", "--acceptance", "AC1")
            run_harness(root, "task", "add", "--id", "T1", "--task", "Example", "--acceptance", "AC1", "--failure-mode", "FM1")
            claim_start_submit(root, "T1")
            review_accept(root, "T1")
            run_harness(root, "validation", "record", "--surface", "Example", "--acceptance", "AC1", "--failure-mode", "FM1", "--commands", "test", "--findings", "passed", "--result", "pass")
            (root / "app.txt").write_text("v2\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.txt"], cwd=root, text=True, capture_output=True, check=True)
            subprocess.run(["git", "commit", "-m", "change app"], cwd=root, text=True, capture_output=True, check=True)
            run_harness(root, "gate", "record", "--reviewer-context", "fresh", "--result", "pass", "--commands", "review", "--evidence", "review")

            validate = run_harness(root, "validate", "--delivery", check=False)

            self.assertNotEqual(validate.returncode, 0)
            self.assertIn("validation source tree hash does not match current code", validate.stdout)

    def test_evidence_test_and_finding_records_are_structured(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")

            evidence = run_harness(root, "evidence", "record", "--id", "EV1", "--kind", "command", "--summary", "pytest passed", "--uri", "local://pytest")
            test = run_harness(root, "test", "record", "--id", "TEST1", "--surface", "API", "--command", "pytest", "--result", "pass", "--evidence", "EV1")
            finding = run_harness(root, "finding", "record", "--id", "F1", "--surface", "API", "--severity", "medium", "--status", "open", "--summary", "Needs follow-up")

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                evidence_row = conn.execute("select kind, summary from evidence where id = 'EV1'").fetchone()
                test_row = conn.execute("select result, evidence_id from tests where id = 'TEST1'").fetchone()
                finding_row = conn.execute("select severity, status from findings where id = 'F1'").fetchone()
            rendered = (root / "docs/harness/evidence.md").read_text(encoding="utf-8")

            self.assertEqual(evidence.returncode, 0, evidence.stdout + evidence.stderr)
            self.assertEqual(test.returncode, 0, test.stdout + test.stderr)
            self.assertEqual(finding.returncode, 0, finding.stdout + finding.stderr)
            self.assertEqual(evidence_row, ("command", "pytest passed"))
            self.assertEqual(test_row, ("pass", "EV1"))
            self.assertEqual(finding_row, ("medium", "open"))
            self.assertIn("pytest passed", rendered)

    def test_quality_gate_records_code_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init"], cwd=root, text=True, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, text=True, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=root, text=True, capture_output=True, check=True)
            (root / "app.txt").write_text("hello\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.txt"], cwd=root, text=True, capture_output=True, check=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=root, text=True, capture_output=True, check=True)
            run_harness(root, "init")

            gate = run_harness(root, "gate", "record", "--reviewer-context", "fresh", "--result", "pass", "--commands", "true", "--evidence", "review")

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                row = conn.execute("select reviewed_commit, base_commit, head_commit, diff_hash, tracked_diff_hash, project_revision from quality_gates").fetchone()

            self.assertEqual(gate.returncode, 0, gate.stdout + gate.stderr)
            self.assertTrue(row[0])
            self.assertTrue(row[1])
            self.assertTrue(row[2])
            self.assertTrue(row[3])
            self.assertTrue(row[4])
            self.assertGreaterEqual(int(row[5]), 1)

    def test_adapter_mode_is_schema_enforced_by_cli(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")

            invalid = run_harness(
                root,
                "adapter",
                "record",
                "--tool",
                "github",
                "--mode",
                "surprise-write",
                "--artifact",
                "Tasks",
                "--idempotency-key",
                "adapter:test",
                check=False,
            )

            self.assertNotEqual(invalid.returncode, 0)
            self.assertIn("invalid choice", invalid.stderr)

    def test_markdown_v1_migration_backs_up_and_imports_core_tables(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".ai-team/requirements").mkdir(parents=True)
            (root / ".ai-team/planning").mkdir(parents=True)
            (root / ".ai-team/requirements/acceptance.md").write_text(
                "# Acceptance Criteria\n\n| ID | Criterion | Priority | Tool Link | Status |\n| --- | --- | --- | --- | --- |\n| AC1 | Imported acceptance | must | | active |\n",
                encoding="utf-8",
            )
            (root / ".ai-team/planning/task-board.md").write_text(
                "# Task Board\n\n| ID | Task | Owner | Status | Acceptance | Failure Modes | Depends On | Tool Link | Evidence |\n| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n| T1 | Imported task | developer | ready | AC1 | | | | |\n",
                encoding="utf-8",
            )

            result = run_harness(root, "migrate", "--from-version", "markdown-v1", "--to-version", "7")

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                acceptance = conn.execute("select criterion from acceptance where id = 'AC1'").fetchone()[0]
                task = conn.execute("select task from tasks where id = 'T1'").fetchone()[0]
            backups = list((root / ".ai-team/backups").glob("*markdown-v1*"))

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(acceptance, "Imported acceptance")
            self.assertEqual(task, "Imported task")
            self.assertTrue(backups)

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

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
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
            run_harness(root, "requirement", "add", "--id", "R1", "--kind", "functional", "--body", "Example")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
            run_harness(root, "requirement", "link", "--requirement", "R1", "--acceptance", "AC1")
            run_harness(root, "phase", "confirmation")
            run_harness(root, "failure-mode", "add", "--id", "FM1", "--feature", "Example", "--scenario", "Risk", "--trigger", "bad input", "--expected", "safe", "--risk", "critical", "--acceptance", "AC1")
            run_harness(root, "task", "add", "--id", "T1", "--task", "Example", "--acceptance", "AC1", "--failure-mode", "FM1")
            run_harness(root, "phase", "planning")
            run_harness(root, "phase", "implementation")
            claim_start_submit(root, "T1")
            run_harness(root, "phase", "qa")
            run_harness(root, "validation", "record", "--surface", "Example", "--acceptance", "AC1", "--commands", "test", "--findings", "failed", "--result", "fail", "--failure-mode", "FM1")
            run_harness(root, "gate", "record", "--reviewer-context", "fresh", "--result", "fail", "--commands", "test", "--evidence", "review")

            validate = run_harness(root, "validate", "--delivery", check=False)
            transition = run_harness(root, "phase", "delivery_readiness", check=False)

            self.assertNotEqual(validate.returncode, 0)
            self.assertIn("validation is not pass", validate.stdout)
            self.assertIn("critical failure mode is not covered by passing validation", validate.stdout)
            self.assertIn("latest quality gate is not pass", validate.stdout)
            self.assertNotEqual(transition.returncode, 0)
            self.assertIn("delivery readiness blocked", transition.stdout)

    def test_legacy_decision_wrapper_writes_sqlite_and_rendered_view(self) -> None:
        legacy_init = REPO_ROOT / "plugins" / "codex-project-harness" / "scripts" / "init_project_harness.py"
        legacy_decision = REPO_ROOT / "plugins" / "codex-project-harness" / "scripts" / "record_decision.py"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["python3", str(legacy_init)], cwd=root, text=True, capture_output=True, check=True)
            result = subprocess.run(
                [
                    "python3",
                    str(legacy_decision),
                    "--decision",
                    'Use SQLite "runtime"',
                    "--reason",
                    "Avoid split-brain markdown writes",
                ],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                decision = conn.execute("select decision, reason from decisions").fetchone()
                event_payload = conn.execute("select payload_json from events where type = 'decision_recorded'").fetchone()[0]
            rendered = (root / ".ai-team/control/decision-log.md").read_text(encoding="utf-8")

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(decision, ('Use SQLite "runtime"', "Avoid split-brain markdown writes"))
            self.assertIn('\\"runtime\\"', event_payload)
            self.assertIn('Use SQLite "runtime"', rendered)


if __name__ == "__main__":
    unittest.main()

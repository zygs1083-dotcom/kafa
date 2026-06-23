from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "plugins" / "codex-project-harness" / "scripts" / "harness.py"
DEFAULT_TEST_COMMAND = "python3 -B -m unittest test_harness_dummy.py"
PLUGIN_ROOT = REPO_ROOT / "plugins" / "codex-project-harness"
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))
SCRIPTS_ROOT = PLUGIN_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))


CONNECTOR_KEY = "test-connector-key"


def connector_env(key: str = CONNECTOR_KEY) -> dict[str, str]:
    return {"HARNESS_CONNECTOR_KEY": key}


def connector_hmac(key: str, payload: str) -> str:
    return hmac.new(key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def ci_hmac(key: str, provider: str, run_id: str, commit_sha: str, conclusion: str) -> str:
    return connector_hmac(key, f"ci:{provider}:{run_id}:{commit_sha}:{conclusion}")


def external_session_hmac(key: str, session_id: str, verifier: str, commit_sha: str, conclusion: str) -> str:
    return connector_hmac(key, f"external-session:{session_id}:{verifier}:{commit_sha}:{conclusion}")


def run_harness(root: Path, *args: str, check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    command_env = None
    if env is not None:
        command_env = {**os.environ, **env}
    return subprocess.run(
        ["python3", str(HARNESS), "--root", str(root), *args],
        text=True,
        capture_output=True,
        check=check,
        env=command_env,
    )


def task_revision(root: Path, task_id: str) -> int:
    with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
        return int(conn.execute("select revision from tasks where id = ?", (task_id,)).fetchone()[0])


def token_from_stdout(stdout: str) -> str:
    marker = "token="
    return stdout.split(marker, 1)[1].split(None, 1)[0].strip()


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


def trusted_artifact(root: Path, suffix: str = "1", *, content: str = "1 passed\n") -> tuple[str, str]:
    artifact = root / ".ai-team" / "runtime" / "test-artifacts" / f"stdout-{suffix}.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(content, encoding="utf-8")
    return artifact.relative_to(root).as_posix(), hashlib.sha256(content.encode("utf-8")).hexdigest()


def ensure_dummy_unittest(root: Path) -> None:
    (root / "test_harness_dummy.py").write_text(
        "import unittest\n\n"
        "class HarnessDummyTest(unittest.TestCase):\n"
        "    def test_dummy(self):\n"
        "        self.assertTrue(True)\n",
        encoding="utf-8",
    )


def register_test_target(root: Path, target_id: str = "TARGET1", *, command: str = DEFAULT_TEST_COMMAND, kind: str = "unit") -> None:
    run_harness(
        root,
        "test-target",
        "add",
        "--id",
        target_id,
        "--kind",
        kind,
        "--command-template",
        command,
        "--description",
        f"{target_id} command target",
    )


def run_executor_evidence(root: Path, *, target_id: str = "TARGET1", command: str = DEFAULT_TEST_COMMAND, code_identity: str = "content-hash") -> str:
    ensure_dummy_unittest(root)
    run_harness(root, "agent", "capability", "add", "--agent", "developer", "--capability", "developer")
    run_harness(root, "dispatch", "plan", "--scope", f"Execute {target_id}")
    command_args = ["dispatch", "run", "--agent", "developer", "--target", target_id, "--command", command]
    if code_identity:
        command_args.extend(["--code-identity", code_identity])
    result = run_harness(root, *command_args)
    return result.stdout.strip().rsplit(" ", 1)[-1]


def record_evidence_and_test(root: Path, suffix: str = "1", *, code_identity: str = "content-hash") -> tuple[str, str]:
    test_id = f"TEST{suffix}"
    target_id = f"TARGET{suffix}"
    register_test_target(root, target_id)
    evidence_id = run_executor_evidence(root, target_id=target_id, code_identity=code_identity)
    run_harness(root, "test", "record", "--id", test_id, "--surface", "Example", "--command", DEFAULT_TEST_COMMAND, "--result", "pass", "--evidence", evidence_id)
    return evidence_id, test_id


def record_pass_validation(root: Path, *, acceptance: str = "AC1", failure_mode: str | None = None, suffix: str = "1", code_identity: str = "content-hash") -> None:
    evidence_id, test_id = record_evidence_and_test(root, suffix, code_identity=code_identity)
    target_id = f"TARGET{suffix}"
    command = [
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
        evidence_id,
        "--target",
        target_id,
    ]
    if failure_mode:
        command.extend(["--failure-mode", failure_mode])
    command.extend(["--code-identity", code_identity])
    run_harness(root, *command)


def confirm_and_freeze(root: Path, baseline_id: str = "B1") -> None:
    run_harness(root, "scope", "confirm", "--by", "project-manager", "--summary", "confirmed")
    run_harness(root, "baseline", "freeze", "--id", baseline_id, "--summary", "baseline")


def move_to_delivery_readiness(root: Path) -> None:
    run_harness(root, "phase", "project_bootstrap")
    run_harness(root, "phase", "requirement_baseline")
    run_harness(root, "phase", "confirmation")
    run_harness(root, "phase", "planning")
    run_harness(root, "phase", "implementation")
    run_harness(root, "phase", "qa")
    run_harness(root, "phase", "delivery_readiness")


def prepare_basic_delivery_project(root: Path, *, failure_mode_risk: str | None = None) -> None:
    run_harness(root, "init")
    run_harness(root, "requirement", "add", "--id", "R1", "--kind", "functional", "--body", "Example")
    run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
    run_harness(root, "requirement", "link", "--requirement", "R1", "--acceptance", "AC1")
    if failure_mode_risk:
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
            "safe output",
            "--risk",
            failure_mode_risk,
            "--acceptance",
            "AC1",
        )
    task_args = ["task", "add", "--id", "T1", "--task", "Example", "--acceptance", "AC1"]
    if failure_mode_risk:
        task_args.extend(["--failure-mode", "FM1"])
    run_harness(root, *task_args)
    claim_start_submit(root, "T1")
    review_accept(root, "T1")
    confirm_and_freeze(root)


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
            self.assertEqual(project[0], 18)
            self.assertIn("tasks", tables)
            self.assertIn("events", tables)
            self.assertIn("test_targets", tables)

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

    def test_invariant_violation_rolls_back_and_runtime_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
            run_harness(root, "task", "add", "--id", "T1", "--task", "Self review", "--acceptance", "AC1")
            claim_start_submit(root, "T1", agent="developer")
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute(
                    """
                    update tasks set status = 'review', lease_agent = 'developer',
                      lease_token = 'producer-token', lease_expires_at = '2099-01-01T00:00:00+00:00',
                      revision = revision + 1
                    where id = 'T1'
                    """
                )
                conn.execute("update agents set lease_task_id = 'T1', status = 'leased' where id = 'developer'")
                conn.commit()
                revision = conn.execute("select revision from tasks where id = 'T1'").fetchone()[0]

            rejected = run_harness(
                root,
                "task",
                "accept",
                "T1",
                "--agent",
                "developer",
                "--lease-token",
                "producer-token",
                "--expected-revision",
                str(revision),
                "--evidence",
                "self-reviewed",
                check=False,
            )
            recovery = run_harness(root, "decision", "record", "--decision", "runtime recovered", "--reason", "rollback worked")

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                task = conn.execute("select status, accepted_by, lease_agent from tasks where id = 'T1'").fetchone()
                decision = conn.execute("select decision from decisions where decision = 'runtime recovered'").fetchone()

            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("producer accepted own task", rejected.stdout)
            self.assertEqual(task, ("review", "", "developer"))
            self.assertEqual(recovery.returncode, 0, recovery.stdout + recovery.stderr)
            self.assertEqual(decision[0], "runtime recovered")

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
            run_harness(root, "migrate", "--from-version", "6", "--to-version", "18")
            run_harness(root, "requirement", "add", "--id", "R1", "--kind", "functional", "--body", "Example")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
            run_harness(root, "requirement", "link", "--requirement", "R1", "--acceptance", "AC1")
            run_harness(root, "task", "add", "--id", "T1", "--task", "Example", "--acceptance", "AC1")
            claim_start_submit(root, "T1")
            review_accept(root, "T1")
            record_pass_validation(root)
            run_harness(root, "gate", "record", "--reviewer-context", "fresh", "--result", "pass", "--commands", "true", "--evidence", "reviewed")
            confirm_and_freeze(root)
            move_to_delivery_readiness(root)
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
            self.assertIn("delivery record requires phase delivery_readiness", blocked.stdout)
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
            confirm_and_freeze(root)
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
            run_harness(root, "failure-mode", "add", "--id", "FM1", "--feature", "Example", "--scenario", "Risk", "--trigger", "bad input", "--expected", "safe", "--risk", "low", "--acceptance", "AC1")
            run_harness(root, "task", "add", "--id", "T1", "--task", "Example", "--acceptance", "AC1", "--failure-mode", "FM1")
            claim_start_submit(root, "T1")
            review_accept(root, "T1")
            confirm_and_freeze(root)
            record_pass_validation(root, failure_mode="FM1")
            run_harness(root, "gate", "record", "--reviewer-context", "fresh", "--result", "pass", "--commands", "test", "--evidence", "review")
            before = run_harness(root, "validate", "--delivery")

            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Changed acceptance")
            stale = run_harness(root, "validate", "--delivery", check=False)
            confirm_and_freeze(root, "B2")
            record_pass_validation(root, failure_mode="FM1", suffix="2")
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
            confirm_and_freeze(root)
            record_pass_validation(root)
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
            run_harness(root, "failure-mode", "add", "--id", "FM1", "--feature", "Example", "--scenario", "Risk", "--trigger", "bad input", "--expected", "safe", "--risk", "low", "--acceptance", "AC1")
            record_pass_validation(root, failure_mode="FM1")

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
            record_pass_validation(root)
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

            migrate = run_harness(root, "migrate", "--from-version", "markdown-v1", "--to-version", "13", "--dry-run")
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
            ensure_dummy_unittest(root)
            subprocess.run(["git", "add", "app.txt", "test_harness_dummy.py"], cwd=root, text=True, capture_output=True, check=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=root, text=True, capture_output=True, check=True)
            run_harness(root, "init")
            run_harness(root, "requirement", "add", "--id", "R1", "--kind", "functional", "--body", "Example")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
            run_harness(root, "requirement", "link", "--requirement", "R1", "--acceptance", "AC1")
            run_harness(root, "failure-mode", "add", "--id", "FM1", "--feature", "Example", "--scenario", "Risk", "--trigger", "bad input", "--expected", "safe", "--risk", "critical", "--acceptance", "AC1")
            run_harness(root, "task", "add", "--id", "T1", "--task", "Example", "--acceptance", "AC1", "--failure-mode", "FM1")
            claim_start_submit(root, "T1")
            review_accept(root, "T1")
            confirm_and_freeze(root)
            record_pass_validation(root, failure_mode="FM1")
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

    def test_test_target_registry_is_structured_and_projected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")

            added = run_harness(root, "test-target", "add", "--id", "UNIT", "--kind", "unit", "--command-template", "python3 -m unittest", "--description", "Unit suite")
            listed = run_harness(root, "test-target", "list")

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                target = conn.execute("select id, kind, command_template, description from test_targets where id = 'UNIT'").fetchone()
            rendered = (root / ".ai-team/control/test-targets.md").read_text(encoding="utf-8")

            self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
            self.assertIn("UNIT", listed.stdout)
            self.assertEqual(target, ("UNIT", "unit", "python3 -m unittest", "Unit suite"))
            self.assertIn("python3 -m unittest", rendered)

    def test_quality_gate_records_code_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init"], cwd=root, text=True, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, text=True, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=root, text=True, capture_output=True, check=True)
            (root / "app.txt").write_text("hello\n", encoding="utf-8")
            ensure_dummy_unittest(root)
            subprocess.run(["git", "add", "app.txt"], cwd=root, text=True, capture_output=True, check=True)
            subprocess.run(["git", "add", "test_harness_dummy.py"], cwd=root, text=True, capture_output=True, check=True)
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

            result = run_harness(root, "migrate", "--from-version", "markdown-v1", "--to-version", "13")

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
            confirm_and_freeze(root)
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

    def test_scope_and_baseline_are_required_for_planning_and_delivery_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "phase", "project_bootstrap")
            run_harness(root, "phase", "requirement_baseline")
            run_harness(root, "requirement", "add", "--id", "R1", "--kind", "functional", "--body", "Example")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
            run_harness(root, "phase", "confirmation")

            no_scope = run_harness(root, "phase", "planning", check=False)
            run_harness(root, "scope", "confirm", "--by", "pm", "--summary", "confirmed")
            no_baseline = run_harness(root, "phase", "planning", check=False)
            run_harness(root, "baseline", "freeze", "--id", "B1", "--summary", "baseline")
            planning = run_harness(root, "phase", "planning")

            self.assertNotEqual(no_scope.returncode, 0)
            self.assertIn("requires confirmed scope", no_scope.stdout)
            self.assertNotEqual(no_baseline.returncode, 0)
            self.assertIn("current frozen baseline", no_baseline.stdout)
            self.assertEqual(planning.returncode, 0, planning.stdout + planning.stderr)

    def test_requirement_change_makes_baseline_invalid_until_refrozen(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "requirement", "add", "--id", "R1", "--kind", "functional", "--body", "Original")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
            run_harness(root, "requirement", "link", "--requirement", "R1", "--acceptance", "AC1")
            confirm_and_freeze(root)

            run_harness(root, "requirement", "add", "--id", "R1", "--kind", "functional", "--body", "Changed")
            stale = run_harness(root, "baseline", "validate", check=False)
            diff = run_harness(root, "baseline", "diff", "--from", "B1")
            run_harness(root, "baseline", "freeze", "--id", "B2", "--summary", "updated")
            current = run_harness(root, "baseline", "validate")

            self.assertNotEqual(stale.returncode, 0)
            self.assertIn("frozen baseline is stale", stale.stdout)
            self.assertIn("requirements: changed", diff.stdout)
            self.assertEqual(current.returncode, 0, current.stdout + current.stderr)

    def test_delivery_requires_validation_test_or_evidence_link(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "requirement", "add", "--id", "R1", "--kind", "functional", "--body", "Example")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
            run_harness(root, "requirement", "link", "--requirement", "R1", "--acceptance", "AC1")
            run_harness(root, "task", "add", "--id", "T1", "--task", "Example", "--acceptance", "AC1")
            claim_start_submit(root, "T1")
            review_accept(root, "T1")
            confirm_and_freeze(root)
            run_harness(root, "validation", "record", "--surface", "Example", "--acceptance", "AC1", "--commands", "test", "--findings", "passed", "--result", "pass")
            run_harness(root, "gate", "record", "--reviewer-context", "fresh", "--result", "pass", "--commands", "test", "--evidence", "review")

            validate = run_harness(root, "validate", "--delivery", check=False)

            self.assertNotEqual(validate.returncode, 0)
            self.assertIn("lacks linked passing test or evidence", validate.stdout)

    def test_delivery_requires_trusted_command_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "requirement", "add", "--id", "R1", "--kind", "functional", "--body", "Example")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
            run_harness(root, "requirement", "link", "--requirement", "R1", "--acceptance", "AC1")
            run_harness(root, "task", "add", "--id", "T1", "--task", "Example", "--acceptance", "AC1")
            claim_start_submit(root, "T1")
            review_accept(root, "T1")
            confirm_and_freeze(root)
            run_harness(root, "evidence", "record", "--id", "EV1", "--kind", "command", "--summary", "free text only")
            run_harness(root, "test", "record", "--id", "TEST1", "--surface", "Example", "--command", "pytest", "--result", "pass", "--evidence", "EV1")
            run_harness(root, "validation", "record", "--surface", "Example", "--acceptance", "AC1", "--commands", "pytest", "--findings", "passed", "--result", "pass", "--test", "TEST1", "--evidence", "EV1")
            run_harness(root, "gate", "record", "--reviewer-context", "fresh", "--result", "pass", "--commands", "review", "--evidence", "review")

            validate = run_harness(root, "validate", "--delivery", check=False)

            self.assertNotEqual(validate.returncode, 0)
            self.assertIn("trusted command evidence", validate.stdout)

    def test_delivery_rejects_nonzero_command_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "requirement", "add", "--id", "R1", "--kind", "functional", "--body", "Example")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
            run_harness(root, "requirement", "link", "--requirement", "R1", "--acceptance", "AC1")
            run_harness(root, "task", "add", "--id", "T1", "--task", "Example", "--acceptance", "AC1")
            claim_start_submit(root, "T1")
            review_accept(root, "T1")
            confirm_and_freeze(root)
            artifact_path, stdout_sha = trusted_artifact(root, "failed-command", content="boom\n")
            evidence = run_harness(
                root,
                "evidence",
                "record",
                "--id",
                "EV1",
                "--kind",
                "command",
                "--summary",
                "command failed",
                "--command",
                "python3 -c 'import sys; sys.exit(2)'",
                "--exit-code",
                "2",
                "--stdout-sha256",
                stdout_sha,
                "--artifact-path",
                artifact_path,
                check=False,
            )
            validation = run_harness(
                root,
                "validation",
                "record",
                "--surface",
                "Example",
                "--acceptance",
                "AC1",
                "--findings",
                "failed command",
                "--result",
                "pass",
                "--evidence",
                "EV1",
                "--command",
                "python3 -c 'import sys; sys.exit(2)'",
                "--exit-code",
                "2",
                "--stdout-sha256",
                stdout_sha,
                "--artifact-path",
                artifact_path,
                check=False,
            )
            run_harness(root, "gate", "record", "--reviewer-context", "fresh", "--result", "pass", "--commands", "review", "--evidence", "review")

            validate = run_harness(root, "validate", "--delivery", check=False)

            self.assertEqual(evidence.returncode, 0, evidence.stdout + evidence.stderr)
            self.assertEqual(validation.returncode, 0, validation.stdout + validation.stderr)
            self.assertNotEqual(validate.returncode, 0)
            self.assertIn("exit_code=2", validate.stdout)

    def test_delivery_requires_validation_command_to_match_registered_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "requirement", "add", "--id", "R1", "--kind", "functional", "--body", "Example")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
            run_harness(root, "requirement", "link", "--requirement", "R1", "--acceptance", "AC1")
            run_harness(root, "task", "add", "--id", "T1", "--task", "Example", "--acceptance", "AC1")
            claim_start_submit(root, "T1")
            review_accept(root, "T1")
            confirm_and_freeze(root)
            register_test_target(root, "TARGET1", command="python3 -c 'print(\"1 passed\")'")
            artifact_path, stdout_sha = trusted_artifact(root, "target-mismatch", content="1 passed\n")
            run_harness(root, "evidence", "record", "--id", "EV1", "--kind", "command", "--summary", "mismatch", "--command", "python3 -c 'print(\"wrong\")'", "--exit-code", "0", "--stdout-sha256", stdout_sha, "--artifact-path", artifact_path, "--target", "TARGET1", "--executed-count", "1")
            run_harness(root, "test", "record", "--id", "TEST1", "--surface", "Example", "--command", "python3 -c 'print(\"wrong\")'", "--result", "pass", "--evidence", "EV1")
            run_harness(root, "validation", "record", "--surface", "Example", "--acceptance", "AC1", "--commands", "wrong", "--findings", "passed", "--result", "pass", "--test", "TEST1", "--evidence", "EV1", "--command", "python3 -c 'print(\"wrong\")'", "--exit-code", "0", "--stdout-sha256", stdout_sha, "--artifact-path", artifact_path, "--target", "TARGET1", "--executed-count", "1")
            run_harness(root, "gate", "record", "--reviewer-context", "fresh", "--result", "pass", "--commands", "review", "--evidence", "review")

            validate = run_harness(root, "validate", "--delivery", check=False)

            self.assertNotEqual(validate.returncode, 0)
            self.assertIn("command does not match target", validate.stdout)

    def test_delivery_rejects_zero_executed_count(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "requirement", "add", "--id", "R1", "--kind", "functional", "--body", "Example")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
            run_harness(root, "requirement", "link", "--requirement", "R1", "--acceptance", "AC1")
            run_harness(root, "task", "add", "--id", "T1", "--task", "Example", "--acceptance", "AC1")
            claim_start_submit(root, "T1")
            review_accept(root, "T1")
            confirm_and_freeze(root)
            register_test_target(root, "TARGET1")
            artifact_path, stdout_sha = trusted_artifact(root, "zero-count", content="0 passed\n")
            run_harness(root, "evidence", "record", "--id", "EV1", "--kind", "command", "--summary", "zero count", "--command", DEFAULT_TEST_COMMAND, "--exit-code", "0", "--stdout-sha256", stdout_sha, "--artifact-path", artifact_path, "--target", "TARGET1", "--executed-count", "0")
            run_harness(root, "test", "record", "--id", "TEST1", "--surface", "Example", "--command", DEFAULT_TEST_COMMAND, "--result", "pass", "--evidence", "EV1")
            run_harness(root, "validation", "record", "--surface", "Example", "--acceptance", "AC1", "--commands", DEFAULT_TEST_COMMAND, "--findings", "passed", "--result", "pass", "--test", "TEST1", "--evidence", "EV1", "--command", DEFAULT_TEST_COMMAND, "--exit-code", "0", "--stdout-sha256", stdout_sha, "--artifact-path", artifact_path, "--target", "TARGET1", "--executed-count", "0")
            run_harness(root, "gate", "record", "--reviewer-context", "fresh", "--result", "pass", "--commands", "review", "--evidence", "review")

            validate = run_harness(root, "validate", "--delivery", check=False)

            self.assertNotEqual(validate.returncode, 0)
            self.assertIn("executed_count=0", validate.stdout)

    def test_delivery_rejects_manual_executed_count_for_passing_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_basic_delivery_project(root)
            register_test_target(root, "TARGET1")
            artifact_path, stdout_sha = trusted_artifact(root, "manual-count")
            run_harness(root, "evidence", "record", "--id", "EV1", "--kind", "command", "--summary", "manual", "--command", DEFAULT_TEST_COMMAND, "--exit-code", "0", "--stdout-sha256", stdout_sha, "--artifact-path", artifact_path, "--target", "TARGET1", "--executed-count", "1")
            run_harness(root, "test", "record", "--id", "TEST1", "--surface", "Example", "--command", DEFAULT_TEST_COMMAND, "--result", "pass", "--evidence", "EV1")
            run_harness(root, "validation", "record", "--surface", "Example", "--acceptance", "AC1", "--commands", DEFAULT_TEST_COMMAND, "--findings", "passed", "--result", "pass", "--test", "TEST1", "--evidence", "EV1", "--command", DEFAULT_TEST_COMMAND, "--exit-code", "0", "--stdout-sha256", stdout_sha, "--artifact-path", artifact_path, "--target", "TARGET1", "--executed-count", "1")
            run_harness(root, "gate", "record", "--reviewer-context", "fresh", "--result", "pass", "--commands", "review", "--evidence", "review")

            validate = run_harness(root, "validate", "--delivery", check=False)

            self.assertNotEqual(validate.returncode, 0)
            self.assertIn("executed_count_source=manual", validate.stdout)

    def test_gate_rejects_non_gateable_test_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_basic_delivery_project(root)
            add = run_harness(root, "test-target", "add", "--id", "ECHO", "--kind", "unit", "--command-template", "echo ok")
            artifact_path, stdout_sha = trusted_artifact(root, "echo", content="1 passed\n")
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                target = conn.execute("select gateable, gate_block_reason from test_targets where id = 'ECHO'").fetchone()
                conn.execute(
                    """
                    insert into evidence
                    (id, kind, summary, command, exit_code, stdout_sha256, artifact_path, source_tree_hash,
                     target_id, executed_count, executed_count_source, policy_status, created_at)
                    values ('EV1', 'command', 'echo', 'echo ok', 0, ?, ?, ?, 'ECHO', 1, 'parsed', 'allowed', 'now')
                    """,
                    (stdout_sha, artifact_path, ""),
                )
                conn.commit()
            run_harness(root, "test", "record", "--id", "TEST1", "--surface", "Example", "--command", "echo ok", "--result", "pass", "--evidence", "EV1")
            run_harness(root, "validation", "record", "--surface", "Example", "--acceptance", "AC1", "--commands", "echo ok", "--findings", "passed", "--result", "pass", "--test", "TEST1", "--evidence", "EV1", "--target", "ECHO")
            run_harness(root, "gate", "record", "--reviewer-context", "fresh", "--result", "pass", "--commands", "review", "--evidence", "review")

            validate = run_harness(root, "validate", "--delivery", check=False)

            self.assertEqual(add.returncode, 0, add.stdout + add.stderr)
            self.assertEqual(target[0], 0)
            self.assertIn("not a gateable test target", target[1])
            self.assertNotEqual(validate.returncode, 0)
            self.assertIn("target is not gateable", validate.stdout)

    def test_high_risk_failure_mode_requires_external_or_ci_trust_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_basic_delivery_project(root, failure_mode_risk="high")
            record_pass_validation(root, failure_mode="FM1")
            run_harness(root, "gate", "record", "--reviewer-context", "fresh", "--result", "pass", "--commands", "review", "--evidence", "review")

            validate = run_harness(root, "validate", "--delivery", check=False)

            self.assertNotEqual(validate.returncode, 0)
            self.assertIn("requires ci or external-session trust anchor", validate.stdout)

    def test_ci_verify_anchor_can_cover_high_risk_failure_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init"], cwd=root, text=True, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, text=True, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=root, text=True, capture_output=True, check=True)
            (root / "app.txt").write_text("hello\n", encoding="utf-8")
            ensure_dummy_unittest(root)
            subprocess.run(["git", "add", "app.txt", "test_harness_dummy.py"], cwd=root, text=True, capture_output=True, check=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=root, text=True, capture_output=True, check=True)
            sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=True).stdout.strip()
            prepare_basic_delivery_project(root, failure_mode_risk="critical")
            run_harness(
                root,
                "adapter",
                "ci-verify",
                "--provider",
                "github",
                "--run-id",
                "run-1",
                "--conclusion",
                "success",
                "--commit-sha",
                sha,
                "--origin",
                "connector",
                env=connector_env(),
            )
            record_pass_validation(root, failure_mode="FM1", code_identity="git")
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                token = conn.execute("select verification_token from ci_verifications where id = 'github:run-1'").fetchone()[0]
                conn.execute("update validations set trust_anchor = 'ci', trust_anchor_id = 'github:run-1'")
                conn.commit()
            self.assertEqual(token, ci_hmac(CONNECTOR_KEY, "github", "run-1", sha, "success"))
            run_harness(root, "gate", "record", "--reviewer-context", "fresh", "--result", "pass", "--commands", "review", "--evidence", "review")

            validate = run_harness(root, "validate", "--delivery", env=connector_env())

            self.assertEqual(validate.returncode, 0, validate.stdout + validate.stderr)

    def test_delivery_without_git_and_without_content_identity_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_basic_delivery_project(root)
            register_test_target(root, "TARGET1")
            evidence_id = run_executor_evidence(root, target_id="TARGET1", code_identity="")
            run_harness(root, "test", "record", "--id", "TEST1", "--surface", "Example", "--command", DEFAULT_TEST_COMMAND, "--result", "pass", "--evidence", evidence_id)
            run_harness(root, "validation", "record", "--surface", "Example", "--acceptance", "AC1", "--commands", DEFAULT_TEST_COMMAND, "--findings", "passed", "--result", "pass", "--test", "TEST1", "--evidence", evidence_id, "--target", "TARGET1")
            run_harness(root, "gate", "record", "--reviewer-context", "fresh", "--result", "pass", "--commands", "review", "--evidence", "review")

            validate = run_harness(root, "validate", "--delivery", check=False)

            self.assertNotEqual(validate.returncode, 0)
            self.assertIn("delivery requires a committed code identity", validate.stdout)

    def test_no_git_content_hash_identity_can_satisfy_low_risk_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_basic_delivery_project(root)
            record_pass_validation(root)
            run_harness(root, "gate", "record", "--reviewer-context", "fresh", "--result", "pass", "--commands", "review", "--evidence", "review")

            validate = run_harness(root, "validate", "--delivery")

            self.assertEqual(validate.returncode, 0, validate.stdout + validate.stderr)

    def test_delivery_rejects_empty_source_hash_and_tampered_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_basic_delivery_project(root)
            record_pass_validation(root)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute("update validations set source_tree_hash = ''")
                conn.commit()
            run_harness(root, "gate", "record", "--reviewer-context", "fresh", "--result", "pass", "--commands", "review", "--evidence", "review")

            missing_hash = run_harness(root, "validate", "--delivery", check=False)
            self.assertNotEqual(missing_hash.returncode, 0)
            self.assertIn("missing source_tree_hash", missing_hash.stdout)

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                row = conn.execute("select artifact_path from validations order by created_at desc limit 1").fetchone()
                artifact = root / row[0]
                artifact.write_text("tampered\n", encoding="utf-8")
            tampered = run_harness(root, "validate", "--delivery", check=False)
            self.assertNotEqual(tampered.returncode, 0)
            self.assertIn("stdout_sha256 mismatch", tampered.stdout)

    def test_external_session_anchor_must_reference_verified_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_basic_delivery_project(root, failure_mode_risk="high")
            record_pass_validation(root, failure_mode="FM1", code_identity="git")
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute("update validations set trust_anchor = 'external-session', trust_anchor_id = 'fake-session'")
                conn.commit()
            run_harness(root, "gate", "record", "--reviewer-context", "fresh", "--result", "pass", "--commands", "review", "--evidence", "review")

            validate = run_harness(root, "validate", "--delivery", check=False)

            self.assertNotEqual(validate.returncode, 0)
            self.assertIn("missing external-session verification", validate.stdout)

    def test_manual_origin_ci_does_not_cover_high_risk_failure_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init"], cwd=root, text=True, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, text=True, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=root, text=True, capture_output=True, check=True)
            (root / "app.txt").write_text("hello\n", encoding="utf-8")
            ensure_dummy_unittest(root)
            subprocess.run(["git", "add", "app.txt", "test_harness_dummy.py"], cwd=root, text=True, capture_output=True, check=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=root, text=True, capture_output=True, check=True)
            sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=True).stdout.strip()
            prepare_basic_delivery_project(root, failure_mode_risk="critical")
            run_harness(root, "adapter", "ci-verify", "--provider", "github", "--run-id", "run-1", "--conclusion", "success", "--commit-sha", sha)
            record_pass_validation(root, failure_mode="FM1", code_identity="git")
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute("update validations set trust_anchor = 'ci', trust_anchor_id = 'github:run-1'")
                conn.commit()
            run_harness(root, "gate", "record", "--reviewer-context", "fresh", "--result", "pass", "--commands", "review", "--evidence", "review")

            validate = run_harness(root, "validate", "--delivery", check=False)

            self.assertNotEqual(validate.returncode, 0)
            self.assertIn("ci verification origin is not connector", validate.stdout)

    def test_connector_ci_without_key_is_downgraded_and_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init"], cwd=root, text=True, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, text=True, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=root, text=True, capture_output=True, check=True)
            (root / "app.txt").write_text("hello\n", encoding="utf-8")
            ensure_dummy_unittest(root)
            subprocess.run(["git", "add", "app.txt", "test_harness_dummy.py"], cwd=root, text=True, capture_output=True, check=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=root, text=True, capture_output=True, check=True)
            sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=True).stdout.strip()
            prepare_basic_delivery_project(root, failure_mode_risk="critical")
            run_harness(root, "adapter", "ci-verify", "--provider", "github", "--run-id", "run-1", "--conclusion", "success", "--commit-sha", sha, "--origin", "connector", "--verification-token", "arbitrary")
            record_pass_validation(root, failure_mode="FM1", code_identity="git")
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                row = conn.execute("select origin, verification_token, token_status from ci_verifications where id = 'github:run-1'").fetchone()
                conn.execute("update validations set trust_anchor = 'ci', trust_anchor_id = 'github:run-1'")
                conn.commit()
            self.assertEqual(row[0], "manual")
            self.assertEqual(row[1], "")
            self.assertEqual(row[2], "downgraded-no-key")
            run_harness(root, "gate", "record", "--reviewer-context", "fresh", "--result", "pass", "--commands", "review", "--evidence", "review")

            validate = run_harness(root, "validate", "--delivery", check=False)

            self.assertNotEqual(validate.returncode, 0)
            self.assertIn("ci verification origin is not connector", validate.stdout)

    def test_connector_ci_rejects_bad_token_when_key_is_available(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")

            bad = run_harness(
                root,
                "adapter",
                "ci-verify",
                "--provider",
                "github",
                "--run-id",
                "run-1",
                "--conclusion",
                "success",
                "--commit-sha",
                "abc123",
                "--origin",
                "connector",
                "--verification-token",
                "arbitrary",
                check=False,
                env=connector_env(),
            )

            self.assertNotEqual(bad.returncode, 0)
            self.assertIn("verification_token does not match connector HMAC", bad.stderr + bad.stdout)

    def test_connector_key_file_path_is_used_without_persisting_key_and_doctor_flags_tracked_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init"], cwd=root, text=True, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, text=True, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=root, text=True, capture_output=True, check=True)
            run_harness(root, "init")
            key = "file-backed-connector-key"
            key_file = root / ".ai-team/runtime/connector.key"
            key_file.parent.mkdir(parents=True, exist_ok=True)
            key_file.write_text(key, encoding="utf-8")
            key_path_file = root / ".ai-team/control/connector-key-path.txt"
            key_path_file.parent.mkdir(parents=True, exist_ok=True)
            key_path_file.write_text(".ai-team/runtime/connector.key\n", encoding="utf-8")

            run_harness(root, "adapter", "ci-verify", "--provider", "github", "--run-id", "run-file", "--conclusion", "success", "--commit-sha", "abc123", "--origin", "connector")

            expected = ci_hmac(key, "github", "run-file", "abc123", "success")
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                row = conn.execute("select origin, verification_token, token_status from ci_verifications where id = 'github:run-file'").fetchone()
                db_dump = "\n".join(conn.iterdump())
            self.assertEqual(row[0], "connector")
            self.assertEqual(row[1], expected)
            self.assertEqual(row[2], "hmac-valid")
            self.assertNotIn(key, db_dump)
            for text_file in (root / ".ai-team").rglob("*.md"):
                self.assertNotIn(key, text_file.read_text(encoding="utf-8"))

            subprocess.run(["git", "add", "-f", ".ai-team/runtime/connector.key"], cwd=root, text=True, capture_output=True, check=True)
            doctor = run_harness(root, "doctor", check=False)

            self.assertNotEqual(doctor.returncode, 0)
            self.assertIn("connector key file is tracked by git", doctor.stdout)

    def test_connector_ci_and_external_session_can_cover_high_risk_failure_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init"], cwd=root, text=True, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, text=True, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=root, text=True, capture_output=True, check=True)
            (root / "app.txt").write_text("hello\n", encoding="utf-8")
            ensure_dummy_unittest(root)
            subprocess.run(["git", "add", "app.txt", "test_harness_dummy.py"], cwd=root, text=True, capture_output=True, check=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=root, text=True, capture_output=True, check=True)
            sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=True).stdout.strip()
            prepare_basic_delivery_project(root, failure_mode_risk="critical")
            run_harness(root, "adapter", "ci-verify", "--provider", "github", "--run-id", "run-1", "--conclusion", "success", "--commit-sha", sha, "--origin", "connector", env=connector_env())
            record_pass_validation(root, failure_mode="FM1", code_identity="git")
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute("update validations set trust_anchor = 'ci', trust_anchor_id = 'github:run-1'")
                conn.commit()
            run_harness(root, "gate", "record", "--reviewer-context", "fresh", "--result", "pass", "--commands", "review", "--evidence", "review")

            ci_validate = run_harness(root, "validate", "--delivery", env=connector_env())
            self.assertEqual(ci_validate.returncode, 0, ci_validate.stdout + ci_validate.stderr)

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute("update ci_verifications set conclusion = 'failure' where id = 'github:run-1'")
                conn.commit()
            tampered_ci = run_harness(root, "validate", "--delivery", check=False, env=connector_env())
            self.assertNotEqual(tampered_ci.returncode, 0)
            self.assertIn("connector HMAC", tampered_ci.stdout)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init"], cwd=root, text=True, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, text=True, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=root, text=True, capture_output=True, check=True)
            (root / "app.txt").write_text("hello\n", encoding="utf-8")
            ensure_dummy_unittest(root)
            subprocess.run(["git", "add", "app.txt", "test_harness_dummy.py"], cwd=root, text=True, capture_output=True, check=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=root, text=True, capture_output=True, check=True)
            sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=True).stdout.strip()
            prepare_basic_delivery_project(root, failure_mode_risk="high")
            verification = run_harness(root, "adapter", "external-session-verify", "--session-id", "session-1", "--verifier", "independent-codex", "--conclusion", "verified", "--commit-sha", sha, "--origin", "connector", env=connector_env())
            verification_id = verification.stdout.strip().rsplit(" ", 1)[-1]
            record_pass_validation(root, failure_mode="FM1", code_identity="git")
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                token = conn.execute("select verification_token from external_session_verifications where id = ?", (verification_id,)).fetchone()[0]
                conn.execute("update validations set trust_anchor = 'external-session', trust_anchor_id = ?", (verification_id,))
                conn.commit()
            self.assertEqual(token, external_session_hmac(CONNECTOR_KEY, "session-1", "independent-codex", sha, "verified"))
            run_harness(root, "gate", "record", "--reviewer-context", "fresh", "--result", "pass", "--commands", "review", "--evidence", "review")

            session_validate = run_harness(root, "validate", "--delivery", env=connector_env())
            self.assertEqual(session_validate.returncode, 0, session_validate.stdout + session_validate.stderr)

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute("update external_session_verifications set commit_sha = 'tampered' where id = ?", (verification_id,))
                conn.commit()
            tampered_session = run_harness(root, "validate", "--delivery", check=False, env=connector_env())
            self.assertNotEqual(tampered_session.returncode, 0)
            self.assertIn("connector HMAC", tampered_session.stdout)

    def test_acceptance_gate_uses_any_trusted_validation_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_basic_delivery_project(root)
            record_pass_validation(root, suffix="1")
            register_test_target(root, "BAD")
            artifact_path, stdout_sha = trusted_artifact(root, "bad-latest")
            run_harness(root, "evidence", "record", "--id", "BAD-EV", "--kind", "command", "--summary", "manual bad", "--command", DEFAULT_TEST_COMMAND, "--exit-code", "0", "--stdout-sha256", stdout_sha, "--artifact-path", artifact_path, "--target", "BAD", "--executed-count", "1", "--code-identity", "content-hash")
            run_harness(root, "test", "record", "--id", "BAD-TEST", "--surface", "Example", "--command", DEFAULT_TEST_COMMAND, "--result", "pass", "--evidence", "BAD-EV")
            run_harness(root, "validation", "record", "--surface", "Example", "--acceptance", "AC1", "--commands", DEFAULT_TEST_COMMAND, "--findings", "manual latest", "--result", "pass", "--test", "BAD-TEST", "--evidence", "BAD-EV", "--target", "BAD", "--code-identity", "content-hash")
            run_harness(root, "gate", "record", "--reviewer-context", "fresh", "--result", "pass", "--commands", "review", "--evidence", "review")

            validate = run_harness(root, "validate", "--delivery")

            self.assertEqual(validate.returncode, 0, validate.stdout + validate.stderr)

    def test_dispatch_allow_unlisted_requires_reason_and_records_sandbox_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")

            missing_reason = run_harness(root, "dispatch", "run", "--agent", "developer", "--command", "python3 -c 'print(\"1 passed\")'", "--allow-unlisted", check=False)
            with_reason = run_harness(root, "dispatch", "run", "--agent", "developer", "--command", "python3 -c 'print(\"1 passed\")'", "--allow-unlisted", "--reason", "diagnostic", "--no-network")

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                row = conn.execute("select allow_unlisted, allow_unlisted_reason, sandbox_profile, sandbox_status from evidence where exit_code = 0 order by created_at desc limit 1").fetchone()

            self.assertNotEqual(missing_reason.returncode, 0)
            self.assertIn("--reason is required", missing_reason.stdout)
            self.assertEqual(with_reason.returncode, 0, with_reason.stdout + with_reason.stderr)
            self.assertEqual(row, (1, "diagnostic", "no-network", "unavailable"))

    def test_quality_gate_can_link_findings(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "evidence", "record", "--id", "EV1", "--kind", "review", "--summary", "manual review")
            run_harness(root, "finding", "record", "--id", "F1", "--surface", "API", "--severity", "low", "--status", "resolved", "--summary", "Resolved", "--evidence", "EV1")
            run_harness(root, "gate", "record", "--reviewer-context", "fresh", "--result", "pass", "--commands", "review", "--evidence", "EV1", "--finding", "F1")

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                linked = conn.execute("select gate_id, finding_id from quality_gate_findings").fetchone()

            self.assertEqual(linked[1], "F1")

    def test_checkpoint_export_import_is_snapshot_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Before checkpoint")
            checkpoint = run_harness(root, "checkpoint", "create", "--label", "after-ac1")
            checkpoint_id = checkpoint.stdout.strip().rsplit(" ", 1)[-1]
            package = root / "checkpoint.json"
            run_harness(root, "checkpoint", "export", "--out", str(package))

            with tempfile.TemporaryDirectory() as imported_temp:
                imported = Path(imported_temp)
                run_harness(imported, "init")
                run_harness(imported, "checkpoint", "import", "--file", str(package), "--apply")
                with closing(sqlite3.connect(imported / ".ai-team/state/harness.db")) as conn:
                    imported_acceptance = conn.execute("select criterion from acceptance where id = 'AC1'").fetchone()[0]

            event_help = run_harness(root, "event", "--help")

            self.assertTrue(checkpoint_id)
            self.assertEqual(imported_acceptance, "Before checkpoint")
            self.assertNotIn("replay", event_help.stdout)

    def test_dispatch_uses_capabilities_and_recovers_stale_assignments(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
            run_harness(root, "task", "add", "--id", "T1", "--task", "Frontend", "--owner", "frontend", "--acceptance", "AC1")
            run_harness(root, "task", "add", "--id", "T2", "--task", "Backend", "--owner", "backend", "--acceptance", "AC1")
            run_harness(root, "agent", "capability", "add", "--agent", "developer", "--capability", "frontend")
            run_harness(root, "dispatch", "plan", "--scope", "Build two slices")

            claimed = run_harness(root, "dispatch", "claim-next", "--agent", "developer")
            blocked_second = run_harness(root, "dispatch", "claim-next", "--agent", "developer", check=False)
            recovered = run_harness(root, "dispatch", "recover-stale")
            reclaimed = run_harness(root, "dispatch", "claim-next", "--agent", "developer")

            self.assertIn("T1", claimed.stdout)
            self.assertNotEqual(blocked_second.returncode, 0)
            self.assertIn("agent already has dispatch assignment", blocked_second.stdout)
            self.assertIn("recovered 1 stale", recovered.stdout)
            self.assertIn("T1", reclaimed.stdout)

    def test_dispatch_run_executes_local_command_and_records_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
            run_harness(root, "task", "add", "--id", "T1", "--task", "Run success", "--owner", "developer", "--acceptance", "AC1")
            run_harness(root, "task", "add", "--id", "T2", "--task", "Run fail", "--owner", "developer", "--acceptance", "AC1")
            run_harness(root, "test-target", "add", "--id", "UNIT", "--kind", "unit", "--command-template", "python3 -c 'print(\"2 passed\")'")
            run_harness(root, "dispatch", "plan", "--scope", "Executor")

            rejected = run_harness(root, "dispatch", "run", "--agent", "developer", "--command", "python3 -c 'print(123)'", "--no-network", check=False)
            success = run_harness(root, "dispatch", "run", "--agent", "developer", "--target", "UNIT", "--command", "python3 -c 'print(\"2 passed\")'", "--no-network", check=False)

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                evidence = conn.execute(
                    """
                    select command, exit_code, stdout_sha256, artifact_path, target_id, executed_count,
                           no_network, policy_status
                    from evidence where kind = 'command' order by created_at
                    """
                ).fetchall()
                statuses = conn.execute("select task_id, status from dispatch_assignments order by task_id").fetchall()

            self.assertEqual(rejected.returncode, 1, rejected.stdout + rejected.stderr)
            self.assertEqual(success.returncode, 0, success.stdout + success.stderr)
            self.assertEqual(evidence[0][1], 126)
            self.assertEqual(len(evidence[0][2]), 64)
            self.assertTrue((root / evidence[0][3]).exists())
            self.assertEqual(evidence[0][5], 0)
            self.assertEqual(evidence[0][6], 1)
            self.assertEqual(evidence[0][7], "rejected")
            self.assertEqual(evidence[1][1], 0)
            self.assertEqual(evidence[1][4], "UNIT")
            self.assertEqual(evidence[1][5], 2)
            self.assertEqual(evidence[1][6], 1)
            self.assertEqual(evidence[1][7], "allowed")
            self.assertEqual(statuses, [("T1", "failed"), ("T2", "completed")])

    def test_executor_parses_executed_count_from_common_test_outputs(self) -> None:
        from core.executor import parse_executed_count

        self.assertEqual(parse_executed_count("3 passed, 1 skipped in 0.12s"), 3)
        self.assertEqual(parse_executed_count("Ran 4 tests in 0.001s\n\nOK"), 4)
        self.assertEqual(parse_executed_count("Tests:       5 passed, 5 total"), 5)
        self.assertEqual(parse_executed_count("0 passing (4ms)"), 0)

    def test_adapter_action_lifecycle_and_reconcile(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            planned = run_harness(root, "adapter", "plan", "--tool", "github", "--mode", "write-confirm", "--artifact", "Issue R1", "--action", "create issue", "--payload-json", '{"title":"R1"}', "--idempotency-key", "adapter-action:test:R1")
            action_id = planned.stdout.strip().rsplit(" ", 1)[-1]

            run_harness(root, "adapter", "draft", "--id", action_id)
            run_harness(root, "adapter", "confirm", "--id", action_id, "--confirmation", "user-confirmed")
            run_harness(root, "adapter", "complete", "--id", action_id, "--external-id", "GH-1", "--external-link", "https://example.invalid/GH-1")
            reconcile = run_harness(root, "adapter", "reconcile")

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                action = conn.execute("select status, confirmation, external_id from adapter_actions where id = ?", (action_id,)).fetchone()
                adapter = conn.execute("select tool, mode, external_id from adapters where idempotency_key = 'adapter-action:test:R1'").fetchone()

            self.assertEqual(reconcile.returncode, 0, reconcile.stdout + reconcile.stderr)
            self.assertEqual(action, ("completed", "user-confirmed", "GH-1"))
            self.assertEqual(adapter, ("github", "write-confirm", "GH-1"))

    def test_risk_sweep_expired_turns_expired_acceptance_back_to_open(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
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
                "high",
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

            swept = run_harness(root, "risk", "sweep-expired")
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                status = conn.execute("select status, accepted_by from failure_modes where id = 'FM1'").fetchone()

            self.assertIn("swept 1 expired", swept.stdout)
            self.assertEqual(status, ("identified", None))

    def test_kernel_cli_commands_and_core_api_boundary_exist(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")

            kernel = run_harness(root, "kernel", "doctor")
            invariant = run_harness(root, "invariant", "validate")
            projection = run_harness(root, "projection", "rebuild")

            harness_text = HARNESS.read_text(encoding="utf-8")
            api_text = (REPO_ROOT / "plugins/codex-project-harness/core/api.py").read_text(encoding="utf-8")

            self.assertIn("OK: kernel doctor passed", kernel.stdout)
            self.assertIn("OK: runtime invariants hold", invariant.stdout)
            self.assertIn("OK: projections rebuilt", projection.stdout)
            self.assertIn("from core.api import", harness_text)
            self.assertIn("def invariant_validate", api_text)

    def test_runtime_enums_have_single_source(self) -> None:
        import harness_db
        from core import schema_guard
        from core import invariant_checker

        self.assertIs(harness_db.TASK_STATUSES, schema_guard.TASK_STATUSES)
        self.assertIs(invariant_checker.TASK_STATUSES, schema_guard.TASK_STATUSES)
        self.assertIs(harness_db.FAILURE_MODE_STATUSES, schema_guard.FAILURE_MODE_STATUSES)
        self.assertIs(invariant_checker.FAILURE_MODE_STATUSES, schema_guard.FAILURE_MODE_STATUSES)
        self.assertIs(harness_db.ADAPTER_MODES, schema_guard.ADAPTER_MODES)

    def test_schema_guard_blocks_invalid_writes_before_db_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")

            invalid = run_harness(root, "task", "add", "--id", "", "--task", "Bad", "--acceptance", "AC1", check=False)

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                count = conn.execute("select count(*) from tasks").fetchone()[0]

            self.assertNotEqual(invalid.returncode, 0)
            self.assertIn("task id is required", invalid.stdout)
            self.assertEqual(count, 0)

    def test_invariant_checker_detects_tampered_accepted_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
            run_harness(root, "task", "add", "--id", "T1", "--task", "Example", "--acceptance", "AC1")
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute("update tasks set status = 'accepted', evidence = '', accepted_by = '' where id = 'T1'")
                conn.commit()

            invariant = run_harness(root, "invariant", "validate", check=False)

            self.assertNotEqual(invariant.returncode, 0)
            self.assertIn("accepted task has no evidence", invariant.stdout)
            self.assertIn("accepted task has no accept actor/event", invariant.stdout)

    def test_repair_clear_invariant_requires_confirm_and_repairs_expired_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
            run_harness(root, "task", "add", "--id", "T1", "--task", "Lease", "--acceptance", "AC1")
            claim = run_harness(root, "task", "claim", "T1", "--agent", "developer", "--expected-revision", "1")
            token = token_from_stdout(claim.stdout)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute("update tasks set lease_token = ?, lease_expires_at = '2000-01-01T00:00:00+00:00' where id = 'T1'", (token,))
                conn.commit()

            plan = run_harness(root, "repair", "--clear-invariant", "expired-lease", check=False)
            repaired = run_harness(root, "repair", "--clear-invariant", "expired-lease", "--confirm", "expired-lease")

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                task = conn.execute("select lease_agent, lease_token, lease_expires_at from tasks where id = 'T1'").fetchone()
                agent = conn.execute("select status, lease_task_id from agents where id = 'developer'").fetchone()

            self.assertNotEqual(plan.returncode, 0)
            self.assertIn("repair requires --confirm expired-lease", plan.stdout)
            self.assertEqual(repaired.returncode, 0, repaired.stdout + repaired.stderr)
            self.assertEqual(task, (None, None, None))
            self.assertEqual(agent, ("available", ""))

    def test_repair_clear_invariant_repairs_producer_self_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
            run_harness(root, "task", "add", "--id", "T1", "--task", "Self accepted", "--owner", "developer", "--acceptance", "AC1")
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute("update tasks set status = 'accepted', evidence = 'manual', accepted_by = 'developer' where id = 'T1'")
                conn.commit()

            repaired = run_harness(root, "repair", "--clear-invariant", "producer-self-accepted", "--confirm", "producer-self-accepted")

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                task = conn.execute("select status, accepted_by, evidence from tasks where id = 'T1'").fetchone()

            self.assertEqual(repaired.returncode, 0, repaired.stdout + repaired.stderr)
            self.assertEqual(task, ("review", "", "manual"))

    def test_directed_invariant_scope_checks_touched_entity_closure(self) -> None:
        from core.invariant_checker import check_runtime_invariants

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
            run_harness(root, "task", "add", "--id", "T1", "--task", "Self accepted", "--owner", "developer", "--acceptance", "AC1")
            run_harness(root, "task", "add", "--id", "T2", "--task", "Unrelated", "--owner", "developer", "--acceptance", "AC1")
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("update tasks set status = 'accepted', evidence = 'manual', accepted_by = 'developer' where id = 'T1'")
                conn.commit()
                issues = check_runtime_invariants(conn, root, scope=[("task", "T1")])

            messages = [str(issue) for issue in issues]
            self.assertTrue(any("producer accepted own task T1" in message for message in messages))
            self.assertFalse(any("T2" in message for message in messages))

    def test_event_validate_requires_replay_snapshots_after_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "checkpoint", "create", "--label", "start")
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute(
                    """
                    insert into events
                    (id, schema_version, type, source, target, payload_json, created_at)
                    values ('bad-replay-event', 11, 'task_tampered', 'test', 'task:T1',
                            '{"entity_type":"task","entity_id":"T1","command":"tamper"}', 'now')
                    """
                )
                conn.commit()

            event_validate = run_harness(root, "event", "validate", check=False)

            self.assertNotEqual(event_validate.returncode, 0)
            self.assertIn("missing after", event_validate.stdout)

    def test_projection_rebuild_restores_generated_views(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
            view = root / ".ai-team/requirements/acceptance.md"
            view.write_text("stale\n", encoding="utf-8")

            run_harness(root, "projection", "rebuild")

            self.assertIn("AC1", view.read_text(encoding="utf-8"))

    def test_projection_writes_are_isolated_to_core_projection_module(self) -> None:
        harness_db = REPO_ROOT / "plugins/codex-project-harness/scripts/harness_db.py"
        projections = REPO_ROOT / "plugins/codex-project-harness/core/projections.py"

        self.assertNotIn("write_state(", harness_db.read_text(encoding="utf-8"))
        self.assertIn("write_state(", projections.read_text(encoding="utf-8"))

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

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "plugins/codex-project-harness/scripts/harness.py"
DEFAULT_TEST_COMMAND = "python3 -B -m unittest test_harness_dummy.py"


def run_harness(
    root: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, str(HARNESS), "--root", str(root), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return result


def db_path(root: Path) -> Path:
    return root / ".ai-team/state/harness.db"


def add_dummy_target(root: Path) -> None:
    (root / "test_harness_dummy.py").write_text(
        "import unittest\n\n"
        "class HarnessDummyTest(unittest.TestCase):\n"
        "    def test_dummy(self):\n"
        "        self.assertTrue(True)\n",
        encoding="utf-8",
    )
    run_harness(
        root,
        "test-target",
        "add",
        "--id",
        "UNIT",
        "--kind",
        "unit",
        "--command-template",
        DEFAULT_TEST_COMMAND,
    )


def qualify_dummy_target(root: Path, acceptance_id: str, qualification_id: str) -> None:
    run_harness(
        root,
        "test-target",
        "qualify",
        "--id",
        qualification_id,
        "--target",
        "UNIT",
        "--acceptance",
        acceptance_id,
        "--rationale",
        "The structured unit target directly verifies this acceptance.",
        "--by",
        "test-controller",
    )


def submit_and_accept(root: Path, task_id: str) -> None:
    run_harness(root, "task", "start", task_id)
    run_harness(
        root,
        "task",
        "submit",
        task_id,
        "--context-id",
        "producer-context",
        "--evidence",
        "implemented",
    )
    run_harness(root, "task", "accept", task_id, "--evidence", "reviewed")


class HarnessOperatingSystemTest(unittest.TestCase):
    def test_missing_database_recovery_sentinel_precedes_init_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / ".ai-team/backups/recovery/migration-manifest.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text('{"status":"rollback-incomplete"}\n', encoding="utf-8")
            sentinel = root / ".ai-team/state/local-core-migration.lock"
            sentinel.parent.mkdir(parents=True)
            sentinel.write_text(
                json.dumps(
                    {
                        "pid": 999999,
                        "status": "rollback-incomplete",
                        "manifest_path": str(manifest),
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            for args in (
                ("status",),
                ("doctor",),
                ("validate",),
                ("quickstart", "status"),
            ):
                with self.subTest(args=args):
                    result = run_harness(root, *args, check=False)
                    output = (result.stdout + result.stderr).lower()
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("rollback-incomplete", output)
                    self.assertIn(str(manifest).lower(), output)
                    self.assertIn("do not remove", output)
                    self.assertNotIn("next:", output)
                    self.assertNotIn(" init", output)
                    self.assertFalse(db_path(root).exists())

    def test_task_dependencies_block_start_until_dependency_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
            run_harness(root, "task", "add", "--id", "T1", "--task", "First", "--acceptance", "AC1")
            run_harness(
                root,
                "task",
                "add",
                "--id",
                "T2",
                "--task",
                "Second",
                "--acceptance",
                "AC1",
                "--depends-on",
                "T1",
            )

            blocked = run_harness(root, "task", "start", "T2", check=False)
            submit_and_accept(root, "T1")
            started = run_harness(root, "task", "start", "T2")

        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("task dependencies are not accepted", blocked.stdout + blocked.stderr)
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)

    def test_acceptance_change_invalidates_validation_and_gate_until_reverified(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "requirement", "add", "--id", "R1", "--kind", "functional", "--body", "Example")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Original")
            run_harness(root, "requirement", "link", "--requirement", "R1", "--acceptance", "AC1")
            run_harness(root, "task", "add", "--id", "T1", "--task", "Example", "--acceptance", "AC1")
            submit_and_accept(root, "T1")
            run_harness(root, "baseline", "confirm", "--id", "B1", "--summary", "original", "--by", "test-controller")
            add_dummy_target(root)
            qualify_dummy_target(root, "AC1", "Q1")
            run_harness(root, "verify", "run", "--target", "UNIT", "--acceptance", "AC1")
            run_harness(root, "gate", "record", "--reviewer-context", "same-context-degraded", "--result", "pass", "--qualification", "Q1", "--residual-risk", "explicit low-risk degraded limitation")
            run_harness(root, "delivery", "ready")
            before = run_harness(root, "validate", "--delivery")

            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Changed")
            stale = run_harness(root, "validate", "--delivery", check=False)
            with closing(sqlite3.connect(db_path(root))) as conn:
                unresolved = conn.execute(
                    "select target_type from invalidations where resolved_at is null order by target_type"
                ).fetchall()

            run_harness(root, "baseline", "confirm", "--id", "B2", "--summary", "updated", "--by", "test-controller")
            qualify_dummy_target(root, "AC1", "Q2")
            run_harness(root, "verify", "run", "--target", "UNIT", "--acceptance", "AC1")
            run_harness(root, "gate", "record", "--reviewer-context", "same-context-degraded", "--result", "pass", "--qualification", "Q2", "--residual-risk", "explicit low-risk degraded limitation")
            run_harness(root, "delivery", "ready")
            after = run_harness(root, "validate", "--delivery")

        self.assertEqual(before.returncode, 0, before.stdout + before.stderr)
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("stale runtime artifact", stale.stdout + stale.stderr)
        self.assertEqual([row[0] for row in unresolved], ["quality_gate", "task", "validation"])
        self.assertEqual(after.returncode, 0, after.stdout + after.stderr)

    def test_requirement_change_makes_baseline_stale_until_refrozen(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "requirement", "add", "--id", "R1", "--kind", "functional", "--body", "Original")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
            run_harness(root, "requirement", "link", "--requirement", "R1", "--acceptance", "AC1")
            run_harness(root, "baseline", "freeze", "--id", "B1", "--summary", "original")

            run_harness(root, "requirement", "add", "--id", "R1", "--kind", "functional", "--body", "Changed")
            stale = run_harness(root, "baseline", "validate", check=False)
            diff = run_harness(root, "baseline", "diff", "--from", "B1")
            run_harness(root, "baseline", "freeze", "--id", "B2", "--summary", "updated")
            current = run_harness(root, "baseline", "validate")

        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("frozen baseline is stale", stale.stdout + stale.stderr)
        self.assertIn("requirements: changed", diff.stdout)
        self.assertEqual(current.returncode, 0, current.stdout + current.stderr)

    def test_traceability_requires_explicit_requirement_acceptance_link(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "requirement", "add", "--id", "R1", "--kind", "functional", "--body", "Example")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
            run_harness(root, "task", "add", "--id", "T1", "--task", "Example", "--acceptance", "AC1")
            submit_and_accept(root, "T1")
            add_dummy_target(root)
            qualify_dummy_target(root, "AC1", "Q1")
            run_harness(root, "verify", "run", "--target", "UNIT", "--acceptance", "AC1")

            blocked = run_harness(root, "trace", "validate", check=False)
            run_harness(root, "requirement", "link", "--requirement", "R1", "--acceptance", "AC1")
            allowed = run_harness(root, "trace", "validate")
            rendered = (root / ".ai-team/requirements/traceability.md").read_text(encoding="utf-8")

        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("requirement has no acceptance link", blocked.stdout + blocked.stderr)
        self.assertEqual(allowed.returncode, 0, allowed.stdout + allowed.stderr)
        self.assertIn("R1", rendered)
        self.assertIn("AC1", rendered)

    def test_doctor_rejects_malformed_compact_audit_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.execute(
                    """
                    insert into events
                    (id, schema_version, event_type, entity_type, entity_id, actor, command,
                     before_json, after_json, correlation_id, created_at)
                    values ('bad-event', 31, 'tampered', 'project', '1', 'test', 'tamper',
                            '{bad json', '{}', 'corr-bad-event', 'now')
                    """
                )
                conn.commit()

            doctor = run_harness(root, "doctor", check=False)

        self.assertNotEqual(doctor.returncode, 0)
        self.assertIn("invalid event before_json: bad-event", doctor.stdout + doctor.stderr)

    def test_doctor_rejects_schema30_row_type_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            with closing(sqlite3.connect(db_path(root))) as conn:
                candidate = conn.execute(
                    "select candidate_sha from delivery_cycles where id = 'CYCLE-current'"
                ).fetchone()[0]
                conn.execute(
                    """
                    insert into quality_gates
                    (id, sequence, cycle_id, candidate_sha, gate_status, gate,
                     producer_context_id, reviewer_context_id, review_status, result,
                     blocking_findings, residual_risk, reviewed_revision, created_at)
                    values ('bad-gate', 1, 'CYCLE-current', ?, 'active', 'independent_qa',
                            '', '', 'same-context-degraded', 'pass', '', '', 'oops', 'now')
                    """,
                    (candidate,),
                )
                conn.commit()

            doctor = run_harness(root, "doctor", check=False)

        self.assertNotEqual(doctor.returncode, 0)
        self.assertIn(
            "schema contract failed: quality_gates.bad-gate.reviewed_revision expected integer",
            doctor.stdout + doctor.stderr,
        )

    def test_doctor_detects_tampered_accepted_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "task", "add", "--id", "T1", "--task", "Example")
            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.execute(
                    "update tasks set status = 'accepted', evidence = '', accepted_by = '' where id = 'T1'"
                )
                conn.commit()

            doctor = run_harness(root, "doctor", check=False)

        output = doctor.stdout + doctor.stderr
        self.assertNotEqual(doctor.returncode, 0)
        self.assertIn("accepted task has no evidence T1", output)
        self.assertIn("accepted task has no accept actor/event T1", output)

    def test_doctor_fails_when_runtime_state_is_tracked_by_git(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=root, check=True)
            run_harness(root, "init")
            subprocess.run(
                ["git", "add", "-f", ".ai-team/state/harness.db"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )

            doctor = run_harness(root, "doctor", check=False)

        self.assertNotEqual(doctor.returncode, 0)
        self.assertIn("runtime state is tracked by git", doctor.stdout + doctor.stderr)

    def test_test_target_registry_is_structured_and_projected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            added = run_harness(
                root,
                "test-target",
                "add",
                "--id",
                "UNIT",
                "--kind",
                "unit",
                "--command-template",
                "python3 -m unittest",
                "--description",
                "Unit suite",
            )
            listed = run_harness(root, "test-target", "list")
            with closing(sqlite3.connect(db_path(root))) as conn:
                target = conn.execute(
                    "select id, kind, command_template, description from test_targets where id = 'UNIT'"
                ).fetchone()
            rendered = (root / ".ai-team/control/test-targets.md").read_text(encoding="utf-8")

        self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
        self.assertIn("UNIT", listed.stdout)
        self.assertEqual(target, ("UNIT", "unit", "python3 -m unittest", "Unit suite"))
        self.assertIn("python3 -m unittest", rendered)

    def test_projection_rebuild_restores_generated_view(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
            view = root / ".ai-team/requirements/acceptance.md"
            view.write_text("stale\n", encoding="utf-8")

            rebuilt = run_harness(root, "projection", "rebuild")
            rendered = view.read_text(encoding="utf-8")

        self.assertEqual(rebuilt.returncode, 0, rebuilt.stdout + rebuilt.stderr)
        self.assertIn("AC1", rendered)


if __name__ == "__main__":
    unittest.main()

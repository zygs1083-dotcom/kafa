from __future__ import annotations

import hashlib
import inspect
import json
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "codex-project-harness"
SCRIPTS = PLUGIN_ROOT / "scripts"
HARNESS = SCRIPTS / "harness.py"
for path in (PLUGIN_ROOT, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from core import delivery as delivery_policy  # noqa: E402
import harness_db  # noqa: E402


TARGET_DIGEST_FIELDS = (
    "kind",
    "command_template",
    "stack_profile",
    "container_image",
    "requires_sandbox",
    "requires_no_network",
    "result_format",
    "result_path",
)


def run_harness(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HARNESS), "--root", str(root), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def db_path(root: Path) -> Path:
    return root / ".ai-team/state/harness.db"


def query_scalar(root: Path, sql: str, values: tuple[object, ...] = ()) -> object:
    with closing(sqlite3.connect(db_path(root))) as conn:
        row = conn.execute(sql, values).fetchone()
        if row is None:
            raise AssertionError(f"query returned no row: {sql}")
        return row[0]


def state_snapshot(root: Path) -> tuple[tuple[object, ...], tuple[object, ...], int]:
    with closing(sqlite3.connect(db_path(root))) as conn:
        project = conn.execute(
            "select phase, status, scope_status, revision from project where id = 1"
        ).fetchone()
        cycle = conn.execute(
            "select status, phase, candidate_sha, closed_at from delivery_cycles "
            "where id = (select current_cycle_id from project where id = 1)"
        ).fetchone()
        delivery_count = int(conn.execute("select count(*) from deliveries").fetchone()[0])
    if project is None or cycle is None:
        raise AssertionError("project or current cycle is missing")
    return tuple(project), tuple(cycle), delivery_count


def write_arithmetic_target(root: Path, *, marker: str = "command-ran.marker") -> str:
    (root / "test_arithmetic.py").write_text(
        "import unittest\n"
        "from pathlib import Path\n\n"
        f"MARKER = Path('.ai-team/runtime/{marker}')\n"
        "MARKER.parent.mkdir(parents=True, exist_ok=True)\n"
        "MARKER.write_text('ran', encoding='utf-8')\n\n"
        "class ArithmeticTest(unittest.TestCase):\n"
        "    def test_addition(self):\n"
        "        self.assertEqual(2 + 2, 4)\n",
        encoding="utf-8",
    )
    return "python3 -B -m unittest test_arithmetic.py"


def initialize_with_audit_evidence(root: Path) -> None:
    """Create passing execution/gate facts without a delivery graph."""

    harness_db.init_runtime(root)
    command = write_arithmetic_target(root)
    harness_db.add_test_target(root, "ARITH", "unit", command, "arithmetic-only target")
    harness_db.verify_run(root, "ARITH")
    harness_db.record_gate(
        root,
        "same-context-degraded",
        "pass",
        residual_risk="low-risk audit-only fixture",
    )


def target_definition_digest(root: Path, target_id: str) -> str:
    with closing(sqlite3.connect(db_path(root))) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("select * from test_targets where id = ?", (target_id,)).fetchone()
        if row is None:
            raise AssertionError(f"missing target: {target_id}")
        payload = {field: row[field] for field in TARGET_DIGEST_FIELDS}
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def seed_qualification(root: Path, qualification_id: str = "Q-AC1-ARITH") -> str:
    """Seed the locked schema-31 fact so old runtime reaches the red assertion.

    The compatibility DDL is test-local. Production schema creation and public
    qualification creation are tested separately and remain expected-red.
    """

    with closing(sqlite3.connect(db_path(root))) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("pragma foreign_keys = on")
        conn.execute(
            """
            create table if not exists acceptance_target_qualifications (
                id text primary key,
                cycle_id text not null,
                acceptance_id text not null,
                acceptance_revision integer not null,
                target_id text not null,
                target_definition_sha256 text not null,
                rationale text not null,
                qualified_by text not null,
                created_at text not null
            )
            """
        )
        validation_columns = {
            str(row[1]) for row in conn.execute("pragma table_info(validations)").fetchall()
        }
        if "qualification_id" not in validation_columns:
            conn.execute("alter table validations add column qualification_id text")
        acceptance = conn.execute(
            "select cycle_id, revision from acceptance where id = 'AC1'"
        ).fetchone()
        if acceptance is None:
            raise AssertionError("AC1 is required before qualification")
        conn.execute(
            """
            insert into acceptance_target_qualifications
            (id, cycle_id, acceptance_id, acceptance_revision, target_id,
             target_definition_sha256, rationale, qualified_by, created_at)
            values (?, ?, 'AC1', ?, 'ARITH', ?, ?, 'test-controller',
                    '2026-07-20T00:00:00Z')
            """,
            (
                qualification_id,
                acceptance["cycle_id"],
                acceptance["revision"],
                target_definition_digest(root, "ARITH"),
                "explicit arithmetic-to-acceptance mapping",
            ),
        )
        conn.commit()
    return qualification_id


def ensure_gate_qualification_table(root: Path) -> None:
    with closing(sqlite3.connect(db_path(root))) as conn:
        conn.execute(
            """
            create table if not exists quality_gate_qualifications (
                gate_id text not null,
                qualification_id text not null,
                cycle_id text not null,
                candidate_sha text not null,
                primary key (gate_id, qualification_id)
            )
            """
        )
        conn.commit()


def link_latest_gate_for_compatibility(root: Path, qualification_id: str) -> None:
    ensure_gate_qualification_table(root)
    with closing(sqlite3.connect(db_path(root))) as conn:
        row = conn.execute(
            "select id, cycle_id, candidate_sha from quality_gates "
            "where gate_status = 'active' order by sequence desc limit 1"
        ).fetchone()
        if row is None:
            raise AssertionError("missing active gate")
        conn.execute(
            "insert or ignore into quality_gate_qualifications "
            "(gate_id, qualification_id, cycle_id, candidate_sha) values (?, ?, ?, ?)",
            (row[0], qualification_id, row[1], row[2]),
        )
        conn.commit()


def record_gate_with_optional_qualification(
    root: Path,
    qualification_id: str,
    *,
    review_qualification: bool,
    degraded: bool = False,
) -> None:
    kwargs: dict[str, object] = {
        "residual_risk": "explicit low-risk local residual risk",
    }
    if review_qualification and "qualifications" in inspect.signature(
        harness_db.record_gate
    ).parameters:
        kwargs["qualifications"] = [qualification_id]
    if degraded:
        harness_db.record_gate(
            root,
            "same-context-degraded",
            "pass",
            **kwargs,
        )
    else:
        harness_db.record_gate(
            root,
            "fresh",
            "pass",
            reviewer_context_id="reviewer-context",
            **kwargs,
        )
    if review_qualification and "qualifications" not in inspect.signature(
        harness_db.record_gate
    ).parameters:
        link_latest_gate_for_compatibility(root, qualification_id)


def force_phase(root: Path, phase: str) -> None:
    with closing(sqlite3.connect(db_path(root))) as conn:
        cycle_id = conn.execute("select current_cycle_id from project where id=1").fetchone()[0]
        conn.execute("update project set phase = ? where id = 1", (phase,))
        conn.execute("update delivery_cycles set phase = ? where id = ?", (phase, cycle_id))
        conn.commit()
    harness_db.render_all(root)


def prepare_full_graph(
    root: Path,
    *,
    review_qualification: bool = True,
    phase: str = "delivery_readiness",
    degraded: bool = False,
) -> str:
    harness_db.init_runtime(root)
    command = write_arithmetic_target(root)
    harness_db.add_requirement(root, "REQ1", "functional", "calculator remains correct")
    harness_db.add_acceptance(root, "AC1", "2 + 2 returns 4")
    harness_db.link_requirement_acceptance(root, "REQ1", "AC1")
    harness_db.add_test_target(root, "ARITH", "unit", command, "qualified arithmetic target")
    harness_db.add_task(root, "T1", "verify arithmetic", acceptance="AC1")
    harness_db.link_task_test_target(root, "T1", "ARITH")
    qualification_id = seed_qualification(root)
    harness_db.start_task(root, "T1")
    _, validation_id = harness_db.verify_run(root, "ARITH", acceptance="AC1")
    with closing(sqlite3.connect(db_path(root))) as conn:
        columns = {
            str(row[1]) for row in conn.execute("pragma table_info(validations)").fetchall()
        }
        if "qualification_id" in columns:
            conn.execute(
                "update validations set qualification_id = ? "
                "where id = ? and qualification_id is null",
                (qualification_id, validation_id),
            )
            conn.commit()
    harness_db.submit_task(
        root,
        "T1",
        "immutable qualified execution",
        context_id="producer-context",
    )
    harness_db.accept_task(root, "T1", "reviewed implementation evidence")
    harness_db.freeze_baseline(root, "BL1", "current confirmed graph", by="test-controller")
    harness_db.confirm_scope(root, "test-controller", "calculator scope confirmed")
    record_gate_with_optional_qualification(
        root,
        qualification_id,
        review_qualification=review_qualification,
        degraded=degraded,
    )
    force_phase(root, phase)
    return qualification_id


def blocker_codes(root: Path, mode: str) -> list[str]:
    evaluator = getattr(delivery_policy, "evaluate_delivery_prerequisites", None)
    if not callable(evaluator):
        raise AssertionError(
            "P0 contract missing API: core.delivery.evaluate_delivery_prerequisites"
        )
    with closing(sqlite3.connect(db_path(root))) as conn:
        conn.row_factory = sqlite3.Row
        blockers = evaluator(
            conn,
            root,
            mode=mode,
            is_expired=harness_db.is_expired,
            observed_at="2026-07-20T00:00:00Z",
        )
    return [str(blocker.code) for blocker in blockers]


def assert_delivery_unchanged(test: unittest.TestCase, root: Path) -> None:
    test.assertEqual(query_scalar(root, "select count(*) from deliveries"), 0)
    test.assertEqual(
        query_scalar(root, "select status from delivery_cycles where id='CYCLE-current'"),
        "active",
    )


class MinimumDeliveryGraphRedTests(unittest.TestCase):
    def test_empty_graph_direct_api_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            initialize_with_audit_evidence(root)

            with self.assertRaisesRegex(
                harness_db.HarnessError,
                r"\[requirement-missing\].*\[acceptance-missing\].*"
                r"\[baseline-missing\].*\[scope-unconfirmed\].*\[phase-not-ready\]",
            ):
                harness_db.record_delivery(root, "must fail closed")

            assert_delivery_unchanged(self, root)

    def test_empty_graph_cli_validation_and_record_fail_closed(self) -> None:
        for surface in ("validate", "record"):
            with self.subTest(surface=surface), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                initialize_with_audit_evidence(root)
                result = (
                    run_harness(root, "validate", "--delivery")
                    if surface == "validate"
                    else run_harness(root, "delivery", "record", "--scope", "must fail")
                )

                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                text = result.stdout + result.stderr
                for code in (
                    "requirement-missing",
                    "acceptance-missing",
                    "baseline-missing",
                    "scope-unconfirmed",
                    "phase-not-ready",
                ):
                    self.assertIn(f"[{code}]", text)
                assert_delivery_unchanged(self, root)

    def test_each_minimum_graph_gap_has_a_stable_blocker_code(self) -> None:
        cases = (
            ("requirement", "requirement-missing"),
            ("acceptance", "acceptance-missing"),
            ("link", "requirement-acceptance-link-missing"),
            ("orphan", "acceptance-orphaned"),
            ("baseline-missing", "baseline-missing"),
            ("baseline-stale", "baseline-stale"),
            ("scope", "scope-unconfirmed"),
            ("phase", "phase-not-ready"),
            ("accepted-task", "accepted-task-missing"),
        )
        for scenario, expected in cases:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                if scenario in {"requirement", "acceptance", "link", "orphan"}:
                    initialize_with_audit_evidence(root)
                    if scenario != "requirement":
                        harness_db.add_requirement(root, "REQ1", "functional", "required")
                    if scenario in {"link", "orphan"}:
                        harness_db.add_acceptance(root, "AC1", "covered acceptance")
                    if scenario == "orphan":
                        harness_db.link_requirement_acceptance(root, "REQ1", "AC1")
                        harness_db.add_acceptance(root, "AC2", "orphan acceptance")
                    harness_db.freeze_baseline(root, "BL1", "partial graph", by="test")
                else:
                    prepare_full_graph(root)
                    if scenario == "baseline-missing":
                        with closing(sqlite3.connect(db_path(root))) as conn:
                            conn.execute("delete from baselines")
                            conn.commit()
                    elif scenario == "baseline-stale":
                        harness_db.add_requirement(
                            root, "REQ1", "functional", "changed after baseline"
                        )
                    elif scenario == "scope":
                        with closing(sqlite3.connect(db_path(root))) as conn:
                            conn.execute(
                                "update project set scope_status='draft' where id=1"
                            )
                            conn.commit()
                    elif scenario == "phase":
                        force_phase(root, "implementation")
                    elif scenario == "accepted-task":
                        with closing(sqlite3.connect(db_path(root))) as conn:
                            conn.execute(
                                "update tasks set status='cancelled' where id='T1'"
                            )
                            conn.commit()

                self.assertIn(expected, blocker_codes(root, "record-delivery"))


class CancelledTaskCoverageRedTests(unittest.TestCase):
    def test_trace_requires_current_candidate_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_full_graph(root)
            self.assertEqual(harness_db.trace_validate(root), [])

            (root / "candidate_drift.py").write_text(
                "CANDIDATE_CHANGED = True\n",
                encoding="utf-8",
            )

            issues = harness_db.trace_validate(root)
            self.assertTrue(
                any(
                    "acceptance has no passing validation in trace" in issue
                    and "AC1" in issue
                    for issue in issues
                ),
                issues,
            )

    def test_trace_rejects_tampered_execution_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_full_graph(root)
            artifact = Path(
                str(
                    query_scalar(
                        root,
                        "select artifact_path from executions order by created_at desc limit 1",
                    )
                )
            )
            (root / artifact).write_text("tampered\n", encoding="utf-8")

            issues = harness_db.trace_validate(root)
            self.assertTrue(
                any("acceptance has no passing validation in trace" in issue for issue in issues),
                issues,
            )

    def test_trace_rejects_legacy_incomplete_schema31_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_full_graph(root)
            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.execute("drop trigger executions_no_update")
                conn.execute(
                    "update executions set provenance_status='legacy-incomplete'"
                )
                conn.commit()

            issues = harness_db.trace_validate(root)
            self.assertTrue(
                any("acceptance has no passing validation in trace" in issue for issue in issues),
                issues,
            )

    def test_trace_rejects_link_to_cancelled_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_full_graph(root)
            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.execute("update acceptance set status='cancelled' where id='AC1'")
                conn.commit()

            issues = harness_db.trace_validate(root)
            self.assertTrue(
                any("[requirement-acceptance-link-missing]" in issue for issue in issues),
                issues,
            )

    def test_trace_rejects_orphan_active_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_full_graph(root)
            harness_db.add_acceptance(root, "AC-ORPHAN", "unlinked active acceptance")

            issues = harness_db.trace_validate(root)
            self.assertTrue(
                any(
                    "[acceptance-orphaned]" in issue and "AC-ORPHAN" in issue
                    for issue in issues
                ),
                issues,
            )

    def test_trace_accepts_active_replacement_for_cancelled_acceptance_history(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_full_graph(root)
            harness_db.add_acceptance(root, "AC2", "active replacement")
            harness_db.link_requirement_acceptance(root, "REQ1", "AC2")
            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.execute("update acceptance set status='cancelled' where id='AC1'")
                conn.commit()

            structural = [
                issue
                for issue in harness_db.trace_validate(root)
                if "[requirement-acceptance-link-missing]" in issue
                or "[acceptance-orphaned]" in issue
            ]
            self.assertEqual(structural, [])

    def prepare_cancelled_sole_coverage(self, root: Path) -> None:
        harness_db.quickstart_minimal(
            root,
            "CANCEL",
            "cancelled work must not deliver",
            "cancelled work is not completed work",
            "produce evidence",
            write_arithmetic_target(root),
            execute=True,
        )
        harness_db.cancel_task(root, "CANCEL-T1", "cancelled before acceptance")
        qualification_ids: list[str] = []
        with closing(sqlite3.connect(db_path(root))) as conn:
            if conn.execute(
                "select 1 from sqlite_master where type='table' "
                "and name='acceptance_target_qualifications'"
            ).fetchone():
                qualification_ids = [
                    str(row[0])
                    for row in conn.execute(
                        "select id from acceptance_target_qualifications order by id"
                    ).fetchall()
                ]
        kwargs: dict[str, object] = {
            "residual_risk": "explicit low-risk local residual risk"
        }
        if "qualifications" in inspect.signature(harness_db.record_gate).parameters:
            kwargs["qualifications"] = qualification_ids
        harness_db.record_gate(root, "same-context-degraded", "pass", **kwargs)
        force_phase(root, "delivery_readiness")

    def test_cancelled_sole_coverage_is_rejected_by_every_delivery_surface(self) -> None:
        for surface in ("trace", "validate", "api"):
            with self.subTest(surface=surface), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                self.prepare_cancelled_sole_coverage(root)
                if surface == "trace":
                    issues = harness_db.trace_validate(root)
                    self.assertTrue(
                        any("[accepted-task-missing]" in issue for issue in issues),
                        issues,
                    )
                elif surface == "validate":
                    result = run_harness(root, "validate", "--delivery")
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("[accepted-task-missing]", result.stdout + result.stderr)
                else:
                    with self.assertRaisesRegex(
                        harness_db.HarnessError, r"\[accepted-task-missing\]"
                    ):
                        harness_db.record_delivery(root, "cancelled task must not deliver")
                    assert_delivery_unchanged(self, root)

    def test_quickstart_cancelled_coverage_requires_replacement_before_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.quickstart_minimal(
                root,
                "CANCEL",
                "cancelled work must not deliver",
                "cancelled work is not completed work",
                "produce evidence",
                write_arithmetic_target(root),
                execute=True,
            )
            harness_db.cancel_task(root, "CANCEL-T1", "cancelled before acceptance")

            status = harness_db.quickstart_status(root)
            self.assertIn("accepted_task", status["missing"])
            self.assertNotIn(
                "gate record",
                "\n".join(status["next_commands"]),
            )
            replacement = next(
                command
                for command in status["next_commands"]
                if " task add " in f" {command} "
            )
            result = subprocess.run(
                shlex.split(replacement),
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unrelated_cancelled_task_does_not_block_accepted_graph(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_full_graph(root)
            harness_db.add_task(root, "T-CANCELLED", "unrelated historical task")
            harness_db.start_task(root, "T-CANCELLED")
            harness_db.cancel_task(root, "T-CANCELLED", "out of scope")

            harness_db.record_delivery(root, "accepted graph with unrelated cancellation")

            self.assertEqual(query_scalar(root, "select count(*) from deliveries"), 1)
            self.assertEqual(
                query_scalar(root, "select status from tasks where id='T-CANCELLED'"),
                "cancelled",
            )

    def test_unlinked_planned_task_does_not_override_per_acceptance_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_full_graph(root)
            harness_db.add_task(root, "T-FUTURE", "future unlinked work")

            harness_db.record_delivery(root, "accepted graph is independently complete")

            self.assertEqual(query_scalar(root, "select count(*) from deliveries"), 1)
            self.assertEqual(
                query_scalar(root, "select status from tasks where id='T-FUTURE'"),
                "planned",
            )

    def test_accepted_replacement_is_sufficient_for_cancelled_original(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_full_graph(root)
            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.execute("update tasks set status='cancelled' where id='T1'")
                conn.execute(
                    "insert into tasks "
                    "(id, cycle_id, task, owner, status, evidence, submitted_context_id, "
                    "accepted_by, revision, updated_at) "
                    "select 'T2', cycle_id, 'replacement', owner, 'accepted', "
                    "'replacement reviewed', 'producer-context', 'root-controller', 1, updated_at "
                    "from tasks where id='T1'"
                )
                conn.execute(
                    "insert into task_acceptance (cycle_id, task_id, acceptance_id) "
                    "select cycle_id, 'T2', acceptance_id from task_acceptance where task_id='T1'"
                )
                conn.commit()

            harness_db.record_delivery(root, "accepted replacement coverage")

            self.assertEqual(query_scalar(root, "select count(*) from deliveries"), 1)


class QualifiedAcceptanceEvidenceRedTests(unittest.TestCase):
    def prepare_unqualified_mapping(self, root: Path) -> Path:
        harness_db.init_runtime(root)
        command = write_arithmetic_target(root, marker="unqualified-command-ran.marker")
        harness_db.add_acceptance(root, "AC1", "expired cards are rejected")
        harness_db.add_test_target(
            root,
            "ARITH",
            "unit",
            command,
            "arithmetic is unrelated to expired-card behavior",
        )
        return root / ".ai-team/runtime/unqualified-command-ran.marker"

    def test_existing_unrelated_target_cannot_claim_acceptance_without_qualification(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            marker = self.prepare_unqualified_mapping(root)

            with self.assertRaisesRegex(
                harness_db.HarnessError, r"\[qualification-missing\].*AC1"
            ):
                harness_db.verify_run(root, "ARITH", acceptance="AC1")

            self.assertFalse(marker.exists(), "verification command ran before qualification")
            self.assertEqual(query_scalar(root, "select count(*) from executions"), 0)
            self.assertEqual(query_scalar(root, "select count(*) from validations"), 0)

    def test_public_qualification_command_records_current_revision_and_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.prepare_unqualified_mapping(root)
            result = run_harness(
                root,
                "test-target",
                "qualify",
                "--id",
                "Q1",
                "--target",
                "ARITH",
                "--acceptance",
                "AC1",
                "--rationale",
                "explicit expired-card mapping review",
                "--by",
                "test-controller",
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            with closing(sqlite3.connect(db_path(root))) as conn:
                row = conn.execute(
                    "select acceptance_revision, target_definition_sha256, rationale, "
                    "qualified_by from acceptance_target_qualifications where id='Q1'"
                ).fetchone()
            self.assertEqual(
                row,
                (
                    1,
                    target_definition_digest(root, "ARITH"),
                    "explicit expired-card mapping review",
                    "test-controller",
                ),
            )

    def test_acceptance_revision_makes_qualification_stale_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            marker = self.prepare_unqualified_mapping(root)
            seed_qualification(root)
            harness_db.add_acceptance(root, "AC1", "changed expired-card criterion")

            with self.assertRaisesRegex(
                harness_db.HarnessError, r"\[qualification-stale\].*AC1"
            ):
                harness_db.verify_run(root, "ARITH", acceptance="AC1")

            self.assertFalse(marker.exists())

    def test_each_execution_relevant_target_change_makes_qualification_stale(self) -> None:
        mutations: dict[str, dict[str, object]] = {
            "command": {"command_template": "python3 -B -m unittest discover -s ."},
            "kind": {"kind": "integration"},
            "result-format": {"result_format": "junit", "result_path": "result.xml"},
            "sandbox": {"requires_sandbox": True},
            "no-network": {"requires_no_network": True},
            "result-path": {"result_path": ".ai-team/runtime/result.txt"},
            "stack": {"stack_profile": "node"},
            "container-image": {
                "container_image": "example.invalid/kafa-test@sha256:" + "1" * 64
            },
        }
        for name, changes in mutations.items():
            with self.subTest(field=name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                marker = self.prepare_unqualified_mapping(root)
                seed_qualification(root)
                values: dict[str, object] = {
                    "kind": "unit",
                    "command_template": "python3 -B -m unittest test_arithmetic.py",
                    "description": "presentation text may change without digest impact",
                    "stack_profile": "python",
                    "container_image": "",
                    "requires_sandbox": False,
                    "requires_no_network": False,
                    "result_format": "regex",
                    "result_path": "",
                }
                values.update(changes)
                harness_db.add_test_target(
                    root,
                    "ARITH",
                    str(values["kind"]),
                    str(values["command_template"]),
                    str(values["description"]),
                    stack_profile=str(values["stack_profile"]),
                    container_image=str(values["container_image"]),
                    requires_sandbox=bool(values["requires_sandbox"]),
                    requires_no_network=bool(values["requires_no_network"]),
                    result_format=str(values["result_format"]),
                    result_path=str(values["result_path"]),
                )

                with self.assertRaisesRegex(
                    harness_db.HarnessError, r"\[qualification-stale\].*ARITH"
                ):
                    harness_db.verify_run(root, "ARITH", acceptance="AC1")

                self.assertFalse(marker.exists())

    def test_qualification_cannot_be_reused_for_another_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            marker = self.prepare_unqualified_mapping(root)
            harness_db.add_acceptance(root, "AC2", "a different acceptance")
            seed_qualification(root)

            with self.assertRaisesRegex(
                harness_db.HarnessError, r"\[qualification-missing\].*AC2"
            ):
                harness_db.verify_run(root, "ARITH", acceptance="AC2")

            self.assertFalse(marker.exists())

    def test_latest_qualification_supersedes_older_digest_after_target_revert(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            marker = self.prepare_unqualified_mapping(root)
            harness_db.qualify_test_target(
                root,
                "Q9",
                "ARITH",
                "AC1",
                "original mapping",
                "controller",
            )
            harness_db.add_test_target(
                root,
                "ARITH",
                "unit",
                "python3 -B -m unittest discover -s .",
                "changed target",
            )
            harness_db.qualify_test_target(
                root,
                "Q10",
                "ARITH",
                "AC1",
                "replacement mapping",
                "controller",
            )
            harness_db.add_test_target(
                root,
                "ARITH",
                "unit",
                "python3 -B -m unittest test_arithmetic.py",
                "reverted target",
            )

            with self.assertRaisesRegex(
                harness_db.HarnessError,
                r"\[qualification-stale\].*Q10",
            ):
                harness_db.verify_run(root, "ARITH", acceptance="AC1")

            self.assertFalse(marker.exists(), "superseded Q9 was incorrectly revived")

    def test_qualification_idempotency_conflict_blank_and_cross_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.prepare_unqualified_mapping(root)
            values = ("Q1", "ARITH", "AC1", "explicit mapping", "controller")
            harness_db.qualify_test_target(root, *values)
            harness_db.qualify_test_target(root, *values)
            self.assertEqual(
                query_scalar(
                    root,
                    "select count(*) from acceptance_target_qualifications where id='Q1'",
                ),
                1,
            )
            self.assertEqual(
                query_scalar(
                    root,
                    "select count(*) from events where event_type='acceptance_target_qualified' "
                    "and entity_id='Q1'",
                ),
                1,
            )
            with self.assertRaisesRegex(
                harness_db.HarnessError,
                "conflicting immutable qualification",
            ):
                harness_db.qualify_test_target(
                    root, "Q1", "ARITH", "AC1", "different mapping", "controller"
                )
            with self.assertRaisesRegex(
                harness_db.HarnessError,
                "requires non-empty rationale",
            ):
                harness_db.qualify_test_target(
                    root, "Q-BLANK", "ARITH", "AC1", "   ", "controller"
                )

            harness_db.cycle_close(root, "archived")
            harness_db.cycle_start(root, "CYCLE-2", "second", "cross-cycle check")
            with self.assertRaisesRegex(
                harness_db.HarnessError,
                "cross-cycle qualification is not allowed",
            ):
                harness_db.qualify_test_target(
                    root, "Q-CROSS", "ARITH", "AC1", "invalid reuse", "controller"
                )

    def test_presentation_only_target_change_keeps_qualification_current(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.prepare_unqualified_mapping(root)
            harness_db.qualify_test_target(
                root, "Q1", "ARITH", "AC1", "explicit mapping", "controller"
            )
            before = target_definition_digest(root, "ARITH")
            harness_db.add_test_target(
                root,
                "ARITH",
                "unit",
                "python3 -B -m unittest test_arithmetic.py",
                "presentation-only description changed",
            )
            self.assertEqual(target_definition_digest(root, "ARITH"), before)

            _, validation_id = harness_db.verify_run(
                root, "ARITH", acceptance="AC1"
            )

            self.assertEqual(
                query_scalar(
                    root,
                    "select qualification_id from validations where id=?",
                    (validation_id,),
                ),
                "Q1",
            )

    def test_gate_without_exact_qualification_review_cannot_deliver(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_full_graph(root, review_qualification=False)

            with self.assertRaisesRegex(
                harness_db.HarnessError, r"\[qualification-unreviewed\].*Q-AC1-ARITH"
            ):
                harness_db.record_delivery(root, "gate did not review mapping")

            assert_delivery_unchanged(self, root)

    def test_gate_links_repeatable_current_qualifications_and_rejects_superseded_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            write_arithmetic_target(root)
            harness_db.add_acceptance(root, "AC1", "first behavior")
            harness_db.add_acceptance(root, "AC2", "second behavior")
            harness_db.add_test_target(
                root,
                "ARITH",
                "unit",
                "python3 -B -m unittest test_arithmetic.py",
                "shared qualified target",
            )
            for acceptance_id, qualification_id in (("AC1", "Q1"), ("AC2", "Q2")):
                harness_db.qualify_test_target(
                    root,
                    qualification_id,
                    "ARITH",
                    acceptance_id,
                    f"explicit mapping for {acceptance_id}",
                    "controller",
                )
                harness_db.verify_run(
                    root, "ARITH", acceptance=acceptance_id
                )
            harness_db.record_gate(
                root,
                "same-context-degraded",
                "pass",
                residual_risk="low-risk local review",
                qualifications=["Q1", "Q2"],
            )
            self.assertEqual(
                query_scalar(
                    root,
                    "select count(*) from quality_gate_qualifications",
                ),
                2,
            )

            harness_db.qualify_test_target(
                root,
                "Q3",
                "ARITH",
                "AC1",
                "newest mapping supersedes Q1",
                "controller",
            )
            with self.assertRaisesRegex(
                harness_db.HarnessError, r"\[qualification-stale\].*Q1"
            ):
                harness_db.record_gate(
                    root,
                    "same-context-degraded",
                    "pass",
                    residual_risk="must not revive Q1",
                    qualifications=["Q1"],
                )
            with self.assertRaisesRegex(
                harness_db.HarnessError, "qualification IDs must be non-empty"
            ):
                harness_db.record_gate(
                    root,
                    "same-context-degraded",
                    "pass",
                    residual_risk="blank is invalid",
                    qualifications=[" "],
                )
            harness_db.cycle_close(root, "archived")
            harness_db.cycle_start(root, "CYCLE-2", "second", "cross-cycle gate")
            with self.assertRaisesRegex(
                harness_db.HarnessError,
                "cross-cycle gate qualification is not allowed",
            ):
                harness_db.record_gate(
                    root,
                    "same-context-degraded",
                    "pass",
                    residual_risk="old-cycle mapping is ineligible",
                    qualifications=["Q2"],
                )

    def test_degraded_gate_link_is_not_labelled_independent(self) -> None:
        self.assertIn(
            "qualifications",
            inspect.signature(harness_db.record_gate).parameters,
            "P0 contract missing record_gate qualifications API",
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            qualification_id = prepare_full_graph(root, degraded=True)
            with closing(sqlite3.connect(db_path(root))) as conn:
                row = conn.execute(
                    """
                    select g.review_status
                    from quality_gate_qualifications qg
                    join quality_gates g on g.id = qg.gate_id
                    where qg.qualification_id = ? and g.gate_status = 'active'
                    """,
                    (qualification_id,),
                ).fetchone()
            self.assertEqual(row, ("same-context-degraded",))
            self.assertNotEqual(row, ("reviewed-local",))


class UnifiedPrerequisiteAndReadinessRedTests(unittest.TestCase):
    def test_cycle_close_cannot_claim_delivered_outside_canonical_delivery_record(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            api_root = Path(temp) / "api"
            harness_db.init_runtime(api_root)

            with self.assertRaisesRegex(
                Exception,
                "delivery record",
            ):
                harness_db.cycle_close(api_root, "delivered")

            with closing(sqlite3.connect(db_path(api_root))) as conn:
                self.assertEqual(
                    conn.execute(
                        "select status from delivery_cycles where id='CYCLE-current'"
                    ).fetchone()[0],
                    "active",
                )
                self.assertEqual(
                    conn.execute("select count(*) from deliveries").fetchone()[0],
                    0,
                )

            cli_root = Path(temp) / "cli"
            self.assertEqual(run_harness(cli_root, "init").returncode, 0)
            rejected = run_harness(
                cli_root,
                "cycle",
                "close",
                "--status",
                "delivered",
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("delivery record", rejected.stdout + rejected.stderr)
            with closing(sqlite3.connect(db_path(cli_root))) as conn:
                self.assertEqual(
                    conn.execute(
                        "select status from delivery_cycles where id='CYCLE-current'"
                    ).fetchone()[0],
                    "active",
                )
                self.assertEqual(
                    conn.execute("select count(*) from deliveries").fetchone()[0],
                    0,
                )

    def test_requirement_missing_code_is_shared_by_all_surfaces(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "evaluator"
            root.mkdir()
            initialize_with_audit_evidence(root)
            self.assertIn(
                "requirement-missing", blocker_codes(root, "enter-readiness")
            )

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "quickstart"
            root.mkdir()
            initialize_with_audit_evidence(root)
            report = harness_db.quickstart_status(root)
            codes = [str(item["code"]) for item in report.get("delivery_blockers", [])]
            self.assertIn("requirement-missing", codes)

        for surface, args in (
            ("readiness", ("delivery", "ready")),
            ("validate", ("validate", "--delivery")),
            ("cli-record", ("delivery", "record", "--scope", "must fail")),
        ):
            with self.subTest(surface=surface), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                initialize_with_audit_evidence(root)
                result = run_harness(root, *args)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("[requirement-missing]", result.stdout + result.stderr)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            initialize_with_audit_evidence(root)
            with self.assertRaisesRegex(
                harness_db.HarnessError, r"\[requirement-missing\]"
            ):
                harness_db.record_delivery(root, "must fail")

    def test_enter_readiness_does_not_require_readiness_phase_in_advance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_full_graph(root, phase="qa")

            self.assertNotIn(
                "phase-not-ready", blocker_codes(root, "enter-readiness")
            )
            enter = getattr(harness_db, "enter_delivery_readiness", None)
            self.assertTrue(
                callable(enter),
                "P0 contract missing API: harness_db.enter_delivery_readiness",
            )
            enter(root)

            self.assertEqual(query_scalar(root, "select phase from project where id=1"), "delivery_readiness")
            self.assertEqual(
                query_scalar(root, "select phase from delivery_cycles where id='CYCLE-current'"),
                "delivery_readiness",
            )

    def test_record_mode_requires_readiness_even_when_all_evidence_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_full_graph(root, phase="implementation")

            self.assertIn("phase-not-ready", blocker_codes(root, "record-delivery"))
            with self.assertRaisesRegex(
                harness_db.HarnessError, r"\[phase-not-ready\]"
            ):
                harness_db.record_delivery(root, "premature direct API")

            assert_delivery_unchanged(self, root)

    def test_delivered_consistency_detects_missing_row_and_phase_split(self) -> None:
        for mutation, expected in (
            ("missing-row", "delivery-row-missing"),
            ("phase-split", "delivered-phase-inconsistent"),
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                prepare_full_graph(root)
                harness_db.record_delivery(root, "consistent delivery")
                with closing(sqlite3.connect(db_path(root))) as conn:
                    if mutation == "missing-row":
                        conn.execute("delete from deliveries")
                    else:
                        conn.execute("update project set phase='implementation' where id=1")
                    conn.commit()

                self.assertIn(
                    expected,
                    blocker_codes(root, "delivered-consistency"),
                )

    def test_failed_delivery_ready_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            initialize_with_audit_evidence(root)
            before = state_snapshot(root)

            result = run_harness(root, "delivery", "ready")

            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("[requirement-missing]", result.stdout + result.stderr)
            self.assertEqual(state_snapshot(root), before)

    def test_baseline_confirm_is_atomic_and_plain_freeze_revokes_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            harness_db.add_requirement(root, "REQ1", "functional", "required")
            harness_db.add_acceptance(root, "AC1", "accepted behavior")
            harness_db.link_requirement_acceptance(root, "REQ1", "AC1")
            before = state_snapshot(root)
            original_emit = harness_db.emit_audit_event

            def fail_confirmation(*args: object, **kwargs: object) -> None:
                if args[1] == "baseline_confirmed":
                    raise RuntimeError("injected confirmation event failure")
                original_emit(*args, **kwargs)

            with mock.patch.object(
                harness_db, "emit_audit_event", side_effect=fail_confirmation
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "injected confirmation event failure"
                ):
                    harness_db.confirm_baseline(
                        root, "B-FAIL", "must rollback", by="controller"
                    )
            self.assertEqual(state_snapshot(root), before)
            self.assertEqual(query_scalar(root, "select count(*) from baselines"), 0)

            harness_db.confirm_baseline(
                root, "B1", "confirmed exact graph", by="controller"
            )
            self.assertEqual(
                query_scalar(root, "select scope_status from project where id=1"),
                "confirmed",
            )
            event_payload = json.loads(
                str(
                    query_scalar(
                        root,
                        "select after_json from events where event_type='baseline_confirmed' "
                        "order by sequence desc limit 1",
                    )
                )
            )
            self.assertEqual(event_payload["id"], "B1")
            self.assertEqual(
                event_payload["digest"],
                query_scalar(root, "select digest from baselines where id='B1'"),
            )

            harness_db.freeze_baseline(root, "B2", "snapshot only", by="controller")
            self.assertEqual(
                query_scalar(root, "select scope_status from project where id=1"),
                "unconfirmed",
            )
            self.assertIn("scope-unconfirmed", blocker_codes(root, "enter-readiness"))

    def test_latest_baseline_uses_write_order_not_user_controlled_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            harness_db.add_requirement(root, "REQ1", "functional", "first graph")
            harness_db.add_acceptance(root, "AC1", "accepted behavior")
            harness_db.link_requirement_acceptance(root, "REQ1", "AC1")
            fixed = "2026-07-21T00:00:00Z"

            with mock.patch.object(harness_db, "now_iso", return_value=fixed):
                harness_db.confirm_baseline(
                    root, "Z-OLDER", "older same-second baseline", by="controller"
                )
                harness_db.add_requirement(
                    root, "REQ1", "functional", "second graph"
                )
                harness_db.confirm_baseline(
                    root, "A-NEWER", "newer same-second baseline", by="controller"
                )

            self.assertEqual(harness_db.baseline_validate(root), [])
            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.row_factory = sqlite3.Row
                from core.cycle_ledger import latest_baseline

                latest = latest_baseline(conn)
            self.assertIsNotNone(latest)
            self.assertEqual(str(latest["id"]), "A-NEWER")

    def test_same_id_baseline_rewrite_remains_the_latest_fact(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            harness_db.add_requirement(root, "REQ1", "functional", "first graph")
            harness_db.add_acceptance(root, "AC1", "accepted behavior")
            harness_db.link_requirement_acceptance(root, "REQ1", "AC1")
            fixed = "2026-07-21T00:00:00Z"

            with mock.patch.object(harness_db, "now_iso", return_value=fixed):
                harness_db.confirm_baseline(
                    root, "BL-SAME", "first snapshot", by="controller"
                )
                harness_db.add_requirement(
                    root, "REQ1", "functional", "intermediate graph"
                )
                harness_db.confirm_baseline(
                    root, "Z-OTHER", "intermediate snapshot", by="controller"
                )
                harness_db.add_requirement(
                    root, "REQ1", "functional", "rewritten graph"
                )
                harness_db.confirm_baseline(
                    root, "BL-SAME", "rewritten snapshot", by="controller"
                )

            self.assertEqual(harness_db.baseline_validate(root), [])
            self.assertEqual(
                query_scalar(root, "select summary from baselines where id='BL-SAME'"),
                "rewritten snapshot",
            )

    def test_record_candidate_checks_rollback_before_insert_and_before_commit(self) -> None:
        for changed_on_call in (1, 2, 3):
            with self.subTest(changed_on_call=changed_on_call), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                prepare_full_graph(root)
                real_candidate = harness_db.current_candidate_sha(root)
                values = [real_candidate, real_candidate, real_candidate]
                values[changed_on_call - 1] = "changed-candidate"
                with mock.patch.object(
                    harness_db, "current_candidate_sha", side_effect=values
                ):
                    with self.assertRaisesRegex(
                        harness_db.HarnessError, "stale candidate"
                    ):
                        harness_db.record_delivery(root, "candidate race")
                assert_delivery_unchanged(self, root)

    def test_delivered_consistency_detects_candidate_and_close_corruption(self) -> None:
        for mutation, expected in (
            ("candidate", "delivered-candidate-inconsistent"),
            ("close", "delivered-cycle-not-closed"),
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                prepare_full_graph(root)
                harness_db.record_delivery(root, "consistent delivery")
                with closing(sqlite3.connect(db_path(root))) as conn:
                    if mutation == "candidate":
                        conn.execute(
                            "update delivery_cycles set candidate_sha='corrupt' "
                            "where id='CYCLE-current'"
                        )
                    else:
                        conn.execute(
                            "update delivery_cycles set closed_at='' "
                            "where id='CYCLE-current'"
                        )
                    conn.commit()
                self.assertIn(expected, blocker_codes(root, "delivered-consistency"))

    def test_delivered_cycle_rejects_public_graph_mutations_until_new_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_full_graph(root)
            harness_db.record_delivery(root, "closed graph")

            with closing(sqlite3.connect(db_path(root))) as conn:
                before = tuple(
                    int(conn.execute(f"select count(*) from {table}").fetchone()[0])
                    for table in (
                        "requirements",
                        "acceptance",
                        "failure_modes",
                        "tasks",
                        "test_targets",
                        "acceptance_target_qualifications",
                        "validations",
                        "findings",
                        "quality_gates",
                        "baselines",
                    )
                )

            operations = (
                (
                    "requirement",
                    lambda: harness_db.add_requirement(
                        root, "REQ-CLOSED", "functional", "must not mutate"
                    ),
                ),
                (
                    "acceptance",
                    lambda: harness_db.add_acceptance(
                        root, "AC-CLOSED", "must not mutate"
                    ),
                ),
                (
                    "failure-mode",
                    lambda: harness_db.add_failure_mode(
                        root,
                        "FM-CLOSED",
                        "closed cycle",
                        "mutation",
                        "public API call",
                        "reject",
                    ),
                ),
                (
                    "requirement-link",
                    lambda: harness_db.link_requirement_acceptance(
                        root, "REQ1", "AC1"
                    ),
                ),
                (
                    "task",
                    lambda: harness_db.add_task(root, "T-CLOSED", "must not mutate"),
                ),
                (
                    "verify",
                    lambda: harness_db.verify_run(root, "ARITH", acceptance="AC1"),
                ),
                (
                    "validation",
                    lambda: harness_db.record_validation(
                        root, "closed-cycle", "must not mutate", "pass"
                    ),
                ),
                (
                    "finding",
                    lambda: harness_db.record_finding(
                        root,
                        "F-CLOSED",
                        "closed-cycle",
                        "low",
                        "open",
                        "must not mutate",
                    ),
                ),
                (
                    "gate",
                    lambda: harness_db.record_gate(
                        root, "same-context-degraded", "fail"
                    ),
                ),
                (
                    "baseline",
                    lambda: harness_db.freeze_baseline(
                        root, "BL-CLOSED", "must not mutate"
                    ),
                ),
                (
                    "scope",
                    lambda: harness_db.confirm_scope(
                        root, "controller", "must not mutate"
                    ),
                ),
                (
                    "qualification",
                    lambda: harness_db.qualify_test_target(
                        root,
                        "Q-CLOSED",
                        "ARITH",
                        "AC1",
                        "must not mutate",
                        "controller",
                    ),
                ),
                (
                    "task-target-link",
                    lambda: harness_db.link_task_test_target(root, "T1", "ARITH"),
                ),
                (
                    "test-target",
                    lambda: harness_db.add_test_target(
                        root,
                        "TARGET-CLOSED",
                        "unit",
                        "python3 -B -m unittest test_arithmetic.py",
                    ),
                ),
            )
            for name, operation in operations:
                with self.subTest(operation=name):
                    with self.assertRaisesRegex(
                        harness_db.HarnessError,
                        r"current cycle is closed for mutation: .*status=delivered",
                    ):
                        operation()

            cli = run_harness(
                root,
                "requirement",
                "add",
                "--id",
                "REQ-CLI-CLOSED",
                "--kind",
                "functional",
                "--body",
                "must not mutate",
            )
            self.assertNotEqual(cli.returncode, 0, cli.stdout + cli.stderr)
            self.assertIn("current cycle is closed for mutation", cli.stdout + cli.stderr)

            with closing(sqlite3.connect(db_path(root))) as conn:
                after = tuple(
                    int(conn.execute(f"select count(*) from {table}").fetchone()[0])
                    for table in (
                        "requirements",
                        "acceptance",
                        "failure_modes",
                        "tasks",
                        "test_targets",
                        "acceptance_target_qualifications",
                        "validations",
                        "findings",
                        "quality_gates",
                        "baselines",
                    )
                )
            self.assertEqual(after, before)

            harness_db.record_outcome_observation(
                root,
                "OBS-CLOSED",
                "escaped-defect",
                0,
                "explicit observed zero remains allowed after delivery",
                "controller",
                "2026-07-21T00:00:00Z",
            )
            harness_db.record_decision(root, "retain evidence", "post-delivery audit")
            harness_db.cycle_start(root, "CYCLE-next", "next", "new delivery work")
            harness_db.add_requirement(
                root, "REQ-NEXT", "functional", "new cycle accepts mutation"
            )
            self.assertEqual(
                query_scalar(
                    root,
                    "select count(*) from requirements where cycle_id='CYCLE-next' "
                    "and id='REQ-NEXT'",
                ),
                1,
            )

    def test_delivered_consistency_replays_graph_after_direct_db_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_full_graph(root)
            harness_db.record_delivery(root, "closed graph")

            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.execute(
                    """
                    insert into requirements
                    (id, cycle_id, kind, body, priority, status, revision, updated_at)
                    values ('REQ-CORRUPT', 'CYCLE-current', 'functional',
                            'unlinked post-delivery mutation', '', 'active', 1,
                            '2026-07-21T00:00:00Z')
                    """
                )
                conn.commit()

            codes = blocker_codes(root, "delivered-consistency")
            self.assertIn("requirement-acceptance-link-missing", codes)
            self.assertIn("baseline-stale", codes)
            self.assertTrue(harness_db.validate_runtime(root, delivery=True))
            status = harness_db.quickstart_status(root)
            self.assertFalse(status["ready_for_delivery"])
            self.assertEqual(status["delivery_evaluation_mode"], "delivered-consistency")
            self.assertEqual(status["next_commands"], [])

    def test_delivered_consistency_normalizes_cancelled_task_blocker_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_full_graph(root)
            harness_db.record_delivery(root, "closed graph")
            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.execute("update tasks set status='cancelled' where id='T1'")
                conn.commit()

            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.row_factory = sqlite3.Row
                report = delivery_policy.evaluate_delivery_report(
                    conn,
                    root,
                    mode="delivered-consistency",
                    is_expired=harness_db.is_expired,
                )
            blockers = [
                blocker
                for blocker in report.blockers
                if blocker.code == "accepted-task-missing"
            ]
            self.assertEqual(len(blockers), 1, blockers)
            self.assertEqual(
                (blockers[0].entity_type, blockers[0].entity_id),
                ("acceptance", "AC1"),
            )
            self.assertNotIn("[accepted-task-missing]", blockers[0].message)
            self.assertEqual(blockers[0].render().count("[accepted-task-missing]"), 1)

    def test_delivered_consistency_normalizes_orphan_and_baseline_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_full_graph(root)
            harness_db.record_delivery(root, "closed graph")
            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.execute(
                    """
                    insert into requirements
                    (id, cycle_id, kind, body, priority, status, revision, updated_at)
                    values ('REQ2', 'CYCLE-current', 'functional', 'corrupt orphan',
                            '', 'active', 1, '2026-07-21T00:00:00Z')
                    """
                )
                conn.commit()

            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.row_factory = sqlite3.Row
                report = delivery_policy.evaluate_delivery_report(
                    conn,
                    root,
                    mode="delivered-consistency",
                    is_expired=harness_db.is_expired,
                )
            link_blockers = [
                blocker
                for blocker in report.blockers
                if blocker.code == "requirement-acceptance-link-missing"
            ]
            baseline_blockers = [
                blocker
                for blocker in report.blockers
                if blocker.code == "baseline-stale"
            ]
            self.assertEqual(len(link_blockers), 1, link_blockers)
            self.assertEqual(
                (link_blockers[0].entity_type, link_blockers[0].entity_id),
                ("requirement", "REQ2"),
            )
            self.assertEqual(len(baseline_blockers), 1, baseline_blockers)
            self.assertEqual(
                (baseline_blockers[0].entity_type, baseline_blockers[0].entity_id),
                ("baseline", "BL1"),
            )
            for blocker in (*link_blockers, *baseline_blockers):
                self.assertNotIn(f"[{blocker.code}]", blocker.message)


class CrossCycleDeliveredHistoryRedTests(unittest.TestCase):
    def test_historical_cycle_audit_is_read_only_after_next_cycle_starts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_full_graph(root)
            harness_db.record_delivery(root, "cycle one")
            harness_db.cycle_start(root, "CYCLE-two", "second", "second delivery")
            (root / "second_cycle_source.py").write_text(
                "SECOND = True\n", encoding="utf-8"
            )
            before_current = query_scalar(
                root, "select current_cycle_id from project where id=1"
            )

            audit = harness_db.cycle_audit(root, "CYCLE-current")

            self.assertTrue(audit["consistent"], audit)
            self.assertEqual(audit["cycle"]["id"], "CYCLE-current")
            self.assertEqual(audit["cycle"]["status"], "delivered")
            self.assertEqual(audit["counts"]["deliveries"], 1)
            self.assertEqual(len(audit["facts_sha256"]), 64)
            self.assertEqual(
                query_scalar(root, "select current_cycle_id from project where id=1"),
                before_current,
            )
            status = harness_db.cycle_status(root, "CYCLE-current")
            self.assertEqual(status["status"], "delivered")

            cli = run_harness(
                root,
                "cycle",
                "audit",
                "--id",
                "CYCLE-current",
                "--json",
            )
            self.assertEqual(cli.returncode, 0, cli.stdout + cli.stderr)
            self.assertTrue(json.loads(cli.stdout)["consistent"])

    def test_historical_cycle_audit_replays_persisted_gate_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_full_graph(root)
            harness_db.record_delivery(root, "cycle one")
            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.execute(
                    """
                    update quality_gates
                    set result='fail', review_status='same-context-degraded',
                        reviewer_context_id=''
                    where cycle_id='CYCLE-current' and gate_status='active'
                    """
                )
                conn.commit()
            harness_db.cycle_start(root, "CYCLE-two", "second", "second delivery")

            audit = harness_db.cycle_audit(root, "CYCLE-current")

            self.assertFalse(audit["consistent"], audit)
            self.assertTrue(
                any(
                    blocker["code"] == "quality-gate-invalid"
                    for blocker in audit["blockers"]
                ),
                audit,
            )
            cli = run_harness(
                root,
                "cycle",
                "audit",
                "--id",
                "CYCLE-current",
                "--json",
            )
            self.assertNotEqual(cli.returncode, 0)

    def test_historical_cycle_audit_rejects_appended_confirmation_revision_forgery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_full_graph(root)
            harness_db.record_delivery(root, "cycle one")
            harness_db.cycle_start(root, "CYCLE-two", "second", "second delivery")
            before = harness_db.cycle_audit(root, "CYCLE-current")
            self.assertTrue(before["consistent"], before)
            with closing(sqlite3.connect(db_path(root))) as conn:
                baseline = conn.execute(
                    "select id, digest from baselines "
                    "where cycle_id='CYCLE-current'"
                ).fetchone()
                gate = conn.execute(
                    "select id, reviewed_revision from quality_gates "
                    "where cycle_id='CYCLE-current' and gate_status='active'"
                ).fetchone()
                if baseline is None or gate is None:
                    self.fail("historical baseline and gate are required")
                forged_revision = int(gate[1]) + 1
                conn.execute(
                    "update quality_gates set reviewed_revision = ? where id = ?",
                    (forged_revision, gate[0]),
                )
                conn.execute(
                    """
                    insert into events
                    (id, schema_version, event_type, entity_type, entity_id,
                     actor, command, before_json, after_json, correlation_id,
                     created_at)
                    values (?, 31, 'baseline_confirmed', 'baseline', ?,
                            'raw-writer', 'forged append', '{}', ?, ?, ?)
                    """,
                    (
                        "EV-FORGED-CONFIRMATION",
                        baseline[0],
                        json.dumps(
                            {
                                "id": baseline[0],
                                "cycle_id": "CYCLE-current",
                                "digest": baseline[1],
                                "project_revision": forged_revision,
                            },
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        "CORR-FORGED-CONFIRMATION",
                        "2026-07-21T00:00:00+00:00",
                    ),
                )
                conn.commit()

            audit = harness_db.cycle_audit(root, "CYCLE-current")

            self.assertFalse(audit["consistent"], audit)
            self.assertIn(
                "historical-event-chain-invalid",
                {blocker["code"] for blocker in audit["blockers"]},
                audit,
            )
            self.assertNotEqual(audit["facts_sha256"], before["facts_sha256"])
            self.assertGreater(audit["counts"]["events"], before["counts"]["events"])
            cli = run_harness(
                root,
                "cycle",
                "audit",
                "--id",
                "CYCLE-current",
                "--json",
            )
            self.assertNotEqual(cli.returncode, 0)

    def test_historical_cycle_audit_replays_gate_revision_and_review_identity(
        self,
    ) -> None:
        mutations = (
            (
                "reviewed_revision = reviewed_revision + 1",
                "quality-gate-invalid",
            ),
            (
                "review_status = 'reviewed-local', "
                "reviewer_context_id = producer_context_id",
                "quality-gate-invalid",
            ),
        )
        for mutation, expected_code in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                prepare_full_graph(root)
                harness_db.record_delivery(root, "cycle one")
                harness_db.cycle_start(
                    root, "CYCLE-two", "second", "second delivery"
                )
                with closing(sqlite3.connect(db_path(root))) as conn:
                    conn.execute(
                        f"update quality_gates set {mutation} "
                        "where cycle_id='CYCLE-current' and gate_status='active'"
                    )
                    conn.commit()

                audit = harness_db.cycle_audit(root, "CYCLE-current")

                self.assertFalse(audit["consistent"], audit)
                self.assertIn(
                    expected_code,
                    {blocker["code"] for blocker in audit["blockers"]},
                    audit,
                )

    def test_historical_cycle_audit_replays_execution_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_full_graph(root)
            harness_db.record_delivery(root, "cycle one")
            harness_db.cycle_start(root, "CYCLE-two", "second", "second delivery")
            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.execute("drop trigger executions_no_update")
                conn.execute(
                    "update executions set provenance_status='legacy-incomplete' "
                    "where cycle_id='CYCLE-current'"
                )
                conn.commit()

            audit = harness_db.cycle_audit(root, "CYCLE-current")

            self.assertFalse(audit["consistent"], audit)
            self.assertIn(
                "current-execution-missing",
                {blocker["code"] for blocker in audit["blockers"]},
                audit,
            )

    def test_accepted_task_evidence_is_required_before_and_after_delivery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_full_graph(root)
            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.execute(
                    "update tasks set evidence='' "
                    "where cycle_id='CYCLE-current' and id='T1'"
                )
                conn.commit()

            self.assertIn("accepted-task-missing", blocker_codes(root, "record-delivery"))
            with self.assertRaisesRegex(
                harness_db.HarnessError,
                r"\[accepted-task-missing\]",
            ):
                harness_db.record_delivery(root, "must fail closed")
            result = run_harness(root, "delivery", "record", "--scope", "must fail")
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("[accepted-task-missing]", result.stdout + result.stderr)
            assert_delivery_unchanged(self, root)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_full_graph(root)
            harness_db.record_delivery(root, "cycle one")
            harness_db.cycle_start(root, "CYCLE-two", "second", "second delivery")
            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.execute(
                    "update tasks set evidence='' "
                    "where cycle_id='CYCLE-current' and id='T1'"
                )
                conn.commit()

            audit = harness_db.cycle_audit(root, "CYCLE-current")

            self.assertFalse(audit["consistent"], audit)
            self.assertIn(
                "accepted-task-missing",
                {blocker["code"] for blocker in audit["blockers"]},
                audit,
            )

    def test_delivery_and_historical_audit_require_immutable_event_triggers(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_full_graph(root)
            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.execute("drop trigger events_no_update")
                conn.commit()

            with self.assertRaisesRegex(
                harness_db.HarnessError,
                r"\[invariant-immutable-trigger-missing\]",
            ):
                harness_db.record_delivery(root, "must fail closed")
            assert_delivery_unchanged(self, root)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_full_graph(root)
            harness_db.record_delivery(root, "cycle one")
            harness_db.cycle_start(root, "CYCLE-two", "second", "second delivery")
            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.execute("drop trigger events_no_update")
                conn.commit()

            audit = harness_db.cycle_audit(root, "CYCLE-current")

            self.assertFalse(audit["consistent"], audit)
            self.assertIn(
                "invariant-immutable-trigger-missing",
                {blocker["code"] for blocker in audit["blockers"]},
                audit,
            )

    def test_doctor_detects_raw_closed_cycle_target_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_full_graph(root)
            harness_db.record_delivery(root, "cycle one")
            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.execute(
                    "update test_targets set command_template='python3 -B changed.py' "
                    "where id='ARITH'"
                )
                conn.commit()

            issues = harness_db.doctor(root)
            self.assertTrue(
                any(
                    "closed-cycle qualification target definition changed" in str(issue)
                    for issue in issues
                ),
                issues,
            )
            audit = harness_db.cycle_audit(root, "CYCLE-current")
            self.assertFalse(audit["consistent"], audit)
            self.assertTrue(
                any(
                    blocker["code"] in {"qualification-stale", "current-execution-missing"}
                    for blocker in audit["blockers"]
                ),
                audit,
            )

    def test_doctor_detects_raw_cross_cycle_gate_finding_link(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_full_graph(root)
            harness_db.record_finding(
                root, "F1", "unit", "low", "open", "cycle-one finding"
            )
            harness_db.record_delivery(root, "cycle one")
            harness_db.cycle_start(root, "CYCLE-two", "second", "second delivery")
            harness_db.record_gate(root, "same-context-degraded", "fail")
            with closing(sqlite3.connect(db_path(root))) as conn:
                gate_id = conn.execute(
                    "select id from quality_gates where cycle_id='CYCLE-two' "
                    "order by sequence desc limit 1"
                ).fetchone()[0]
                conn.execute(
                    "insert into quality_gate_findings (gate_id, finding_id) "
                    "values (?, 'F1')",
                    (gate_id,),
                )
                conn.commit()

            issues = harness_db.doctor(root)
            self.assertTrue(
                any(
                    "quality gate links a finding from a different cycle or candidate"
                    in str(issue)
                    for issue in issues
                ),
                issues,
            )

    def test_decision_records_current_cycle_and_candidate_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            harness_db.record_decision(root, "retain contract", "cycle-scoped audit")

            with closing(sqlite3.connect(db_path(root))) as conn:
                row = conn.execute(
                    "select cycle_id, candidate_sha from decisions order by created_at desc limit 1"
                ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "CYCLE-current")
            self.assertEqual(row[1], harness_db.current_candidate_sha(root))

    def test_quickstart_reuses_cycle_scoped_local_ids_without_global_collisions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            command = write_arithmetic_target(root)
            harness_db.quickstart_minimal(
                root,
                "REUSE",
                "first delivery",
                "arithmetic passes",
                "verify first",
                command,
                execute=True,
            )
            harness_db.accept_task(root, "REUSE-T1", "first review")
            harness_db.record_gate(
                root,
                "fresh",
                "pass",
                reviewer_context_id="reviewer-one",
                residual_risk="low-risk local review",
                qualifications=["REUSE-Q1"],
            )
            harness_db.enter_delivery_readiness(root)
            harness_db.record_delivery(root, "first delivery")
            harness_db.cycle_start(root, "CYCLE-two", "second", "second delivery")

            harness_db.quickstart_minimal(
                root,
                "REUSE",
                "second delivery",
                "arithmetic still passes",
                "verify second",
                command,
                execute=True,
            )

            self.assertEqual(
                query_scalar(
                    root,
                    "select count(*) from acceptance_target_qualifications "
                    "where cycle_id='CYCLE-two' and id='REUSE-CYCLE-two-Q1'",
                ),
                1,
            )
            self.assertEqual(
                query_scalar(
                    root,
                    "select count(*) from baselines where cycle_id='CYCLE-two' "
                    "and id='REUSE-CYCLE-two-BL1'",
                ),
                1,
            )

    def test_cycle_start_resets_scope_and_baseline_collision_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_full_graph(root)
            harness_db.record_delivery(root, "cycle one")
            baseline_before = (
                query_scalar(root, "select cycle_id from baselines where id='BL1'"),
                query_scalar(root, "select digest from baselines where id='BL1'"),
            )

            harness_db.cycle_start(root, "CYCLE-two", "second", "second delivery")
            self.assertEqual(
                query_scalar(root, "select scope_status from project where id=1"),
                "unconfirmed",
            )
            with self.assertRaisesRegex(
                harness_db.HarnessError,
                r"baseline ID BL1 belongs to .*use a new baseline ID",
            ):
                harness_db.confirm_baseline(
                    root, "BL1", "must not replace history", by="controller"
                )
            baseline_after = (
                query_scalar(root, "select cycle_id from baselines where id='BL1'"),
                query_scalar(root, "select digest from baselines where id='BL1'"),
            )
            self.assertEqual(baseline_after, baseline_before)

    def test_historical_target_exact_registration_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_full_graph(root)
            harness_db.record_delivery(root, "cycle one")
            with closing(sqlite3.connect(db_path(root))) as conn:
                before = conn.execute(
                    "select * from test_targets where id='ARITH'"
                ).fetchone()
            harness_db.cycle_start(root, "CYCLE-two", "second", "second delivery")

            with mock.patch.object(
                harness_db, "now_iso", return_value="2030-01-01T00:00:00Z"
            ):
                harness_db.add_test_target(
                    root,
                    "ARITH",
                    "unit",
                    "python3 -B -m unittest test_arithmetic.py",
                    "qualified arithmetic target",
                )
            with closing(sqlite3.connect(db_path(root))) as conn:
                after = conn.execute(
                    "select * from test_targets where id='ARITH'"
                ).fetchone()
            self.assertEqual(after, before)

    def test_historical_target_change_requires_new_id_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_full_graph(root)
            harness_db.record_delivery(root, "cycle one")
            with closing(sqlite3.connect(db_path(root))) as conn:
                before = conn.execute(
                    "select * from test_targets where id='ARITH'"
                ).fetchone()
            harness_db.cycle_start(root, "CYCLE-two", "second", "second delivery")

            with self.assertRaisesRegex(
                harness_db.HarnessError,
                r"test target ARITH is referenced by closed cycle .*use a new target ID",
            ):
                harness_db.add_test_target(
                    root,
                    "ARITH",
                    "unit",
                    "python3 -B -m unittest changed.py",
                    "changed definition",
                )
            with closing(sqlite3.connect(db_path(root))) as conn:
                after = conn.execute(
                    "select * from test_targets where id='ARITH'"
                ).fetchone()
            self.assertEqual(after, before)

    def test_finding_and_qualification_collisions_name_the_new_id_remediation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_full_graph(root)
            harness_db.record_finding(
                root, "F1", "unit", "low", "open", "cycle-one finding"
            )
            harness_db.record_delivery(root, "cycle one")
            harness_db.cycle_start(root, "CYCLE-two", "second", "second delivery")
            harness_db.add_acceptance(root, "AC1", "second-cycle acceptance")

            with self.assertRaisesRegex(
                harness_db.HarnessError,
                r"finding ID F1 belongs to .*use a new finding ID",
            ):
                harness_db.record_finding(
                    root, "F1", "unit", "low", "open", "must not move history"
                )
            with self.assertRaisesRegex(
                harness_db.HarnessError,
                r"qualification ID Q-AC1-ARITH belongs to .*use a new qualification ID",
            ):
                harness_db.qualify_test_target(
                    root,
                    "Q-AC1-ARITH",
                    "ARITH",
                    "AC1",
                    "second-cycle mapping",
                    "controller",
                )
            self.assertEqual(
                query_scalar(root, "select cycle_id from findings where id='F1'"),
                "CYCLE-current",
            )
            self.assertEqual(
                query_scalar(
                    root,
                    "select cycle_id from acceptance_target_qualifications "
                    "where id='Q-AC1-ARITH'",
                ),
                "CYCLE-current",
            )

    def test_gate_cannot_link_a_finding_from_another_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_full_graph(root)
            harness_db.record_finding(
                root, "F1", "unit", "low", "open", "cycle-one finding"
            )
            harness_db.record_delivery(root, "cycle one")
            gate_count = int(query_scalar(root, "select count(*) from quality_gates"))
            harness_db.cycle_start(root, "CYCLE-two", "second", "second delivery")

            with self.assertRaisesRegex(
                harness_db.HarnessError,
                r"cross-cycle gate finding is not allowed: F1 .*current=CYCLE-two",
            ):
                harness_db.record_gate(
                    root,
                    "same-context-degraded",
                    "fail",
                    findings="F1",
                )
            self.assertEqual(
                query_scalar(root, "select count(*) from quality_gates"),
                gate_count,
            )

    def test_historical_audit_detects_raw_cross_cycle_gate_finding_link(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_full_graph(root)
            harness_db.record_delivery(root, "cycle one")
            old_gate = str(
                query_scalar(
                    root,
                    "select id from quality_gates "
                    "where cycle_id='CYCLE-current' and gate_status='active'",
                )
            )
            harness_db.cycle_start(root, "CYCLE-two", "second", "second delivery")
            harness_db.record_finding(
                root,
                "F-CYCLE-two",
                "unit",
                "low",
                "open",
                "second-cycle finding",
            )
            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.execute(
                    "insert into quality_gate_findings (gate_id, finding_id) "
                    "values (?, 'F-CYCLE-two')",
                    (old_gate,),
                )
                conn.commit()

            audit = harness_db.cycle_audit(root, "CYCLE-current")

            self.assertFalse(audit["consistent"], audit)
            self.assertIn(
                "invariant-cross-cycle-gate-finding",
                {blocker["code"] for blocker in audit["blockers"]},
                audit,
            )

    def test_second_cycle_delivers_with_new_global_fact_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_full_graph(root)
            harness_db.record_delivery(root, "cycle one")
            with closing(sqlite3.connect(db_path(root))) as conn:
                old_baseline = conn.execute(
                    "select * from baselines where id='BL1'"
                ).fetchone()
                old_target = conn.execute(
                    "select * from test_targets where id='ARITH'"
                ).fetchone()
                old_qualification = conn.execute(
                    "select * from acceptance_target_qualifications "
                    "where id='Q-AC1-ARITH'"
                ).fetchone()

            harness_db.cycle_start(root, "CYCLE-two", "second", "second delivery")
            harness_db.add_requirement(root, "REQ1", "functional", "second graph")
            harness_db.add_acceptance(root, "AC1", "2 + 2 remains 4")
            harness_db.link_requirement_acceptance(root, "REQ1", "AC1")
            harness_db.add_test_target(
                root,
                "ARITH",
                "unit",
                "python3 -B -m unittest test_arithmetic.py",
                "qualified arithmetic target",
            )
            harness_db.add_task(root, "T1", "verify second cycle", acceptance="AC1")
            harness_db.link_task_test_target(root, "T1", "ARITH")
            qualification = harness_db.qualify_test_target(
                root,
                "Q-CYCLE-two-AC1-ARITH",
                "ARITH",
                "AC1",
                "second-cycle procedural mapping",
                "controller",
            )
            harness_db.start_task(root, "T1")
            harness_db.verify_run(root, "ARITH", acceptance="AC1")
            harness_db.submit_task(
                root, "T1", "second immutable execution", context_id="producer-two"
            )
            harness_db.accept_task(root, "T1", "reviewed second cycle")
            harness_db.confirm_baseline(
                root, "BL-CYCLE-two", "second scope", by="controller"
            )
            harness_db.record_gate(
                root,
                "fresh",
                "pass",
                reviewer_context_id="reviewer-two",
                residual_risk="low-risk local review",
                qualifications=[qualification],
            )
            force_phase(root, "delivery_readiness")
            harness_db.record_delivery(root, "cycle two")

            self.assertEqual(
                query_scalar(
                    root,
                    "select status from delivery_cycles where id='CYCLE-two'",
                ),
                "delivered",
            )
            with closing(sqlite3.connect(db_path(root))) as conn:
                self.assertEqual(
                    conn.execute("select * from baselines where id='BL1'").fetchone(),
                    old_baseline,
                )
                self.assertEqual(
                    conn.execute("select * from test_targets where id='ARITH'").fetchone(),
                    old_target,
                )
                self.assertEqual(
                    conn.execute(
                        "select * from acceptance_target_qualifications "
                        "where id='Q-AC1-ARITH'"
                    ).fetchone(),
                    old_qualification,
                )


class PublicJourneyAndProjectionTests(unittest.TestCase):
    def test_quickstart_gate_suggestion_is_directly_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            command = write_arithmetic_target(root)
            harness_db.add_requirement(root, "REQ1", "functional", "required")
            harness_db.add_acceptance(root, "AC1", "accepted behavior")
            harness_db.link_requirement_acceptance(root, "REQ1", "AC1")
            harness_db.add_test_target(root, "ARITH", "unit", command)
            harness_db.add_task(root, "T1", "verify", acceptance="AC1")
            harness_db.link_task_test_target(root, "T1", "ARITH")
            qualification_id = harness_db.qualify_test_target(
                root,
                "Q1",
                "ARITH",
                "AC1",
                "explicit procedural mapping",
                "controller",
            )
            harness_db.start_task(root, "T1")
            harness_db.verify_run(root, "ARITH", acceptance="AC1")
            harness_db.submit_task(
                root,
                "T1",
                "immutable execution",
                context_id="producer-context",
            )
            harness_db.accept_task(root, "T1", "reviewed")
            harness_db.confirm_baseline(
                root,
                "BL1",
                "confirmed exact graph",
                by="controller",
            )

            status = harness_db.quickstart_status(root)
            gate_command = next(
                command
                for command in status["next_commands"]
                if " gate record " in f" {command} "
            )
            parts = shlex.split(gate_command)
            self.assertIn("--residual-risk", parts)
            risk_index = parts.index("--residual-risk")
            self.assertLess(risk_index + 1, len(parts))
            self.assertTrue(parts[risk_index + 1].strip())
            self.assertIn(qualification_id, parts)

            result = subprocess.run(
                parts,
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(
                query_scalar(
                    root,
                    "select count(*) from quality_gates where gate_status='active' "
                    "and result='pass'",
                ),
                1,
            )

    def test_quickstart_status_requires_qualification_for_every_active_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            write_arithmetic_target(root)
            harness_db.add_acceptance(root, "AC1", "first acceptance")
            harness_db.add_acceptance(root, "AC2", "second acceptance")
            harness_db.add_test_target(
                root,
                "ARITH",
                "unit",
                "python3 -B -m unittest test_arithmetic.py",
                "shared target",
            )
            harness_db.qualify_test_target(
                root, "Q1", "ARITH", "AC1", "first mapping", "controller"
            )

            status = harness_db.quickstart_status(root)

            self.assertIn("qualification", status["missing"])
            commands = "\n".join(status["next_commands"])
            self.assertIn("--acceptance AC2", commands)
            self.assertNotIn("gate record", commands)

    def test_public_cli_journey_reaches_delivery_without_private_phase_or_sql(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_arithmetic_target(root)
            commands = (
                ("init",),
                ("requirement", "add", "--id", "REQ1", "--kind", "functional", "--body", "calculator works"),
                ("acceptance", "add", "--id", "AC1", "--criterion", "2 + 2 returns 4"),
                ("requirement", "link", "--requirement", "REQ1", "--acceptance", "AC1"),
                ("task", "add", "--id", "T1", "--task", "verify calculator", "--acceptance", "AC1"),
                ("test-target", "add", "--id", "ARITH", "--kind", "unit", "--command-template", "python3 -B -m unittest test_arithmetic.py"),
                ("test-target", "link", "--task", "T1", "--target", "ARITH"),
                ("baseline", "confirm", "--id", "B1", "--summary", "calculator scope", "--by", "controller"),
                ("test-target", "qualify", "--id", "Q1", "--target", "ARITH", "--acceptance", "AC1", "--rationale", "explicit calculator mapping", "--by", "controller"),
                ("task", "start", "T1"),
                ("verify", "run", "--target", "ARITH", "--acceptance", "AC1"),
                ("task", "submit", "T1", "--context-id", "producer-context", "--evidence", "qualified immutable execution"),
                ("task", "accept", "T1", "--evidence", "reviewed locally"),
                ("gate", "record", "--reviewer-context", "fresh", "--reviewer-context-id", "reviewer-context", "--result", "pass", "--qualification", "Q1"),
                ("delivery", "ready"),
                ("delivery", "record", "--scope", "verified local handoff"),
                ("validate", "--delivery"),
            )
            milestones = (
                ("baseline", "confirm"),
                ("test-target", "qualify"),
                ("verify", "run"),
                ("task", "accept"),
                ("gate", "record"),
                ("delivery", "ready"),
                ("delivery", "record"),
            )
            positions = [
                next(
                    index
                    for index, command in enumerate(commands)
                    if command[: len(milestone)] == milestone
                )
                for milestone in milestones
            ]
            self.assertEqual(positions, sorted(positions))
            for command in commands:
                result = run_harness(root, *command)
                self.assertEqual(
                    result.returncode,
                    0,
                    f"command failed: {command}\n{result.stdout}{result.stderr}",
                )
            self.assertEqual(query_scalar(root, "select count(*) from deliveries"), 1)
            self.assertEqual(
                query_scalar(
                    root,
                    "select status from delivery_cycles where id='CYCLE-current'",
                ),
                "delivered",
            )
            self.assertEqual(
                query_scalar(root, "select schema_version from project where id=1"),
                31,
            )
            with closing(sqlite3.connect(db_path(root))) as conn:
                provenance = conn.execute(
                    "select provenance_status, length(target_definition_sha256), "
                    "length(runtime_executable_sha256), policy_version "
                    "from executions order by created_at desc limit 1"
                ).fetchone()
            self.assertEqual(
                provenance,
                ("complete", 64, 64, "schema31-v2"),
            )

    def test_trace_and_projection_do_not_promote_judgment_only_or_superseded_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_full_graph(root)
            harness_db.record_validation(
                root,
                "manual-claim",
                "judgment only",
                "pass",
                acceptance="AC1",
            )
            judgment_id = str(
                query_scalar(
                    root,
                    "select id from validations where surface='manual-claim'",
                )
            )
            trace = harness_db.trace_show(root)
            validation_cells = [line for line in trace if line.startswith("| REQ1")]
            self.assertEqual(len(validation_cells), 1)
            self.assertNotIn(judgment_id, validation_cells[0])
            self.assertNotIn(
                "acceptance has no passing validation",
                "\n".join(trace),
            )

            harness_db.record_gate(
                root,
                "same-context-degraded",
                "pass",
                residual_risk="new gate intentionally reviews no qualification",
            )
            from core.projections import render_test_targets

            render_test_targets(root)
            projection = (
                root / ".ai-team/control/test-targets.md"
            ).read_text(encoding="utf-8")
            self.assertIn("Gate Candidate", projection)
            self.assertIn("Gate Status", projection)
            self.assertIn("Acceptance Revision", projection)
            self.assertIn("Target Definition SHA-256", projection)
            self.assertIn("explicit arithmetic-to-acceptance mapping", projection)
            self.assertIn("test-controller", projection)
            self.assertIn("superseded", projection)
            self.assertIn("reviewed-local", projection)

    def test_retained_skill_proxy_help_exposes_new_public_actions(self) -> None:
        proxy = (
            PLUGIN_ROOT
            / "skills"
            / "project-harness"
            / "scripts"
            / "harness.py"
        )
        cases = (
            (("--help",), "test-target"),
            (("test-target", "--help"), "qualify"),
            (("baseline", "--help"), "confirm"),
            (("delivery", "--help"), "ready"),
            (("verify", "run", "--help"), "already-local"),
        )
        for args, expected in cases:
            with self.subTest(args=args):
                result = subprocess.run(
                    [sys.executable, str(proxy), *args],
                    cwd=REPO_ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(expected, result.stdout)


if __name__ == "__main__":
    unittest.main()

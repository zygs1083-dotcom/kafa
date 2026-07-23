from __future__ import annotations

import copy
import json
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

from core.execution import LocalExecutor  # noqa: E402
from core import api  # noqa: E402
from core.delivery_plan import DeliveryPlan, DeliveryPlanTest  # noqa: E402
from core.projections import PROJECTION_PATHS, projection_content_issues  # noqa: E402
import harness_db  # noqa: E402


PLAN_TABLES = (
    "requirements",
    "acceptance",
    "requirement_acceptance",
    "failure_modes",
    "failure_mode_acceptance",
    "tasks",
    "task_acceptance",
    "task_failure_modes",
    "task_dependencies",
    "test_targets",
    "task_test_targets",
    "acceptance_target_qualifications",
    "executions",
    "validations",
    "validation_executions",
    "quality_gates",
    "deliveries",
    "events",
)

VERIFIED_PATCH_FIELDS = {
    "kind",
    "verification_status",
    "task_status",
    "gate_status",
    "delivery_status",
    "cycle_id",
    "candidate_sha",
    "qualification_id",
    "target_id",
    "target_definition_sha256",
    "execution_id",
    "validation_id",
}


def db_path(root: Path) -> Path:
    return root / ".ai-team" / "state" / "harness.db"


def run_harness(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HARNESS), "--root", str(root), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def write_candidate(root: Path) -> str:
    (root / "patch_contract.py").write_text(
        "def add(left, right):\n"
        "    return left + right\n",
        encoding="utf-8",
    )
    (root / "test_patch_contract.py").write_text(
        "import unittest\n\n"
        "from patch_contract import add\n\n"
        "class PatchContractTest(unittest.TestCase):\n"
        "    def test_add(self):\n"
        "        self.assertEqual(add(2, 3), 5)\n",
        encoding="utf-8",
    )
    return "python3 -B -m unittest test_patch_contract.py"


def delivery_plan(
    root: Path,
    *,
    plan_id: str = "PATCH",
    include_failure_mode: bool = True,
) -> dict[str, object]:
    failure_mode: dict[str, object] | None = None
    if include_failure_mode:
        failure_mode = {
            "feature": "calculator",
            "scenario": "addition regression",
            "trigger": "implementation changes addition",
            "expected": "the exact unit target fails closed",
            "risk": "low",
            "recovery": "restore correct addition and rerun verification",
            "data_safety": "no persisted business data is modified",
        }
    return {
        "version": 1,
        "id": plan_id,
        "goal": "Keep calculator addition correct",
        "acceptance": "add(2, 3) returns 5",
        "task": "Implement and verify the calculator patch",
        "test": {
            "kind": "unit",
            "command": write_candidate(root),
        },
        "failure_mode": failure_mode,
    }


def sqlite_snapshot(root: Path) -> dict[str, tuple[tuple[object, ...], ...]]:
    with closing(sqlite3.connect(db_path(root))) as conn:
        snapshot = {
            table: tuple(
                tuple(row)
                for row in conn.execute(f"select * from {table} order by rowid")
            )
            for table in PLAN_TABLES
        }
        snapshot["project"] = tuple(
            tuple(row) for row in conn.execute("select * from project order by id")
        )
        snapshot["delivery_cycles"] = tuple(
            tuple(row)
            for row in conn.execute("select * from delivery_cycles order by id")
        )
    return snapshot


def projection_snapshot(root: Path) -> dict[str, bytes | None]:
    return {
        relative.as_posix(): (
            (root / relative).read_bytes() if (root / relative).exists() else None
        )
        for relative in PROJECTION_PATHS
    }


def table_count(root: Path, table: str) -> int:
    with closing(sqlite3.connect(db_path(root))) as conn:
        return int(conn.execute(f"select count(*) from {table}").fetchone()[0])


class DeliveryPlanVerifiedPatchContractTest(unittest.TestCase):
    def apply_plan(
        self,
        root: Path,
        plan: dict[str, object],
        *,
        dry_run: bool = False,
    ) -> dict[str, object]:
        operation = getattr(harness_db, "apply_delivery_plan", None)
        self.assertTrue(
            callable(operation),
            "missing transactional API harness_db.apply_delivery_plan",
        )
        result = operation(root, plan, dry_run=dry_run)
        self.assertIsInstance(result, dict)
        return result

    def verify_patch(self, root: Path, plan_id: str) -> dict[str, object]:
        operation = getattr(harness_db, "verified_patch", None)
        self.assertTrue(
            callable(operation),
            "missing immutable API harness_db.verified_patch",
        )
        result = operation(root, plan_id)
        self.assertIsInstance(result, dict)
        return result

    def test_valid_plan_atomically_creates_complete_linked_graph_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plan = delivery_plan(root)
            harness_db.init_runtime(root)

            result = self.apply_plan(root, plan)
            ids = result["ids"]

            self.assertEqual(result["plan_id"], "PATCH")
            self.assertEqual(
                set(ids),
                {
                    "requirement_id",
                    "acceptance_id",
                    "failure_mode_id",
                    "task_id",
                    "target_id",
                    "qualification_id",
                },
            )
            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.row_factory = sqlite3.Row
                requirement = conn.execute(
                    "select * from requirements where id = ?",
                    (ids["requirement_id"],),
                ).fetchone()
                acceptance = conn.execute(
                    "select * from acceptance where id = ?",
                    (ids["acceptance_id"],),
                ).fetchone()
                failure_mode = conn.execute(
                    "select * from failure_modes where id = ?",
                    (ids["failure_mode_id"],),
                ).fetchone()
                task = conn.execute(
                    "select * from tasks where id = ?",
                    (ids["task_id"],),
                ).fetchone()
                target = conn.execute(
                    "select * from test_targets where id = ?",
                    (ids["target_id"],),
                ).fetchone()
                qualification = conn.execute(
                    "select * from acceptance_target_qualifications where id = ?",
                    (ids["qualification_id"],),
                ).fetchone()
                project = conn.execute(
                    "select phase, scope_status from project where id = 1"
                ).fetchone()
                cycle = conn.execute(
                    "select status, phase from delivery_cycles "
                    "where id = (select current_cycle_id from project where id = 1)"
                ).fetchone()

                self.assertEqual(requirement["body"], plan["goal"])
                self.assertEqual(acceptance["criterion"], plan["acceptance"])
                self.assertEqual(failure_mode["risk"], "low")
                self.assertEqual(task["task"], plan["task"])
                self.assertEqual(task["status"], "planned")
                self.assertEqual(target["kind"], "unit")
                self.assertEqual(target["command_template"], plan["test"]["command"])
                self.assertEqual(int(target["gateable"]), 1)
                self.assertEqual(
                    qualification["acceptance_id"], ids["acceptance_id"]
                )
                self.assertEqual(qualification["target_id"], ids["target_id"])
                self.assertTrue(qualification["target_definition_sha256"])
                self.assertTrue(qualification["rationale"])
                self.assertTrue(qualification["qualified_by"])
                self.assertEqual(tuple(project), ("intake", "unconfirmed"))
                self.assertEqual(tuple(cycle), ("active", "intake"))

                expected_links = {
                    "requirement_acceptance": (
                        ids["requirement_id"],
                        ids["acceptance_id"],
                    ),
                    "failure_mode_acceptance": (
                        ids["failure_mode_id"],
                        ids["acceptance_id"],
                    ),
                    "task_acceptance": (ids["task_id"], ids["acceptance_id"]),
                    "task_failure_modes": (
                        ids["task_id"],
                        ids["failure_mode_id"],
                    ),
                    "task_test_targets": (ids["task_id"], ids["target_id"]),
                }
                for table, expected in expected_links.items():
                    columns = {
                        "requirement_acceptance": "requirement_id, acceptance_id",
                        "failure_mode_acceptance": "failure_mode_id, acceptance_id",
                        "task_acceptance": "task_id, acceptance_id",
                        "task_failure_modes": "task_id, failure_mode_id",
                        "task_test_targets": "task_id, target_id",
                    }[table]
                    row = conn.execute(
                        f"select {columns} from {table}"
                    ).fetchone()
                    self.assertEqual(tuple(row), expected, table)

                for table in (
                    "executions",
                    "validations",
                    "quality_gates",
                    "deliveries",
                    "baselines",
                ):
                    self.assertEqual(
                        conn.execute(f"select count(*) from {table}").fetchone()[0],
                        0,
                        table,
                    )

    def test_plan_projection_without_validations_skips_candidate_identity_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plan = delivery_plan(root)
            harness_db.init_runtime(root)

            with mock.patch(
                "core.cycle_ledger.current_candidate_sha",
                side_effect=AssertionError(
                    "candidate identity is irrelevant before any validation exists"
                ),
            ):
                result = self.apply_plan(root, plan)

        self.assertTrue(result["changed"])

    def test_exact_plan_replay_is_a_byte_and_fact_noop(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plan = delivery_plan(root)
            harness_db.init_runtime(root)
            first = self.apply_plan(root, plan)
            fact_before = sqlite_snapshot(root)
            projection_before = projection_snapshot(root)

            second = self.apply_plan(root, copy.deepcopy(plan))

            self.assertEqual(second["ids"], first["ids"])
            self.assertFalse(second["changed"])
            self.assertEqual(second["mutations"], [])
            self.assertEqual(sqlite_snapshot(root), fact_before)
            self.assertEqual(projection_snapshot(root), projection_before)

            dry_replay = self.apply_plan(root, copy.deepcopy(plan), dry_run=True)
            self.assertTrue(dry_replay["dry_run"])
            self.assertFalse(dry_replay["changed"])
            self.assertEqual(dry_replay["mutations"], [])
            self.assertEqual(sqlite_snapshot(root), fact_before)
            self.assertEqual(projection_snapshot(root), projection_before)

    def test_exact_replay_does_not_reset_evolved_task_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plan = delivery_plan(root, include_failure_mode=False)
            harness_db.init_runtime(root)
            applied = self.apply_plan(root, plan)
            harness_db.cancel_task(
                root,
                applied["ids"]["task_id"],
                "controller cancelled this patch",
            )
            fact_before = sqlite_snapshot(root)
            projection_before = projection_snapshot(root)

            replay = self.apply_plan(root, copy.deepcopy(plan))

            self.assertFalse(replay["changed"])
            self.assertEqual(sqlite_snapshot(root), fact_before)
            self.assertEqual(projection_snapshot(root), projection_before)

    def test_frozen_model_cannot_bypass_closed_v1_parser(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            poisoned = DeliveryPlan(
                version=2,
                id="lowercase poisoned id",
                goal="goal",
                acceptance="acceptance",
                task="task",
                test=DeliveryPlanTest(
                    kind="unit",
                    command="python3 -B -m unittest test_patch.py",
                ),
                failure_mode=None,
            )

            with self.assertRaisesRegex(
                harness_db.HarnessError,
                "delivery-plan version must be integer 1",
            ):
                self.apply_plan(root, poisoned, dry_run=True)

            self.assertFalse((root / ".ai-team").exists())

    def test_conflicting_same_id_plan_fails_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plan = delivery_plan(root)
            harness_db.init_runtime(root)
            self.apply_plan(root, plan)
            before = sqlite_snapshot(root)
            projections_before = projection_snapshot(root)
            conflict = copy.deepcopy(plan)
            conflict["acceptance"] = "add(2, 3) returns an unrelated value"

            with self.assertRaisesRegex(
                harness_db.HarnessError,
                "conflict|different semantic|new plan ID",
            ):
                self.apply_plan(root, conflict)

            self.assertEqual(sqlite_snapshot(root), before)
            self.assertEqual(projection_snapshot(root), projections_before)

    def test_non_gateable_final_target_rolls_back_entire_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            plan = delivery_plan(root)
            plan["test"] = {"kind": "unit", "command": "echo pass"}
            before = sqlite_snapshot(root)
            projections_before = projection_snapshot(root)

            with self.assertRaisesRegex(
                harness_db.HarnessError,
                "gateable|test target|command",
            ):
                self.apply_plan(root, plan)

            self.assertEqual(sqlite_snapshot(root), before)
            self.assertEqual(projection_snapshot(root), projections_before)

    def test_late_qualification_failure_rolls_back_entire_fact_graph(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            plan = delivery_plan(root)
            before = sqlite_snapshot(root)
            projections_before = projection_snapshot(root)

            with mock.patch.object(
                harness_db,
                "_qualify_test_target_conn",
                side_effect=harness_db.HarnessError(
                    "injected final qualification failure"
                ),
            ):
                with self.assertRaisesRegex(
                    harness_db.HarnessError,
                    "injected final qualification failure",
                ):
                    self.apply_plan(root, plan)

            self.assertEqual(sqlite_snapshot(root), before)
            self.assertEqual(projection_snapshot(root), projections_before)

    def test_silent_missing_final_relation_fails_postcondition_and_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            plan = delivery_plan(root)
            before = sqlite_snapshot(root)
            projections_before = projection_snapshot(root)

            with mock.patch.object(
                harness_db,
                "_link_task_test_target_conn",
                return_value=None,
            ):
                with self.assertRaisesRegex(
                    harness_db.HarnessError,
                    "postcondition failed.*missing task_test_target relation",
                ):
                    self.apply_plan(root, plan)

            self.assertEqual(sqlite_snapshot(root), before)
            self.assertEqual(projection_snapshot(root), projections_before)

    def test_corrupted_generated_task_state_fails_postcondition_and_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            plan = delivery_plan(root)
            before = sqlite_snapshot(root)
            projections_before = projection_snapshot(root)
            original = harness_db._create_task_conn

            def create_then_corrupt(conn, *args, **kwargs):
                row = original(conn, *args, **kwargs)
                conn.execute(
                    "update tasks set status='accepted', evidence='' where uid=?",
                    (row["uid"],),
                )
                return row

            with mock.patch.object(
                harness_db,
                "_create_task_conn",
                side_effect=create_then_corrupt,
            ):
                with self.assertRaisesRegex(
                    harness_db.HarnessError,
                    "postcondition failed.*task must remain planned",
                ):
                    self.apply_plan(root, plan)

            self.assertEqual(sqlite_snapshot(root), before)
            self.assertEqual(projection_snapshot(root), projections_before)

    def test_projection_failure_leaves_complete_facts_detectable_and_rebuildable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            plan = delivery_plan(root)

            with mock.patch.object(
                harness_db,
                "render_affected",
                side_effect=RuntimeError("injected projection publication failure"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "injected projection publication failure",
                ):
                    self.apply_plan(root, plan)

            with closing(sqlite3.connect(db_path(root))) as conn:
                self.assertEqual(
                    conn.execute(
                        "select count(*) from events "
                        "where event_type='delivery_plan_applied'"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    conn.execute("select count(*) from requirements").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    conn.execute("select count(*) from acceptance").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    conn.execute("select count(*) from tasks").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    conn.execute("select count(*) from test_targets").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    conn.execute(
                        "select count(*) from acceptance_target_qualifications"
                    ).fetchone()[0],
                    1,
                )

            issues = projection_content_issues(root)
            self.assertTrue(
                any("stale or invalid view content" in issue for issue in issues),
                issues,
            )
            api.projection_rebuild(root)
            self.assertEqual(projection_content_issues(root), [])
            self.assertFalse(self.apply_plan(root, plan)["changed"])

    def test_closed_cycle_rejects_plan_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plan = delivery_plan(root)
            harness_db.init_runtime(root)
            harness_db.cycle_close(root, "archived")
            before = sqlite_snapshot(root)

            with self.assertRaisesRegex(
                harness_db.HarnessError,
                "closed|active|new cycle",
            ):
                self.apply_plan(root, plan)

            self.assertEqual(sqlite_snapshot(root), before)

    def test_dry_run_validates_and_generates_ids_without_initializing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plan = delivery_plan(root, include_failure_mode=False)

            result = self.apply_plan(root, plan, dry_run=True)

            self.assertTrue(result["dry_run"])
            self.assertEqual(result["plan_id"], "PATCH")
            self.assertEqual(result["validations"], [])
            self.assertGreater(len(result["mutations"]), 0)
            self.assertEqual(
                set(result["ids"]),
                {
                    "requirement_id",
                    "acceptance_id",
                    "task_id",
                    "target_id",
                    "qualification_id",
                },
            )
            self.assertFalse((root / ".ai-team").exists())
            self.assertFalse((root / ".gitignore").exists())

    def test_real_apply_requires_init_without_creating_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plan = delivery_plan(root, include_failure_mode=False)

            with self.assertRaisesRegex(
                harness_db.HarnessError,
                "not initialized.*never initializes implicitly",
            ):
                self.apply_plan(root, plan)

            self.assertFalse((root / ".ai-team").exists())
            self.assertFalse((root / ".gitignore").exists())

    def test_initialized_dry_run_reuses_apply_conflict_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            plan = delivery_plan(root, include_failure_mode=False)
            self.apply_plan(root, plan)
            before = sqlite_snapshot(root)
            projections_before = projection_snapshot(root)
            conflicting = copy.deepcopy(plan)
            conflicting["acceptance"] = "conflicting dry-run semantic content"

            with self.assertRaisesRegex(
                harness_db.HarnessError,
                "conflict|different semantic|new plan ID",
            ):
                self.apply_plan(root, conflicting, dry_run=True)

            self.assertEqual(sqlite_snapshot(root), before)
            self.assertEqual(projection_snapshot(root), projections_before)

    def test_delivery_plan_v1_rejects_missing_extra_and_wrong_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            valid = delivery_plan(root, include_failure_mode=False)

            missing = copy.deepcopy(valid)
            del missing["goal"]
            extra = copy.deepcopy(valid)
            extra["notes"] = "version 1 is closed"
            wrong = copy.deepcopy(valid)
            wrong_test = wrong["test"]
            self.assertIsInstance(wrong_test, dict)
            wrong_test["command_text"] = wrong_test.pop("command")

            cases = (
                ("missing", missing, r"keys mismatch: missing=\['goal'\] extra=\[\]"),
                ("extra", extra, r"keys mismatch: missing=\[\] extra=\['notes'\]"),
                (
                    "wrong",
                    wrong,
                    r"delivery-plan test keys mismatch: "
                    r"missing=\['command'\] extra=\['command_text'\]",
                ),
            )
            for label, invalid, expected_error in cases:
                with self.subTest(label=label):
                    with self.assertRaisesRegex(
                        harness_db.HarnessError,
                        expected_error,
                    ):
                        self.apply_plan(root, invalid, dry_run=True)

            self.assertFalse((root / ".ai-team").exists())
            self.assertFalse((root / ".gitignore").exists())

    def test_delivery_plan_v1_rejects_boolean_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plan = delivery_plan(root, include_failure_mode=False)
            plan["version"] = True

            with self.assertRaisesRegex(
                harness_db.HarnessError,
                "delivery-plan version must be integer 1",
            ):
                self.apply_plan(root, plan, dry_run=True)

            self.assertFalse((root / ".ai-team").exists())
            self.assertFalse((root / ".gitignore").exists())

    def test_verified_patch_records_only_immutable_verification_and_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            applied = self.apply_plan(
                root,
                delivery_plan(root, include_failure_mode=False),
            )
            ids = applied["ids"]
            with closing(sqlite3.connect(db_path(root))) as conn:
                task_before = conn.execute(
                    "select * from tasks where id = ?", (ids["task_id"],)
                ).fetchone()
                project_before = conn.execute(
                    "select phase, status, scope_status from project where id = 1"
                ).fetchone()
                cycle_before = conn.execute(
                    "select status, phase from delivery_cycles "
                    "where id = (select current_cycle_id from project where id = 1)"
                ).fetchone()

            result = self.verify_patch(root, "PATCH")

            self.assertEqual(set(result), VERIFIED_PATCH_FIELDS)
            self.assertEqual(result["kind"], "verified-patch")
            self.assertEqual(result["verification_status"], "pass")
            self.assertEqual(result["task_status"], "planned")
            self.assertEqual(result["gate_status"], "not-run")
            self.assertEqual(result["delivery_status"], "not-run")
            self.assertEqual(result["qualification_id"], ids["qualification_id"])
            self.assertEqual(result["target_id"], ids["target_id"])
            self.assertTrue(result["candidate_sha"])
            self.assertTrue(result["target_definition_sha256"])
            self.assertTrue(result["execution_id"])
            self.assertTrue(result["validation_id"])

            with closing(sqlite3.connect(db_path(root))) as conn:
                execution = conn.execute(
                    "select cycle_id, candidate_sha, target_id, "
                    "target_definition_sha256, semantic_status from executions "
                    "where id = ?",
                    (result["execution_id"],),
                ).fetchone()
                validation = conn.execute(
                    "select cycle_id, candidate_sha, acceptance_id, result "
                    "from validations where id = ?",
                    (result["validation_id"],),
                ).fetchone()
                link = conn.execute(
                    "select count(*) from validation_executions "
                    "where execution_id = ? and validation_id = ?",
                    (result["execution_id"], result["validation_id"]),
                ).fetchone()[0]
                self.assertEqual(
                    tuple(execution),
                    (
                        result["cycle_id"],
                        result["candidate_sha"],
                        result["target_id"],
                        result["target_definition_sha256"],
                        "pass",
                    ),
                )
                self.assertEqual(
                    tuple(validation),
                    (
                        result["cycle_id"],
                        result["candidate_sha"],
                        ids["acceptance_id"],
                        "pass",
                    ),
                )
                self.assertEqual(link, 1)
                self.assertEqual(
                    conn.execute(
                        "select * from tasks where id = ?", (ids["task_id"],)
                    ).fetchone(),
                    task_before,
                )
                self.assertEqual(
                    conn.execute(
                        "select phase, status, scope_status from project where id = 1"
                    ).fetchone(),
                    project_before,
                )
                self.assertEqual(
                    conn.execute(
                        "select status, phase from delivery_cycles "
                        "where id = (select current_cycle_id from project where id = 1)"
                    ).fetchone(),
                    cycle_before,
                )
                self.assertEqual(
                    conn.execute("select count(*) from quality_gates").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    conn.execute("select count(*) from deliveries").fetchone()[0],
                    0,
                )

    def test_verified_patch_rejects_stale_revision_target_and_qualification(self) -> None:
        for mutation in ("acceptance", "target", "qualification"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                harness_db.init_runtime(root)
                plan = delivery_plan(root, include_failure_mode=False)
                applied = self.apply_plan(root, plan)
                ids = applied["ids"]
                if mutation == "acceptance":
                    harness_db.add_acceptance(
                        root,
                        ids["acceptance_id"],
                        "changed acceptance makes the generated mapping stale",
                    )
                elif mutation == "target":
                    harness_db.add_test_target(
                        root,
                        ids["target_id"],
                        "unit",
                        "python3 -B -m unittest discover -s . -p 'test_*.py'",
                        "changed target definition",
                    )
                else:
                    harness_db.qualify_test_target(
                        root,
                        "PATCH-Q-REPLACEMENT",
                        ids["target_id"],
                        ids["acceptance_id"],
                        "a newer qualification supersedes the plan mapping",
                        "root-controller",
                    )

                with self.assertRaisesRegex(
                    harness_db.HarnessError,
                    "stale|superseded|qualification",
                ):
                    self.verify_patch(root, "PATCH")

                self.assertEqual(table_count(root, "executions"), 0)
                self.assertEqual(table_count(root, "validations"), 0)
                self.assertEqual(table_count(root, "quality_gates"), 0)
                self.assertEqual(table_count(root, "deliveries"), 0)

    def test_verified_patch_discards_result_when_candidate_changes_during_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            self.apply_plan(root, delivery_plan(root, include_failure_mode=False))
            original_run = LocalExecutor.run

            def run_then_change(executor, *args, **kwargs):
                result = original_run(executor, *args, **kwargs)
                candidate = root / "patch_contract.py"
                candidate.write_text(
                    candidate.read_text(encoding="utf-8") + "\n# changed in race\n",
                    encoding="utf-8",
                )
                return result

            with mock.patch.object(LocalExecutor, "run", new=run_then_change):
                with self.assertRaisesRegex(harness_db.HarnessError, "stale candidate"):
                    self.verify_patch(root, "PATCH")

            self.assertEqual(table_count(root, "executions"), 0)
            self.assertEqual(table_count(root, "validations"), 0)
            self.assertEqual(table_count(root, "quality_gates"), 0)
            self.assertEqual(table_count(root, "deliveries"), 0)

    def test_verified_patch_rechecks_candidate_before_returning_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            self.apply_plan(root, delivery_plan(root, include_failure_mode=False))

            def change_after_verification(*_args) -> None:
                candidate = root / "patch_contract.py"
                candidate.write_text(
                    candidate.read_text(encoding="utf-8")
                    + "\n# changed before envelope\n",
                    encoding="utf-8",
                )

            with mock.patch.object(
                harness_db,
                "_before_verified_patch_envelope",
                side_effect=change_after_verification,
            ):
                with self.assertRaisesRegex(
                    harness_db.HarnessError,
                    "stale candidate.*envelope",
                ):
                    self.verify_patch(root, "PATCH")

            self.assertEqual(table_count(root, "executions"), 1)
            self.assertEqual(table_count(root, "validations"), 1)
            self.assertEqual(table_count(root, "quality_gates"), 0)
            self.assertEqual(table_count(root, "deliveries"), 0)

    def test_verified_patch_ignores_superseded_plan_linked_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            applied = self.apply_plan(
                root,
                delivery_plan(root, include_failure_mode=False),
            )
            ids = applied["ids"]
            first = self.verify_patch(root, "PATCH")
            self.assertEqual(first["gate_status"], "not-run")
            harness_db.start_task(root, ids["task_id"])
            harness_db.submit_task(
                root,
                ids["task_id"],
                "current immutable verification",
                context_id="producer-context",
            )
            harness_db.accept_task(
                root,
                ids["task_id"],
                "controller accepted current verification",
            )
            harness_db.record_gate(
                root,
                "fresh",
                "pass",
                reviewer_context_id="reviewer-one",
                qualifications=[ids["qualification_id"]],
            )
            harness_db.record_gate(
                root,
                "fresh",
                "blocked",
                reviewer_context_id="reviewer-two",
                qualifications=[],
            )

            report = self.verify_patch(root, "PATCH")

            self.assertEqual(report["gate_status"], "not-run")
            with closing(sqlite3.connect(db_path(root))) as conn:
                rows = conn.execute(
                    "select gate_status, result from quality_gates order by sequence"
                ).fetchall()
            self.assertEqual(rows, [("superseded", "pass"), ("active", "blocked")])

    def test_cancelled_plan_task_remains_cancelled_and_delivery_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            applied = self.apply_plan(
                root,
                delivery_plan(root, include_failure_mode=False),
            )
            harness_db.cancel_task(root, applied["ids"]["task_id"], "not shipped")

            result = self.verify_patch(root, "PATCH")
            delivery_issues = harness_db.validate_runtime(root, delivery=True)

            self.assertEqual(result["verification_status"], "pass")
            self.assertEqual(result["task_status"], "cancelled")
            self.assertEqual(result["gate_status"], "not-run")
            self.assertEqual(result["delivery_status"], "not-run")
            self.assertTrue(
                any("[accepted-task-missing]" in issue for issue in delivery_issues),
                delivery_issues,
            )
            self.assertEqual(table_count(root, "quality_gates"), 0)
            self.assertEqual(table_count(root, "deliveries"), 0)

    def test_cli_delivery_plan_and_verified_patch_emit_one_json_object(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plan = delivery_plan(root, include_failure_mode=False)
            plan_path = root / "delivery-plan.json"
            plan_path.write_text(
                json.dumps(plan, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )

            dry_run = run_harness(
                root,
                "quickstart",
                "delivery-plan",
                "--file",
                str(plan_path),
                "--dry-run",
                "--json",
            )
            self.assertEqual(dry_run.returncode, 0, dry_run.stdout + dry_run.stderr)
            dry_report = json.loads(dry_run.stdout)
            self.assertTrue(dry_report["dry_run"])
            self.assertFalse((root / ".ai-team").exists())

            harness_db.init_runtime(root)
            applied = run_harness(
                root,
                "quickstart",
                "delivery-plan",
                "--file",
                str(plan_path),
                "--json",
            )
            self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
            applied_report = json.loads(applied.stdout)
            self.assertEqual(applied_report["plan_id"], "PATCH")

            verified = run_harness(
                root,
                "quickstart",
                "verified-patch",
                "--id",
                "PATCH",
                "--json",
            )
            self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
            verified_report = json.loads(verified.stdout)
            self.assertEqual(set(verified_report), VERIFIED_PATCH_FIELDS)
            self.assertEqual(verified_report["verification_status"], "pass")
            self.assertEqual(verified_report["task_status"], "planned")
            self.assertEqual(verified_report["gate_status"], "not-run")
            self.assertEqual(verified_report["delivery_status"], "not-run")

    def test_cli_rejects_duplicate_json_key_with_one_clean_json_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plan = delivery_plan(root, include_failure_mode=False)
            plan_path = root / "duplicate-delivery-plan.json"
            plan_path.write_text(
                "{" + ",".join(
                    (
                        '"version":1',
                        '"id":"PATCH"',
                        '"id":"SHADOW"',
                        '"goal":' + json.dumps(plan["goal"], ensure_ascii=False),
                        '"acceptance":'
                        + json.dumps(plan["acceptance"], ensure_ascii=False),
                        '"task":' + json.dumps(plan["task"], ensure_ascii=False),
                        '"test":' + json.dumps(plan["test"], ensure_ascii=False),
                        '"failure_mode":null',
                    )
                ) + "}",
                encoding="utf-8",
            )

            result = run_harness(
                root,
                "quickstart",
                "delivery-plan",
                "--file",
                str(plan_path),
                "--dry-run",
                "--json",
            )

            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertEqual(result.stderr, "")
            rendered = result.stdout.strip()
            report, end = json.JSONDecoder().raw_decode(rendered)
            self.assertEqual(rendered[end:].strip(), "")
            self.assertIsInstance(report, dict)
            self.assertEqual(set(report), {"error", "ok"})
            self.assertIs(report["ok"], False)
            self.assertIn("duplicate delivery-plan key: id", report["error"])
            self.assertFalse((root / ".ai-team").exists())
            self.assertFalse((root / ".gitignore").exists())


if __name__ == "__main__":
    unittest.main()

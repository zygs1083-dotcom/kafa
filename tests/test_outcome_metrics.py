from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins/codex-project-harness"
SCRIPTS = PLUGIN_ROOT / "scripts"
HARNESS = SCRIPTS / "harness.py"
for path in (PLUGIN_ROOT, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from core import api as harness_api  # noqa: E402
import harness_db  # noqa: E402


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


def observation_count(root: Path) -> int:
    with closing(sqlite3.connect(db_path(root))) as conn:
        return int(conn.execute("select count(*) from outcome_observations").fetchone()[0])


METRIC_IDS = {
    "false_green_prevented_count",
    "escaped_defect_count",
    "rework_rate_per_delivery",
    "migration_recovery_success_rate",
    "time_to_verified_delivery_seconds",
    "qualification_coverage_rate",
}
REPORT_AT = "2026-07-21T10:00:00+00:00"


def outcome_report_at(root: Path) -> dict[str, object]:
    with patch.object(harness_db, "now_iso", return_value=REPORT_AT):
        return harness_api.outcome_report(root)


def set_cycle_window(
    root: Path,
    *,
    started_at: str = "2026-07-21T09:00:00Z",
    closed_at: str = "",
    status: str = "active",
) -> str:
    cycle_id = harness_db.cycle_status(root)["id"]
    with closing(sqlite3.connect(db_path(root))) as conn:
        conn.execute(
            "update delivery_cycles set started_at=?, closed_at=?, status=? where id=?",
            (started_at, closed_at, status, cycle_id),
        )
        conn.commit()
    return str(cycle_id)


class OutcomeObservationContractTests(unittest.TestCase):
    def test_public_schema_rejects_unbounded_or_malformed_observations(self) -> None:
        schema = json.loads(
            (PLUGIN_ROOT / "schemas/outcome-observation.schema.json").read_text(
                encoding="utf-8"
            )
        )
        valid = {
            "id": "OUT-1",
            "cycle_id": "CYCLE-current",
            "kind": "escaped-defect",
            "value": 0,
            "details": "defect escaped the verified candidate",
            "recorded_by": "qa-local",
            "observed_at": "2026-07-21T09:30:00Z",
            "created_at": "2026-07-21T09:31:00Z",
        }
        self.assertEqual(
            harness_db.validate_object_against_schema(
                "outcome observation",
                valid,
                schema,
            ),
            [],
        )
        invalid_values = {
            "kind": "invented-outcome",
            "value": -1,
            "details": "   ",
            "recorded_by": "\t",
            "observed_at": "2026-02-30T09:30:00Z",
            "created_at": "2026-13-01T09:31:00Z",
        }
        for field, value in invalid_values.items():
            with self.subTest(field=field):
                row = dict(valid)
                row[field] = value
                issues = harness_db.validate_object_against_schema(
                    "outcome observation",
                    row,
                    schema,
                )
                self.assertTrue(any(field in issue for issue in issues), issues)

    def test_api_records_one_current_cycle_observation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            current_cycle = harness_db.cycle_status(root)["id"]

            harness_api.record_outcome_observation(
                root,
                "OUT-1",
                "escaped-defect",
                0,
                "zero escaped defects in the bounded observation",
                "qa-local",
                "2026-07-21T09:30:00Z",
            )

            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "select * from outcome_observations where id='OUT-1'"
                ).fetchone()
                self.assertIsNotNone(row)
                assert row is not None
                self.assertEqual(row["cycle_id"], current_cycle)
                self.assertEqual(row["kind"], "escaped-defect")
                self.assertEqual(row["value"], 0)
                self.assertEqual(row["recorded_by"], "qa-local")
                self.assertEqual(row["observed_at"], "2026-07-21T09:30:00Z")
                self.assertRegex(
                    row["created_at"],
                    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
                )
                self.assertEqual(
                    int(
                        conn.execute(
                            "select count(*) from events where event_type='outcome_observation_recorded'"
                        ).fetchone()[0]
                    ),
                    1,
                )

    def test_api_rejects_each_invalid_observation_without_writing(self) -> None:
        invalid_cases = {
            "unknown-kind": {"kind": "invented-outcome"},
            "negative-value": {"value": -1},
            "real-value": {"value": 1.5},
            "boolean-value": {"value": True},
            "blank-details": {"details": "   "},
            "blank-actor": {"recorded_by": "\t"},
            "invalid-cycle": {"cycle_id": "CYCLE-missing"},
            "malformed-timestamp": {"observed_at": "yesterday"},
        }
        defaults: dict[str, object] = {
            "kind": "rework",
            "value": 1,
            "details": "one unit of verified rework",
            "recorded_by": "qa-local",
            "observed_at": "2026-07-21T09:30:00Z",
            "cycle_id": "",
        }
        for name, override in invalid_cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                harness_db.init_runtime(root)
                values = {**defaults, **override}
                with closing(sqlite3.connect(db_path(root))) as conn:
                    event_count = int(
                        conn.execute("select count(*) from events").fetchone()[0]
                    )
                    project_revision = int(
                        conn.execute(
                            "select revision from project where id=1"
                        ).fetchone()[0]
                    )
                with self.assertRaises(harness_api.HarnessError):
                    harness_api.record_outcome_observation(
                        root,
                        "OUT-invalid",
                        str(values["kind"]),
                        values["value"],  # type: ignore[arg-type]
                        str(values["details"]),
                        str(values["recorded_by"]),
                        str(values["observed_at"]),
                        cycle_id=str(values["cycle_id"]),
                    )
                self.assertEqual(observation_count(root), 0)
                with closing(sqlite3.connect(db_path(root))) as conn:
                    self.assertEqual(
                        int(conn.execute("select count(*) from events").fetchone()[0]),
                        event_count,
                    )
                    self.assertEqual(
                        int(
                            conn.execute(
                                "select revision from project where id=1"
                            ).fetchone()[0]
                        ),
                        project_revision,
                    )

    def test_cli_records_current_cycle_observation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            current_cycle = harness_db.cycle_status(root)["id"]

            result = run_harness(
                root,
                "cycle",
                "outcome-record",
                "--id",
                "OUT-CLI",
                "--kind",
                "false-green-prevented",
                "--value",
                "4",
                "--details",
                "four deterministic false-delivery scenarios now fail closed",
                "--by",
                "root-controller",
                "--observed-at",
                "2026-07-21T09:30:00+00:00",
            )
            self.assertIn("OK: outcome observation recorded OUT-CLI", result.stdout)
            with closing(sqlite3.connect(db_path(root))) as conn:
                row = conn.execute(
                    "select cycle_id, kind, value from outcome_observations where id='OUT-CLI'"
                ).fetchone()
                self.assertEqual(
                    row,
                    (current_cycle, "false-green-prevented", 4),
                )

    def test_cli_outcome_report_json_has_stable_local_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            current_cycle = harness_db.cycle_status(root)["id"]

            result = run_harness(root, "cycle", "outcome-report", "--json")
            payload = json.loads(result.stdout)

            self.assertEqual(payload["report_version"], "kafa-outcome-v1")
            self.assertEqual(payload["evidence_scope"], "local-only")
            self.assertEqual(payload["cycle_id"], current_cycle)
            self.assertEqual(payload["observation_count"], 0)
            self.assertEqual(payload["observations"], [])

    def test_cli_rejects_invalid_observation_without_writing(self) -> None:
        invalid_args = {
            "unknown-kind": ("--kind", "invented-outcome"),
            "negative-value": ("--value", "-1"),
            "non-integer-value": ("--value", "not-an-integer"),
            "blank-details": ("--details", "   "),
            "blank-actor": ("--by", "\t"),
            "invalid-cycle": ("--cycle-id", "CYCLE-missing"),
            "malformed-timestamp": ("--observed-at", "yesterday"),
        }
        base = [
            "cycle",
            "outcome-record",
            "--id",
            "OUT-invalid",
            "--kind",
            "rework",
            "--value",
            "1",
            "--details",
            "one unit of verified rework",
            "--by",
            "qa-local",
            "--observed-at",
            "2026-07-21T09:30:00Z",
        ]
        for name, (flag, value) in invalid_args.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                run_harness(root, "init")
                with closing(sqlite3.connect(db_path(root))) as conn:
                    event_count = int(
                        conn.execute("select count(*) from events").fetchone()[0]
                    )
                    project_revision = int(
                        conn.execute(
                            "select revision from project where id=1"
                        ).fetchone()[0]
                    )
                args = list(base)
                if flag in args:
                    args[args.index(flag) + 1] = value
                else:
                    args.extend([flag, value])
                result = run_harness(root, *args, check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(observation_count(root), 0)
                with closing(sqlite3.connect(db_path(root))) as conn:
                    self.assertEqual(
                        int(conn.execute("select count(*) from events").fetchone()[0]),
                        event_count,
                    )
                    self.assertEqual(
                        int(
                            conn.execute(
                                "select revision from project where id=1"
                            ).fetchone()[0]
                        ),
                        project_revision,
                    )

    def test_ddl_rejects_invalid_bounded_fields_and_cycle_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            current_cycle = harness_db.cycle_status(root)["id"]
            valid = (
                "OUT-1",
                current_cycle,
                "rework",
                0,
                "one unit of rework",
                "qa-local",
                "2026-07-21T09:30:00Z",
                "2026-07-21T09:31:00Z",
            )
            invalid_rows = (
                (*valid[:2], "invented-outcome", *valid[3:]),
                (*valid[:3], -1, *valid[4:]),
                (*valid[:3], "1", *valid[4:]),
                (*valid[:3], "abc", *valid[4:]),
                (*valid[:3], 1.5, *valid[4:]),
                (*valid[:4], "   ", *valid[5:]),
                (*valid[:5], "\t", *valid[6:]),
                (valid[0], "CYCLE-missing", *valid[2:]),
                (*valid[:6], "yesterday", valid[7]),
                (*valid[:6], "2026-02-30T09:30:00Z", valid[7]),
                (*valid[:7], "2026-13-01T09:31:00Z"),
            )
            statement = """
                insert into outcome_observations
                (id, cycle_id, kind, value, details, recorded_by, observed_at, created_at)
                values (?, ?, ?, ?, ?, ?, ?, ?)
            """
            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.execute("pragma foreign_keys=on")
                conn.execute(statement, valid)
                conn.commit()
                self.assertEqual(
                    conn.execute(
                        "select value, typeof(value) from outcome_observations where id=?",
                        (valid[0],),
                    ).fetchone(),
                    (0, "integer"),
                )
            for index, row in enumerate(invalid_rows):
                invalid_row = (f"OUT-invalid-{index}", *row[1:])
                with self.subTest(index=index), closing(
                    sqlite3.connect(db_path(root))
                ) as conn:
                    conn.execute("pragma foreign_keys=on")
                    with self.assertRaises(sqlite3.IntegrityError):
                        conn.execute(statement, invalid_row)

    def test_outcome_observations_are_insert_only_audit_facts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            harness_api.record_outcome_observation(
                root,
                "OUT-IMMUTABLE",
                "rework",
                1,
                "one immutable bounded outcome fact",
                "qa-local",
                "2026-07-21T09:30:00Z",
            )

            with closing(sqlite3.connect(db_path(root))) as conn:
                trigger_names = {
                    str(row[0])
                    for row in conn.execute(
                        "select name from sqlite_master where type='trigger'"
                    )
                }
                self.assertIn("outcome_observations_no_update", trigger_names)
                self.assertIn("outcome_observations_no_delete", trigger_names)
                with self.assertRaisesRegex(
                    sqlite3.DatabaseError,
                    "outcome observations are immutable",
                ):
                    conn.execute(
                        "update outcome_observations set value=9 where id='OUT-IMMUTABLE'"
                    )
                with self.assertRaisesRegex(
                    sqlite3.DatabaseError,
                    "outcome observations are immutable",
                ):
                    conn.execute(
                        "delete from outcome_observations where id='OUT-IMMUTABLE'"
                    )
                self.assertEqual(
                    conn.execute(
                        "select value from outcome_observations where id='OUT-IMMUTABLE'"
                    ).fetchone()[0],
                    1,
                )

            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("drop trigger outcome_observations_no_update")
                issues = harness_db.runtime_schema_issues(conn)
            self.assertTrue(
                any(
                    "immutable trigger contract incomplete" in issue
                    and "outcome_observations_no_update" in issue
                    for issue in issues
                ),
                issues,
            )


class OutcomeMetricDefinitionTests(unittest.TestCase):
    def assert_metric_contract(self, metric: dict[str, object]) -> None:
        self.assertEqual(metric["metric_version"], "kafa-outcome-metric-v1")
        self.assertEqual(metric["evidence_mode"], "field")
        self.assertIn(metric["status"], {"observed", "computed", "insufficient-data"})
        self.assertIn("event_definition", metric)
        self.assertIn("unit", metric)
        self.assertIn("value", metric)
        self.assertEqual(metric["missing_data_semantics"], "insufficient-data")
        self.assertIsInstance(metric["not_applicable_when"], str)
        self.assertTrue(str(metric["not_applicable_when"]).strip())

        numerator = metric["numerator"]
        denominator = metric["denominator"]
        window = metric["window"]
        self.assertIsInstance(numerator, dict)
        self.assertIsInstance(denominator, dict)
        self.assertIsInstance(window, dict)
        assert isinstance(numerator, dict)
        assert isinstance(denominator, dict)
        assert isinstance(window, dict)
        self.assertTrue(str(numerator["definition"]).strip())
        self.assertIn("value", numerator)
        self.assertTrue(str(denominator["definition"]).strip())
        self.assertIn("value", denominator)
        self.assertIn(
            denominator["applicability"],
            {"required", "not-applicable"},
        )
        self.assertIn("kind", window)
        self.assertIn("start_at", window)
        self.assertIn("end_at", window)
        self.assertIn("complete", window)

    def test_empty_report_declares_all_metrics_without_fabricating_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)

            report = harness_api.outcome_report(root)

            self.assertEqual(report["metrics_version"], "kafa-outcome-metrics-v1")
            self.assertEqual(report["evidence_mode"], "field")
            self.assertEqual(set(report["metrics"]), METRIC_IDS)
            for metric_id, metric in report["metrics"].items():
                with self.subTest(metric_id=metric_id):
                    self.assert_metric_contract(metric)
                    self.assertEqual(metric["status"], "insufficient-data")
                    self.assertIsNone(metric["value"])

    def test_explicit_zero_observation_is_observed_not_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            cycle_id = set_cycle_window(
                root,
                closed_at="2026-07-21T10:00:00Z",
                status="delivered",
            )
            harness_api.record_outcome_observation(
                root,
                "OUT-ESCAPED-ZERO",
                "escaped-defect",
                0,
                "bounded field observation found zero escaped defects",
                "qa-local",
                "2026-07-21T09:30:00Z",
            )

            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.execute(
                    """
                    insert into deliveries
                    (id, cycle_id, candidate_sha, scope, created_at)
                    values ('DELIVERY-ZERO', ?, 'candidate-zero', 'metric fixture',
                            '2026-07-21T09:10:00Z')
                    """,
                    (cycle_id,),
                )
                conn.commit()

            metrics = outcome_report_at(root)["metrics"]
            escaped = metrics["escaped_defect_count"]
            false_green = metrics["false_green_prevented_count"]

            self.assertEqual(escaped["status"], "observed")
            self.assertEqual(escaped["value"], 0)
            self.assertEqual(escaped["numerator"]["value"], 0)
            self.assertEqual(escaped["denominator"]["applicability"], "not-applicable")
            self.assertTrue(escaped["window"]["complete"])
            self.assertEqual(false_green["status"], "insufficient-data")
            self.assertIsNone(false_green["value"])

    def test_report_computes_six_metrics_from_declared_local_facts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            harness_api.add_acceptance(root, "AC-1", "first active acceptance")
            harness_api.add_acceptance(root, "AC-2", "second active acceptance")
            harness_api.add_test_target(
                root,
                "TARGET-1",
                "unit",
                "python3 -m unittest tests.test_example",
            )
            harness_api.qualify_test_target(
                root,
                "QUAL-1",
                "TARGET-1",
                "AC-1",
                "target directly exercises the first acceptance contract",
                "qa-local",
            )
            cycle_id = set_cycle_window(
                root,
                closed_at="2026-07-21T10:00:00Z",
                status="delivered",
            )
            for observation_id, kind, value, at in (
                ("OUT-FALSE-GREEN", "false-green-prevented", 4, "2026-07-21T09:20:00Z"),
                ("OUT-ESCAPED", "escaped-defect", 0, "2026-07-21T09:30:00Z"),
                ("OUT-REWORK", "rework", 3, "2026-07-21T09:40:00Z"),
            ):
                harness_api.record_outcome_observation(
                    root,
                    observation_id,
                    kind,
                    value,
                    f"bounded local observation for {kind}",
                    "qa-local",
                    at,
                )

            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.execute(
                    """
                    insert into deliveries
                    (id, cycle_id, candidate_sha, scope, created_at)
                    values ('DELIVERY-1', ?, 'candidate-local', 'metric fixture',
                            '2026-07-21T09:10:00Z')
                    """,
                    (cycle_id,),
                )
                for index, status in enumerate(("rolled-back", "rollback-incomplete"), 1):
                    conn.execute(
                        """
                        insert into migrations
                        (from_version, to_version, source_sha256, backup_path,
                         manifest_path, row_counts_json, dropped_table_count,
                         status, applied_at)
                        values (30, 31, ?, ?, ?, '{}', 0, ?, ?)
                        """,
                        (
                            f"source-{index}",
                            f"backup-{index}",
                            f"manifest-{index}",
                            status,
                            f"2026-07-21T08:0{index}:00Z",
                        ),
                    )
                conn.commit()

            report = outcome_report_at(root)
            metrics = report["metrics"]

            self.assertEqual(report["observation_count"], 3)
            self.assertEqual(metrics["false_green_prevented_count"]["value"], 4)
            self.assertEqual(metrics["escaped_defect_count"]["value"], 0)
            self.assertEqual(metrics["rework_rate_per_delivery"]["value"], 3.0)
            self.assertEqual(
                metrics["migration_recovery_success_rate"]["value"],
                0.5,
            )
            self.assertEqual(
                metrics["time_to_verified_delivery_seconds"]["value"],
                600,
            )
            self.assertEqual(metrics["qualification_coverage_rate"]["value"], 0.5)
            self.assertEqual(
                metrics["rework_rate_per_delivery"]["numerator"]["value"],
                3,
            )
            self.assertEqual(
                metrics["rework_rate_per_delivery"]["denominator"]["value"],
                1,
            )
            self.assertEqual(
                metrics["migration_recovery_success_rate"]["numerator"]["value"],
                1,
            )
            self.assertEqual(
                metrics["migration_recovery_success_rate"]["denominator"]["value"],
                2,
            )
            self.assertEqual(
                metrics["qualification_coverage_rate"]["numerator"]["value"],
                1,
            )
            self.assertEqual(
                metrics["qualification_coverage_rate"]["denominator"]["value"],
                2,
            )
            for metric_id, metric in metrics.items():
                with self.subTest(metric_id=metric_id):
                    self.assert_metric_contract(metric)
                    self.assertEqual(metric["evidence_mode"], "field")
                    self.assertNotEqual(metric["evidence_mode"], "regression-benchmark")

    def test_stale_target_digest_removes_qualification_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            harness_api.add_acceptance(root, "AC-1", "active acceptance")
            harness_api.add_test_target(root, "TARGET-1", "unit", "python3 -m unittest")
            harness_api.qualify_test_target(
                root,
                "QUAL-1",
                "TARGET-1",
                "AC-1",
                "the declared unit target qualifies this acceptance",
                "qa-local",
            )
            before = harness_api.outcome_report(root)["metrics"][
                "qualification_coverage_rate"
            ]
            self.assertEqual(before["value"], 1.0)

            harness_api.add_test_target(
                root,
                "TARGET-1",
                "unit",
                "python3 -m unittest changed_target",
            )
            after = harness_api.outcome_report(root)["metrics"][
                "qualification_coverage_rate"
            ]

            self.assertEqual(after["status"], "computed")
            self.assertEqual(after["numerator"]["value"], 0)
            self.assertEqual(after["denominator"]["value"], 1)
            self.assertEqual(after["value"], 0.0)

            harness_api.qualify_test_target(
                root,
                "QUAL-2",
                "TARGET-1",
                "AC-1",
                "the changed target is explicitly requalified",
                "qa-local",
            )
            self.assertEqual(
                outcome_report_at(root)["metrics"]["qualification_coverage_rate"][
                    "value"
                ],
                1.0,
            )
            harness_api.add_acceptance(root, "AC-1", "revised active acceptance")
            revised = outcome_report_at(root)["metrics"][
                "qualification_coverage_rate"
            ]
            self.assertEqual(revised["numerator"]["value"], 0)
            self.assertEqual(revised["denominator"]["value"], 1)
            self.assertEqual(revised["value"], 0.0)

    def test_rate_and_duration_require_real_denominator_or_completed_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            set_cycle_window(root)
            harness_api.record_outcome_observation(
                root,
                "OUT-REWORK",
                "rework",
                2,
                "two units of bounded local rework",
                "qa-local",
                "2026-07-21T09:30:00Z",
            )

            metrics = outcome_report_at(root)["metrics"]
            rework = metrics["rework_rate_per_delivery"]
            recovery = metrics["migration_recovery_success_rate"]
            delivery_time = metrics["time_to_verified_delivery_seconds"]
            qualification = metrics["qualification_coverage_rate"]

            self.assertEqual(rework["numerator"]["value"], 2)
            self.assertEqual(rework["denominator"]["value"], 0)
            self.assertEqual(rework["status"], "insufficient-data")
            self.assertIsNone(rework["value"])
            for metric in (recovery, delivery_time, qualification):
                self.assertEqual(metric["status"], "insufficient-data")
                self.assertIsNone(metric["value"])

    def test_windows_exclude_out_of_scope_observations_and_historical_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            cycle_id = set_cycle_window(root)
            for observation_id, kind, value, at in (
                ("OUT-FALSE-EARLY", "false-green-prevented", 99, "2026-07-21T08:59:59Z"),
                ("OUT-FALSE-BOUNDARY", "false-green-prevented", 1, "2026-07-21T09:00:00Z"),
                ("OUT-ESCAPED-EARLY", "escaped-defect", 99, "2026-07-21T09:05:00Z"),
                ("OUT-ESCAPED-AFTER", "escaped-defect", 1, "2026-07-21T09:11:00Z"),
                ("OUT-REWORK", "rework", 2, "2026-07-21T09:30:00Z"),
            ):
                harness_api.record_outcome_observation(
                    root,
                    observation_id,
                    kind,
                    value,
                    f"window boundary fixture for {kind}",
                    "qa-local",
                    at,
                )
            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.execute(
                    """
                    insert into deliveries
                    (id, cycle_id, candidate_sha, scope, decision_status, created_at)
                    values ('DELIVERY-HISTORICAL', ?, 'legacy-candidate', 'historical',
                            'historical-migrated', '2026-07-21T09:05:00Z')
                    """,
                    (cycle_id,),
                )
                conn.execute(
                    """
                    insert into deliveries
                    (id, cycle_id, candidate_sha, scope, decision_status, created_at)
                    values ('DELIVERY-CURRENT', ?, 'candidate-current', 'verified',
                            'delivered', '2026-07-21T09:10:00Z')
                    """,
                    (cycle_id,),
                )
                conn.commit()

            metrics = outcome_report_at(root)["metrics"]

            self.assertEqual(metrics["false_green_prevented_count"]["value"], 1)
            self.assertEqual(metrics["escaped_defect_count"]["value"], 1)
            self.assertEqual(metrics["rework_rate_per_delivery"]["value"], 2.0)
            self.assertEqual(
                metrics["rework_rate_per_delivery"]["denominator"]["value"],
                1,
            )
            self.assertEqual(
                metrics["time_to_verified_delivery_seconds"]["value"],
                600,
            )

    def test_invalid_or_negative_persisted_windows_are_insufficient(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            cycle_id = set_cycle_window(root, started_at="2026-07-21T10:00:00Z")
            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.execute(
                    """
                    insert into deliveries
                    (id, cycle_id, candidate_sha, scope, created_at)
                    values ('DELIVERY-EARLY', ?, 'candidate-current', 'invalid interval',
                            '2026-07-21T09:59:59Z')
                    """,
                    (cycle_id,),
                )
                conn.execute(
                    """
                    insert into migrations
                    (from_version, to_version, status, applied_at)
                    values (30, 31, 'rolled-back', 'not-a-timestamp')
                    """
                )
                conn.commit()

            metrics = outcome_report_at(root)["metrics"]

            self.assertEqual(
                metrics["time_to_verified_delivery_seconds"]["status"],
                "insufficient-data",
            )
            self.assertIsNone(
                metrics["time_to_verified_delivery_seconds"]["value"]
            )
            self.assertEqual(
                metrics["migration_recovery_success_rate"]["status"],
                "insufficient-data",
            )
            self.assertIsNone(
                metrics["migration_recovery_success_rate"]["value"]
            )

    def test_future_delivery_does_not_complete_time_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            cycle_id = set_cycle_window(root)
            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.execute(
                    """
                    insert into deliveries
                    (id, cycle_id, candidate_sha, scope, created_at)
                    values ('DELIVERY-FUTURE', ?, 'candidate-current', 'future fact',
                            '2026-07-21T10:00:01Z')
                    """,
                    (cycle_id,),
                )
                conn.commit()

            metric = outcome_report_at(root)["metrics"][
                "time_to_verified_delivery_seconds"
            ]

            self.assertEqual(metric["status"], "insufficient-data")
            self.assertIsNone(metric["value"])

    def test_report_is_a_read_only_repeatable_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            with closing(sqlite3.connect(db_path(root))) as conn:
                before = {
                    "events": conn.execute("select count(*) from events").fetchone()[0],
                    "revision": conn.execute(
                        "select revision from project where id=1"
                    ).fetchone()[0],
                    "outcomes": conn.execute(
                        "select count(*) from outcome_observations"
                    ).fetchone()[0],
                }

            first = outcome_report_at(root)
            second = outcome_report_at(root)

            self.assertEqual(first, second)
            with closing(sqlite3.connect(db_path(root))) as conn:
                after = {
                    "events": conn.execute("select count(*) from events").fetchone()[0],
                    "revision": conn.execute(
                        "select revision from project where id=1"
                    ).fetchone()[0],
                    "outcomes": conn.execute(
                        "select count(*) from outcome_observations"
                    ).fetchone()[0],
                }
            self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()

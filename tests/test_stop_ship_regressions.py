from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from tests.test_local_delivery_policy import (
    create_schema30_delivery_fixture,
    schema30_issues,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "plugins/codex-project-harness/scripts/harness.py"


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


def cycle_fact_rows(
    conn: sqlite3.Connection,
    table: str,
    value_column: str,
    local_id: str,
) -> list[tuple[str, str]]:
    columns = {row[1] for row in conn.execute(f"pragma table_info({table})")}
    identity_column = "local_id" if "local_id" in columns else "id"
    return conn.execute(
        f"select cycle_id, {value_column} from {table} "
        f"where {identity_column} = ? order by cycle_id",
        (local_id,),
    ).fetchall()


def insert_finding(
    root: Path,
    *,
    finding_id: str,
    status: str,
    cycle_id: str = "CYCLE-current",
    waived_by: str = "",
    waiver_reason: str = "",
    waiver_scope: str = "",
    waived_revision: int | None = None,
    waiver_expires_at: str = "",
) -> None:
    with closing(sqlite3.connect(db_path(root))) as conn:
        candidate = conn.execute(
            "select candidate_sha from delivery_cycles where id = ?",
            (cycle_id,),
        ).fetchone()[0]
        conn.execute(
            """
            insert into findings
            (id, cycle_id, candidate_sha, surface, severity, status, summary,
             waived_by, waiver_reason, waiver_scope, waived_revision,
             waiver_expires_at, created_at)
            values (?, ?, ?, 'delivery', 'critical', ?, 'stop-ship finding',
                    ?, ?, ?, ?, ?, '2026-07-11T00:00:00Z')
            """,
            (
                finding_id,
                cycle_id,
                candidate,
                status,
                waived_by,
                waiver_reason,
                waiver_scope,
                waived_revision,
                waiver_expires_at,
            ),
        )
        conn.commit()


def link_finding_to_active_gate(root: Path, finding_id: str) -> None:
    """Link a legacy schema-30 fixture finding without invoking schema-31 CLI writes."""

    with closing(sqlite3.connect(db_path(root))) as conn:
        conn.execute(
            "insert into quality_gate_findings (gate_id, finding_id) values ('G1', ?)",
            (finding_id,),
        )
        conn.commit()


def append_legacy_gate_result(root: Path, *, gate_id: str, result: str) -> None:
    """Construct historical gate ordering for the schema-30 policy compatibility fixture."""

    with closing(sqlite3.connect(db_path(root))) as conn:
        candidate = conn.execute(
            "select candidate_sha from delivery_cycles where id = 'CYCLE-current'"
        ).fetchone()[0]
        previous = conn.execute(
            "select id from quality_gates where gate_status = 'active'"
        ).fetchone()
        if previous:
            conn.execute(
                "update quality_gates set gate_status = 'superseded', superseded_by = ? where id = ?",
                (gate_id, previous[0]),
            )
        sequence = int(conn.execute("select coalesce(max(sequence), 0) + 1 from quality_gates").fetchone()[0])
        conn.execute(
            """
            insert into quality_gates
            (id, sequence, cycle_id, candidate_sha, gate_status, gate,
             producer_context_id, reviewer_context_id, review_status, result,
             blocking_findings, residual_risk, reviewed_revision, created_at)
            values (?, ?, 'CYCLE-current', ?, 'active', 'independent_qa',
                    'producer-context', 'reviewer-context', 'reviewed-local', ?,
                    '', '', 1, '2026-07-11T10:00:00Z')
            """,
            (gate_id, sequence, candidate, result),
        )
        conn.commit()


class StopShipRegressionTest(unittest.TestCase):
    def test_open_critical_finding_links_to_gate_and_blocks_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            create_schema30_delivery_fixture(root)
            insert_finding(root, finding_id="F-critical", status="open")
            link_finding_to_active_gate(root, "F-critical")
            with closing(sqlite3.connect(db_path(root))) as conn:
                linked = conn.execute(
                    """
                    select f.severity, f.status
                    from quality_gate_findings qgf
                    join findings f on f.id = qgf.finding_id
                    join quality_gates g on g.id = qgf.gate_id
                    where f.id = 'F-critical' and g.gate_status = 'active'
                    """
                ).fetchone()
            issues = schema30_issues(root)

        self.assertEqual(linked, ("critical", "open"))
        self.assertIn("critical finding blocks delivery: F-critical", " ".join(issues))

    def test_same_second_newer_failed_gate_wins_by_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            create_schema30_delivery_fixture(root)
            append_legacy_gate_result(root, gate_id="G2", result="pass")
            append_legacy_gate_result(root, gate_id="G3", result="fail")
            with closing(sqlite3.connect(db_path(root))) as conn:
                pass_id = conn.execute(
                    "select id from quality_gates where sequence = 2"
                ).fetchone()[0]
                fail_id = conn.execute(
                    "select id from quality_gates where sequence = 3"
                ).fetchone()[0]
                timestamp = "2026-07-11T10:00:00Z"
                conn.execute(
                    "update quality_gates set id = 'a-new-fail', created_at = ? where id = ?",
                    (timestamp, fail_id),
                )
                conn.execute(
                    "update quality_gates set superseded_by = 'a-new-fail' where sequence = 2"
                )
                conn.execute(
                    "update quality_gates set id = 'z-old-pass', created_at = ? where id = ?",
                    (timestamp, pass_id),
                )
                conn.execute(
                    "update quality_gates set superseded_by = 'z-old-pass', created_at = ? where sequence = 1",
                    (timestamp,),
                )
                conn.commit()
                rows = conn.execute(
                    "select sequence, id, gate_status, superseded_by, result "
                    "from quality_gates order by sequence"
                ).fetchall()
            issues = schema30_issues(root)

        self.assertEqual(
            rows,
            [
                (1, "G1", "superseded", "z-old-pass", "pass"),
                (2, "z-old-pass", "superseded", "a-new-fail", "pass"),
                (3, "a-new-fail", "active", None, "fail"),
            ],
        )
        self.assertIn("latest quality gate is not pass", " ".join(issues))

    def test_resolved_and_complete_current_waiver_are_allowed(self) -> None:
        cases = (
            ("resolved", {}, False),
            (
                "accepted",
                {
                    "waived_by": "user",
                    "waiver_reason": "explicit candidate waiver",
                    "waiver_scope": "candidate",
                    "waived_revision": 1,
                    "waiver_expires_at": "2026-07-12T00:00:00Z",
                },
                False,
            ),
            (
                "accepted",
                {
                    "waived_by": "user",
                    "waiver_reason": "expired waiver",
                    "waiver_scope": "candidate",
                    "waived_revision": 1,
                    "waiver_expires_at": "2000-01-01T00:00:00Z",
                },
                True,
            ),
            (
                "accepted",
                {
                    "waived_by": "user",
                    "waiver_reason": "stale waiver",
                    "waiver_scope": "candidate",
                    "waived_revision": 2,
                    "waiver_expires_at": "2026-07-12T00:00:00Z",
                },
                True,
            ),
        )
        for index, (status, waiver, should_block) in enumerate(cases, start=1):
            with self.subTest(status=status, waiver=waiver), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                create_schema30_delivery_fixture(root)
                finding_id = f"F-{index}"
                insert_finding(root, finding_id=finding_id, status=status, **waiver)
                issues = schema30_issues(root)

            self.assertEqual(
                any(finding_id in issue for issue in issues),
                should_block,
                issues,
            )

    def test_open_finding_from_old_cycle_does_not_block_current_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            create_schema30_delivery_fixture(root)
            with closing(sqlite3.connect(db_path(root))) as conn:
                candidate = conn.execute(
                    "select candidate_sha from delivery_cycles where id = 'CYCLE-current'"
                ).fetchone()[0]
                conn.execute(
                    """
                    insert into delivery_cycles
                    (id, name, goal, status, phase, base_ref, candidate_sha, started_at,
                     closed_at, created_at, updated_at)
                    values ('CYCLE-old', 'Old', 'Historical', 'archived',
                            'delivery_readiness', '', ?, '2026-07-10T00:00:00Z',
                            '2026-07-10T01:00:00Z', '2026-07-10T00:00:00Z',
                            '2026-07-10T01:00:00Z')
                    """,
                    (candidate,),
                )
                conn.commit()
            insert_finding(root, finding_id="F-old", status="open", cycle_id="CYCLE-old")

            issues = schema30_issues(root)

        self.assertFalse(any("F-old" in issue for issue in issues), issues)

    def test_cycle_local_ids_preserve_history_and_project_current_facts_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "requirement", "add", "--id", "R1", "--kind", "functional", "--body", "Original requirement")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Original acceptance")
            run_harness(root, "task", "add", "--id", "T1", "--task", "Original task", "--acceptance", "AC1")
            run_harness(root, "cycle", "close", "--status", "archived")
            run_harness(root, "cycle", "start", "--id", "CYCLE-next", "--name", "Next", "--goal", "Iterate")
            run_harness(root, "requirement", "add", "--id", "R1", "--kind", "functional", "--body", "Next requirement")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Next acceptance")
            run_harness(root, "task", "add", "--id", "T1", "--task", "Next task", "--acceptance", "AC1")
            started = run_harness(root, "task", "start", "T1")
            trace = run_harness(root, "trace", "validate", check=False)
            with closing(sqlite3.connect(db_path(root))) as conn:
                requirements = cycle_fact_rows(conn, "requirements", "body", "R1")
                acceptance = cycle_fact_rows(conn, "acceptance", "criterion", "AC1")
                tasks = cycle_fact_rows(conn, "tasks", "task", "T1")
                task_states = conn.execute(
                    "select cycle_id, status, revision from tasks where id = 'T1' order by cycle_id"
                ).fetchall()
            task_board = (root / ".ai-team/planning/task-board.md").read_text(encoding="utf-8")

        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        self.assertEqual(
            requirements,
            [("CYCLE-current", "Original requirement"), ("CYCLE-next", "Next requirement")],
        )
        self.assertEqual(
            acceptance,
            [("CYCLE-current", "Original acceptance"), ("CYCLE-next", "Next acceptance")],
        )
        self.assertEqual(
            tasks,
            [("CYCLE-current", "Original task"), ("CYCLE-next", "Next task")],
        )
        self.assertEqual(
            task_states,
            [("CYCLE-current", "planned", 1), ("CYCLE-next", "active", 2)],
        )
        self.assertIn("Next task", task_board)
        self.assertNotIn("Original task", task_board)
        self.assertNotEqual(trace.returncode, 0)
        self.assertIn("requirement has no acceptance link: R1", trace.stdout + trace.stderr)


if __name__ == "__main__":
    unittest.main()

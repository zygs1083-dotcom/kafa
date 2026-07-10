from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "plugins" / "codex-project-harness" / "scripts" / "harness.py"
DEFAULT_TEST_COMMAND = "python3 -B -m unittest test_harness_dummy.py"


def run_harness(
    root: Path,
    *args: str,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    result = subprocess.run(
        [sys.executable, str(HARNESS), "--root", str(root), *args],
        text=True,
        capture_output=True,
        check=False,
        env=merged_env,
    )
    if check and result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return result


def db_path(root: Path) -> Path:
    return root / ".ai-team" / "state" / "harness.db"


def task_revision(root: Path, task_id: str) -> int:
    with closing(sqlite3.connect(db_path(root))) as conn:
        return int(conn.execute("select revision from tasks where id = ?", (task_id,)).fetchone()[0])


def stdout_field(stdout: str, name: str) -> str:
    return stdout.split(f"{name}=", 1)[1].split(None, 1)[0].strip()


def cycle_fact_rows(
    conn: sqlite3.Connection,
    table: str,
    value_column: str,
    local_id: str,
) -> list[tuple[str, str]]:
    columns = {row[1] for row in conn.execute(f"pragma table_info({table})")}
    identity_column = "local_id" if "local_id" in columns else "id"
    return conn.execute(
        f"select cycle_id, {value_column} from {table} where {identity_column} = ? order by cycle_id",
        (local_id,),
    ).fetchall()


def accept_task(root: Path, task_id: str) -> None:
    claim = run_harness(
        root,
        "task",
        "claim",
        task_id,
        "--agent",
        "developer",
        "--expected-revision",
        str(task_revision(root, task_id)),
    )
    producer_token = stdout_field(claim.stdout, "token")
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
        "implemented",
    )
    review = run_harness(
        root,
        "task",
        "review",
        task_id,
        "--agent",
        "qa-reviewer",
        "--expected-revision",
        str(task_revision(root, task_id)),
    )
    reviewer_token = stdout_field(review.stdout, "token")
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


def prepare_delivery_candidate(root: Path) -> None:
    run_harness(root, "init")
    run_harness(root, "requirement", "add", "--id", "R1", "--kind", "functional", "--body", "Example")
    run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example acceptance")
    run_harness(root, "requirement", "link", "--requirement", "R1", "--acceptance", "AC1")
    run_harness(root, "task", "add", "--id", "T1", "--task", "Example task", "--acceptance", "AC1")
    accept_task(root, "T1")
    run_harness(root, "scope", "confirm", "--by", "project-manager", "--summary", "confirmed")
    run_harness(root, "baseline", "freeze", "--id", "B1", "--summary", "baseline")
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
    evidence_id = run_harness(
        root,
        "dispatch",
        "run",
        "--agent",
        "developer",
        "--target",
        "UNIT",
        "--command",
        DEFAULT_TEST_COMMAND,
        "--code-identity",
        "content-hash",
    ).stdout.strip().rsplit(" ", 1)[-1]
    run_harness(
        root,
        "test",
        "record",
        "--id",
        "TEST1",
        "--surface",
        "Example",
        "--command",
        DEFAULT_TEST_COMMAND,
        "--result",
        "pass",
        "--evidence",
        evidence_id,
    )
    run_harness(
        root,
        "validation",
        "record",
        "--surface",
        "Example",
        "--acceptance",
        "AC1",
        "--commands",
        DEFAULT_TEST_COMMAND,
        "--findings",
        "passed",
        "--result",
        "pass",
        "--test",
        "TEST1",
        "--evidence",
        evidence_id,
        "--target",
        "UNIT",
        "--code-identity",
        "content-hash",
    )


def move_to_delivery_readiness(root: Path) -> subprocess.CompletedProcess[str]:
    for phase in ["project_bootstrap", "requirement_baseline", "confirmation", "planning", "implementation", "qa"]:
        run_harness(root, "phase", phase)
    return run_harness(root, "phase", "delivery_readiness", check=False)


def git_init_with_tiny_project(root: Path) -> None:
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=root, check=True)
    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (root / "test_calc.py").write_text(
        "import unittest\n\n"
        "from calc import add\n\n"
        "class CalcTest(unittest.TestCase):\n"
        "    def test_add(self):\n"
        "        self.assertEqual(add(2, 3), 5)\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "calc.py", "test_calc.py"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=root, check=True, capture_output=True, text=True)


class StopShipRegressionTest(unittest.TestCase):
    def test_dt_001_open_critical_finding_blocks_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_delivery_candidate(root)
            run_harness(
                root,
                "finding",
                "record",
                "--id",
                "F-critical",
                "--surface",
                "delivery",
                "--severity",
                "critical",
                "--status",
                "open",
                "--summary",
                "Known release blocker",
            )
            run_harness(
                root,
                "gate",
                "record",
                "--reviewer-context",
                "fresh",
                "--result",
                "pass",
                "--commands",
                DEFAULT_TEST_COMMAND,
                "--evidence",
                "reviewed",
                "--finding",
                "F-critical",
            )
            with closing(sqlite3.connect(db_path(root))) as conn:
                linked_finding = conn.execute(
                    """
                    select f.severity, f.status
                    from quality_gate_findings qgf
                    join findings f on f.id = qgf.finding_id
                    where f.id = 'F-critical'
                    """
                ).fetchone()
            self.assertEqual(linked_finding, ("critical", "open"))

            readiness = move_to_delivery_readiness(root)

        self.assertNotEqual(
            readiness.returncode,
            0,
            "DT-001: a passing gate linked to an open critical finding incorrectly reached delivery_readiness",
        )
        self.assertIn("F-critical", readiness.stdout + readiness.stderr)

    def test_dt_002_same_second_newer_fail_gate_wins(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_delivery_candidate(root)
            for result in ["pass", "fail"]:
                run_harness(
                    root,
                    "gate",
                    "record",
                    "--reviewer-context",
                    "fresh",
                    "--result",
                    result,
                    "--commands",
                    DEFAULT_TEST_COMMAND,
                    "--evidence",
                    result,
                )
            with closing(sqlite3.connect(db_path(root))) as conn:
                timestamp = "2026-07-10T10:00:00Z"
                conn.execute("update quality_gates set id = 'z-old-pass', created_at = ? where result = 'pass'", (timestamp,))
                conn.execute("update quality_gates set id = 'a-new-fail', created_at = ? where result = 'fail'", (timestamp,))
                conn.commit()
                insertion_order = [
                    row[0] for row in conn.execute("select result from quality_gates order by rowid")
                ]
            self.assertEqual(insertion_order, ["pass", "fail"])

            readiness = move_to_delivery_readiness(root)

        output = readiness.stdout + readiness.stderr
        self.assertNotEqual(
            readiness.returncode,
            0,
            "DT-002: a newer fail gate was hidden by an older pass gate written in the same second",
        )
        self.assertIn("latest quality gate is not pass", output)

    def test_cy_001_reusing_local_ids_does_not_move_old_cycle_history(self) -> None:
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
            second_task = run_harness(
                root,
                "task",
                "add",
                "--id",
                "T1",
                "--task",
                "Next task",
                "--acceptance",
                "AC1",
                check=False,
            )
            claimed = run_harness(
                root,
                "task",
                "claim",
                "T1",
                "--agent",
                "developer",
                "--expected-revision",
                "1",
                check=False,
            )
            trace = run_harness(root, "trace", "validate", check=False)
            with closing(sqlite3.connect(db_path(root))) as conn:
                requirements = cycle_fact_rows(conn, "requirements", "body", "R1")
                acceptance = cycle_fact_rows(conn, "acceptance", "criterion", "AC1")
                tasks = cycle_fact_rows(conn, "tasks", "task", "T1")
                task_states = conn.execute(
                    "select cycle_id, status, revision from tasks where id = 'T1' order by cycle_id"
                ).fetchall()
            task_board = (root / ".ai-team/planning/task-board.md").read_text(encoding="utf-8")

        self.assertEqual(
            (requirements, acceptance, second_task.returncode, claimed.returncode, tasks, task_states),
            (
                [("CYCLE-current", "Original requirement"), ("CYCLE-next", "Next requirement")],
                [("CYCLE-current", "Original acceptance"), ("CYCLE-next", "Next acceptance")],
                0,
                0,
                [("CYCLE-current", "Original task"), ("CYCLE-next", "Next task")],
                [("CYCLE-current", "ready", 1), ("CYCLE-next", "claimed", 2)],
            ),
            "CY-001: cycle-local IDs must preserve old requirement, acceptance, and task history",
        )
        self.assertIn("Next task", task_board)
        self.assertNotIn("Original task", task_board)
        self.assertNotEqual(trace.returncode, 0)
        self.assertIn("requirement has no acceptance link: R1", trace.stdout + trace.stderr)

    def test_tr_001_cli_cannot_self_issue_connector_attestation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")

            result = run_harness(
                root,
                "session",
                "attest",
                "--session-id",
                "S-review",
                "--agent",
                "qa-reviewer",
                "--role",
                "qa-reviewer",
                "--context-id",
                "ctx-review",
                "--origin",
                "connector",
                check=False,
                env={"HARNESS_CONNECTOR_KEY": "test-secret"},
            )
            with closing(sqlite3.connect(db_path(root))) as conn:
                trusted_rows = conn.execute(
                    "select count(*) from session_attestations where trust_level = 'connector'"
                ).fetchone()[0]

        self.assertNotEqual(
            result.returncode,
            0,
            "TR-001: the ordinary Kernel CLI self-issued connector trust without an external receipt",
        )
        self.assertEqual(trusted_rows, 0)

    def test_qs_001_quickstart_stops_before_independent_qa_and_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            git_init_with_tiny_project(root)

            result = run_harness(
                root,
                "quickstart",
                "minimal",
                "--id",
                "SMOKE",
                "--goal",
                "Keep add working",
                "--acceptance",
                "add(2, 3) returns 5",
                "--task",
                "Verify calculator add",
                "--test-command",
                "python3 -B -m unittest discover -s . -p 'test_*.py'",
                "--execute",
            )
            cycle = json.loads(run_harness(root, "cycle", "status", "--json").stdout)
            with closing(sqlite3.connect(db_path(root))) as conn:
                delivery_count = conn.execute("select count(*) from deliveries").fetchone()[0]
                fresh_pass_count = conn.execute(
                    "select count(*) from quality_gates where reviewer_context = 'fresh' and result = 'pass'"
                ).fetchone()[0]

        self.assertEqual(
            (
                cycle["status"] != "delivered",
                delivery_count == 0,
                fresh_pass_count == 0,
                "NEXT:" in result.stdout,
            ),
            (True, True, True, True),
            "QS-001: quickstart must stop at verified setup and print the independent reviewer next step",
        )

    def test_in_001_user_marketplace_source_resolves_to_copied_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            home.mkdir()
            env = os.environ.copy()
            env["HOME"] = str(home)
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "kafa.cli",
                    "plugin",
                    "install",
                    "--repo",
                    str(REPO_ROOT),
                    "--scope",
                    "user",
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            marketplace_path = home / ".agents" / "plugins" / "marketplace.json"
            marketplace = json.loads(marketplace_path.read_text(encoding="utf-8"))
            entry = next(plugin for plugin in marketplace["plugins"] if plugin["name"] == "codex-project-harness")
            resolved_source = (home / entry["source"]["path"]).resolve()
            copied_plugin = (home / ".agents" / "plugins" / "codex-project-harness").resolve()

        self.assertEqual(
            resolved_source,
            copied_plugin,
            "IN-001: the user marketplace source path does not resolve to the plugin copied by kafa",
        )


if __name__ == "__main__":
    unittest.main()

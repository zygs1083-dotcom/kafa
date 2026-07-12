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
PLUGIN_ROOT = REPO_ROOT / "plugins" / "codex-project-harness"
HARNESS = PLUGIN_ROOT / "scripts" / "harness.py"
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from core import SCHEMA_VERSION  # noqa: E402


def run_harness(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HARNESS), "--root", str(root), *args],
        text=True,
        capture_output=True,
        check=False,
    )


def query_one(root: Path, sql: str) -> tuple[object, ...]:
    with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
        row = conn.execute(sql).fetchone()
        if row is None:
            raise AssertionError(f"no row for {sql}")
        return tuple(row)


def prepare_verified_candidate(root: Path) -> None:
    (root / "test_candidate.py").write_text(
        "import unittest\n\n"
        "class CandidateTest(unittest.TestCase):\n"
        "    def test_candidate(self):\n"
        "        self.assertTrue(True)\n",
        encoding="utf-8",
    )
    commands = [
        (
            "quickstart",
            "minimal",
            "--id",
            "SMOKE",
            "--goal",
            "local delivery",
            "--acceptance",
            "candidate passes",
            "--task",
            "implement candidate",
            "--test-command",
            "python3 -B -m unittest test_candidate.py",
            "--execute",
        ),
        ("task", "accept", "SMOKE-T1", "--evidence", "independent review returned"),
        (
            "gate",
            "record",
            "--reviewer-context",
            "fresh",
            "--reviewer-context-id",
            "reviewer-context",
            "--result",
            "pass",
        ),
    ]
    for args in commands:
        result = run_harness(root, *args)
        if result.returncode != 0:
            raise AssertionError(result.stdout + result.stderr)


class DeliveryCyclesTest(unittest.TestCase):
    def test_init_creates_schema30_active_current_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = run_harness(root, "init")
            project = query_one(
                root, "select schema_version, current_cycle_id from project where id=1"
            )
            cycle = query_one(
                root,
                "select id, status, name, goal from delivery_cycles "
                "where id='CYCLE-current'",
            )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(project, (SCHEMA_VERSION, "CYCLE-current"))
        self.assertEqual(cycle[:2], ("CYCLE-current", "active"))
        self.assertTrue(cycle[2])
        self.assertTrue(cycle[3])

    def test_cycle_start_requires_a_closed_current_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(run_harness(root, "init").returncode, 0)
            premature = run_harness(
                root,
                "cycle",
                "start",
                "--id",
                "CYCLE-next",
                "--name",
                "Next",
                "--goal",
                "Iterate",
            )
            self.assertEqual(
                run_harness(root, "cycle", "close", "--status", "archived").returncode,
                0,
            )
            started = run_harness(
                root,
                "cycle",
                "start",
                "--id",
                "CYCLE-next",
                "--name",
                "Next",
                "--goal",
                "Iterate",
            )
            status = json.loads(run_harness(root, "cycle", "status", "--json").stdout)

        self.assertNotEqual(premature.returncode, 0)
        self.assertIn("current cycle is not closed", premature.stdout + premature.stderr)
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        self.assertEqual((status["id"], status["status"]), ("CYCLE-next", "active"))

    def test_delivery_record_closes_the_verified_current_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_verified_candidate(root)
            delivery = run_harness(
                root, "delivery", "record", "--scope", "local", "--acceptance", "SMOKE-AC1"
            )
            validation = run_harness(root, "validate", "--delivery")
            cycle = query_one(
                root,
                "select status, closed_at, candidate_sha from delivery_cycles "
                "where id='CYCLE-current'",
            )

        self.assertEqual(delivery.returncode, 0, delivery.stdout + delivery.stderr)
        self.assertEqual(validation.returncode, 0, validation.stdout + validation.stderr)
        self.assertEqual(cycle[0], "delivered")
        self.assertTrue(cycle[1])
        self.assertTrue(cycle[2])

    def test_candidate_change_after_gate_requires_new_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prepare_verified_candidate(root)
            (root / "candidate.py").write_text("changed = True\n", encoding="utf-8")
            delivery = run_harness(
                root, "delivery", "record", "--scope", "stale", "--acceptance", "SMOKE-AC1"
            )
            delivery_count = query_one(root, "select count(*) from deliveries")[0]

        self.assertNotEqual(delivery.returncode, 0)
        self.assertIn("current candidate", delivery.stdout + delivery.stderr)
        self.assertEqual(delivery_count, 0)


if __name__ == "__main__":
    unittest.main()

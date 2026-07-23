from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = (
    REPO_ROOT
    / "plugins"
    / "codex-project-harness"
    / "scripts"
    / "harness.py"
)
PLUGIN_ROOT = REPO_ROOT / "plugins" / "codex-project-harness"
for module_path in (PLUGIN_ROOT, PLUGIN_ROOT / "scripts"):
    if str(module_path) not in sys.path:
        sys.path.insert(0, str(module_path))

import harness_db  # noqa: E402
from core import delivery as delivery_core  # noqa: E402
from core.operator_output import build_operator_envelope, render_concise  # noqa: E402


def run_harness(
    root: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, "-B", str(HARNESS), "--root", str(root), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return result


class ConciseOperatorOutputTest(unittest.TestCase):
    maxDiff = None

    def init_runtime(self, root: Path) -> None:
        run_harness(root, "init")

    def assert_concise_card(self, stdout: str) -> list[str]:
        lines = stdout.rstrip("\n").splitlines()
        self.assertEqual(
            len(lines),
            3,
            "default operator output must contain exactly state, blocker, and next",
        )
        self.assertRegex(lines[0], r"^state: \S.+$")
        self.assertRegex(lines[1], r"^blocker: \S.+$")
        self.assertRegex(lines[2], r"^next: \S.+$")
        return lines

    def assert_json_envelope(self, result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
        self.assertEqual(
            result.stderr,
            "",
            "JSON mode must not emit a second human diagnostics stream",
        )
        self.assertNotIn("ERROR:", result.stdout)
        payload = json.loads(result.stdout)
        self.assertIsInstance(payload, dict)
        self.assertTrue(
            {"state", "blockers", "actions", "details"}.issubset(payload),
            payload,
        )
        self.assertIsInstance(payload["state"], str)
        self.assertIsInstance(payload["blockers"], list)
        self.assertIsInstance(payload["actions"], list)
        self.assertIsInstance(payload["details"], dict)
        for blocker in payload["blockers"]:
            self.assertIsInstance(blocker, dict)
            self.assertIsInstance(blocker.get("code"), str)
            self.assertIsInstance(blocker.get("message"), str)
        for action in payload["actions"]:
            self.assertIsInstance(action, str)
        return payload

    def test_status_default_is_only_the_three_line_operator_card(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.init_runtime(root)

            result = run_harness(root, "status")

        lines = self.assert_concise_card(result.stdout)
        self.assertNotIn("phase:", result.stdout)
        self.assertNotIn("intake", result.stdout)
        self.assertNotIn("CYCLE-current", lines[0])
        self.assertIn("CYCLE-current", lines[1])
        self.assertNotIn("CYCLE-current", lines[2])
        self.assertNotIn("schema_version:", result.stdout)
        self.assertNotIn("tasks:", result.stdout)
        self.assertNotIn("revision:", result.stdout)
        self.assertNotEqual(lines[1], "blocker: none")

    def test_status_verbose_retains_the_complete_existing_human_detail(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.init_runtime(root)

            result = run_harness(root, "status", "--verbose")

        self.assertIn("# Harness Status", result.stdout)
        self.assertIn("status: draft", result.stdout)
        self.assertIn("phase: intake", result.stdout)
        self.assertIn("cycle: CYCLE-current", result.stdout)
        self.assertIn("scope_status: unconfirmed", result.stdout)
        self.assertIn("schema_version: 31", result.stdout)
        self.assertIn("tasks: 0", result.stdout)
        self.assertIn("events:", result.stdout)
        self.assertIn("[requirement-missing]", result.stdout)
        self.assertIn("next_commands:", result.stdout)
        self.assertIn("requirement add", result.stdout)

    def test_status_json_is_one_complete_parseable_object(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.init_runtime(root)

            result = run_harness(root, "status", "--json")

        payload = self.assert_json_envelope(result)
        self.assertGreater(len(payload["blockers"]), 1)
        self.assertGreater(len(payload["actions"]), 1)
        self.assertEqual(payload["details"]["phase"], "intake")
        self.assertEqual(payload["details"]["cycle_id"], "CYCLE-current")
        self.assertEqual(payload["details"]["schema_version"], 31)
        self.assertEqual(payload["details"]["tasks"], 0)

    def test_quickstart_default_selects_first_canonical_blocker_and_one_action(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.init_runtime(root)

            result = run_harness(root, "quickstart", "status")

        lines = self.assert_concise_card(result.stdout)
        self.assertIn("[requirement-missing]", lines[1])
        self.assertIn("requirement add", lines[2])
        self.assertNotIn("acceptance add", result.stdout)
        self.assertNotIn("baseline confirm", result.stdout)
        self.assertNotIn("phase:", result.stdout)
        self.assertNotIn("cycle:", result.stdout)
        self.assertNotIn("CYCLE-current", lines[0])
        self.assertIn("CYCLE-current", lines[1])
        self.assertNotIn("CYCLE-current", lines[2])

    def test_quickstart_verbose_retains_all_blockers_and_scaffold_actions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.init_runtime(root)

            result = run_harness(root, "quickstart", "status", "--verbose")

        self.assertIn("# Kafa Quickstart Status", result.stdout)
        self.assertIn("phase: intake", result.stdout)
        self.assertIn("cycle: CYCLE-current (active)", result.stdout)
        self.assertIn("[requirement-missing]", result.stdout)
        self.assertIn("[acceptance-missing]", result.stdout)
        self.assertIn("next_commands:", result.stdout)
        self.assertIn("requirement add", result.stdout)
        self.assertIn("acceptance add", result.stdout)

    def test_quickstart_json_retains_every_blocker_action_and_detail(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.init_runtime(root)

            result = run_harness(root, "quickstart", "status", "--json")

        payload = self.assert_json_envelope(result)
        self.assertEqual(payload["blockers"][0]["code"], "requirement-missing")
        self.assertGreater(len(payload["blockers"]), 1)
        self.assertGreater(len(payload["actions"]), 1)
        details = payload["details"]
        self.assertEqual(details["phase"], "intake")
        self.assertEqual(details["cycle_id"], "CYCLE-current")
        self.assertGreater(len(details["delivery_blockers"]), 1)
        self.assertGreater(len(details["next_commands"]), 1)

    def test_cycle_identifier_is_preserved_only_when_it_is_the_blocker_diagnostic(self) -> None:
        envelope = build_operator_envelope(
            state="blocked",
            blockers=(
                {
                    "code": "cycle-closed",
                    "message": "cycle CYCLE-explicit-review is closed",
                },
            ),
            actions=(),
            details={"phase": "retrospective", "cycle_id": "CYCLE-explicit-review"},
        )

        rendered = render_concise(envelope)

        self.assertIn("[cycle-closed] cycle CYCLE-explicit-review is closed", rendered)
        self.assertNotIn("phase:", rendered)
        self.assertNotIn("cycle_id:", rendered)

    def test_healthy_doctor_default_has_no_blocker_or_action(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.init_runtime(root)

            result = run_harness(root, "doctor")

        self.assertEqual(
            result.stdout.splitlines(),
            ["state: healthy", "blocker: none", "next: none"],
        )

    def test_doctor_verbose_and_json_preserve_all_failure_details(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.init_runtime(root)
            (root / ".gitignore").write_text("user-rule.log\n", encoding="utf-8")

            concise = run_harness(root, "doctor", check=False)
            verbose = run_harness(root, "doctor", "--verbose", check=False)
            json_result = run_harness(root, "doctor", "--json", check=False)

        self.assertNotEqual(concise.returncode, 0)
        self.assert_concise_card(concise.stdout)
        self.assertEqual(concise.stdout.count("missing .gitignore runtime pattern"), 1)
        self.assertNotEqual(verbose.returncode, 0)
        self.assertGreaterEqual(
            verbose.stdout.count("ERROR: missing .gitignore runtime pattern"),
            2,
        )
        self.assertIn("NEXT:", verbose.stdout)
        self.assertIn("repair --dry-run", verbose.stdout)
        self.assertNotEqual(json_result.returncode, 0)
        payload = self.assert_json_envelope(json_result)
        self.assertGreaterEqual(len(payload["blockers"]), 2)
        self.assertGreaterEqual(len(payload["details"]["issues"]), 2)

    def test_doctor_prioritizes_integrity_over_configuration_noise(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.init_runtime(root)
            (root / ".gitignore").write_text("user-rule.log\n", encoding="utf-8")
            conn = sqlite3.connect(root / ".ai-team/state/harness.db")
            try:
                conn.execute("pragma foreign_keys = off")
                conn.execute(
                    "insert into task_acceptance "
                    "(cycle_id, task_id, acceptance_id) values (?, ?, ?)",
                    ("CYCLE-current", "missing-task", "missing-acceptance"),
                )
                conn.commit()
            finally:
                conn.close()

            concise = run_harness(root, "doctor", check=False)
            verbose = run_harness(root, "doctor", "--verbose", check=False)
            json_result = run_harness(root, "doctor", "--json", check=False)

        self.assertNotEqual(concise.returncode, 0)
        self.assertIn("[foreign-key-integrity]", concise.stdout)
        self.assertLess(
            verbose.stdout.index("sqlite foreign key check failed"),
            verbose.stdout.index("missing .gitignore runtime pattern"),
        )
        payload = self.assert_json_envelope(json_result)
        codes = [blocker["code"] for blocker in payload["blockers"]]
        self.assertEqual(codes[0], "foreign-key-integrity")
        self.assertGreater(codes.index("gitignore-missing"), 0)

    def test_json_error_is_not_mixed_with_human_error_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            for args in (
                ("status", "--json"),
                ("doctor", "--json"),
                ("quickstart", "status", "--json"),
            ):
                with self.subTest(args=args):
                    result = run_harness(root, *args, check=False)
                    payload = self.assert_json_envelope(result)
                    self.assertTrue(payload["blockers"])
                    self.assertIn(
                        "not initialized",
                        payload["blockers"][0]["message"].lower(),
                    )

    def test_existing_unreadable_database_never_recommends_initialization(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / ".ai-team/state/harness.db"
            database.parent.mkdir(parents=True)
            database.write_bytes(b"not-a-sqlite-database")

            for args in (
                ("status", "--json"),
                ("doctor", "--json"),
                ("quickstart", "status", "--json"),
            ):
                with self.subTest(args=args):
                    result = run_harness(root, *args, check=False)
                    payload = self.assert_json_envelope(result)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(payload["state"], "error")
                    self.assertEqual(payload["actions"], [])
                    self.assertIn(
                        "existing harness database is unreadable",
                        payload["blockers"][0]["message"],
                    )

            direct = harness_db.quickstart_status(root)
            direct_lines = harness_db.quickstart_status_lines(root)

        self.assertFalse(direct["initialized"])
        self.assertEqual(direct["missing"], ["state"])
        self.assertEqual(direct["next_commands"], [])
        self.assertIn("unreadable", "\n".join(direct_lines))
        self.assertNotIn("next_commands:", "\n".join(direct_lines))

    def test_partial_sqlite_schema_returns_one_json_error_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / ".ai-team/state/harness.db"
            database.parent.mkdir(parents=True)
            with closing(sqlite3.connect(database)) as conn:
                conn.execute("create table project (id integer primary key)")
                conn.execute("insert into project (id) values (1)")
                conn.commit()

            for args in (
                ("status", "--json"),
                ("doctor", "--json"),
                ("quickstart", "status", "--json"),
            ):
                with self.subTest(args=args):
                    result = run_harness(root, *args, check=False)
                    self.assertNotEqual(result.returncode, 0)
                    payload = self.assert_json_envelope(result)
                    self.assertEqual(payload["state"], "error")
                    self.assertEqual(payload["actions"], [])
                    self.assertTrue(payload["blockers"])
                    encoded = json.dumps(payload, ensure_ascii=False).lower()
                    self.assertNotIn(" init", encoded)
                    self.assertNotIn("traceback", encoded)

    def test_candidate_change_during_report_fails_closed_without_mixed_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            harness_candidates = iter(("candidate-a", "candidate-a"))
            with (
                patch.object(
                    harness_db,
                    "current_candidate_sha",
                    side_effect=lambda _root: next(harness_candidates),
                ) as status_candidate,
                patch.object(
                    delivery_core,
                    "current_candidate_sha",
                    return_value="candidate-b",
                ) as evaluator_candidate,
            ):
                report = harness_db.quickstart_operator_report(root)

        self.assertEqual(status_candidate.call_count, 2)
        self.assertEqual(evaluator_candidate.call_count, 1)
        self.assertEqual(report.blockers[0].code, "candidate-snapshot-changed")
        self.assertEqual(report.actions, ())
        self.assertFalse(report.details["ready_for_delivery"])
        self.assertIn("current_candidate", report.details["missing"])

    def test_canonical_evaluator_uses_one_candidate_and_one_final_recheck(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            candidates = iter(("candidate-a", "candidate-b"))
            with harness_db.connection(root) as conn:
                with patch.object(
                    delivery_core,
                    "current_candidate_sha",
                    side_effect=lambda _root: next(candidates),
                ) as candidate:
                    report = delivery_core.evaluate_delivery_report(
                        conn,
                        root,
                        mode="enter-readiness",
                        is_expired=harness_db.is_expired,
                    )

        self.assertEqual(candidate.call_count, 2)
        self.assertEqual(report.candidate_sha, "candidate-a")
        self.assertEqual(report.blockers[0].code, "candidate-snapshot-changed")
        self.assertFalse(report.trust.delivery_allowed)

    def test_recovery_sentinel_is_first_and_never_recommends_init(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / ".ai-team/backups/recovery/migration-manifest.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(
                '{"status":"rollback-incomplete"}\n',
                encoding="utf-8",
            )
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

            commands = (
                ("status",),
                ("doctor",),
                ("quickstart", "status"),
            )
            for args in commands:
                with self.subTest(args=args, mode="concise"):
                    result = run_harness(root, *args, check=False)
                    self.assertNotEqual(result.returncode, 0)
                    lines = self.assert_concise_card(result.stdout)
                    self.assertIn("rollback-incomplete", lines[1].lower())
                    self.assertEqual(lines[2], "next: none")
                    self.assertNotIn(" init", result.stdout.lower())

                with self.subTest(args=args, mode="verbose"):
                    result = run_harness(root, *args, "--verbose", check=False)
                    self.assertNotEqual(result.returncode, 0)
                    output = result.stdout.lower()
                    self.assertIn("rollback-incomplete", output)
                    self.assertIn(str(manifest).lower(), output)
                    self.assertIn("do not remove", output)
                    self.assertNotIn(" init", output)

                with self.subTest(args=args, mode="json"):
                    result = run_harness(root, *args, "--json", check=False)
                    self.assertNotEqual(result.returncode, 0)
                    payload = self.assert_json_envelope(result)
                    self.assertTrue(payload["blockers"])
                    self.assertIn(
                        "rollback-incomplete",
                        payload["blockers"][0]["message"].lower(),
                    )
                    encoded = json.dumps(payload, ensure_ascii=False).lower()
                    self.assertIn("do not remove", encoded)
                    self.assertNotIn(" init", encoded)


if __name__ == "__main__":
    unittest.main()

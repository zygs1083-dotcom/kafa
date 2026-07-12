from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "codex-project-harness"
SCRIPTS = PLUGIN_ROOT / "scripts"
HARNESS = SCRIPTS / "harness.py"

for path in (PLUGIN_ROOT, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import harness  # noqa: E402
import harness_db  # noqa: E402
from core.execution import ContainerExecutor, ExecutionPolicyError, LocalExecutor  # noqa: E402


def run_harness(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HARNESS), "--root", str(root), *args],
        text=True,
        capture_output=True,
        check=False,
    )


def cli_surface(parser: argparse.ArgumentParser) -> set[str]:
    surface: set[str] = set()

    def walk(current: argparse.ArgumentParser, prefix: tuple[str, ...] = ()) -> None:
        for action in current._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, subparser in action.choices.items():
                    path = prefix + (name,)
                    surface.add(".".join(path))
                    walk(subparser, path)

    walk(parser)
    return surface


def create_candidate(root: Path) -> None:
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "Kafa Test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "kafa@example.invalid"], cwd=root, check=True)
    (root / "test_candidate.py").write_text(
        "import unittest\n\n"
        "class CandidateTest(unittest.TestCase):\n"
        "    def test_candidate(self):\n"
        "        self.assertTrue(True)\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "test_candidate.py"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "candidate"], cwd=root, check=True, capture_output=True, text=True)


def initialize_target(root: Path, *, target_id: str = "UNIT", command: str = "python3 -B -m unittest test_candidate.py") -> None:
    for args in (
        ("init",),
        ("acceptance", "add", "--id", "AC1", "--criterion", "candidate passes"),
        (
            "test-target",
            "add",
            "--id",
            target_id,
            "--kind",
            "unit",
            "--command-template",
            command,
        ),
    ):
        result = run_harness(root, *args)
        if result.returncode != 0:
            raise AssertionError(result.stdout + result.stderr)


def execution_fact_counts(root: Path) -> tuple[int, int, int]:
    with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
        return tuple(
            int(conn.execute(f"select count(*) from {table}").fetchone()[0])
            for table in ("executions", "validations", "validation_executions")
        )


class ImmutableExecutionTests(unittest.TestCase):
    def test_verify_run_atomically_records_execution_validation_links_and_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            create_candidate(root)
            initialize_target(root)

            verified = run_harness(root, "verify", "run", "--target", "UNIT", "--acceptance", "AC1")
            self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                counts = tuple(
                    int(conn.execute(f"select count(*) from {table}").fetchone()[0])
                    for table in ("executions", "validations", "validation_executions")
                )
                event_count = int(
                    conn.execute("select count(*) from events where event_type = 'verification_recorded'").fetchone()[0]
                )

        self.assertEqual(counts, (1, 1, 1))
        self.assertEqual(event_count, 1)

    def test_execution_insert_is_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            initialized = run_harness(root, "init")
            self.assertEqual(initialized.returncode, 0, initialized.stdout + initialized.stderr)
            db = root / ".ai-team/state/harness.db"
            with closing(sqlite3.connect(db)) as conn:
                tables = {row[0] for row in conn.execute("select name from sqlite_master where type='table'")}
                self.assertIn("executions", tables)
                conn.execute(
                    """
                    insert into executions
                    (id, cycle_id, candidate_sha, target_id, command, exit_code, stdout_sha256,
                     artifact_path, executed_count, result_format, semantic_status, runner,
                     sandbox_status, no_network, policy_status, created_at)
                    values ('EX1', 'CYCLE-current', 'candidate', '', 'true', 0, ?, '', 1,
                            'regex', 'pass', 'local', 'not-requested', 0, 'pass', 'now')
                    """,
                    ("0" * 64,),
                )
                conn.commit()
                with self.assertRaises(sqlite3.DatabaseError):
                    conn.execute("update executions set command = 'false' where id = 'EX1'")
                with self.assertRaises(sqlite3.DatabaseError):
                    conn.execute("delete from executions where id = 'EX1'")

    def test_manual_claim_cannot_create_gate_eligible_execution(self) -> None:
        surface = cli_surface(harness.build_parser())
        self.assertNotIn("evidence.record", surface)
        self.assertNotIn("test.record", surface)

    def test_structured_result_missing_malformed_failed_or_zero_fails_closed(self) -> None:
        source = (PLUGIN_ROOT / "core/execution.py")
        self.assertTrue(source.exists(), "schema 30 execution engine is missing")
        text = source.read_text(encoding="utf-8")
        for contract_marker in ("malformed", "executed_count", "semantic_status", "result_path"):
            self.assertIn(contract_marker, text)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            create_candidate(root)
            scripts = {
                "missing": "print('command completed')\n",
                "malformed": "from pathlib import Path\nPath('malformed.json').write_text('{bad')\n",
                "failed": "from pathlib import Path\nPath('failed.json').write_text('{\"summary\":{\"total\":1,\"passed\":0,\"failed\":1,\"errors\":0}}')\n",
                "zero": "from pathlib import Path\nPath('zero.json').write_text('{\"summary\":{\"total\":0,\"passed\":0,\"failed\":0,\"errors\":0}}')\n",
            }
            for name, body in scripts.items():
                (root / f"emit_{name}.py").write_text(body, encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "structured cases"], cwd=root, check=True, capture_output=True)
            self.assertEqual(run_harness(root, "init").returncode, 0)
            self.assertEqual(
                run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "structured").returncode,
                0,
            )
            for name in scripts:
                target = name.upper()
                result_path = "missing.json" if name == "missing" else f"{name}.json"
                added = run_harness(
                    root,
                    "test-target",
                    "add",
                    "--id",
                    target,
                    "--kind",
                    "build",
                    "--command-template",
                    f"python3 emit_{name}.py",
                    "--result-format",
                    "pytest-json",
                    "--result-path",
                    result_path,
                )
                self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
                verified = run_harness(root, "verify", "run", "--target", target, "--acceptance", "AC1")
                self.assertNotEqual(verified.returncode, 0, name)
            self.assertEqual(execution_fact_counts(root), (0, 0, 0))

    def test_stale_candidate_discards_completed_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            create_candidate(root)
            initialize_target(root)
            original_run = LocalExecutor.run

            def run_then_change(executor, *args, **kwargs):
                result = original_run(executor, *args, **kwargs)
                candidate = root / "test_candidate.py"
                candidate.write_text(candidate.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
                return result

            with patch.object(LocalExecutor, "run", new=run_then_change):
                with self.assertRaisesRegex(harness_db.HarnessError, "stale candidate"):
                    harness_db.verify_run(root, "UNIT", acceptance="AC1")
            self.assertEqual(execution_fact_counts(root), (0, 0, 0))

    def test_artifact_digest_mismatch_fails_before_fact_insert(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            create_candidate(root)
            initialize_target(root)
            original_run = LocalExecutor.run

            def run_then_tamper(executor, *args, **kwargs):
                result = original_run(executor, *args, **kwargs)
                (root / result.artifact_path).write_text("tampered\n", encoding="utf-8")
                return result

            with patch.object(LocalExecutor, "run", new=run_then_tamper):
                with self.assertRaisesRegex(harness_db.HarnessError, "digest mismatch"):
                    harness_db.verify_run(root, "UNIT", acceptance="AC1")
            self.assertEqual(execution_fact_counts(root), (0, 0, 0))

    def test_event_failure_rolls_back_execution_validation_and_link(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            create_candidate(root)
            initialize_target(root)
            with patch.object(
                harness_db,
                "emit_audit_event",
                side_effect=harness_db.HarnessError("injected event failure"),
            ):
                with self.assertRaisesRegex(harness_db.HarnessError, "injected event failure"):
                    harness_db.verify_run(root, "UNIT", acceptance="AC1")
            self.assertEqual(execution_fact_counts(root), (0, 0, 0))

    def test_unavailable_container_fails_closed_without_fact_insert(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            create_candidate(root)
            initialize_target(root)
            with patch("core.execution.shutil.which", return_value=None):
                with self.assertRaisesRegex(ExecutionPolicyError, "sandbox-unavailable"):
                    ContainerExecutor(root).run(
                        "python3 -B -m unittest test_candidate.py",
                        target_id="UNIT",
                        target_command_template="python3 -B -m unittest test_candidate.py",
                    )
                with self.assertRaisesRegex(harness_db.HarnessError, "sandbox-unavailable"):
                    harness_db.verify_run(
                        root,
                        "UNIT",
                        acceptance="AC1",
                        runner="container",
                    )
            self.assertEqual(execution_fact_counts(root), (0, 0, 0))

    def test_container_executor_marks_only_real_no_network_invocation_available(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            create_candidate(root)
            observed: list[str] = []

            def fake_container_run(argv, **kwargs):
                observed.extend(argv)
                artifact_mount = next(
                    value for value in argv if value.endswith(":/artifacts:rw")
                )
                artifact_dir = Path(artifact_mount.removesuffix(":/artifacts:rw"))
                (artifact_dir / "stdout.txt").write_text(
                    "Ran 1 test in 0.001s\nOK\n", encoding="utf-8"
                )
                return subprocess.CompletedProcess(argv, 0, "", "")

            with patch("core.execution.shutil.which", return_value="/usr/bin/docker"), patch(
                "core.execution.subprocess.run", side_effect=fake_container_run
            ):
                result = ContainerExecutor(root).run(
                    "python3 -B -m unittest test_candidate.py",
                    target_id="UNIT",
                    target_command_template="python3 -B -m unittest test_candidate.py",
                )

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.executed_count, 1)
            self.assertEqual(result.semantic_status, "pass")
            self.assertTrue(result.no_network)
            self.assertEqual(result.sandbox_status, "available")
            self.assertIn("--network", observed)
            self.assertEqual(observed[observed.index("--network") + 1], "none")

    def test_manual_validation_pass_is_not_gate_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            create_candidate(root)
            initialize_target(root)
            recorded = run_harness(
                root,
                "validation",
                "record",
                "--surface",
                "manual claim",
                "--acceptance",
                "AC1",
                "--findings",
                "claimed pass",
                "--result",
                "pass",
            )
            self.assertEqual(recorded.returncode, 0, recorded.stdout + recorded.stderr)
            self.assertEqual(execution_fact_counts(root), (0, 1, 0))
            with harness_db.connection(root) as conn:
                issues = harness_db.validate_delivery(conn, root)
            self.assertTrue(
                any("no linked immutable execution" in issue for issue in issues),
                issues,
            )

    def test_local_cli_delivery_e2e_consumes_immutable_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            create_candidate(root)
            (root / ".gitignore").write_text(
                ".ai-team/state/\n.ai-team/backups/\n.ai-team/runtime/\n"
                "__pycache__/\n*.pyc\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", ".gitignore"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "ignore local runtime"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            commands = (
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
                ("delivery", "record", "--scope", "local", "--acceptance", "SMOKE-AC1"),
                ("validate", "--delivery"),
            )
            for args in commands:
                result = run_harness(root, *args)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                task_status = conn.execute("select status from tasks where id='SMOKE-T1'").fetchone()[0]
                execution_count = int(conn.execute("select count(*) from executions").fetchone()[0])
                linked_count = int(conn.execute("select count(*) from validation_executions").fetchone()[0])
                gate = conn.execute("select result, review_status from quality_gates").fetchone()
                delivery = conn.execute("select decision_status from deliveries").fetchone()[0]
                cycle_status = conn.execute(
                    "select status from delivery_cycles where id='CYCLE-current'"
                ).fetchone()[0]
            self.assertEqual(task_status, "accepted")
            self.assertEqual((execution_count, linked_count), (1, 1))
            self.assertEqual(tuple(gate), ("pass", "reviewed-local"))
            self.assertEqual(delivery, "delivered")
            self.assertEqual(cycle_status, "delivered")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "codex-project-harness"
SCRIPTS = PLUGIN_ROOT / "scripts"
HARNESS = SCRIPTS / "harness.py"

for path in (PLUGIN_ROOT, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import harness  # noqa: E402


RETIRED_HOST_COMMANDS = {
    "dispatch.provider",
    "dispatch.provider.start",
    "dispatch.provider.status",
    "dispatch.provider.collect",
    "dispatch.provider.cancel",
    "dispatch.provider.reconcile",
    "dispatch.export-csv",
    "dispatch.import-csv",
    "dispatch.native-export",
    "dispatch.native-import",
}

RETIRED_MODEL_LIFECYCLE_MARKERS = {
    "harness_codex_model",
    "harness_codex_spark",
    "model_selector",
    "selected_model",
    "model_policy",
    "spark-deterministic",
}


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


def run_harness(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HARNESS), "--root", str(root), *args],
        text=True,
        capture_output=True,
        check=False,
    )


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class NativeHostOwnershipTests(unittest.TestCase):
    def test_legacy_host_csv_and_native_receipt_commands_are_absent(self) -> None:
        actual = cli_surface(harness.build_parser())
        self.assertEqual(actual & RETIRED_HOST_COMMANDS, set())

    def test_legacy_host_implementation_files_are_removed(self) -> None:
        retired = {
            PLUGIN_ROOT / "core/agent_provider.py",
            PLUGIN_ROOT / "core/agent_runner.py",
        }
        self.assertEqual({path.relative_to(PLUGIN_ROOT).as_posix() for path in retired if path.exists()}, set())

    def test_active_runtime_contains_no_kafa_owned_model_selector(self) -> None:
        roots = [
            PLUGIN_ROOT / "core",
            PLUGIN_ROOT / "scripts",
            PLUGIN_ROOT / "hooks",
            PLUGIN_ROOT / "schemas",
        ]
        active_files = sorted(
            path
            for root in roots
            for path in root.rglob("*")
            if path.is_file() and path.suffix in {".py", ".json"}
        )
        hits: dict[str, list[str]] = {}
        for path in active_files:
            text = path.read_text(encoding="utf-8").lower()
            markers = sorted(marker for marker in RETIRED_MODEL_LIFECYCLE_MARKERS if marker in text)
            if markers:
                hits[path.relative_to(PLUGIN_ROOT).as_posix()] = markers

        self.assertEqual(hits, {})

    def test_retired_provider_paths_cannot_spawn_or_mutate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            initialized = run_harness(root, "init")
            self.assertEqual(initialized.returncode, 0, initialized.stdout + initialized.stderr)
            db = root / ".ai-team/state/harness.db"
            before = digest(db)
            results = [
                run_harness(root, "dispatch", "provider", "status", "--run-id", "retired-run"),
                run_harness(root, "dispatch", "export-csv", "retired-run"),
                run_harness(root, "dispatch", "native-export", "retired-run"),
            ]
            after = digest(db)

        for result in results:
            output = (result.stdout + result.stderr).lower()
            self.assertNotEqual(result.returncode, 0, output)
            self.assertIn("removed", output)
            self.assertIn("v2", output)
        self.assertEqual(after, before)

    def test_controller_verifies_current_candidate_without_provider_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
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

            self.assertEqual(run_harness(root, "init").returncode, 0)
            self.assertEqual(
                run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "candidate test passes").returncode,
                0,
            )
            self.assertEqual(
                run_harness(
                    root,
                    "test-target",
                    "add",
                    "--id",
                    "UNIT",
                    "--kind",
                    "unit",
                    "--command-template",
                    "python3 -B -m unittest test_candidate.py",
                ).returncode,
                0,
            )
            self.assertEqual(
                run_harness(
                    root,
                    "test-target",
                    "qualify",
                    "--id",
                    "UNIT-Q1",
                    "--target",
                    "UNIT",
                    "--acceptance",
                    "AC1",
                    "--rationale",
                    "UNIT proves the candidate acceptance criterion",
                    "--by",
                    "controller",
                ).returncode,
                0,
            )
            verified = run_harness(root, "verify", "run", "--target", "UNIT", "--acceptance", "AC1")
            self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                execution_count = int(conn.execute("select count(*) from executions").fetchone()[0])
                provider_tables = {
                    row[0]
                    for row in conn.execute(
                        "select name from sqlite_master where type='table' and name like 'agent_provider_%'"
                    )
                }

        self.assertEqual(execution_count, 1)
        self.assertEqual(provider_tables, set())


if __name__ == "__main__":
    unittest.main()

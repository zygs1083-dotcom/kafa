from __future__ import annotations

import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "plugins/codex-project-harness/scripts/harness.py"


def run_harness(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["python3", str(HARNESS), "--root", str(root), *args], text=True, capture_output=True, check=False)
    if check and result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return result


class TargetSandboxPolicyTest(unittest.TestCase):
    def test_test_target_add_records_policy_and_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")

            run_harness(
                root,
                "test-target",
                "add",
                "--id",
                "NODE",
                "--kind",
                "unit",
                "--command-template",
                "npm test",
                "--stack-profile",
                "node",
                "--container-image",
                "node:22-slim",
                "--requires-sandbox",
                "--requires-no-network",
                "--result-format",
                "jest-json",
                "--result-path",
                "jest.json",
            )

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                row = conn.execute(
                    """
                    select stack_profile, container_image, requires_sandbox, requires_no_network,
                           result_format, result_path
                    from test_targets where id = 'NODE'
                    """
                ).fetchone()
            self.assertEqual(tuple(row), ("node", "node:22-slim", 1, 1, "jest-json", "jest.json"))

    def test_requires_sandbox_target_rejects_local_verify(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "sandbox")
            run_harness(
                root,
                "test-target",
                "add",
                "--id",
                "UNIT",
                "--kind",
                "unit",
                "--command-template",
                "python3 -B -m unittest test_sample.py",
                "--requires-sandbox",
                "--requires-no-network",
            )

            result = run_harness(
                root,
                "verify",
                "run",
                "--target",
                "UNIT",
                "--acceptance",
                "AC1",
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("requires sandbox and no-network container verification", result.stderr + result.stdout)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                counts = tuple(
                    conn.execute(f"select count(*) from {table}").fetchone()[0]
                    for table in ("executions", "validations", "validation_executions")
                )
            self.assertEqual(counts, (0, 0, 0))


if __name__ == "__main__":
    unittest.main()

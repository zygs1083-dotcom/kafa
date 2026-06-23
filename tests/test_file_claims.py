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


class FileClaimsTest(unittest.TestCase):
    def test_two_agents_can_claim_different_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")

            run_harness(root, "dispatch", "file-claim", "add", "--task", "T1", "--agent", "developer", "--path", "a.py")
            run_harness(root, "dispatch", "file-claim", "add", "--task", "T2", "--agent", "qa-reviewer", "--path", "b.py")

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                count = conn.execute("select count(*) from task_file_claims where status = 'active'").fetchone()[0]
            self.assertEqual(count, 2)

    def test_same_file_claim_conflicts_until_released(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            run_harness(root, "dispatch", "file-claim", "add", "--task", "T1", "--agent", "developer", "--path", "a.py")

            conflict = run_harness(root, "dispatch", "file-claim", "add", "--task", "T2", "--agent", "qa-reviewer", "--path", "a.py", check=False)
            self.assertNotEqual(conflict.returncode, 0)
            self.assertIn("file-claim-conflict", conflict.stdout)

            run_harness(root, "dispatch", "file-claim", "release", "--task", "T1", "--agent", "developer", "--path", "a.py")
            run_harness(root, "dispatch", "file-claim", "add", "--task", "T2", "--agent", "qa-reviewer", "--path", "a.py")

    def test_invalid_claim_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_harness(root, "init")
            for bad in ["/tmp/a.py", "../a.py", ""]:
                result = run_harness(root, "dispatch", "file-claim", "add", "--task", "T1", "--agent", "developer", "--path", bad, check=False)
                self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()

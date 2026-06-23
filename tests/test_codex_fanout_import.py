import csv
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "plugins/codex-project-harness/scripts/harness.py"
SCRIPTS = REPO_ROOT / "plugins/codex-project-harness/scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from harness_lib import source_tree_hash_for_mode  # noqa: E402


def run_harness(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["python3", str(HARNESS), "--root", str(root), *args], text=True, capture_output=True, check=False)
    if check and result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return result


def git_repo(root: Path) -> None:
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
    (root / "README.md").write_text("root\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)


def bootstrap_export(root: Path) -> tuple[str, dict[str, str]]:
    run_harness(root, "init")
    run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
    run_harness(root, "test-target", "add", "--id", "UNIT", "--kind", "unit", "--command-template", "python3 -m unittest")
    run_harness(root, "task", "add", "--id", "T1", "--task", "Example", "--acceptance", "AC1")
    run_id = run_harness(root, "dispatch", "plan", "--scope", "Example").stdout.strip().split()[-1]
    run_harness(root, "dispatch", "export-csv", run_id)
    input_csv = root / ".ai-team/runtime/codex-fanout" / run_id / "input.csv"
    with input_csv.open(encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    subprocess.run(["git", "branch", row["branch_name"]], cwd=root, check=True)
    return run_id, row


def write_result_csv(root: Path, run_id: str, row: dict[str, str], result: dict[str, object], *, status: str = "success", error: str = "") -> Path:
    output = root / ".ai-team/runtime/codex-fanout" / run_id / "output.csv"
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["job_id", "item_id", "status", "last_error", "result_json"])
        writer.writeheader()
        writer.writerow({"job_id": "job-1", "item_id": row["item_id"], "status": status, "last_error": error, "result_json": json.dumps(result)})
    return output


def trusted_result(root: Path, row: dict[str, str]) -> dict[str, object]:
    artifact = root / ".ai-team/runtime/codex-fanout" / "stdout.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("Ran 1 tests\n", encoding="utf-8")
    stdout_hash = hashlib.sha256(artifact.read_bytes()).hexdigest()
    return {
        "command": "python3 -m unittest",
        "exit_code": 0,
        "stdout_sha256": stdout_hash,
        "artifact_path": artifact.relative_to(root).as_posix(),
        "executed_count": 1,
        "executed_count_source": "parsed",
        "source_tree_hash": source_tree_hash_for_mode(root, "auto"),
        "branch_name": row["branch_name"],
        "status": "success",
        "target_id": "UNIT",
    }


class CodexFanoutImportTest(unittest.TestCase):
    def test_import_success_records_evidence_and_worktree_branch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            git_repo(root)
            run_id, row = bootstrap_export(root)
            output = write_result_csv(root, run_id, row, trusted_result(root, row))

            result = run_harness(root, "dispatch", "import-csv", run_id, "--result", str(output))

            self.assertIn("imported 1 result", result.stdout)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                evidence = conn.execute("select executed_count_source from evidence where id like 'CODEX-%'").fetchone()
                assignment = conn.execute("select status, evidence from dispatch_assignments where run_id = ? and task_id = 'T1'", (run_id,)).fetchone()
                worktree = conn.execute("select branch_name from dispatch_worktrees where run_id = ?", (run_id,)).fetchone()
            self.assertEqual(evidence[0], "parsed")
            self.assertEqual(assignment[0], "completed")
            self.assertTrue(assignment[1].startswith("CODEX-"))
            self.assertEqual(worktree[0], row["branch_name"])

    def test_import_rejects_untrusted_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            git_repo(root)
            run_id, row = bootstrap_export(root)
            result_json = trusted_result(root, row)
            result_json["executed_count_source"] = "manual"
            output = write_result_csv(root, run_id, row, result_json)

            result = run_harness(root, "dispatch", "import-csv", run_id, "--result", str(output), check=False)

            self.assertNotEqual(result.returncode, 0)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                finding = conn.execute("select summary from findings where surface = 'dispatch-integration'").fetchone()[0]
                status = conn.execute("select status from dispatch_runs where id = ?", (run_id,)).fetchone()[0]
            self.assertIn("executed_count_source", finding)
            self.assertEqual(status, "verification_failed")

    def test_import_marks_missing_worker_report_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            git_repo(root)
            run_id, _row = bootstrap_export(root)
            output = root / ".ai-team/runtime/codex-fanout" / run_id / "output.csv"
            with output.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["job_id", "item_id", "status", "last_error", "result_json"])
                writer.writeheader()

            result = run_harness(root, "dispatch", "import-csv", run_id, "--result", str(output), check=False)

            self.assertNotEqual(result.returncode, 0)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                finding = conn.execute("select summary from findings where surface = 'dispatch-integration'").fetchone()[0]
            self.assertIn("did not report", finding)

    def test_import_request_id_replay_does_not_duplicate_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            git_repo(root)
            run_id, row = bootstrap_export(root)
            output = write_result_csv(root, run_id, row, trusted_result(root, row))
            args = ["dispatch", "import-csv", run_id, "--result", str(output), "--request-id", "REQ-import"]

            first = run_harness(root, *args)
            second = run_harness(root, *args)

            self.assertEqual(first.stdout, second.stdout)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                evidence_count = conn.execute("select count(*) from evidence where id like 'CODEX-%'").fetchone()[0]
                log_count = conn.execute("select count(*) from command_log where request_id = 'REQ-import'").fetchone()[0]
            self.assertEqual(evidence_count, 1)
            self.assertEqual(log_count, 1)


if __name__ == "__main__":
    unittest.main()

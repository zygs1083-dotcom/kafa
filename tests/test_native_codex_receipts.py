from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "plugins/codex-project-harness/scripts/harness.py"


def run_harness(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["python3", str(HARNESS), "--root", str(root), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return result


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=True)
    return result.stdout.strip()


def bootstrap(root: Path, *, requires_sandbox: bool = False) -> str:
    git(root, "init")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.invalid")
    (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(root, "add", "app.py")
    git(root, "commit", "-m", "initial")
    run_harness(root, "init")
    run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "Example")
    target_args = [
        "test-target",
        "add",
        "--id",
        "UNIT",
        "--kind",
        "unit",
        "--command-template",
        "python3 -m unittest",
    ]
    if requires_sandbox:
        target_args.append("--requires-sandbox")
    run_harness(root, *target_args)
    run_harness(root, "task", "add", "--id", "T1", "--task", "Example", "--owner", "developer", "--acceptance", "AC1")
    run_harness(root, "test-target", "link", "--task", "T1", "--target", "UNIT")
    return run_harness(root, "dispatch", "plan", "--scope", "Native Codex").stdout.strip().split()[-1]


def export_package(root: Path, run_id: str) -> tuple[Path, dict[str, object]]:
    result = run_harness(root, "dispatch", "native-export", run_id)
    manifest_path = Path(result.stdout.strip().split()[-1])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    package_path = root / str(manifest["packages"][0]["path"])
    return package_path, json.loads(package_path.read_text(encoding="utf-8"))


def prepare_branch(root: Path, branch: str) -> tuple[str, str]:
    base = git(root, "rev-parse", "HEAD")
    git(root, "switch", "-c", branch)
    (root / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    git(root, "add", "app.py")
    git(root, "commit", "-m", "native change")
    head = git(root, "rev-parse", "HEAD")
    git(root, "switch", "-")
    return base, head


def receipt_for(package: dict[str, object], base_sha: str, head_sha: str, *, policy: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "receipt_version": "1",
        "package_sha256": package["package_sha256"],
        "run_id": package["run_id"],
        "assignment_id": package["assignment_id"],
        "host": {
            "surface": "codex-app",
            "task_id": "task_01JTESTNATIVE",
            "thread_id": "thread_01JTESTNATIVE",
            "parent_thread_id": "thread_parent_01JTEST",
            "worktree_id": "worktree_01JTESTNATIVE",
            "worktree_path": "/host/managed/worktree",
            "handoff_id": "",
        },
        "policy": policy
        or {
            "approval_mode": "host-policy",
            "sandbox": "workspace-write",
            "network": "restricted",
            "selected_model": "host-selected-model",
            "reasoning": "host-selected",
        },
        "status": "completed",
        "branch": package["target_branch"],
        "base_sha": base_sha,
        "head_sha": head_sha,
        "report": {
            "command": "python3 -m unittest",
            "exit_code": 0,
            "stdout_sha256": "0" * 64,
            "artifact_path": "native/stdout.txt",
            "executed_count": 1,
            "executed_count_source": "parsed",
            "source_tree_hash": "raw-host-value",
            "branch_name": package["target_branch"],
            "status": "success",
            "target_id": "UNIT",
        },
        "started_at": "2026-07-10T00:00:00+00:00",
        "completed_at": "2026-07-10T00:01:00+00:00",
        "provenance": {
            "kind": "audit-only",
            "issuer": "codex-app-controller",
            "payload_sha256": "1" * 64,
            "signature": "",
        },
    }


def db_count(root: Path, table: str) -> int:
    with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
        return int(conn.execute(f"select count(*) from {table}").fetchone()[0])


class NativeCodexReceiptTest(unittest.TestCase):
    def test_export_writes_immutable_package_without_runtime_state_or_model_slug(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_id = bootstrap(root)

            package_path, package = export_package(root, run_id)
            stored_hash = str(package.pop("package_sha256"))
            canonical = json.dumps(package, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            session = None
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.row_factory = sqlite3.Row
                session = conn.execute("select * from agent_provider_sessions where run_id = ? and provider = 'native-codex'", (run_id,)).fetchone()
            package_exists = package_path.exists()

        self.assertTrue(package_exists)
        self.assertEqual(stored_hash, hashlib.sha256(canonical.encode("utf-8")).hexdigest())
        serialized = json.dumps(package, sort_keys=True)
        self.assertNotIn("harness.db", serialized)
        self.assertNotIn("sqlite_home", serialized)
        self.assertNotIn("gpt-5.3-codex-spark", serialized)
        self.assertEqual(package["state_transport"], "root-controller-only")
        self.assertEqual(session["status"], "package_exported")
        self.assertEqual(session["provider_session_id"], "")

    def test_import_records_real_host_ids_and_raw_report_without_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_id = bootstrap(root)
            _package_path, package = export_package(root, run_id)
            base, head = prepare_branch(root, str(package["target_branch"]))
            receipt = receipt_for(package, base, head)
            receipt_path = root / "receipt.json"
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

            result = run_harness(root, "dispatch", "native-import", run_id, "--receipt", str(receipt_path))
            doctor = run_harness(root, "kernel", "doctor", check=False)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.row_factory = sqlite3.Row
                session = conn.execute("select * from agent_provider_sessions where run_id = ? and provider = 'native-codex'", (run_id,)).fetchone()
                attempt = conn.execute("select * from task_attempts where run_id = ?", (run_id,)).fetchone()
            report_count = db_count(root, "agent_reports")
            evidence_count = db_count(root, "evidence")

        self.assertIn("imported 1 native receipt", result.stdout)
        self.assertEqual(session["provider_session_id"], "thread_01JTESTNATIVE")
        self.assertEqual(session["provider_job_id"], "task_01JTESTNATIVE")
        self.assertEqual(session["status"], "receipt_imported")
        self.assertEqual(attempt["status"], "reported")
        self.assertEqual(attempt["head_commit_sha"], head)
        self.assertEqual(report_count, 1)
        self.assertEqual(evidence_count, 0)
        self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)
        self.assertNotIn("schema contract failed", doctor.stdout + doctor.stderr)

    def test_import_rejects_placeholder_identity_and_wrong_package_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_id = bootstrap(root)
            _package_path, package = export_package(root, run_id)
            base, head = prepare_branch(root, str(package["target_branch"]))
            receipt = receipt_for(package, base, head)
            receipt["host"]["thread_id"] = "sdk-turn"
            receipt["package_sha256"] = "f" * 64
            receipt_path = root / "bad-receipt.json"
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

            result = run_harness(root, "dispatch", "native-import", run_id, "--receipt", str(receipt_path), check=False)
            report_count = db_count(root, "agent_reports")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("package hash", result.stdout + result.stderr)
        self.assertEqual(report_count, 0)

    def test_import_rejects_placeholder_identity_with_valid_package_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_id = bootstrap(root)
            _package_path, package = export_package(root, run_id)
            base, head = prepare_branch(root, str(package["target_branch"]))
            receipt = receipt_for(package, base, head)
            receipt["host"]["thread_id"] = "sdk-turn"
            receipt_path = root / "placeholder-receipt.json"
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

            result = run_harness(root, "dispatch", "native-import", run_id, "--receipt", str(receipt_path), check=False)
            report_count = db_count(root, "agent_reports")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("real host thread_id", result.stdout + result.stderr)
        self.assertEqual(report_count, 0)

    def test_import_rejects_task_constraint_drift_after_export(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_id = bootstrap(root)
            _package_path, package = export_package(root, run_id)
            base, head = prepare_branch(root, str(package["target_branch"]))
            run_harness(root, "test-target", "add", "--id", "LINT", "--kind", "unit", "--command-template", "python3 -m unittest")
            run_harness(root, "test-target", "link", "--task", "T1", "--target", "LINT")
            receipt = receipt_for(package, base, head)
            receipt_path = root / "stale-receipt.json"
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

            result = run_harness(root, "dispatch", "native-import", run_id, "--receipt", str(receipt_path), check=False)
            report_count = db_count(root, "agent_reports")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("test targets changed", result.stdout + result.stderr)
        self.assertEqual(report_count, 0)

    def test_import_is_exactly_once_and_conflicting_receipt_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_id = bootstrap(root)
            _package_path, package = export_package(root, run_id)
            base, head = prepare_branch(root, str(package["target_branch"]))
            receipt = receipt_for(package, base, head)
            receipt_path = root / "receipt.json"
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

            run_harness(root, "dispatch", "native-import", run_id, "--receipt", str(receipt_path))
            run_harness(root, "dispatch", "native-import", run_id, "--receipt", str(receipt_path))
            receipt["host"]["task_id"] = "task_DIFFERENT"
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            conflict = run_harness(root, "dispatch", "native-import", run_id, "--receipt", str(receipt_path), check=False)
            report_count = db_count(root, "agent_reports")
            attempt_count = db_count(root, "task_attempts")

        self.assertEqual(report_count, 1)
        self.assertEqual(attempt_count, 1)
        self.assertNotEqual(conflict.returncode, 0)
        self.assertIn("receipt conflict", conflict.stdout + conflict.stderr)

    def test_required_sandbox_rejects_unknown_host_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_id = bootstrap(root, requires_sandbox=True)
            _package_path, package = export_package(root, run_id)
            base, head = prepare_branch(root, str(package["target_branch"]))
            receipt = receipt_for(
                package,
                base,
                head,
                policy={
                    "approval_mode": "unknown",
                    "sandbox": "unknown",
                    "network": "unknown",
                    "selected_model": "host-selected-model",
                    "reasoning": "host-selected",
                },
            )
            receipt_path = root / "receipt.json"
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

            result = run_harness(root, "dispatch", "native-import", run_id, "--receipt", str(receipt_path), check=False)
            report_count = db_count(root, "agent_reports")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires reported sandbox", result.stdout + result.stderr)
        self.assertEqual(report_count, 0)


if __name__ == "__main__":
    unittest.main()

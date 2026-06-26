from __future__ import annotations

import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "plugins/codex-project-harness/scripts/harness.py"
PLUGIN_ROOT = REPO_ROOT / "plugins/codex-project-harness"
if str(PLUGIN_ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(PLUGIN_ROOT))


def run_harness(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["python3", str(HARNESS), "--root", str(root), *args], text=True, capture_output=True, check=False)
    if check and result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return result


class StructuredResultParserTest(unittest.TestCase):
    def test_parsers_accept_successful_structured_outputs(self) -> None:
        from core.executor import parse_structured_result

        samples = {
            "junit": b'<testsuite tests="2" failures="0" errors="0"><testcase name="a"/><testcase name="b"/></testsuite>',
            "pytest-json": b'{"summary":{"total":2,"passed":2,"failed":0,"errors":0}}',
            "jest-json": b'{"success":true,"numTotalTests":2,"numPassedTests":2,"numFailedTests":0}',
            "go-json": b'{"Action":"run","Test":"TestA"}\n{"Action":"pass","Test":"TestA"}\n{"Action":"pass","Package":"example"}\n',
            "cargo-nextest-json": b'{"type":"test","event":"passed","name":"test_a"}\n{"type":"run","event":"finished","test_count":1,"failed":0}\n',
            "playwright-json": b'{"stats":{"expected":2,"unexpected":0,"flaky":0,"skipped":0}}',
        }
        for result_format, payload in samples.items():
            with self.subTest(result_format=result_format):
                parsed = parse_structured_result(result_format, payload)
                self.assertEqual(parsed.semantic_status, "pass")
                self.assertGreater(parsed.executed_count, 0)
                self.assertEqual(parsed.executed_count_source, "structured")

    def test_structured_targets_do_not_accept_regex_like_stdout(self) -> None:
        from core.executor import parse_structured_result

        parsed = parse_structured_result("pytest-json", b"10 passed in 0.2s")

        self.assertEqual(parsed.semantic_status, "fail")
        self.assertEqual(parsed.executed_count, 0)
        self.assertIn("malformed", parsed.reason)

    def test_local_executor_records_structured_result_from_result_path(self) -> None:
        from core.executor import LocalExecutor

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "emit.py").write_text(
                "from pathlib import Path\nPath('pytest.json').write_text('{\"summary\":{\"total\":1,\"passed\":1,\"failed\":0,\"errors\":0}}')\n",
                encoding="utf-8",
            )

            result = LocalExecutor(root).run(
                "python3 emit.py",
                target_id="UNIT",
                target_command_template="python3 emit.py",
                result_format="pytest-json",
                result_path="pytest.json",
            )

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.semantic_status, "pass")
            self.assertEqual(result.executed_count, 1)
            self.assertEqual(result.executed_count_source, "structured")
            self.assertEqual(result.result_format, "pytest-json")
            self.assertTrue(result.result_path.endswith("structured-result"))


class StructuredResultGateTest(unittest.TestCase):
    def test_structured_cli_verify_records_semantic_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
            (root / "emit.py").write_text(
                "from pathlib import Path\nPath('pytest.json').write_text('{\"summary\":{\"total\":1,\"passed\":1,\"failed\":0,\"errors\":0}}')\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "emit.py"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            run_harness(root, "init")
            run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "structured")
            run_harness(
                root,
                "test-target",
                "add",
                "--id",
                "UNIT",
                "--kind",
                "build",
                "--command-template",
                "python3 emit.py",
                "--result-format",
                "pytest-json",
                "--result-path",
                "pytest.json",
            )
            run_harness(root, "task", "add", "--id", "T1", "--task", "structured", "--acceptance", "AC1")
            run_harness(root, "test-target", "link", "--task", "T1", "--target", "UNIT")
            run_id = run_harness(root, "dispatch", "plan", "--scope", "structured").stdout.strip().split()[-1]
            branch = f"agent/{run_id}/T1/developer"
            subprocess.run(["git", "branch", branch, "HEAD"], cwd=root, check=True, capture_output=True)
            head = subprocess.run(["git", "rev-parse", branch], cwd=root, text=True, capture_output=True, check=True).stdout.strip()
            tree = subprocess.run(["git", "rev-parse", f"{branch}^{{tree}}"], cwd=root, text=True, capture_output=True, check=True).stdout.strip()
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                conn.execute("update dispatch_assignments set agent_id = 'developer', status = 'reported' where run_id = ? and task_id = 'T1'", (run_id,))
                conn.execute(
                    """
                    insert into task_attempts
                    (id, run_id, task_id, agent_id, fence, base_commit_sha, head_commit_sha, tree_sha,
                     branch_name, target_id, status, provider_session_id, agent_session_id, report_id, evidence_id, started_at, finished_at)
                    values ('ATTEMPT1', ?, 'T1', 'developer', 0, ?, ?, ?, ?, 'UNIT', 'reported', '', '', '', '', 'now', '')
                    """,
                    (run_id, head, head, tree, branch),
                )
                conn.execute(
                    """
                    insert into dispatch_worktrees
                    (id, run_id, task_id, agent_id, branch_name, worktree_path, status, created_at, cleaned_at)
                    values ('WT1', ?, 'T1', 'developer', ?, '', 'active', 'now', '')
                    """,
                    (run_id, branch),
                )
                conn.commit()

            run_harness(root, "dispatch", "verify-attempt", "--run-id", run_id, "--task", "T1")

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                evidence = conn.execute("select result_format, result_path, semantic_status, executed_count_source from evidence").fetchone()
                validation = conn.execute("select result_format, result_path, semantic_status, executed_count_source from validations").fetchone()
            self.assertEqual(tuple(evidence), ("pytest-json", "pytest.json", "pass", "structured"))
            self.assertEqual(tuple(validation), ("pytest-json", "pytest.json", "pass", "structured"))


if __name__ == "__main__":
    unittest.main()

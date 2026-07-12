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
    def test_regex_parser_counts_common_runner_outputs(self) -> None:
        from core.execution import parse_executed_count

        self.assertEqual(parse_executed_count("3 passed, 1 skipped in 0.12s"), 3)
        self.assertEqual(parse_executed_count("Ran 4 tests in 0.001s\n\nOK"), 4)
        self.assertEqual(parse_executed_count("Tests:       5 passed, 5 total"), 5)
        self.assertEqual(parse_executed_count("0 passing (4ms)"), 0)

    def test_parsers_accept_successful_structured_outputs(self) -> None:
        from core.execution import parse_structured_result

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
        from core.execution import parse_structured_result

        parsed = parse_structured_result("pytest-json", b"10 passed in 0.2s")

        self.assertEqual(parsed.semantic_status, "fail")
        self.assertEqual(parsed.executed_count, 0)
        self.assertIn("malformed", parsed.reason)

    def test_local_executor_records_structured_result_from_result_path(self) -> None:
        from core.execution import LocalExecutor

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
            run_harness(root, "verify", "run", "--target", "UNIT", "--acceptance", "AC1")

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                recorded = conn.execute(
                    """
                    select e.result_format, e.semantic_status, e.executed_count, e.exit_code,
                           e.artifact_path, e.policy_status, v.acceptance_id, v.surface,
                           v.result, e.candidate_sha = v.candidate_sha
                    from executions e
                    join validation_executions ve on ve.execution_id = e.id
                    join validations v on v.id = ve.validation_id
                    """
                ).fetchone()
                with self.assertRaises(sqlite3.DatabaseError):
                    conn.execute("update executions set semantic_status = 'fail'")

            self.assertEqual(recorded[:4], ("pytest-json", "pass", 1, 0))
            self.assertTrue(recorded[4].endswith("stdout.txt"))
            self.assertTrue((root / recorded[4]).is_file())
            self.assertEqual(recorded[5:], ("allowed", "AC1", "test-target:UNIT", "pass", 1))


if __name__ == "__main__":
    unittest.main()

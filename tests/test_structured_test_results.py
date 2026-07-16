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
        self.assertEqual(
            parse_executed_count(
                "Ran 1 test in 0.001s\n\nOK (skipped=1)"
            ),
            0,
        )
        self.assertEqual(
            parse_executed_count(
                "Ran 3 tests in 0.001s\n\nOK (skipped=1, expected failures=1)"
            ),
            1,
        )
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

    def test_all_skipped_structured_outputs_fail_closed(self) -> None:
        from core.execution import parse_structured_result

        samples = {
            "junit": b'<testsuite tests="1" failures="0" errors="0" skipped="1"><testcase name="a"><skipped/></testcase></testsuite>',
            "pytest-json": b'{"summary":{"total":1,"passed":0,"failed":0,"errors":0,"skipped":1}}',
            "jest-json": b'{"success":true,"numTotalTests":1,"numPassedTests":0,"numFailedTests":0,"numPendingTests":1}',
            "cargo-nextest-json": b'{"type":"test","event":"skipped","name":"test_a"}\n{"type":"run","event":"finished","test_count":1,"passed":0,"failed":0,"skipped":1}\n',
        }
        for result_format, payload in samples.items():
            with self.subTest(result_format=result_format):
                parsed = parse_structured_result(result_format, payload)
                self.assertEqual(parsed.semantic_status, "fail")
                self.assertEqual(parsed.executed_count, 0)

    def test_mixed_passed_and_skipped_outputs_count_only_executed_passes(self) -> None:
        from core.execution import parse_structured_result

        samples = {
            "junit": b'<testsuite tests="2" failures="0" errors="0" skipped="1"><testcase name="a"/><testcase name="b"><skipped/></testcase></testsuite>',
            "pytest-json": b'{"summary":{"total":2,"passed":1,"failed":0,"errors":0,"skipped":1}}',
            "jest-json": b'{"success":true,"numTotalTests":2,"numPassedTests":1,"numFailedTests":0,"numPendingTests":1}',
        }
        for result_format, payload in samples.items():
            with self.subTest(result_format=result_format):
                parsed = parse_structured_result(result_format, payload)
                self.assertEqual(parsed.semantic_status, "pass")
                self.assertEqual(parsed.executed_count, 1)

    def test_structured_parsers_reject_negative_and_contradictory_counts(self) -> None:
        from core.execution import parse_structured_result

        samples = {
            "junit-negative": (
                "junit",
                b'<testsuite tests="1" failures="-1" errors="0" skipped="0"/>',
            ),
            "pytest-negative": (
                "pytest-json",
                b'{"summary":{"total":1,"passed":2,"failed":-1,"errors":0,"skipped":0}}',
            ),
            "pytest-contradictory": (
                "pytest-json",
                b'{"summary":{"total":1,"passed":2,"failed":0,"errors":0,"skipped":0}}',
            ),
            "jest-negative": (
                "jest-json",
                b'{"success":true,"numTotalTests":1,"numPassedTests":2,"numFailedTests":-1,"numPendingTests":0}',
            ),
            "playwright-negative": (
                "playwright-json",
                b'{"stats":{"expected":1,"unexpected":-1,"skipped":0}}',
            ),
            "nextest-negative": (
                "cargo-nextest-json",
                b'{"type":"run","event":"finished","test_count":1,"failed":-1}\n',
            ),
            "pytest-string-count": (
                "pytest-json",
                b'{"summary":{"total":1,"passed":"1","failed":0,"errors":0,"skipped":0}}',
            ),
            "junit-fractional-count": (
                "junit",
                b'<testsuite tests="1" failures="0.5"/>',
            ),
            "jest-nonboolean-success": (
                "jest-json",
                b'{"success":1,"numTotalTests":1,"numPassedTests":1,"numFailedTests":0}',
            ),
            "go-nonobject-event": (
                "go-json",
                b'42\n{"Action":"pass","Test":"TestA"}\n',
            ),
            "nextest-nonobject-event": (
                "cargo-nextest-json",
                b'null\n{"event":"passed","name":"test_a"}\n',
            ),
        }
        for name, (result_format, payload) in samples.items():
            with self.subTest(name=name):
                parsed = parse_structured_result(result_format, payload)
                self.assertEqual(parsed.semantic_status, "fail")
                self.assertEqual(parsed.executed_count, 0)
                self.assertIn("malformed", parsed.reason)

    def test_junit_child_outcomes_cannot_be_hidden_by_missing_aggregates(self) -> None:
        from core.execution import parse_structured_result

        failing = {
            "failure": b'<testsuite tests="1"><testcase name="a"><failure>boom</failure></testcase></testsuite>',
            "error": b'<testsuite tests="1"><testcase name="a"><error>boom</error></testcase></testsuite>',
            "namespaced-failure": b'<testsuite xmlns="urn:junit" tests="1"><testcase name="a"><failure>boom</failure></testcase></testsuite>',
        }
        for name, payload in failing.items():
            with self.subTest(name=name):
                parsed = parse_structured_result("junit", payload)
                self.assertEqual(parsed.semantic_status, "fail")
                self.assertEqual(parsed.executed_count, 1)

        skipped = parse_structured_result(
            "junit",
            b'<testsuite tests="1"><testcase name="a"><skipped/></testcase></testsuite>',
        )
        self.assertEqual(skipped.semantic_status, "fail")
        self.assertEqual(skipped.executed_count, 0)

    def test_junit_rejects_aggregate_counts_that_contradict_children(self) -> None:
        from core.execution import parse_structured_result

        samples = (
            b'<testsuite tests="1" failures="0"><testcase name="a"><failure/></testcase></testsuite>',
            b'<testsuite tests="2" skipped="0"><testcase name="a"><skipped/></testcase></testsuite>',
        )
        for payload in samples:
            with self.subTest(payload=payload):
                parsed = parse_structured_result("junit", payload)
                self.assertEqual(parsed.semantic_status, "fail")
                self.assertEqual(parsed.executed_count, 0)
                self.assertIn("malformed", parsed.reason)

    def test_junit_rejects_testcases_outside_a_junit_root(self) -> None:
        from core.execution import parse_structured_result

        for payload in (
            b'<garbage><testcase name="a"/></garbage>',
            b'<testsuites tests="1"><testcase name="a"/></testsuites>',
        ):
            with self.subTest(payload=payload):
                parsed = parse_structured_result("junit", payload)
                self.assertEqual(parsed.semantic_status, "fail")
                self.assertEqual(parsed.executed_count, 0)
                self.assertIn("malformed", parsed.reason)

    def test_junit_disabled_and_notrun_cases_are_not_executed(self) -> None:
        from core.execution import parse_structured_result

        samples = (
            b'<testsuite tests="1" disabled="1" failures="0" errors="0" skipped="0"/>',
            b'<testsuite tests="1"><testcase name="a" status="notrun"/></testsuite>',
            b'<testsuite tests="1"><testcase name="a" result="skipped"/></testsuite>',
        )
        for payload in samples:
            with self.subTest(payload=payload):
                parsed = parse_structured_result("junit", payload)
                self.assertEqual(parsed.semantic_status, "fail")
                self.assertEqual(parsed.executed_count, 0)

        mixed = parse_structured_result(
            "junit",
            b'<testsuite tests="2"><testcase name="ok" status="run"/><testcase name="off" status="notrun" result="suppressed"/></testsuite>',
        )
        self.assertEqual(mixed.semantic_status, "pass")
        self.assertEqual(mixed.executed_count, 1)

    def test_pytest_xfail_categories_reconcile_without_becoming_pass_facts(self) -> None:
        from core.execution import parse_structured_result

        mixed = parse_structured_result(
            "pytest-json",
            b'{"summary":{"total":2,"passed":1,"failed":0,"errors":0,"skipped":0,"xfailed":1,"xpassed":0}}',
        )
        self.assertEqual(mixed.semantic_status, "pass")
        self.assertEqual(mixed.executed_count, 1)

        all_xfailed = parse_structured_result(
            "pytest-json",
            b'{"summary":{"total":1,"passed":0,"failed":0,"errors":0,"skipped":0,"xfailed":1,"xpassed":0}}',
        )
        self.assertEqual(all_xfailed.semantic_status, "fail")
        self.assertEqual(all_xfailed.executed_count, 0)

    def test_json_child_failures_cannot_contradict_passing_aggregates(self) -> None:
        from core.execution import parse_structured_result

        samples = {
            "pytest-json": b'{"summary":{"total":1,"passed":1,"failed":0,"errors":0,"skipped":0},"tests":[{"nodeid":"test_a","outcome":"failed"}]}',
            "jest-json": b'{"success":true,"numTotalTests":1,"numPassedTests":1,"numFailedTests":0,"numPendingTests":0,"testResults":[{"status":"passed","assertionResults":[{"title":"a","status":"failed"}]}]}',
            "playwright-json": b'{"stats":{"expected":1,"unexpected":0,"flaky":0,"skipped":0},"suites":[{"specs":[{"tests":[{"status":"expected","results":[{"status":"failed"}]}]}]}]}',
            "cargo-nextest-json": b'{"type":"test","event":"passed","name":"test_a"}\n{"type":"run","event":"finished","test_count":1,"passed":0,"failed":0,"skipped":1}\n',
        }
        for result_format, payload in samples.items():
            with self.subTest(result_format=result_format):
                parsed = parse_structured_result(result_format, payload)
                self.assertEqual(parsed.semantic_status, "fail")
                self.assertEqual(parsed.executed_count, 0)
                self.assertIn("malformed", parsed.reason)

    def test_playwright_child_outcomes_require_executed_attempts(self) -> None:
        from core.execution import parse_structured_result

        samples = (
            b'{"stats":{"expected":1,"unexpected":0,"flaky":0,"skipped":0},"suites":[{"specs":[{"tests":[{"status":"expected","results":[]}]}]}]}',
            b'{"stats":{"expected":1,"unexpected":0,"flaky":0,"skipped":0},"suites":[{"specs":[{"tests":[{"status":"expected","results":[{"status":"skipped"}]}]}]}]}',
            b'{"stats":{"expected":0,"unexpected":0,"flaky":1,"skipped":0},"suites":[{"specs":[{"tests":[{"status":"flaky","results":[]}]}]}]}',
        )
        for payload in samples:
            with self.subTest(payload=payload):
                parsed = parse_structured_result("playwright-json", payload)
                self.assertEqual(parsed.semantic_status, "fail")
                self.assertEqual(parsed.executed_count, 0)
                self.assertIn("malformed", parsed.reason)

    def test_playwright_reconciles_outcomes_with_expected_status(self) -> None:
        from core.execution import parse_structured_result

        expected_failure = parse_structured_result(
            "playwright-json",
            b'{"stats":{"expected":1,"unexpected":0,"flaky":0,"skipped":0},"suites":[{"specs":[{"tests":[{"status":"expected","expectedStatus":"failed","results":[{"status":"failed"}]}]}]}]}',
        )
        self.assertEqual(expected_failure.semantic_status, "pass")
        self.assertEqual(expected_failure.executed_count, 1)

        flaky = parse_structured_result(
            "playwright-json",
            b'{"stats":{"expected":0,"unexpected":0,"flaky":1,"skipped":0},"suites":[{"specs":[{"tests":[{"status":"flaky","expectedStatus":"passed","results":[{"status":"failed"},{"status":"passed"}]}]}]}]}',
        )
        self.assertEqual(flaky.semantic_status, "pass")
        self.assertEqual(flaky.executed_count, 1)

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

    def test_local_executor_rejects_unchanged_prior_structured_result(self) -> None:
        from core.execution import ExecutionPolicyError, LocalExecutor

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stale = root / "pytest.json"
            stale.write_text(
                '{"summary":{"total":1,"passed":1,"failed":0,"errors":0}}',
                encoding="utf-8",
            )
            before = stale.stat().st_mtime_ns

            with self.assertRaisesRegex(
                ExecutionPolicyError,
                "structured-result-stale",
            ):
                LocalExecutor(root).run(
                    "python3 -c pass",
                    target_id="UNIT",
                    target_command_template="python3 -c pass",
                    result_format="pytest-json",
                    result_path="pytest.json",
                )

            self.assertEqual(stale.stat().st_mtime_ns, before)


class StructuredResultGateTest(unittest.TestCase):
    def test_structured_cli_verify_records_semantic_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
            (root / "emit.py").write_text(
                "from pathlib import Path\n"
                "result = Path('.ai-team/runtime/pytest.json')\n"
                "result.parent.mkdir(parents=True, exist_ok=True)\n"
                "result.write_text('{\"summary\":{\"total\":1,\"passed\":1,\"failed\":0,\"errors\":0}}')\n",
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
                ".ai-team/runtime/pytest.json",
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

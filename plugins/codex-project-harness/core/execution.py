"""Controller-owned local and no-network container execution.

Execution facts are produced here, validated before persistence, and stored by
the root controller through the runtime API.  Callers never supply a claimed
exit code, digest, count, or sandbox status.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from xml.etree import ElementTree

from .project_fs import ProjectFS, _PathSnapshot


DEFAULT_TIMEOUT_SECONDS = 120
MAX_STDOUT_BYTES = 1024 * 1024


@dataclass(frozen=True)
class CommandResult:
    command: str
    exit_code: int
    stdout_sha256: str
    artifact_path: str
    timed_out: bool = False
    target_id: str = ""
    executed_count: int = 0
    executed_count_source: str = ""
    result_format: str = "regex"
    result_path: str = ""
    semantic_status: str = ""
    allow_unlisted: bool = False
    no_network: bool = False
    sandbox_profile: str = "none"
    sandbox_status: str = ""
    allow_unlisted_reason: str = ""
    policy_status: str = "allowed"
    policy_reason: str = ""


@dataclass(frozen=True)
class StructuredResult:
    semantic_status: str
    executed_count: int
    executed_count_source: str = "structured"
    reason: str = ""


@dataclass(frozen=True)
class _StructuredResultBaseline:
    snapshot: _PathSnapshot
    sha256: str | None


@dataclass(frozen=True)
class TargetExecutionPolicy:
    """Immutable target snapshot used for one controller verification."""

    id: str
    command_template: str
    result_format: str = "regex"
    result_path: str = ""
    requires_sandbox: bool = False
    requires_no_network: bool = False
    container_image: str = "python:3.12-slim"


class ExecutionPolicyError(ValueError):
    """Raised when execution output cannot become a trusted execution fact."""


def normalize_command(command: str) -> str:
    try:
        return " ".join(shlex.split(command))
    except ValueError:
        return " ".join(command.split())


def command_matches_template(command: str, template: str) -> bool:
    return normalize_command(command) == normalize_command(template)


def command_matches_prefix(command: str, prefix: str) -> bool:
    normalized_command = normalize_command(command)
    normalized_prefix = normalize_command(prefix)
    return normalized_command == normalized_prefix or normalized_command.startswith(normalized_prefix + " ")


def parse_executed_count(stdout: str | bytes) -> int:
    text = stdout.decode("utf-8", errors="replace") if isinstance(stdout, bytes) else stdout
    unittest_run = re.search(
        r"Ran\s+(\d+)\s+tests?",
        text,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    if unittest_run:
        non_passing = 0
        for label in ("skipped", "expected failures"):
            match = re.search(
                rf"{label}\s*=\s*(\d+)",
                text,
                flags=re.IGNORECASE | re.MULTILINE,
            )
            if match:
                non_passing += int(match.group(1))
        return max(0, int(unittest_run.group(1)) - non_passing)
    patterns = [
        r"(\d+)\s+passed(?:,|\s|$)",
        r"Tests:\s+(\d+)\s+passed",
        r"(\d+)\s+passing\b",
        r"(\d+)\s+tests?\s+passed",
        r"PASS\s+.*?\((\d+)\s+tests?\)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
        if match:
            return int(match.group(1))
    return 0


def _json_loads(payload: str | bytes, result_format: str) -> object:
    text = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else payload
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed {result_format}: {exc.msg}") from exc


def _json_lines(payload: str | bytes, result_format: str) -> list[dict[str, object]]:
    text = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else payload
    rows: list[dict[str, object]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"malformed {result_format}: {exc.msg}") from exc
        if not isinstance(value, dict):
            raise ValueError(
                f"malformed {result_format}: event must be an object"
            )
        rows.append(value)
    if not rows:
        raise ValueError(f"malformed {result_format}: no events")
    return rows


def _json_count(
    values: dict[str, object],
    key: str,
    result_format: str,
    *,
    default: int = 0,
) -> int:
    if key not in values:
        return default
    value = values[key]
    if type(value) is not int or value < 0:
        raise ValueError(
            f"malformed {result_format}: {key} must be a non-negative integer"
        )
    return value


def _json_alias_count(
    values: dict[str, object],
    keys: tuple[str, ...],
    result_format: str,
) -> int:
    present = [key for key in keys if key in values]
    if not present:
        return 0
    counts = [_json_count(values, key, result_format) for key in present]
    if len(set(counts)) != 1:
        raise ValueError(
            f"malformed {result_format}: contradictory {'/'.join(keys)} counts"
        )
    return counts[0]


def _xml_count(
    values: dict[str, str],
    key: str,
    result_format: str,
    *,
    default: int = 0,
) -> int:
    if key not in values:
        return default
    value = values[key]
    if not value or not value.isdecimal():
        raise ValueError(
            f"malformed {result_format}: {key} must be a non-negative integer"
        )
    return int(value)


def _xml_local_name(tag: object) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _junit_case_counts(
    cases: list[ElementTree.Element],
) -> tuple[int, int, int, int, int]:
    failures = 0
    errors = 0
    skipped = 0
    disabled = 0
    for case in cases:
        outcomes = {
            _xml_local_name(child.tag)
            for child in case
            if _xml_local_name(child.tag) in {"failure", "error", "skipped"}
        }
        if len(outcomes) > 1:
            raise ValueError(
                "malformed junit: testcase has contradictory outcomes"
            )
        status_not_run = str(case.attrib.get("status", "")).lower() in {
            "notrun",
            "not-run",
            "disabled",
        }
        result_skipped = str(case.attrib.get("result", "")).lower() in {
            "skipped",
            "suppressed",
        }
        if (status_not_run or result_skipped) and outcomes & {
            "failure",
            "error",
        }:
            raise ValueError(
                "malformed junit: testcase has contradictory outcomes"
            )
        failures += int("failure" in outcomes)
        errors += int("error" in outcomes)
        skipped += int("skipped" in outcomes or result_skipped)
        disabled += int(
            status_not_run
            and "skipped" not in outcomes
            and not result_skipped
        )
    return len(cases), failures, errors, skipped, disabled


def _junit_counts(root: ElementTree.Element) -> tuple[int, int, int, int, int]:
    root_name = _xml_local_name(root.tag)
    if root_name not in {"testsuite", "testsuites"}:
        raise ValueError("malformed junit: root must be testsuite or testsuites")

    def validate_membership(
        element: ElementTree.Element,
        *,
        inside_suite: bool,
    ) -> None:
        name = _xml_local_name(element.tag)
        if name == "testcase" and not inside_suite:
            raise ValueError(
                "malformed junit: testcase must belong to a testsuite"
            )
        child_inside_suite = inside_suite or name == "testsuite"
        for child in element:
            validate_membership(
                child,
                inside_suite=child_inside_suite,
            )

    validate_membership(root, inside_suite=False)
    cases = [
        element
        for element in root.iter()
        if _xml_local_name(element.tag) == "testcase"
    ]
    suites = [
        element
        for element in root.iter()
        if _xml_local_name(element.tag) == "testsuite"
    ]
    if cases:
        totals = _junit_case_counts(cases)
        aggregate_nodes = list(suites)
        if root_name == "testsuites":
            aggregate_nodes.append(root)
        for suite in aggregate_nodes:
            suite_cases = [
                element
                for element in suite.iter()
                if _xml_local_name(element.tag) == "testcase"
            ]
            derived = _junit_case_counts(suite_cases)
            for index, key in enumerate(
                ("tests", "failures", "errors", "skipped", "disabled")
            ):
                if key in suite.attrib and _xml_count(
                    suite.attrib,
                    key,
                    "junit",
                ) != derived[index]:
                    raise ValueError(
                        f"malformed junit: {key} contradicts testcase outcomes"
                    )
        return totals

    aggregate_nodes: list[ElementTree.Element]
    if root_name in {"testsuite", "testsuites"} and "tests" in root.attrib:
        aggregate_nodes = [root]
    else:
        aggregate_nodes = [
            child
            for child in root
            if _xml_local_name(child.tag) == "testsuite"
        ]
    if not aggregate_nodes:
        raise ValueError("malformed junit: missing testsuite/testcase results")
    totals = [0, 0, 0, 0, 0]
    for suite in aggregate_nodes:
        counts = (
            _xml_count(suite.attrib, "tests", "junit"),
            _xml_count(suite.attrib, "failures", "junit"),
            _xml_count(suite.attrib, "errors", "junit"),
            _xml_count(suite.attrib, "skipped", "junit"),
            _xml_count(suite.attrib, "disabled", "junit"),
        )
        if sum(counts[1:]) > counts[0]:
            raise ValueError("malformed junit: outcome counts exceed tests")
        for index, count in enumerate(counts):
            totals[index] += count
    return tuple(totals)  # type: ignore[return-value]


def _pytest_child_counts(
    data: dict[str, object],
    result_format: str,
) -> tuple[int, int, int] | None:
    if "tests" not in data:
        return None
    tests = data["tests"]
    if not isinstance(tests, list):
        raise ValueError(f"malformed {result_format}: tests must be a list")
    passed = 0
    failed = 0
    nonexecuted = 0
    for test in tests:
        if not isinstance(test, dict) or not isinstance(test.get("outcome"), str):
            raise ValueError(
                f"malformed {result_format}: test outcome must be a string"
            )
        outcome = test["outcome"].lower()
        if outcome in {"passed", "xpassed"}:
            passed += 1
        elif outcome in {"failed", "error"}:
            failed += 1
        elif outcome in {"skipped", "xfailed"}:
            nonexecuted += 1
        else:
            raise ValueError(
                f"malformed {result_format}: unknown test outcome {outcome!r}"
            )
    return passed, failed, nonexecuted


def _jest_child_counts(
    data: dict[str, object],
    result_format: str,
) -> tuple[int, int, int, bool] | None:
    if "testResults" not in data:
        return None
    test_results = data["testResults"]
    if not isinstance(test_results, list):
        raise ValueError(
            f"malformed {result_format}: testResults must be a list"
        )
    passed = 0
    failed = 0
    nonexecuted = 0
    failed_suite = False
    for suite in test_results:
        if not isinstance(suite, dict):
            raise ValueError(
                f"malformed {result_format}: testResults entry must be an object"
            )
        suite_status = suite.get("status")
        if suite_status is not None:
            if not isinstance(suite_status, str):
                raise ValueError(
                    f"malformed {result_format}: suite status must be a string"
                )
            if suite_status not in {"passed", "failed"}:
                raise ValueError(
                    f"malformed {result_format}: unknown suite status {suite_status!r}"
                )
            failed_suite = failed_suite or suite_status == "failed"
        assertions = suite.get("assertionResults", [])
        if not isinstance(assertions, list):
            raise ValueError(
                f"malformed {result_format}: assertionResults must be a list"
            )
        for assertion in assertions:
            if not isinstance(assertion, dict) or not isinstance(
                assertion.get("status"),
                str,
            ):
                raise ValueError(
                    f"malformed {result_format}: assertion status must be a string"
                )
            status = assertion["status"].lower()
            if status == "passed":
                passed += 1
            elif status == "failed":
                failed += 1
            elif status in {"pending", "todo", "disabled", "skipped"}:
                nonexecuted += 1
            else:
                raise ValueError(
                    f"malformed {result_format}: unknown assertion status {status!r}"
                )
    return passed, failed, nonexecuted, failed_suite


def _playwright_child_counts(
    data: dict[str, object],
    result_format: str,
) -> tuple[int, int, int, int] | None:
    if "suites" not in data:
        return None
    roots = data["suites"]
    if not isinstance(roots, list):
        raise ValueError(f"malformed {result_format}: suites must be a list")
    tests: list[dict[str, object]] = []

    def collect_suite(suite: object) -> None:
        if not isinstance(suite, dict):
            raise ValueError(
                f"malformed {result_format}: suite must be an object"
            )
        nested = suite.get("suites", [])
        specs = suite.get("specs", [])
        if not isinstance(nested, list) or not isinstance(specs, list):
            raise ValueError(
                f"malformed {result_format}: suites/specs must be lists"
            )
        for child in nested:
            collect_suite(child)
        for spec in specs:
            if not isinstance(spec, dict) or not isinstance(
                spec.get("tests", []),
                list,
            ):
                raise ValueError(
                    f"malformed {result_format}: spec tests must be a list"
                )
            for test in spec.get("tests", []):
                if not isinstance(test, dict):
                    raise ValueError(
                        f"malformed {result_format}: test must be an object"
                    )
                tests.append(test)

    for suite in roots:
        collect_suite(suite)

    counts = {"expected": 0, "unexpected": 0, "flaky": 0, "skipped": 0}
    for test in tests:
        results = test.get("results", [])
        if not isinstance(results, list):
            raise ValueError(
                f"malformed {result_format}: results must be a list"
            )
        result_statuses: list[str] = []
        for result in results:
            if not isinstance(result, dict) or not isinstance(
                result.get("status"),
                str,
            ):
                raise ValueError(
                    f"malformed {result_format}: result status must be a string"
                )
            status = result["status"]
            if status not in {
                "passed",
                "failed",
                "timedOut",
                "interrupted",
                "skipped",
            }:
                raise ValueError(
                    f"malformed {result_format}: unknown result status {status!r}"
                )
            result_statuses.append(status)
        expected_status = test.get("expectedStatus", "passed")
        if not isinstance(expected_status, str) or expected_status not in {
            "passed",
            "failed",
            "timedOut",
            "skipped",
            "interrupted",
        }:
            raise ValueError(
                f"malformed {result_format}: unknown expectedStatus {expected_status!r}"
            )
        executed_statuses = [
            status for status in result_statuses if status != "skipped"
        ]
        final_status = test.get("status")
        if final_status is None:
            if not executed_statuses:
                final_status = "skipped"
            elif executed_statuses[-1] != expected_status:
                final_status = "unexpected"
            elif any(
                status != expected_status
                for status in executed_statuses[:-1]
            ):
                final_status = "flaky"
            else:
                final_status = "expected"
        if not isinstance(final_status, str) or final_status not in counts:
            raise ValueError(
                f"malformed {result_format}: unknown test status {final_status!r}"
            )
        expected_outcome = (
            bool(executed_statuses)
            and len(executed_statuses) == len(result_statuses)
            and expected_status != "skipped"
            and all(
                status == expected_status for status in executed_statuses
            )
        )
        unexpected_outcome = (
            bool(executed_statuses)
            and executed_statuses[-1] != expected_status
        )
        flaky_outcome = (
            len(executed_statuses) >= 2
            and len(executed_statuses) == len(result_statuses)
            and executed_statuses[-1] == expected_status
            and any(
                status != expected_status
                for status in executed_statuses[:-1]
            )
        )
        skipped_outcome = not executed_statuses
        if not {
            "expected": expected_outcome,
            "unexpected": unexpected_outcome,
            "flaky": flaky_outcome,
            "skipped": skipped_outcome,
        }[final_status]:
            raise ValueError(
                f"malformed {result_format}: test status contradicts result statuses"
            )
        counts[final_status] += 1
    return (
        counts["expected"],
        counts["unexpected"],
        counts["flaky"],
        counts["skipped"],
    )


def _structured_pass(count: int, failed: int = 0, reason: str = "") -> StructuredResult:
    if count < 0 or failed < 0:
        return StructuredResult("fail", 0, reason="malformed structured counts")
    if failed > 0:
        return StructuredResult("fail", count, reason=reason or f"failed={failed}")
    if count <= 0:
        return StructuredResult("fail", 0, reason="zero tests")
    return StructuredResult("pass", count)


def parse_structured_result(result_format: str, payload: str | bytes) -> StructuredResult:
    if result_format == "regex":
        count = parse_executed_count(payload)
        return StructuredResult("pass" if count > 0 else "fail", count, "parsed" if count > 0 else "structured", "zero tests" if count <= 0 else "")
    try:
        if result_format == "junit":
            root = ElementTree.fromstring(payload)
            tests, failures, errors, skipped, disabled = _junit_counts(root)
            executed = tests - skipped - disabled
            return _structured_pass(
                executed,
                failures + errors,
                f"failures={failures} errors={errors} skipped={skipped} disabled={disabled}",
            )
        if result_format == "pytest-json":
            data = _json_loads(payload, result_format)
            if not isinstance(data, dict) or not isinstance(data.get("summary"), dict):
                raise ValueError("malformed pytest-json: missing summary")
            summary = data["summary"]
            passed = _json_count(summary, "passed", result_format)
            failed_tests = _json_count(summary, "failed", result_format)
            errors = _json_alias_count(
                summary,
                ("errors", "error"),
                result_format,
            )
            skipped = _json_count(summary, "skipped", result_format)
            xfailed = _json_count(summary, "xfailed", result_format)
            xpassed = _json_count(summary, "xpassed", result_format)
            known_total = (
                passed
                + failed_tests
                + errors
                + skipped
                + xfailed
                + xpassed
            )
            total = _json_count(
                summary,
                "total",
                result_format,
                default=known_total,
            )
            if total != known_total:
                raise ValueError(
                    "malformed pytest-json: total contradicts outcome counts"
                )
            child_counts = _pytest_child_counts(data, result_format)
            aggregate_child_buckets = (
                passed + xpassed,
                failed_tests + errors,
                skipped + xfailed,
            )
            if (
                child_counts is not None
                and child_counts != aggregate_child_buckets
            ):
                raise ValueError(
                    "malformed pytest-json: summary contradicts test outcomes"
                )
            failed = failed_tests + errors
            executed = passed + failed + xpassed
            return _structured_pass(
                executed,
                failed,
                f"total={total} passed={passed} failed={failed} skipped={skipped}",
            )
        if result_format == "jest-json":
            data = _json_loads(payload, result_format)
            if not isinstance(data, dict):
                raise ValueError("malformed jest-json: expected object")
            passed = _json_count(data, "numPassedTests", result_format)
            failed = _json_count(data, "numFailedTests", result_format)
            skipped = _json_count(data, "numPendingTests", result_format)
            todo = _json_count(data, "numTodoTests", result_format)
            known_total = passed + failed + skipped + todo
            total = _json_count(
                data,
                "numTotalTests",
                result_format,
                default=known_total,
            )
            if total != known_total:
                raise ValueError(
                    "malformed jest-json: total contradicts outcome counts"
                )
            if "success" in data and type(data["success"]) is not bool:
                raise ValueError("malformed jest-json: success must be boolean")
            success = data.get("success", failed == 0)
            child_counts = _jest_child_counts(data, result_format)
            if child_counts is not None:
                child_passed, child_failed, child_nonexecuted, failed_suite = (
                    child_counts
                )
                if (
                    (child_passed, child_failed, child_nonexecuted)
                    != (passed, failed, skipped + todo)
                    or (success and failed_suite)
                ):
                    raise ValueError(
                        "malformed jest-json: aggregate contradicts test outcomes"
                    )
            semantic_failures = 0 if success and failed == 0 else max(1, failed)
            return _structured_pass(
                passed + failed,
                semantic_failures,
                f"total={total} passed={passed} failed={failed} skipped={skipped}",
            )
        if result_format == "go-json":
            events = _json_lines(payload, result_format)
            passed_tests = {str(event["Test"]) for event in events if event.get("Action") == "pass" and event.get("Test")}
            failed = any(event.get("Action") == "fail" for event in events)
            return _structured_pass(len(passed_tests), 1 if failed else 0, "go test failed")
        if result_format == "cargo-nextest-json":
            events = _json_lines(payload, result_format)
            outcomes: dict[str, str] = {}
            for event in events:
                event_status = event.get("event")
                name_value = (
                    event.get("name")
                    or event.get("test")
                    or event.get("test_name")
                )
                if event_status not in {
                    "passed",
                    "ok",
                    "failed",
                    "failure",
                    "skipped",
                } or not name_value:
                    continue
                name = str(name_value)
                outcome = (
                    "passed"
                    if event_status in {"passed", "ok"}
                    else "failed"
                    if event_status in {"failed", "failure"}
                    else "skipped"
                )
                previous = outcomes.get(name)
                if previous is not None and previous != outcome:
                    raise ValueError(
                        f"malformed {result_format}: contradictory outcomes for {name}"
                    )
                outcomes[name] = outcome
            passed = {
                name for name, outcome in outcomes.items() if outcome == "passed"
            }
            failed_names = {
                name for name, outcome in outcomes.items() if outcome == "failed"
            }
            skipped_names = {
                name for name, outcome in outcomes.items() if outcome == "skipped"
            }
            failed = bool(failed_names)
            for event in events:
                if event.get("event") == "finished":
                    finished_failed = _json_count(
                        event,
                        "failed",
                        result_format,
                    )
                    _json_count(event, "passed", result_format)
                    _json_count(event, "skipped", result_format)
                    _json_count(event, "test_count", result_format)
                    comparisons = {
                        "passed": len(passed),
                        "failed": len(failed_names),
                        "skipped": len(skipped_names),
                        "test_count": len(outcomes),
                    }
                    for key, actual in comparisons.items():
                        if (
                            key in event
                            and _json_count(event, key, result_format) != actual
                        ):
                            raise ValueError(
                                f"malformed {result_format}: finished {key} contradicts test outcomes"
                            )
                    if finished_failed > 0:
                        failed = True
            return _structured_pass(len(passed), 1 if failed else 0, "nextest failed")
        if result_format == "playwright-json":
            data = _json_loads(payload, result_format)
            if not isinstance(data, dict):
                raise ValueError("malformed playwright-json: expected object")
            stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
            expected = _json_alias_count(
                stats,
                ("expected", "passed"),
                result_format,
            )
            unexpected = _json_alias_count(
                stats,
                ("unexpected", "failed"),
                result_format,
            )
            flaky = _json_count(stats, "flaky", result_format)
            _json_count(stats, "skipped", result_format)
            if "status" in data and not isinstance(data["status"], str):
                raise ValueError(
                    "malformed playwright-json: status must be a string"
                )
            status = data.get("status", "")
            child_counts = _playwright_child_counts(data, result_format)
            aggregate_counts = (expected, unexpected, flaky, _json_count(
                stats,
                "skipped",
                result_format,
            ))
            if child_counts is not None and child_counts != aggregate_counts:
                raise ValueError(
                    "malformed playwright-json: stats contradict test outcomes"
                )
            if status and status not in {"passed", "ok"}:
                unexpected = max(unexpected, 1)
            return _structured_pass(
                expected + flaky,
                unexpected,
                f"unexpected={unexpected}",
            )
    except ElementTree.ParseError as exc:
        return StructuredResult("fail", 0, reason=f"malformed {result_format}: {exc}")
    except (TypeError, ValueError) as exc:
        return StructuredResult("fail", 0, reason=str(exc))
    return StructuredResult("fail", 0, reason=f"unknown result format: {result_format}")


def minimal_env() -> dict[str, str]:
    keep = ["PATH", "HOME", "TMPDIR", "LANG", "LC_ALL"]
    return {key: os.environ[key] for key in keep if key in os.environ}


def _capture_structured_result_baseline(
    project_fs: ProjectFS,
    relative: Path,
) -> _StructuredResultBaseline:
    snapshot = project_fs._snapshot(relative, allow_missing=True)
    if not snapshot.exists:
        return _StructuredResultBaseline(snapshot, None)
    payload = project_fs.read_bytes(relative, expected=snapshot)
    project_fs._assert_unchanged(relative, snapshot)
    return _StructuredResultBaseline(
        snapshot,
        hashlib.sha256(payload).hexdigest(),
    )


def _read_fresh_structured_result(
    project_fs: ProjectFS,
    relative: Path,
    baseline: _StructuredResultBaseline,
) -> bytes | None:
    snapshot = project_fs._snapshot(relative, allow_missing=True)
    if not snapshot.exists:
        return None
    payload = project_fs.read_bytes(relative, expected=snapshot)
    project_fs._assert_unchanged(relative, snapshot)
    if (
        baseline.snapshot.exists
        and snapshot == baseline.snapshot
        and hashlib.sha256(payload).hexdigest() == baseline.sha256
    ):
        raise ExecutionPolicyError(
            "structured-result-stale: declared result was not created or "
            f"updated by the current execution: {relative.as_posix()}"
        )
    return payload


class LocalExecutor:
    def __init__(self, root: Path, *, max_stdout_bytes: int = MAX_STDOUT_BYTES) -> None:
        self.root = root.resolve()
        self.max_stdout_bytes = max_stdout_bytes

    def run(
        self,
        command: str,
        *,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        target_id: str = "",
        target_command_template: str = "",
        allowed_prefixes: list[str] | None = None,
        allow_unlisted: bool = False,
        no_network: bool = False,
        sandbox_profile: str = "none",
        allow_unlisted_reason: str = "",
        executed_count: int | None = None,
        result_format: str = "regex",
        result_path: str = "",
    ) -> CommandResult:
        if not command.strip():
            raise ValueError("command is required")
        normalized_result_path = _safe_result_path(result_path)
        execution_id = uuid.uuid4().hex
        artifact_relative = Path(
            f".ai-team/runtime/executions/{execution_id}/stdout.txt"
        )
        structured_relative = artifact_relative.parent / "structured-result"
        structured_baseline: _StructuredResultBaseline | None = None
        with ProjectFS.open(self.root) as project_fs:
            project_fs.audit(
                (artifact_relative, structured_relative),
                allow_missing=True,
            )
            artifact_snapshot = project_fs._snapshot(
                artifact_relative,
                allow_missing=True,
            )
            structured_snapshot = project_fs._snapshot(
                structured_relative,
                allow_missing=True,
            )
            if artifact_snapshot.exists or structured_snapshot.exists:
                raise ExecutionPolicyError(
                    "execution-artifact-collision: generated destination already exists"
                )
            if normalized_result_path:
                source_relative = Path(normalized_result_path)
                project_fs.audit(
                    (source_relative,),
                    allow_missing=True,
                )
                structured_baseline = _capture_structured_result_baseline(
                    project_fs,
                    source_relative,
                )
        profile = "no-network" if no_network else sandbox_profile
        sandbox_status = "unavailable" if profile == "no-network" else ""
        policy_status, policy_reason = self._policy(
            command,
            target_id,
            target_command_template,
            allowed_prefixes or [],
            allow_unlisted,
            allow_unlisted_reason,
        )
        if policy_status == "rejected":
            stdout = f"command rejected by policy: {policy_reason}\n".encode("utf-8")
            return self._write_result(
                command,
                stdout,
                exit_code=126,
                target_id=target_id,
                executed_count=0,
                executed_count_source="policy",
                allow_unlisted=allow_unlisted,
                no_network=no_network,
                sandbox_profile=profile,
                sandbox_status=sandbox_status,
                allow_unlisted_reason=allow_unlisted_reason,
                policy_status=policy_status,
                policy_reason=policy_reason,
                result_format=result_format,
                result_path=normalized_result_path,
                semantic_status="fail" if result_format != "regex" else "",
                execution_id=execution_id,
                artifact_expected=artifact_snapshot,
                structured_expected=structured_snapshot,
            )
        args = shlex.split(command)
        if not args:
            raise ValueError("command is required")
        timed_out = False
        try:
            completed = subprocess.run(
                args,
                cwd=self.root,
                env=minimal_env(),
                capture_output=True,
                check=False,
                timeout=timeout,
            )
            exit_code = completed.returncode
            stdout = (completed.stdout or b"") + (completed.stderr or b"")
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = 124
            stdout = exc.stdout or b""
        except OSError as exc:
            exit_code = 127
            stdout = str(exc).encode("utf-8", errors="replace")
        stdout = stdout[: self.max_stdout_bytes]
        semantic_status = ""
        structured_payload_for_artifact: bytes | None = None
        if result_format != "regex":
            structured_payload = stdout
            if normalized_result_path:
                assert structured_baseline is not None
                with ProjectFS.open(self.root) as project_fs:
                    source_relative = Path(normalized_result_path)
                    current_payload = _read_fresh_structured_result(
                        project_fs,
                        source_relative,
                        structured_baseline,
                    )
                    if current_payload is not None:
                        structured_payload = current_payload
                        structured_payload_for_artifact = current_payload
                    else:
                        structured_payload = b""
            parsed = parse_structured_result(result_format, structured_payload)
            count = parsed.executed_count
            count_source = parsed.executed_count_source
            semantic_status = parsed.semantic_status
        else:
            count_source = "manual" if executed_count is not None else "parsed"
            count = int(executed_count) if executed_count is not None else parse_executed_count(stdout)
            semantic_status = "pass" if exit_code == 0 and count > 0 else "fail"
        return self._write_result(
            command,
            stdout,
            exit_code=exit_code,
            timed_out=timed_out,
            target_id=target_id,
            executed_count=count,
            executed_count_source=count_source,
            allow_unlisted=allow_unlisted,
            no_network=no_network,
            sandbox_profile=profile,
            sandbox_status=sandbox_status,
            allow_unlisted_reason=allow_unlisted_reason,
            policy_status=policy_status,
            policy_reason=policy_reason,
            result_format=result_format,
            result_path=normalized_result_path,
            semantic_status=semantic_status,
            structured_payload=structured_payload_for_artifact,
            execution_id=execution_id,
            artifact_expected=artifact_snapshot,
            structured_expected=structured_snapshot,
        )

    def _policy(
        self,
        command: str,
        target_id: str,
        target_command_template: str,
        allowed_prefixes: list[str],
        allow_unlisted: bool,
        allow_unlisted_reason: str,
    ) -> tuple[str, str]:
        if target_id:
            if not target_command_template:
                return "rejected", f"unknown target: {target_id}"
            if not command_matches_template(command, target_command_template):
                return "rejected", f"command does not match target {target_id}"
            return "allowed", f"target {target_id}"
        for prefix in allowed_prefixes:
            if command_matches_prefix(command, prefix):
                return "allowed", f"prefix {prefix}"
        if allow_unlisted:
            if not allow_unlisted_reason.strip():
                return "rejected", "--reason is required when --allow-unlisted is used"
            return "allowed", f"explicit allow-unlisted: {allow_unlisted_reason}"
        return "rejected", "command is not registered target or allowed prefix"

    def _write_result(
        self,
        command: str,
        stdout: bytes,
        *,
        exit_code: int,
        timed_out: bool = False,
        target_id: str = "",
        executed_count: int = 0,
        executed_count_source: str = "",
        allow_unlisted: bool = False,
        no_network: bool = False,
        sandbox_profile: str = "none",
        sandbox_status: str = "",
        allow_unlisted_reason: str = "",
        policy_status: str = "allowed",
        policy_reason: str = "",
        result_format: str = "regex",
        result_path: str = "",
        semantic_status: str = "",
        structured_payload: bytes | None = None,
        execution_id: str = "",
        artifact_expected: _PathSnapshot | None = None,
        structured_expected: _PathSnapshot | None = None,
    ) -> CommandResult:
        if not execution_id:
            execution_id = uuid.uuid4().hex
        artifact_relative = Path(
            f".ai-team/runtime/executions/{execution_id}/stdout.txt"
        )
        structured_relative = artifact_relative.parent / "structured-result"
        with ProjectFS.open(self.root) as project_fs:
            project_fs.atomic_write(
                artifact_relative,
                stdout,
                mode=0o600,
                expected_destination=artifact_expected,
            )
        stored_result_path = result_path
        if result_format != "regex" and structured_payload is not None:
            with ProjectFS.open(self.root) as project_fs:
                project_fs.atomic_write(
                    structured_relative,
                    structured_payload,
                    mode=0o600,
                    expected_destination=structured_expected,
                )
            stored_result_path = structured_relative.as_posix()
        return CommandResult(
            command=command,
            exit_code=exit_code,
            stdout_sha256=hashlib.sha256(stdout).hexdigest(),
            artifact_path=artifact_relative.as_posix(),
            timed_out=timed_out,
            target_id=target_id,
            executed_count=executed_count,
            executed_count_source=executed_count_source,
            result_format=result_format,
            result_path=stored_result_path,
            semantic_status=semantic_status,
            allow_unlisted=allow_unlisted,
            no_network=no_network,
            sandbox_profile=sandbox_profile,
            sandbox_status=sandbox_status,
            allow_unlisted_reason=allow_unlisted_reason,
            policy_status=policy_status,
            policy_reason=policy_reason,
        )


class ContainerExecutor:
    """Run one registered target in a disposable, no-network container."""

    def __init__(self, root: Path, *, max_stdout_bytes: int = MAX_STDOUT_BYTES) -> None:
        self.root = root.resolve()
        self.max_stdout_bytes = max_stdout_bytes

    def run(
        self,
        command: str,
        *,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        target_id: str = "",
        target_command_template: str = "",
        container_image: str = "python:3.12-slim",
        result_format: str = "regex",
        result_path: str = "",
    ) -> CommandResult:
        engine = shutil.which("docker") or shutil.which("podman")
        if not engine:
            raise ExecutionPolicyError(
                "sandbox-unavailable: Docker or Podman is required for container verification"
            )
        policy_status, policy_reason = LocalExecutor(self.root)._policy(
            command,
            target_id,
            target_command_template,
            [],
            False,
            "",
        )
        if policy_status != "allowed":
            raise ExecutionPolicyError(f"command rejected by policy: {policy_reason}")
        normalized_result_path = _safe_result_path(result_path)
        execution_id = uuid.uuid4().hex
        artifact_relative = Path(
            f".ai-team/runtime/executions/{execution_id}/stdout.txt"
        )
        structured_relative = artifact_relative.parent / "structured-result"
        with ProjectFS.open(self.root) as project_fs:
            project_fs.audit(
                (artifact_relative, structured_relative),
                allow_missing=True,
            )
            artifact_snapshot = project_fs._snapshot(
                artifact_relative,
                allow_missing=True,
            )
            structured_destination_snapshot = project_fs._snapshot(
                structured_relative,
                allow_missing=True,
            )
            if (
                artifact_snapshot.exists
                or structured_destination_snapshot.exists
            ):
                raise ExecutionPolicyError(
                    "execution-artifact-collision: generated destination already exists"
                )
            if normalized_result_path:
                project_fs.audit(
                    (Path(normalized_result_path),),
                    allow_missing=True,
                )
        container_name = f"kafa-verify-{execution_id[:12]}"
        result_reset = ""
        result_copy = ""
        if normalized_result_path:
            quoted_result = shlex.quote(normalized_result_path)
            # The container receives a disposable workspace copy.  Remove any
            # copied prior report there so only this command can create the
            # declared structured result; the host project is never mutated.
            result_reset = f"rm -f -- {quoted_result}; "
            result_copy = (
                f"; if [ -f {quoted_result} ]; then "
                f"cp -a {quoted_result} /artifacts/structured-result; fi"
            )
        script = (
            "rm -rf /workspace/* /workspace/.[!.]* /workspace/..?* 2>/dev/null || true; "
            "cp -a /src/. /workspace/; "
            f"cd /workspace && {result_reset}"
            f"({command}) > /artifacts/stdout.txt 2>&1; "
            f"rc=$?{result_copy}; exit $rc"
        )
        timed_out = False
        structured_payload: bytes | None = None
        with tempfile.TemporaryDirectory(
            prefix="kafa-container-artifacts-"
        ) as artifact_temp:
            artifact_directory = Path(artifact_temp).resolve()
            argv = [
                engine,
                "run",
                "--rm",
                "--name",
                container_name,
                "--network",
                "none",
                "--cpus",
                "1",
                "--memory",
                "512m",
                "--pids-limit",
                "256",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=64m",
                "--tmpfs",
                "/workspace:rw,exec,nosuid,size=512m",
                "-v",
                f"{self.root}:/src:ro",
                "-v",
                f"{artifact_directory}:/artifacts:rw",
                "-w",
                "/workspace",
                container_image,
                "/bin/sh",
                "-lc",
                script,
            ]
            fallback_stdout = b""
            try:
                completed = subprocess.run(
                    argv,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=timeout,
                )
                exit_code = completed.returncode
                fallback_stdout = (
                    (completed.stdout or "") + (completed.stderr or "")
                ).encode("utf-8")
            except subprocess.TimeoutExpired as exc:
                timed_out = True
                exit_code = 124
                subprocess.run(
                    [engine, "rm", "-f", container_name],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                stdout = exc.stdout or b""
                stderr = exc.stderr or b""
                stdout_value = (
                    stdout if isinstance(stdout, bytes) else stdout.encode("utf-8")
                )
                stderr_value = (
                    stderr if isinstance(stderr, bytes) else stderr.encode("utf-8")
                )
                fallback_stdout = stdout_value + stderr_value

            with ProjectFS.open(artifact_directory) as artifact_fs:
                stdout_snapshot = artifact_fs._snapshot(
                    Path("stdout.txt"),
                    allow_missing=True,
                )
                complete_stdout = (
                    artifact_fs.read_bytes(
                        Path("stdout.txt"),
                        expected=stdout_snapshot,
                    )
                    if stdout_snapshot.exists
                    else fallback_stdout
                )
                structured_snapshot = artifact_fs._snapshot(
                    Path("structured-result"),
                    allow_missing=True,
                )
                if structured_snapshot.exists:
                    structured_payload = artifact_fs.read_bytes(
                        Path("structured-result"),
                        expected=structured_snapshot,
                    )

            stdout_bytes = complete_stdout[: self.max_stdout_bytes]
            with ProjectFS.open(self.root) as project_fs:
                project_fs.atomic_write(
                    artifact_relative,
                    stdout_bytes,
                    mode=0o600,
                    expected_destination=artifact_snapshot,
                )
                if structured_payload is not None:
                    project_fs.atomic_write(
                        structured_relative,
                        structured_payload,
                        mode=0o600,
                        expected_destination=structured_destination_snapshot,
                    )
        if result_format == "regex":
            executed_count = parse_executed_count(stdout_bytes)
            executed_count_source = "parsed"
            semantic_status = "pass" if exit_code == 0 and executed_count > 0 else "fail"
        else:
            parsed = parse_structured_result(
                result_format,
                structured_payload or b"",
            )
            executed_count = parsed.executed_count
            executed_count_source = parsed.executed_count_source
            semantic_status = parsed.semantic_status
        return CommandResult(
            command=command,
            exit_code=exit_code,
            stdout_sha256=hashlib.sha256(stdout_bytes).hexdigest(),
            artifact_path=artifact_relative.as_posix(),
            timed_out=timed_out,
            target_id=target_id,
            executed_count=executed_count,
            executed_count_source=executed_count_source,
            result_format=result_format,
            result_path=(
                structured_relative.as_posix()
                if structured_payload is not None
                else normalized_result_path
            ),
            semantic_status=semantic_status,
            no_network=True,
            sandbox_profile="no-network",
            sandbox_status="available",
            policy_status=policy_status,
            policy_reason=policy_reason,
        )


def _safe_result_path(result_path: str) -> str:
    if not result_path:
        return ""
    candidate = Path(result_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ExecutionPolicyError("result_path must be a project-relative path")
    return candidate.as_posix()


def target_policy_from_row(row: Mapping[str, object]) -> TargetExecutionPolicy:
    """Freeze the registered target fields used by one execution."""

    return TargetExecutionPolicy(
        id=str(row["id"]),
        command_template=str(row["command_template"]),
        result_format=str(row.get("result_format") or "regex"),
        result_path=str(row.get("result_path") or ""),
        requires_sandbox=bool(int(row.get("requires_sandbox") or 0)),
        requires_no_network=bool(int(row.get("requires_no_network") or 0)),
        container_image=str(row.get("container_image") or ""),
    )


def validate_execution_result(
    root: Path,
    target: TargetExecutionPolicy,
    result: CommandResult,
    *,
    runner: str,
) -> None:
    """Fail closed unless a result can become an immutable execution fact."""

    issues: list[str] = []
    if not command_matches_template(result.command, target.command_template):
        issues.append("command does not match registered target")
    if result.target_id != target.id:
        issues.append("execution target does not match registered target")
    if result.policy_status != "allowed":
        issues.append(f"policy_status={result.policy_status or 'empty'}")
    if result.exit_code != 0:
        issues.append(f"exit_code={result.exit_code}")
    if result.executed_count <= 0:
        issues.append(f"executed_count={result.executed_count}")
    if result.semantic_status != "pass":
        issues.append(f"semantic_status={result.semantic_status or 'empty'}")
    if result.result_format != target.result_format:
        issues.append("result format changed during execution")
    if target.result_format != "regex" and result.executed_count_source != "structured":
        issues.append(f"structured result is malformed or missing: {target.result_path or 'result_path'}")
    if target.requires_sandbox and (
        runner != "container" or result.sandbox_status != "available"
    ):
        issues.append("target requires an available container sandbox")
    if target.requires_no_network and (
        runner != "container"
        or result.sandbox_status != "available"
        or not result.no_network
    ):
        issues.append("target requires an available no-network container sandbox")
    try:
        artifact_relative = Path(_safe_result_path(result.artifact_path))
    except ExecutionPolicyError:
        issues.append("artifact path escapes project root")
    else:
        with ProjectFS.open(root) as project_fs:
            snapshot = project_fs._snapshot(
                artifact_relative,
                allow_missing=True,
            )
            if not snapshot.exists:
                issues.append("execution artifact is missing")
            elif hashlib.sha256(
                project_fs.read_bytes(artifact_relative)
            ).hexdigest() != result.stdout_sha256:
                issues.append("execution artifact digest mismatch")
    if issues:
        raise ExecutionPolicyError("verification failed: " + "; ".join(issues))

"""Controller-owned local and no-network container execution.

Execution facts are produced here, validated before persistence, and stored by
the root controller through the runtime API.  Callers never supply a claimed
exit code, digest, count, or sandbox status.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform as runtime_platform
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from xml.etree import ElementTree

from .project_fs import ProjectFS, _PathSnapshot
from .schema_guard import EXECUTION_POLICY_VERSION


DEFAULT_TIMEOUT_SECONDS = 120
MAX_STDOUT_BYTES = 1024 * 1024
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_CONTAINER_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_LOCAL_DOCKER_UNIX_ENDPOINT = re.compile(r"^unix:///[^\x00]+$")
_LOCAL_DOCKER_NPIPE_ENDPOINT = re.compile(
    r"^npipe:////\./pipe/[^\x00/\\]+$",
    flags=re.IGNORECASE,
)
_CONTAINER_ROUTING_ENV = (
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_TLS_VERIFY",
    "DOCKER_CERT_PATH",
    "CONTAINER_HOST",
    "CONTAINER_CONNECTION",
)

TARGET_DEFINITION_DIGEST_FIELDS = (
    "kind",
    "command_template",
    "stack_profile",
    "container_image",
    "requires_sandbox",
    "requires_no_network",
    "result_format",
    "result_path",
)


def latest_acceptance_target_qualification(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    acceptance_id: str,
    target_id: str,
) -> sqlite3.Row | None:
    """Return the one newest immutable qualification for an acceptance/target.

    Runtime timestamps historically had one-second precision. SQLite rowid is
    therefore the deterministic local tie-breaker for insert-only qualification
    rows created within the same second; the public ID never determines age.
    """

    return conn.execute(
        """
        select q.* from acceptance_target_qualifications q
        where q.cycle_id = ? and q.acceptance_id = ? and q.target_id = ?
        order by q.created_at desc, q.rowid desc
        limit 1
        """,
        (cycle_id, acceptance_id, target_id),
    ).fetchone()


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
    target_definition_sha256: str = ""
    platform: str = ""
    runtime_executable: str = ""
    runtime_version: str = ""
    runtime_executable_sha256: str = ""
    policy_version: str = ""
    container_engine: str = ""
    container_engine_version: str = ""
    container_engine_endpoint: str = ""
    container_image_requested: str = ""
    container_image_digest: str = ""
    provenance_status: str = "legacy-incomplete"


@dataclass(frozen=True)
class ControllerRuntimeProvenance:
    target_definition_sha256: str
    platform: str
    runtime_executable: str
    runtime_version: str
    runtime_executable_sha256: str
    policy_version: str


@dataclass(frozen=True)
class ContainerImageProvenance:
    engine: str
    engine_version: str
    requested_image: str
    image_digest: str
    engine_endpoint: str = ""


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
    target_definition_sha256: str = ""


class ExecutionPolicyError(ValueError):
    """Raised when execution output cannot become a trusted execution fact."""


def _regular_file_sha256(path: Path) -> str:
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise ExecutionPolicyError(
            f"runtime-executable-unavailable: not a regular file: {resolved}"
        )
    digest = hashlib.sha256()
    with resolved.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def controller_runtime_provenance(
    target_definition_sha256: str,
) -> ControllerRuntimeProvenance:
    """Capture the exact controller runtime before command execution."""

    executable = Path(sys.executable).expanduser().resolve(strict=True)
    return ControllerRuntimeProvenance(
        target_definition_sha256=str(target_definition_sha256).strip(),
        platform=f"{sys.platform}:{os.name}:{runtime_platform.machine()}",
        runtime_executable=str(executable),
        runtime_version=runtime_platform.python_version(),
        runtime_executable_sha256=_regular_file_sha256(executable),
        policy_version=EXECUTION_POLICY_VERSION,
    )


def _engine_command(
    engine: str,
    *args: str,
    endpoint: str = "",
) -> list[str]:
    if _container_engine_kind(engine) == "podman":
        return [engine, "--remote=false", *args]
    if endpoint:
        return [engine, "--host", endpoint, *args]
    return [engine, *args]


def _container_engine_kind(engine: str) -> str:
    name = str(engine).replace("\\", "/").rsplit("/", 1)[-1].lower()
    if name in {"docker", "docker.exe"}:
        return "docker"
    if name in {"podman", "podman.exe"}:
        return "podman"
    return ""


def _engine_version_command(engine: str, endpoint: str) -> list[str]:
    if _container_engine_kind(engine) == "podman":
        return _engine_command(
            engine,
            "version",
            "--format",
            "{{.Version}}",
            endpoint=endpoint,
        )
    return _engine_command(
        engine,
        "version",
        "--format",
        "{{.Server.Version}}",
        endpoint=endpoint,
    )


def _clean_container_engine_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in _CONTAINER_ROUTING_ENV:
        env.pop(key, None)
    return env


def _container_engine_env() -> dict[str, str]:
    return _clean_container_engine_env()


def _docker_context_endpoint(engine: str, context: str) -> str:
    try:
        result = subprocess.run(
            [engine, "context", "inspect", context],
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
            env=_clean_container_engine_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExecutionPolicyError(
            "container-engine-unavailable: cannot inspect Docker context: "
            f"{exc}"
        ) from exc
    if result.returncode != 0:
        detail = str(result.stderr or "").strip()
        raise ExecutionPolicyError(
            "container-engine-unavailable: cannot inspect Docker context "
            f"{context}" + (f": {detail}" if detail else "")
        )
    try:
        payload = json.loads(str(result.stdout or ""))
        record = payload[0] if isinstance(payload, list) and payload else None
        endpoints = record.get("Endpoints") if isinstance(record, dict) else None
        docker_endpoint = (
            endpoints.get("docker") if isinstance(endpoints, dict) else None
        )
        endpoint = (
            str(docker_endpoint.get("Host") or "").strip()
            if isinstance(docker_endpoint, dict)
            else ""
        )
    except (json.JSONDecodeError, TypeError) as exc:
        raise ExecutionPolicyError(
            "container-engine-unavailable: malformed Docker context inspection"
        ) from exc
    if not endpoint:
        raise ExecutionPolicyError(
            "container-engine-unavailable: Docker context has no daemon endpoint"
        )
    return endpoint


def _local_container_engine_endpoint(engine: str) -> str:
    engine_kind = _container_engine_kind(engine)
    if not engine_kind:
        raise ExecutionPolicyError(
            "container-engine-unavailable: only Docker or Podman is supported"
        )
    if engine_kind == "podman":
        if not sys.platform.startswith("linux"):
            raise ExecutionPolicyError(
                "container-engine-endpoint-unverified: Podman machine/remote "
                "routing is not eligible for local provenance on this platform"
            )
        remote_override = next(
            (
                key
                for key in ("CONTAINER_HOST", "CONTAINER_CONNECTION")
                if str(os.environ.get(key) or "").strip()
            ),
            "",
        )
        if remote_override:
            raise ExecutionPolicyError(
                "container-engine-non-local: Podman remote routing is not eligible "
                f"for local provenance ({remote_override})"
            )
        return "local-process"

    docker_host = str(os.environ.get("DOCKER_HOST") or "").strip()
    docker_context = str(os.environ.get("DOCKER_CONTEXT") or "").strip()
    if docker_host and docker_context:
        raise ExecutionPolicyError(
            "container-engine-routing-ambiguous: DOCKER_HOST and DOCKER_CONTEXT "
            "cannot both select container provenance"
        )
    if docker_context:
        endpoint = _docker_context_endpoint(engine, docker_context)
    elif docker_host:
        endpoint = docker_host
    else:
        try:
            result = subprocess.run(
                [engine, "context", "show"],
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
                env=_clean_container_engine_env(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ExecutionPolicyError(
                "container-engine-unavailable: cannot resolve active Docker context: "
                f"{exc}"
            ) from exc
        context = str(result.stdout or "").strip()
        if result.returncode != 0 or not context:
            detail = str(result.stderr or "").strip()
            raise ExecutionPolicyError(
                "container-engine-unavailable: cannot resolve active Docker context"
                + (f": {detail}" if detail else "")
            )
        endpoint = _docker_context_endpoint(engine, context)
    if _LOCAL_DOCKER_UNIX_ENDPOINT.fullmatch(endpoint):
        return endpoint
    if os.name == "nt" and _LOCAL_DOCKER_NPIPE_ENDPOINT.fullmatch(endpoint):
        return endpoint
    raise ExecutionPolicyError(
        "container-engine-non-local: Docker daemon endpoint must be a local "
        f"Unix socket or Windows named pipe; actual={endpoint}"
    )


def resolve_container_image_provenance(
    requested_image: str,
    *,
    expected_engine: str = "",
    expected_endpoint: str = "",
) -> ContainerImageProvenance:
    """Resolve one already-local image without pulling or mutating engine state."""

    requested = str(requested_image).strip()
    if not requested:
        raise ExecutionPolicyError("container-image-unavailable: image is required")
    discovered = shutil.which("docker") or shutil.which("podman")
    if not discovered:
        raise ExecutionPolicyError(
            "sandbox-unavailable: Docker or Podman is required for container verification"
        )
    engine = str(Path(discovered).expanduser().absolute())
    if expected_engine:
        expected = str(Path(expected_engine).expanduser().absolute())
        if engine != expected:
            raise ExecutionPolicyError(
                "container-engine-changed: "
                f"recorded={expected} current={engine}"
            )
    endpoint = _local_container_engine_endpoint(engine)
    if expected_endpoint and endpoint != expected_endpoint:
        raise ExecutionPolicyError(
            "container-engine-endpoint-changed: "
            f"recorded={expected_endpoint} current={endpoint}"
        )
    engine_env = _container_engine_env()
    try:
        version_result = subprocess.run(
            _engine_version_command(engine, endpoint),
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
            env=engine_env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExecutionPolicyError(
            f"container-engine-unavailable: cannot inspect engine version: {exc}"
        ) from exc
    engine_version = str(version_result.stdout or "").strip()
    if version_result.returncode != 0 or not engine_version:
        detail = str(version_result.stderr or "").strip()
        raise ExecutionPolicyError(
            "container-engine-unavailable: cannot inspect engine version"
            + (f": {detail}" if detail else "")
        )
    try:
        inspect_result = subprocess.run(
            _engine_command(
                engine,
                "image",
                "inspect",
                requested,
                endpoint=endpoint,
            ),
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
            env=engine_env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExecutionPolicyError(
            f"container-image-unavailable: cannot inspect local image {requested}: {exc}"
        ) from exc
    if inspect_result.returncode != 0:
        detail = str(inspect_result.stderr or "").strip()
        raise ExecutionPolicyError(
            f"container-image-unavailable: local image not found: {requested}"
            + (f": {detail}" if detail else "")
        )
    try:
        payload = json.loads(str(inspect_result.stdout or ""))
        record = payload[0] if isinstance(payload, list) and payload else None
    except (json.JSONDecodeError, TypeError) as exc:
        raise ExecutionPolicyError(
            f"container-image-unavailable: malformed image inspection for {requested}"
        ) from exc
    if not isinstance(record, dict):
        raise ExecutionPolicyError(
            f"container-image-unavailable: malformed image inspection for {requested}"
        )
    image_digest = str(record.get("Id") or "").strip().lower()
    if _SHA256_HEX.fullmatch(image_digest):
        image_digest = f"sha256:{image_digest}"
    if not _CONTAINER_IMAGE_DIGEST.fullmatch(image_digest):
        for repo_digest in record.get("RepoDigests") or []:
            candidate = str(repo_digest).rsplit("@", 1)[-1].strip().lower()
            if _CONTAINER_IMAGE_DIGEST.fullmatch(candidate):
                image_digest = candidate
                break
    if not _CONTAINER_IMAGE_DIGEST.fullmatch(image_digest):
        raise ExecutionPolicyError(
            f"container-image-unavailable: no immutable local identity for {requested}"
        )
    return ContainerImageProvenance(
        engine=engine,
        engine_version=engine_version,
        requested_image=requested,
        image_digest=image_digest,
        engine_endpoint=endpoint,
    )


def target_definition_digest(target: Mapping[str, object]) -> str:
    """Return the stable digest of execution-relevant target policy fields.

    Identity, timestamps, descriptions, and presentation fields are excluded so
    they cannot create false staleness.  Every field capable of changing what is
    run or how its result is interpreted is included.
    """

    payload = {
        field: target[field]
        for field in TARGET_DEFINITION_DIGEST_FIELDS
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
            active_tests: set[tuple[str, str]] = set()
            packages_with_tests: set[str] = set()
            passed_count = 0
            failed_count = 0
            failed_packages_from_tests: set[str] = set()
            package_terminals: dict[str, str] = {}
            for event in events:
                action = event.get("Action")
                package = str(event.get("Package") or "").strip()
                test_name = str(event.get("Test") or "").strip()
                if test_name:
                    if not package:
                        raise ValueError(
                            "malformed go-json: test event is missing Package"
                        )
                    if package in package_terminals:
                        raise ValueError(
                            "malformed go-json: event order places test after "
                            f"terminal package event for {package}"
                        )
                    key = (package, test_name)
                    if action == "run":
                        if key in active_tests:
                            raise ValueError(
                                "malformed go-json: duplicate run event before "
                                f"terminal outcome for {package}:{test_name}"
                            )
                        active_tests.add(key)
                        packages_with_tests.add(package)
                        continue
                    if action not in {"pass", "fail", "skip"}:
                        continue
                    if key not in active_tests:
                        raise ValueError(
                            "malformed go-json: terminal test outcome has no "
                            "preceding run event; event order is invalid for "
                            f"{package}:{test_name}"
                        )
                    active_tests.remove(key)
                    if action == "pass":
                        passed_count += 1
                    elif action == "fail":
                        failed_count += 1
                        failed_packages_from_tests.add(package)
                    continue
                if package and action in {"pass", "fail"}:
                    if package in package_terminals:
                        raise ValueError(
                            "malformed go-json: duplicate terminal package event "
                            f"for {package}"
                        )
                    if any(key[0] == package for key in active_tests):
                        raise ValueError(
                            "malformed go-json: event order closes package before "
                            f"active tests have terminal outcomes for {package}"
                        )
                    package_terminals[package] = str(action)
            if active_tests:
                raise ValueError(
                    "malformed go-json: started test is missing terminal outcome"
                )
            if passed_count + failed_count <= 0:
                raise ValueError(
                    "malformed go-json: no executed terminal test outcomes"
                )
            missing_packages = sorted(
                packages_with_tests - set(package_terminals)
            )
            if missing_packages:
                raise ValueError(
                    "malformed go-json: missing terminal package event for "
                    + ", ".join(missing_packages)
                )
            contradictory_packages = sorted(
                package
                for package in failed_packages_from_tests
                if package_terminals.get(package) == "pass"
            )
            if contradictory_packages:
                raise ValueError(
                    "malformed go-json: passing package terminal contradicts "
                    "failed test outcome"
                )
            failed = failed_count > 0 or any(
                action == "fail" for action in package_terminals.values()
            )
            return _structured_pass(
                passed_count + failed_count,
                1 if failed else 0,
                "go test failed",
            )
        if result_format == "cargo-nextest-json":
            events = _json_lines(payload, result_format)
            current: dict[str, object] | None = None
            suite_count = 0
            total_executed = 0
            total_failed = 0
            for event in events:
                event_type = event.get("type")
                event_status = event.get("event")
                if event_type == "suite" and event_status == "started":
                    if current is not None:
                        raise ValueError(
                            f"malformed {result_format}: event order starts a new "
                            "suite before the prior suite terminal"
                        )
                    current = {
                        "test_count": _json_count(
                            event,
                            "test_count",
                            result_format,
                        ),
                        "active": set(),
                        "outcomes": {},
                        "measured": set(),
                    }
                    continue
                if event_type == "suite" and event_status in {"ok", "failed"}:
                    if current is None:
                        raise ValueError(
                            f"malformed {result_format}: event order has terminal "
                            "suite before suite started"
                        )
                    active = current["active"]
                    assert isinstance(active, set)
                    if active:
                        raise ValueError(
                            f"malformed {result_format}: event order closes suite "
                            "with active tests missing terminal outcomes"
                        )
                    outcomes = current["outcomes"]
                    measured = current["measured"]
                    assert isinstance(outcomes, dict)
                    assert isinstance(measured, set)
                    comparisons = {
                        "passed": sum(
                            1 for outcome in outcomes.values() if outcome == "passed"
                        ),
                        "failed": sum(
                            1 for outcome in outcomes.values() if outcome == "failed"
                        ),
                        "ignored": sum(
                            1 for outcome in outcomes.values() if outcome == "ignored"
                        ),
                        "measured": len(measured),
                    }
                    for key, actual in comparisons.items():
                        if key not in event:
                            raise ValueError(
                                f"malformed {result_format}: terminal suite event "
                                f"is missing {key}"
                            )
                        if _json_count(event, key, result_format) != actual:
                            raise ValueError(
                                f"malformed {result_format}: terminal {key} "
                                "contradicts test outcomes"
                            )
                    if "filtered_out" not in event:
                        raise ValueError(
                            f"malformed {result_format}: terminal suite event "
                            "is missing filtered_out"
                        )
                    _json_count(event, "filtered_out", result_format)
                    reconciled_count = sum(comparisons.values())
                    if current["test_count"] != reconciled_count:
                        raise ValueError(
                            f"malformed {result_format}: suite test_count "
                            "contradicts terminal outcomes"
                        )
                    expected_terminal = (
                        "failed" if comparisons["failed"] else "ok"
                    )
                    if event_status != expected_terminal:
                        raise ValueError(
                            f"malformed {result_format}: suite terminal status "
                            "contradicts failures"
                        )
                    total_executed += (
                        comparisons["passed"]
                        + comparisons["failed"]
                        + comparisons["measured"]
                    )
                    total_failed += comparisons["failed"]
                    suite_count += 1
                    current = None
                    continue
                if event_type not in {"test", "bench"}:
                    continue
                if current is None:
                    raise ValueError(
                        f"malformed {result_format}: event order has test outside "
                        "an active suite"
                    )
                name_value = event.get("name")
                if not name_value:
                    raise ValueError(
                        f"malformed {result_format}: test event is missing name"
                    )
                name = str(name_value)
                active = current["active"]
                outcomes = current["outcomes"]
                measured = current["measured"]
                assert isinstance(active, set)
                assert isinstance(outcomes, dict)
                assert isinstance(measured, set)
                if event_type == "test" and event_status == "started":
                    if name in active or name in outcomes or name in measured:
                        raise ValueError(
                            f"malformed {result_format}: duplicate test started "
                            f"event for {name}"
                        )
                    active.add(name)
                    continue
                if event_type == "test" and event_status in {
                    "ok",
                    "failed",
                    "ignored",
                }:
                    if name not in active:
                        raise ValueError(
                            f"malformed {result_format}: terminal test outcome "
                            "has no preceding started event; event order is "
                            f"invalid for {name}"
                        )
                    active.remove(name)
                    outcomes[name] = (
                        "passed" if event_status == "ok" else str(event_status)
                    )
                    continue
                if event_type == "bench":
                    if name not in active:
                        raise ValueError(
                            f"malformed {result_format}: benchmark outcome has "
                            "no preceding started event"
                        )
                    active.remove(name)
                    measured.add(name)
            if current is not None:
                raise ValueError(
                    f"malformed {result_format}: started suite is missing "
                    "terminal suite event"
                )
            if suite_count <= 0:
                raise ValueError(
                    f"malformed {result_format}: no complete suite events"
                )
            return _structured_pass(
                total_executed,
                total_failed,
                "nextest failed",
            )
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
        target_definition_sha256: str = "",
    ) -> CommandResult:
        if not command.strip():
            raise ValueError("command is required")
        runtime_provenance = controller_runtime_provenance(
            target_definition_sha256
        )
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
                runtime_provenance=runtime_provenance,
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
        stdout_truncated = len(stdout) > self.max_stdout_bytes
        stdout = stdout[: self.max_stdout_bytes]
        semantic_status = ""
        structured_payload_for_artifact: bytes | None = None
        if result_format != "regex":
            if stdout_truncated and not normalized_result_path:
                raise ExecutionPolicyError(
                    "structured-result-truncated: captured stdout exceeded "
                    f"{self.max_stdout_bytes} bytes"
                )
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
            runtime_provenance=runtime_provenance,
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
        runtime_provenance: ControllerRuntimeProvenance,
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
            target_definition_sha256=(
                runtime_provenance.target_definition_sha256
            ),
            platform=runtime_provenance.platform,
            runtime_executable=runtime_provenance.runtime_executable,
            runtime_version=runtime_provenance.runtime_version,
            runtime_executable_sha256=(
                runtime_provenance.runtime_executable_sha256
            ),
            policy_version=runtime_provenance.policy_version,
            provenance_status=(
                "complete"
                if _SHA256_HEX.fullmatch(
                    runtime_provenance.target_definition_sha256
                )
                else "legacy-incomplete"
            ),
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
        target_definition_sha256: str = "",
    ) -> CommandResult:
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
        runtime_provenance = controller_runtime_provenance(
            target_definition_sha256
        )
        container_provenance = resolve_container_image_provenance(
            container_image
        )
        engine = container_provenance.engine
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
                "; rm -f -- /artifacts/structured-result; "
                f"if [ -f {quoted_result} ]; then "
                f"cp -a {quoted_result} /artifacts/structured-result; fi"
            )
        else:
            result_copy = (
                "; if [ -e /artifacts/structured-result ] "
                "|| [ -L /artifacts/structured-result ]; then exit 125; fi"
            )
        script = (
            "set -eu; "
            "rm -rf /workspace/* /workspace/.[!.]* /workspace/..?* 2>/dev/null || true; "
            "cp -a /src/. /workspace/; "
            f"cd /workspace; {result_reset}"
            "set +e; "
            f"({command}) > /artifacts/stdout.txt 2>&1; "
            "rc=$?; set -e"
            f"{result_copy}; exit \"$rc\""
        )
        timed_out = False
        structured_payload: bytes | None = None
        with tempfile.TemporaryDirectory(
            prefix="kafa-container-artifacts-"
        ) as artifact_temp:
            artifact_directory = Path(artifact_temp).resolve()
            argv = _engine_command(
                engine,
                "run",
                "--rm",
                "--pull=never",
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
                "--entrypoint",
                "/bin/sh",
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
                container_provenance.image_digest,
                "-lc",
                script,
                endpoint=container_provenance.engine_endpoint,
            )
            try:
                completed = subprocess.run(
                    argv,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=timeout,
                    env=_container_engine_env(),
                )
                exit_code = completed.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
                exit_code = 124
                subprocess.run(
                    _engine_command(
                        engine,
                        "rm",
                        "-f",
                        container_name,
                        endpoint=container_provenance.engine_endpoint,
                    ),
                    text=True,
                    capture_output=True,
                    check=False,
                    env=_container_engine_env(),
                )

            with ProjectFS.open(artifact_directory) as artifact_fs:
                stdout_snapshot = artifact_fs._snapshot(
                    Path("stdout.txt"),
                    allow_missing=True,
                )
                if not stdout_snapshot.exists:
                    raise ExecutionPolicyError(
                        "container-execution-artifact-missing: controlled "
                        "entrypoint did not create /artifacts/stdout.txt"
                    )
                complete_stdout = artifact_fs.read_bytes(
                    Path("stdout.txt"),
                    expected=stdout_snapshot,
                )
                structured_snapshot = artifact_fs._snapshot(
                    Path("structured-result"),
                    allow_missing=True,
                )
                if structured_snapshot.exists:
                    if not normalized_result_path:
                        raise ExecutionPolicyError(
                            "container-structured-artifact-unexpected: target "
                            "created an undeclared structured-result artifact"
                        )
                    structured_payload = artifact_fs.read_bytes(
                        Path("structured-result"),
                        expected=structured_snapshot,
                    )

            stdout_truncated = len(complete_stdout) > self.max_stdout_bytes
            stdout_bytes = complete_stdout[: self.max_stdout_bytes]
            if (
                result_format != "regex"
                and structured_payload is None
                and not normalized_result_path
                and stdout_truncated
            ):
                raise ExecutionPolicyError(
                    "structured-result-truncated: captured container stdout "
                    f"exceeded {self.max_stdout_bytes} bytes"
                )
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
                (
                    structured_payload
                    if structured_payload is not None
                    else (b"" if normalized_result_path else stdout_bytes)
                ),
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
            target_definition_sha256=(
                runtime_provenance.target_definition_sha256
            ),
            platform=runtime_provenance.platform,
            runtime_executable=runtime_provenance.runtime_executable,
            runtime_version=runtime_provenance.runtime_version,
            runtime_executable_sha256=(
                runtime_provenance.runtime_executable_sha256
            ),
            policy_version=runtime_provenance.policy_version,
            container_engine=container_provenance.engine,
            container_engine_version=container_provenance.engine_version,
            container_engine_endpoint=container_provenance.engine_endpoint,
            container_image_requested=container_provenance.requested_image,
            container_image_digest=container_provenance.image_digest,
            provenance_status=(
                "complete"
                if _SHA256_HEX.fullmatch(
                    runtime_provenance.target_definition_sha256
                )
                else "legacy-incomplete"
            ),
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
        target_definition_sha256=target_definition_digest(row),
    )


def recorded_execution_provenance_issues(
    execution: Mapping[str, object] | sqlite3.Row,
) -> list[str]:
    """Validate persisted provenance without consulting mutable host state."""

    def value(field: str) -> str:
        try:
            raw = execution[field]
        except (KeyError, IndexError):
            return ""
        return str(raw or "").strip()

    issues: list[str] = []
    status = value("provenance_status")
    if status != "complete":
        issues.append(
            "execution provenance_status must be complete; "
            f"actual={status or 'empty'}"
        )
    target_digest = value("target_definition_sha256")
    if not _SHA256_HEX.fullmatch(target_digest):
        issues.append("execution provenance target_definition_sha256 is missing or invalid")
    runtime_digest = value("runtime_executable_sha256")
    if not _SHA256_HEX.fullmatch(runtime_digest):
        issues.append(
            "execution provenance runtime_executable_sha256 is missing or invalid"
        )
    for field in (
        "platform",
        "runtime_executable",
        "runtime_version",
        "policy_version",
    ):
        if not value(field):
            issues.append(f"execution provenance {field} is missing")
    if value("policy_version") and value("policy_version") != EXECUTION_POLICY_VERSION:
        issues.append(
            "execution provenance policy_version is stale: "
            f"recorded={value('policy_version')} current={EXECUTION_POLICY_VERSION}"
        )
    runner = value("runner")
    container_fields = (
        "container_engine",
        "container_engine_version",
        "container_engine_endpoint",
        "container_image_requested",
        "container_image_digest",
    )
    if runner == "local":
        for field in container_fields:
            if value(field):
                issues.append(
                    f"local execution provenance {field} must be empty"
                )
    elif runner == "container":
        for field in container_fields[:-1]:
            if not value(field):
                issues.append(f"container execution provenance {field} is missing")
        engine = value("container_engine")
        endpoint = value("container_engine_endpoint")
        engine_kind = _container_engine_kind(engine)
        engine_has_path = "/" in engine or "\\" in engine
        endpoint_matches_engine = engine_has_path and (
            (engine_kind == "podman" and endpoint == "local-process")
            or (
                engine_kind == "docker"
                and bool(
                    _LOCAL_DOCKER_UNIX_ENDPOINT.fullmatch(endpoint)
                    or _LOCAL_DOCKER_NPIPE_ENDPOINT.fullmatch(endpoint)
                )
            )
        )
        if engine and endpoint and not endpoint_matches_engine:
            issues.append(
                "container execution provenance engine/endpoint pair is "
                f"unsupported: engine={engine} endpoint={endpoint}"
            )
        if not _CONTAINER_IMAGE_DIGEST.fullmatch(
            value("container_image_digest")
        ):
            issues.append(
                "container execution provenance container_image_digest is missing or invalid"
            )
    return issues


def _command_result_provenance_issues(
    target: TargetExecutionPolicy,
    result: CommandResult,
    *,
    runner: str,
) -> list[str]:
    if not target.target_definition_sha256:
        return []
    issues = recorded_execution_provenance_issues(
        {
            "target_definition_sha256": result.target_definition_sha256,
            "platform": result.platform,
            "runtime_executable": result.runtime_executable,
            "runtime_version": result.runtime_version,
            "runtime_executable_sha256": result.runtime_executable_sha256,
            "policy_version": result.policy_version,
            "container_engine": result.container_engine,
            "container_engine_version": result.container_engine_version,
            "container_engine_endpoint": result.container_engine_endpoint,
            "container_image_requested": result.container_image_requested,
            "container_image_digest": result.container_image_digest,
            "provenance_status": result.provenance_status,
            "runner": runner,
        }
    )
    if result.target_definition_sha256 != target.target_definition_sha256:
        issues.append(
            "execution provenance target definition changed during execution"
        )
    if issues:
        return issues

    try:
        current_runtime = controller_runtime_provenance(
            target.target_definition_sha256
        )
    except (OSError, ExecutionPolicyError) as exc:
        return [f"stale runtime provenance: {exc}"]
    for field in (
        "target_definition_sha256",
        "platform",
        "runtime_executable",
        "runtime_version",
        "runtime_executable_sha256",
        "policy_version",
    ):
        if getattr(result, field) != getattr(current_runtime, field):
            issues.append(
                f"stale runtime provenance: {field} changed before commit"
            )
    if runner == "container" and not issues:
        try:
            current_container = resolve_container_image_provenance(
                result.container_image_requested,
                expected_engine=result.container_engine,
                expected_endpoint=result.container_engine_endpoint,
            )
        except ExecutionPolicyError as exc:
            issues.append(f"stale container provenance: {exc}")
        else:
            comparisons = (
                ("container_engine", result.container_engine, current_container.engine),
                (
                    "container_engine_version",
                    result.container_engine_version,
                    current_container.engine_version,
                ),
                (
                    "container_engine_endpoint",
                    result.container_engine_endpoint,
                    current_container.engine_endpoint,
                ),
                (
                    "container_image_requested",
                    result.container_image_requested,
                    current_container.requested_image,
                ),
                (
                    "container_image_digest",
                    result.container_image_digest,
                    current_container.image_digest,
                ),
            )
            for field, recorded, current in comparisons:
                if recorded != current:
                    issues.append(
                        f"stale container provenance: {field} changed before commit"
                    )
    return issues


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
    issues.extend(
        _command_result_provenance_issues(
            target,
            result,
            runner=runner,
        )
    )
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

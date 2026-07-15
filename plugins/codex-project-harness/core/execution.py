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
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from xml.etree import ElementTree

from .project_fs import ProjectFS


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
    patterns = [
        r"Ran\s+(\d+)\s+tests?",
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
        if isinstance(value, dict):
            rows.append(value)
    if not rows:
        raise ValueError(f"malformed {result_format}: no events")
    return rows


def _structured_pass(count: int, failed: int = 0, reason: str = "") -> StructuredResult:
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
            suites = [root] if root.tag.endswith("testsuite") else list(root.findall(".//testsuite"))
            if not suites:
                suites = [root]
            tests = sum(int(float(suite.attrib.get("tests", "0") or "0")) for suite in suites)
            failures = sum(int(float(suite.attrib.get("failures", "0") or "0")) for suite in suites)
            errors = sum(int(float(suite.attrib.get("errors", "0") or "0")) for suite in suites)
            if tests <= 0:
                tests = len(root.findall(".//testcase"))
            return _structured_pass(tests, failures + errors, f"failures={failures} errors={errors}")
        if result_format == "pytest-json":
            data = _json_loads(payload, result_format)
            if not isinstance(data, dict) or not isinstance(data.get("summary"), dict):
                raise ValueError("malformed pytest-json: missing summary")
            summary = data["summary"]
            total = int(summary.get("total") or 0)
            passed = int(summary.get("passed") or 0)
            failed = int(summary.get("failed") or 0) + int(summary.get("errors") or 0)
            return _structured_pass(total or passed, failed, f"failed={failed}")
        if result_format == "jest-json":
            data = _json_loads(payload, result_format)
            if not isinstance(data, dict):
                raise ValueError("malformed jest-json: expected object")
            total = int(data.get("numTotalTests") or 0)
            failed = int(data.get("numFailedTests") or 0)
            success = bool(data.get("success")) if "success" in data else failed == 0
            return _structured_pass(total, 0 if success and failed == 0 else max(1, failed), f"failed={failed}")
        if result_format == "go-json":
            events = _json_lines(payload, result_format)
            passed_tests = {str(event["Test"]) for event in events if event.get("Action") == "pass" and event.get("Test")}
            failed = any(event.get("Action") == "fail" for event in events)
            return _structured_pass(len(passed_tests), 1 if failed else 0, "go test failed")
        if result_format == "cargo-nextest-json":
            events = _json_lines(payload, result_format)
            passed = {
                str(event.get("name") or event.get("test") or event.get("test_name"))
                for event in events
                if event.get("event") in {"passed", "ok"} and (event.get("name") or event.get("test") or event.get("test_name"))
            }
            failed = any(event.get("event") in {"failed", "failure"} for event in events)
            for event in events:
                if event.get("event") == "finished" and int(event.get("failed") or 0) > 0:
                    failed = True
                if event.get("event") == "finished" and not passed and int(event.get("test_count") or 0) > 0 and int(event.get("failed") or 0) == 0:
                    return _structured_pass(int(event.get("test_count") or 0), 0)
            return _structured_pass(len(passed), 1 if failed else 0, "nextest failed")
        if result_format == "playwright-json":
            data = _json_loads(payload, result_format)
            if not isinstance(data, dict):
                raise ValueError("malformed playwright-json: expected object")
            stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
            expected = int(stats.get("expected") or stats.get("passed") or 0)
            unexpected = int(stats.get("unexpected") or stats.get("failed") or 0)
            status = str(data.get("status") or "")
            if status and status not in {"passed", "ok"}:
                unexpected = max(unexpected, 1)
            return _structured_pass(expected, unexpected, f"unexpected={unexpected}")
    except ElementTree.ParseError as exc:
        return StructuredResult("fail", 0, reason=f"malformed {result_format}: {exc}")
    except (TypeError, ValueError) as exc:
        return StructuredResult("fail", 0, reason=str(exc))
    return StructuredResult("fail", 0, reason=f"unknown result format: {result_format}")


def minimal_env() -> dict[str, str]:
    keep = ["PATH", "HOME", "TMPDIR", "LANG", "LC_ALL"]
    return {key: os.environ[key] for key in keep if key in os.environ}


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
        with ProjectFS.open(self.root) as project_fs:
            project_fs.audit(
                (artifact_relative, structured_relative),
                allow_missing=True,
            )
            if normalized_result_path:
                project_fs.audit(
                    (Path(normalized_result_path),),
                    allow_missing=True,
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
                with ProjectFS.open(self.root) as project_fs:
                    source_relative = Path(normalized_result_path)
                    snapshot = project_fs._snapshot(
                        source_relative,
                        allow_missing=True,
                    )
                    if snapshot.exists:
                        structured_payload = project_fs.read_bytes(source_relative)
                        structured_payload_for_artifact = structured_payload
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
            )
        stored_result_path = result_path
        if result_format != "regex" and structured_payload is not None:
            with ProjectFS.open(self.root) as project_fs:
                project_fs.atomic_write(
                    structured_relative,
                    structured_payload,
                    mode=0o600,
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
            artifact_directory = project_fs.ensure_directory(
                artifact_relative.parent
            )
        container_name = f"kafa-verify-{execution_id[:12]}"
        result_copy = ""
        if normalized_result_path:
            quoted_result = shlex.quote(normalized_result_path)
            result_copy = (
                f"; if [ -f {quoted_result} ]; then "
                f"cp {quoted_result} /artifacts/structured-result; fi"
            )
        script = (
            "rm -rf /workspace/* /workspace/.[!.]* /workspace/..?* 2>/dev/null || true; "
            "cp -a /src/. /workspace/; "
            f"cd /workspace && ({command}) > /artifacts/stdout.txt 2>&1; "
            f"rc=$?{result_copy}; exit $rc"
        )
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
        timed_out = False
        try:
            completed = subprocess.run(
                argv,
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout,
            )
            exit_code = completed.returncode
            with ProjectFS.open(self.root) as project_fs:
                if not project_fs._snapshot(
                    artifact_relative,
                    allow_missing=True,
                ).exists:
                    project_fs.atomic_write(
                        artifact_relative,
                        ((completed.stdout or "") + (completed.stderr or "")).encode(
                            "utf-8"
                        ),
                        mode=0o600,
                    )
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
            stdout_bytes = stdout if isinstance(stdout, bytes) else stdout.encode("utf-8")
            stderr_bytes = stderr if isinstance(stderr, bytes) else stderr.encode("utf-8")
            with ProjectFS.open(self.root) as project_fs:
                project_fs.atomic_write(
                    artifact_relative,
                    (stdout_bytes + stderr_bytes)[: self.max_stdout_bytes],
                    mode=0o600,
                )
        with ProjectFS.open(self.root) as project_fs:
            complete_stdout = project_fs.read_bytes(artifact_relative)
            stdout_bytes = complete_stdout[: self.max_stdout_bytes]
            if len(complete_stdout) != len(stdout_bytes):
                project_fs.atomic_write(
                    artifact_relative,
                    stdout_bytes,
                    mode=0o600,
                )
            structured_snapshot = project_fs._snapshot(
                structured_relative,
                allow_missing=True,
            )
            structured_payload = (
                project_fs.read_bytes(structured_relative)
                if structured_snapshot.exists
                else None
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

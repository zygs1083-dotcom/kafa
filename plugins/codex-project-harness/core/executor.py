"""Local command executor for trusted runtime evidence."""

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
from typing import Protocol
from xml.etree import ElementTree


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


class Executor(Protocol):
    def run(self, command: str, *, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> CommandResult:
        """Run a command and return durable evidence metadata."""


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
                result_path=result_path,
                semantic_status="fail" if result_format != "regex" else "",
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
        if result_format != "regex":
            structured_payload = stdout
            if result_path:
                candidate = (self.root / result_path).resolve()
                try:
                    candidate.relative_to(self.root)
                except ValueError:
                    candidate = self.root / "__missing_structured_result__"
                if candidate.exists() and candidate.is_file():
                    structured_payload = candidate.read_bytes()
                else:
                    structured_payload = b""
            parsed = parse_structured_result(result_format, structured_payload)
            count = parsed.executed_count
            count_source = parsed.executed_count_source
            semantic_status = parsed.semantic_status
        else:
            count_source = "manual" if executed_count is not None else "parsed"
            count = int(executed_count) if executed_count is not None else parse_executed_count(stdout)
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
            result_path=result_path,
            semantic_status=semantic_status,
            structured_source_path=result_path,
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
        structured_source_path: str = "",
    ) -> CommandResult:
        execution_id = uuid.uuid4().hex
        artifact = self.root / ".ai-team" / "runtime" / "executions" / execution_id / "stdout.txt"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(stdout)
        stored_result_path = result_path
        if result_format != "regex" and structured_source_path:
            source = (self.root / structured_source_path).resolve()
            try:
                source.relative_to(self.root)
            except ValueError:
                source = self.root / "__missing_structured_result__"
            if source.exists() and source.is_file():
                structured_artifact = artifact.parent / "structured-result"
                shutil.copyfile(source, structured_artifact)
                stored_result_path = structured_artifact.relative_to(self.root).as_posix()
        return CommandResult(
            command=command,
            exit_code=exit_code,
            stdout_sha256=hashlib.sha256(stdout).hexdigest(),
            artifact_path=artifact.relative_to(self.root).as_posix(),
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

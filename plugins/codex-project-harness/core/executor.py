"""Local command executor for trusted runtime evidence."""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


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
    allow_unlisted: bool = False
    no_network: bool = False
    sandbox_profile: str = "none"
    sandbox_status: str = ""
    allow_unlisted_reason: str = ""
    policy_status: str = "allowed"
    policy_reason: str = ""


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
    ) -> CommandResult:
        execution_id = uuid.uuid4().hex
        artifact = self.root / ".ai-team" / "runtime" / "executions" / execution_id / "stdout.txt"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(stdout)
        return CommandResult(
            command=command,
            exit_code=exit_code,
            stdout_sha256=hashlib.sha256(stdout).hexdigest(),
            artifact_path=artifact.relative_to(self.root).as_posix(),
            timed_out=timed_out,
            target_id=target_id,
            executed_count=executed_count,
            executed_count_source=executed_count_source,
            allow_unlisted=allow_unlisted,
            no_network=no_network,
            sandbox_profile=sandbox_profile,
            sandbox_status=sandbox_status,
            allow_unlisted_reason=allow_unlisted_reason,
            policy_status=policy_status,
            policy_reason=policy_reason,
        )

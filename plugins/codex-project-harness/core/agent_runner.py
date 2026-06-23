"""Agent runner abstractions for dispatch execution."""

from __future__ import annotations

import hashlib
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
import shutil
from typing import Any
from typing import Protocol

from core.executor import CommandResult, LocalExecutor, MAX_STDOUT_BYTES, parse_executed_count


@dataclass(frozen=True)
class RunnerRequest:
    root: Path
    work_dir: Path
    command: str
    timeout: int = 120
    target_id: str = ""
    target_command_template: str = ""
    allowed_prefixes: list[str] = field(default_factory=list)
    allow_unlisted: bool = False
    no_network: bool = False
    sandbox_profile: str = "none"
    allow_unlisted_reason: str = ""
    executed_count: int | None = None
    container_image: str = "python:3.12-slim"


@dataclass(frozen=True)
class RunnerResult:
    evidence: CommandResult
    work_dir: Path
    runner: str
    sandbox_execution: dict[str, Any] = field(default_factory=dict)


class AgentRunner(Protocol):
    name: str

    def run(self, request: RunnerRequest) -> RunnerResult:
        """Run an agent command and return executor-compatible evidence."""


class NullRunner:
    name = "null"

    def run(self, request: RunnerRequest) -> RunnerResult:
        result = LocalExecutor(request.root).run(
            request.command,
            timeout=request.timeout,
            target_id=request.target_id,
            target_command_template=request.target_command_template,
            allowed_prefixes=request.allowed_prefixes,
            allow_unlisted=request.allow_unlisted,
            no_network=request.no_network,
            sandbox_profile=request.sandbox_profile,
            allow_unlisted_reason=request.allow_unlisted_reason,
            executed_count=request.executed_count,
        )
        return RunnerResult(evidence=result, work_dir=request.root, runner=self.name)


class LocalProcessRunner:
    name = "local-process"

    def run(self, request: RunnerRequest) -> RunnerResult:
        result = LocalExecutor(request.work_dir).run(
            request.command,
            timeout=request.timeout,
            target_id=request.target_id,
            target_command_template=request.target_command_template,
            allowed_prefixes=request.allowed_prefixes,
            allow_unlisted=request.allow_unlisted,
            no_network=request.no_network,
            sandbox_profile=request.sandbox_profile,
            allow_unlisted_reason=request.allow_unlisted_reason,
            executed_count=request.executed_count,
        )
        return RunnerResult(evidence=result, work_dir=request.work_dir, runner=self.name)


class ContainerRunner:
    name = "container"

    def run(self, request: RunnerRequest) -> RunnerResult:
        engine = shutil.which("docker") or shutil.which("podman")
        if not engine:
            raise RuntimeError("sandbox-unavailable: Docker or Podman is required for container runner")
        policy_status, policy_reason = LocalExecutor(request.work_dir)._policy(
            request.command,
            request.target_id,
            request.target_command_template,
            request.allowed_prefixes,
            request.allow_unlisted,
            request.allow_unlisted_reason,
        )
        artifact = request.root / ".ai-team" / "runtime" / "container-executions" / uuid.uuid4().hex / "stdout.txt"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        if policy_status == "rejected":
            stdout = f"command rejected by policy: {policy_reason}\n".encode("utf-8")
            artifact.write_bytes(stdout)
            result = CommandResult(
                command=request.command,
                exit_code=126,
                stdout_sha256=hashlib.sha256(stdout).hexdigest(),
                artifact_path=artifact.relative_to(request.root).as_posix(),
                target_id=request.target_id,
                executed_count=0,
                executed_count_source="policy",
                allow_unlisted=request.allow_unlisted,
                no_network=True,
                sandbox_profile="no-network",
                sandbox_status="available",
                allow_unlisted_reason=request.allow_unlisted_reason,
                policy_status=policy_status,
                policy_reason=policy_reason,
            )
            return RunnerResult(
                evidence=result,
                work_dir=request.work_dir,
                runner=self.name,
                sandbox_execution={
                    "engine": Path(engine).name,
                    "image": request.container_image,
                    "network": "none",
                    "cpus": "1",
                    "memory": "512m",
                    "pids_limit": "256",
                },
            )

        container_name = f"codex-harness-{uuid.uuid4().hex[:12]}"
        script = f"cd /workspace && {request.command} > /artifacts/stdout.txt 2>&1"
        command = [
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
            "-v",
            f"{request.work_dir.resolve()}:/workspace:ro",
            "-v",
            f"{artifact.parent.resolve()}:/artifacts:rw",
            "-w",
            "/workspace",
            request.container_image,
            "/bin/sh",
            "-lc",
            script,
        ]
        timed_out = False
        try:
            completed = subprocess.run(command, text=True, capture_output=True, check=False, timeout=request.timeout)
            exit_code = completed.returncode
            if not artifact.exists():
                artifact.write_text((completed.stdout or "") + (completed.stderr or ""), encoding="utf-8")
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = 124
            subprocess.run([engine, "rm", "-f", container_name], text=True, capture_output=True, check=False)
            stdout = (exc.stdout or b"") if isinstance(exc.stdout, bytes) else (exc.stdout or "").encode("utf-8")
            stderr = (exc.stderr or b"") if isinstance(exc.stderr, bytes) else (exc.stderr or "").encode("utf-8")
            artifact.write_bytes((stdout + stderr)[:MAX_STDOUT_BYTES])
        stdout_bytes = artifact.read_bytes()[:MAX_STDOUT_BYTES]
        if len(stdout_bytes) != artifact.stat().st_size:
            artifact.write_bytes(stdout_bytes)
        count_source = "manual" if request.executed_count is not None else "parsed"
        count = int(request.executed_count) if request.executed_count is not None else parse_executed_count(stdout_bytes)
        result = CommandResult(
            command=request.command,
            exit_code=exit_code,
            stdout_sha256=hashlib.sha256(stdout_bytes).hexdigest(),
            artifact_path=artifact.relative_to(request.root).as_posix(),
            timed_out=timed_out,
            target_id=request.target_id,
            executed_count=count,
            executed_count_source=count_source,
            allow_unlisted=request.allow_unlisted,
            no_network=True,
            sandbox_profile="no-network",
            sandbox_status="available",
            allow_unlisted_reason=request.allow_unlisted_reason,
            policy_status=policy_status,
            policy_reason=policy_reason,
        )
        return RunnerResult(
            evidence=result,
            work_dir=request.work_dir,
            runner=self.name,
            sandbox_execution={
                "engine": Path(engine).name,
                "image": request.container_image,
                "network": "none",
                "cpus": "1",
                "memory": "512m",
                "pids_limit": "256",
            },
        )


def runner_for(name: str) -> AgentRunner:
    if name == "null":
        return NullRunner()
    if name == "local-process":
        return LocalProcessRunner()
    if name == "container":
        return ContainerRunner()
    raise ValueError(f"unknown agent runner: {name}")

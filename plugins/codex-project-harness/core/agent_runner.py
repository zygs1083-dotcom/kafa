"""Agent runner abstractions for dispatch execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import shutil
from typing import Protocol

from core.executor import CommandResult, LocalExecutor


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


@dataclass(frozen=True)
class RunnerResult:
    evidence: CommandResult
    work_dir: Path
    runner: str


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
        # The harness records container intent here, but does not pretend to
        # provide OS-level isolation when Docker/Podman is unavailable.
        has_container = bool(shutil.which("docker") or shutil.which("podman"))
        result = LocalExecutor(request.work_dir).run(
            request.command,
            timeout=request.timeout,
            target_id=request.target_id,
            target_command_template=request.target_command_template,
            allowed_prefixes=request.allowed_prefixes,
            allow_unlisted=request.allow_unlisted,
            no_network=True,
            sandbox_profile="no-network",
            allow_unlisted_reason=request.allow_unlisted_reason,
            executed_count=request.executed_count,
        )
        if not has_container and result.sandbox_status != "unavailable":
            # LocalExecutor currently marks no-network as unavailable. Keep the
            # branch explicit so future real container support remains honest.
            pass
        return RunnerResult(evidence=result, work_dir=request.work_dir, runner=self.name)


def runner_for(name: str) -> AgentRunner:
    if name == "null":
        return NullRunner()
    if name == "local-process":
        return LocalProcessRunner()
    if name == "container":
        return ContainerRunner()
    raise ValueError(f"unknown agent runner: {name}")

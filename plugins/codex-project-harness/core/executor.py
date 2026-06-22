"""Local command executor for trusted runtime evidence."""

from __future__ import annotations

import hashlib
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


class Executor(Protocol):
    def run(self, command: str, *, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> CommandResult:
        """Run a command and return durable evidence metadata."""


class LocalExecutor:
    def __init__(self, root: Path, *, max_stdout_bytes: int = MAX_STDOUT_BYTES) -> None:
        self.root = root.resolve()
        self.max_stdout_bytes = max_stdout_bytes

    def run(self, command: str, *, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> CommandResult:
        args = shlex.split(command)
        if not args:
            raise ValueError("command is required")
        timed_out = False
        try:
            completed = subprocess.run(
                args,
                cwd=self.root,
                capture_output=True,
                check=False,
                timeout=timeout,
            )
            exit_code = completed.returncode
            stdout = completed.stdout or b""
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = 124
            stdout = exc.stdout or b""
        except OSError as exc:
            exit_code = 127
            stdout = str(exc).encode("utf-8", errors="replace")
        stdout = stdout[: self.max_stdout_bytes]
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
        )

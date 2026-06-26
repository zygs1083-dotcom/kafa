"""AgentProvider lifecycle contracts.

Providers manage external or fixture-backed agent sessions. Provider reports
are raw inputs only; controller verification remains responsible for trusted
delivery evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import json
import signal
import subprocess
import sys
import time
from typing import Any, Callable, Protocol


@dataclass(frozen=True)
class AgentJobRequest:
    root: Path
    run_id: str
    task_id: str
    agent_id: str
    branch_name: str
    fence: int
    target_id: str
    command_template: str
    instruction: str
    input_json: dict[str, Any]
    session_id: str = ""
    provider_session_id: str = ""
    worktree_path: str = ""


@dataclass(frozen=True)
class AgentJobHandle:
    provider: str
    provider_session_id: str
    provider_job_id: str
    status: str
    message: str = ""


@dataclass(frozen=True)
class AgentJobReport:
    provider: str
    provider_session_id: str
    provider_job_id: str
    status: str
    last_error: str
    result_json: str


class AgentProvider(Protocol):
    name: str

    def spawn(self, request: AgentJobRequest) -> AgentJobHandle:
        """Start or register a provider-managed agent session."""

    def status(self, handle: AgentJobHandle) -> AgentJobHandle:
        """Return latest provider status for a session."""

    def heartbeat(self, handle: AgentJobHandle) -> AgentJobHandle:
        """Refresh local view of session liveness."""

    def collect(self, handle: AgentJobHandle, *, root: Path, run_id: str, task_id: str) -> AgentJobReport | None:
        """Collect a raw provider report, if one is available."""

    def cancel(self, handle: AgentJobHandle, reason: str) -> AgentJobHandle:
        """Cancel a provider-managed session."""


class ManualCsvProvider:
    name = "manual-csv"

    def spawn(self, request: AgentJobRequest) -> AgentJobHandle:
        return AgentJobHandle(
            provider=self.name,
            provider_session_id=request.provider_session_id or f"manual-csv:{request.run_id}:{request.task_id}",
            provider_job_id=request.task_id,
            status="running",
            message="waiting for external spawn_agents_on_csv result",
        )

    def status(self, handle: AgentJobHandle) -> AgentJobHandle:
        return handle

    def heartbeat(self, handle: AgentJobHandle) -> AgentJobHandle:
        return handle

    def collect(self, handle: AgentJobHandle, *, root: Path, run_id: str, task_id: str) -> AgentJobReport | None:
        return None

    def cancel(self, handle: AgentJobHandle, reason: str) -> AgentJobHandle:
        return AgentJobHandle(handle.provider, handle.provider_session_id, handle.provider_job_id, "cancelled", reason)


class FixtureAgentProvider:
    name = "fixture"

    def spawn(self, request: AgentJobRequest) -> AgentJobHandle:
        return AgentJobHandle(
            provider=self.name,
            provider_session_id=request.provider_session_id or f"fixture:{request.run_id}:{request.task_id}",
            provider_job_id=request.task_id,
            status="running",
        )

    def status(self, handle: AgentJobHandle) -> AgentJobHandle:
        return handle

    def heartbeat(self, handle: AgentJobHandle) -> AgentJobHandle:
        return handle

    def collect(self, handle: AgentJobHandle, *, root: Path, run_id: str, task_id: str) -> AgentJobReport | None:
        path = root / ".ai-team" / "runtime" / "provider-fixtures" / run_id / f"{task_id}.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        result_json = data.get("result_json")
        if result_json is None:
            result_json = json.dumps(data.get("result", {}), sort_keys=True)
        return AgentJobReport(
            provider=self.name,
            provider_session_id=handle.provider_session_id,
            provider_job_id=handle.provider_job_id,
            status=str(data.get("status", "success")),
            last_error=str(data.get("last_error", "")),
            result_json=str(result_json),
        )

    def cancel(self, handle: AgentJobHandle, reason: str) -> AgentJobHandle:
        return AgentJobHandle(handle.provider, handle.provider_session_id, handle.provider_job_id, "cancelled", reason)


def _json_object_from_text(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("missing final JSON object")


def _host_codex_prompt(request: AgentJobRequest) -> str:
    expected = {
        "command": request.command_template,
        "exit_code": 0,
        "stdout_sha256": "<sha256>",
        "artifact_path": "<repo-relative-path>",
        "executed_count": 1,
        "executed_count_source": "parsed",
        "source_tree_hash": "<tree-sha-or-content-hash>",
        "branch_name": request.branch_name,
        "status": "success",
        "target_id": request.target_id,
        "fence": request.fence,
        "agent_id": request.agent_id,
    }
    return (
        f"{request.instruction}\n\n"
        "You are running as a Codex host-managed worker for Codex Project Harness.\n"
        "Work only on the assigned task and branch. When finished, return exactly one JSON object "
        "matching this provider report contract. Do not wrap it in Markdown.\n\n"
        f"Expected report shape:\n{json.dumps(expected, sort_keys=True)}\n\n"
        f"Task input:\n{json.dumps(request.input_json, sort_keys=True)}\n"
    )


def _provider_report_output_schema() -> dict[str, Any]:
    properties: dict[str, Any] = {
        "command": {"type": "string"},
        "exit_code": {"type": "integer"},
        "stdout_sha256": {"type": "string"},
        "artifact_path": {"type": "string"},
        "executed_count": {"type": "integer"},
        "executed_count_source": {"type": "string"},
        "source_tree_hash": {"type": "string"},
        "branch_name": {"type": "string"},
        "status": {"type": "string"},
        "target_id": {"type": "string"},
        "fence": {"type": "integer"},
        "agent_id": {"type": "string"},
    }
    return {
        "type": "object",
        "additionalProperties": True,
        "properties": properties,
        "required": list(properties),
    }


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _update_host_codex_report(path: Path, updates: dict[str, Any]) -> None:
    data = _read_json_object(path)
    metadata = data.get("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    update_metadata = updates.pop("metadata", {})
    if isinstance(update_metadata, dict):
        metadata.update(update_metadata)
    for key in [
        "worker_pid",
        "app_server_pid",
        "thread_id",
        "turn_id",
        "app_server_command",
        "sdk",
        "sdk_sandbox",
        "sdk_approval_mode",
        "sdk_model",
        "codex_bin",
        "worktree_path",
        "duration_seconds",
        "job_path",
        "report_path",
        "timeout_seconds",
    ]:
        if key in updates:
            metadata[key] = updates.pop(key)
    data.update(updates)
    data["metadata"] = metadata
    _write_json_atomic(path, data)


def _request_from_job(job: dict[str, Any]) -> AgentJobRequest:
    request = job.get("request", {})
    if not isinstance(request, dict):
        raise RuntimeError("host-codex job missing request")
    return AgentJobRequest(
        root=Path(str(request["root"])),
        run_id=str(request["run_id"]),
        task_id=str(request["task_id"]),
        agent_id=str(request["agent_id"]),
        branch_name=str(request["branch_name"]),
        fence=int(request["fence"]),
        target_id=str(request.get("target_id", "")),
        command_template=str(request.get("command_template", "")),
        instruction=str(request.get("instruction", "")),
        input_json=request.get("input_json", {}) if isinstance(request.get("input_json", {}), dict) else {},
        session_id=str(request.get("session_id", "")),
        provider_session_id=str(request.get("provider_session_id", "")),
        worktree_path=str(request.get("worktree_path", "")),
    )


class HostCodexProvider:
    name = "host-codex"

    def spawn(self, request: AgentJobRequest) -> AgentJobHandle:
        timeout = float(os.environ.get("HARNESS_CODEX_TURN_TIMEOUT_SECONDS", "1800"))
        codex_bin = os.environ.get("HARNESS_CODEX_BIN", "")
        model = os.environ.get("HARNESS_CODEX_MODEL", "")
        provider_session_id = request.provider_session_id or f"host-codex:{request.run_id}:{request.task_id}"
        job_path = self._job_path(request.root, request.run_id, request.task_id)
        report_path = self._report_path(request.root, request.run_id, request.task_id)
        if not request.worktree_path:
            return AgentJobHandle(
                provider=self.name,
                provider_session_id=provider_session_id,
                provider_job_id=request.task_id,
                status="spawn_failed",
                message="host-codex requires an isolated worktree",
            )
        job_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        job = {
            "request": {
                "root": str(request.root),
                "run_id": request.run_id,
                "task_id": request.task_id,
                "agent_id": request.agent_id,
                "branch_name": request.branch_name,
                "fence": request.fence,
                "target_id": request.target_id,
                "command_template": request.command_template,
                "instruction": request.instruction,
                "input_json": request.input_json,
                "session_id": request.session_id,
                "provider_session_id": provider_session_id,
                "worktree_path": request.worktree_path,
            },
            "provider": self.name,
            "provider_session_id": provider_session_id,
            "codex_bin": codex_bin,
            "model": model,
            "timeout": timeout,
            "report_path": str(report_path),
            "created_at": time.time(),
        }
        _write_json_atomic(
            report_path,
            {
                "status": "running",
                "last_error": "",
                "result_json": "",
                "metadata": {
                    "sdk": "openai-codex",
                    "sdk_sandbox": "workspace_write",
                    "sdk_approval_mode": "deny_all",
                    "sdk_model": model,
                    "codex_bin": codex_bin,
                    "worktree_path": request.worktree_path,
                    "job_path": job_path.relative_to(request.root).as_posix(),
                    "report_path": report_path.relative_to(request.root).as_posix(),
                    "timeout_seconds": timeout,
                },
            },
        )
        try:
            _write_json_atomic(job_path, job)
            worker = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "host-codex-worker", str(job_path)],
                cwd=request.root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as exc:  # noqa: BLE001 - provider errors are recorded as session failures.
            return AgentJobHandle(
                provider=self.name,
                provider_session_id=provider_session_id,
                provider_job_id=request.task_id,
                status="spawn_failed",
                message=json.dumps({"error": str(exc), "app_server_command": command}, sort_keys=True),
            )
        metadata = {
            "worker_pid": worker.pid,
            "sdk": "openai-codex",
            "sdk_sandbox": "workspace_write",
            "sdk_approval_mode": "deny_all",
            "sdk_model": model,
            "codex_bin": codex_bin,
            "worktree_path": request.worktree_path,
            "job_path": job_path.relative_to(request.root).as_posix(),
            "report_path": report_path.relative_to(request.root).as_posix(),
            "timeout_seconds": timeout,
        }
        _update_host_codex_report(report_path, dict(metadata))
        return AgentJobHandle(
            provider=self.name,
            provider_session_id=provider_session_id,
            provider_job_id=f"worker:{worker.pid}",
            status="running",
            message=json.dumps(metadata, sort_keys=True),
        )

    def status(self, handle: AgentJobHandle) -> AgentJobHandle:
        return handle

    def heartbeat(self, handle: AgentJobHandle) -> AgentJobHandle:
        return handle

    def collect(self, handle: AgentJobHandle, *, root: Path, run_id: str, task_id: str) -> AgentJobReport | None:
        path = self._report_path(root, run_id, task_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        status = str(data.get("status", "running"))
        if status == "running":
            return None
        metadata = data.get("metadata", {})
        provider_job_id = handle.provider_job_id
        if isinstance(metadata, dict) and metadata.get("turn_id"):
            provider_job_id = str(metadata["turn_id"])
        return AgentJobReport(
            provider=self.name,
            provider_session_id=handle.provider_session_id,
            provider_job_id=provider_job_id,
            status=status,
            last_error=str(data.get("last_error", "")),
            result_json=str(data.get("result_json", "")),
        )

    def cancel(self, handle: AgentJobHandle, reason: str) -> AgentJobHandle:
        try:
            metadata = json.loads(handle.message or "{}")
        except json.JSONDecodeError:
            metadata = {}
        worker_pid = metadata.get("worker_pid") if isinstance(metadata, dict) else None
        if worker_pid:
            try:
                os.kill(int(worker_pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
            except OSError as exc:
                return AgentJobHandle(handle.provider, handle.provider_session_id, handle.provider_job_id, "cancelled", f"{reason}; cancel signal failed: {exc}")
        return AgentJobHandle(handle.provider, handle.provider_session_id, handle.provider_job_id, "cancelled", reason)

    def _run_sdk_turn(
        self,
        request: AgentJobRequest,
        *,
        codex_bin: str,
        model: str,
        state_update: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        if not request.worktree_path:
            raise RuntimeError("host-codex job missing worktree_path")
        worktree = request.root / request.worktree_path
        if not worktree.exists():
            raise RuntimeError(f"host-codex worktree missing: {request.worktree_path}")
        worktree = worktree.resolve()
        from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox

        config = CodexConfig(
            codex_bin=codex_bin or None,
            client_name="codex_project_harness",
            client_title="Codex Project Harness",
            client_version="1.21.0-beta.1",
        )
        kwargs: dict[str, Any] = {
            "cwd": str(worktree),
            "sandbox": Sandbox.workspace_write,
            "approval_mode": ApprovalMode.deny_all,
        }
        if model:
            kwargs["model"] = model
        if state_update is not None:
            state_update(
                {
                    "sdk": "openai-codex",
                    "sdk_sandbox": "workspace_write",
                    "sdk_approval_mode": "deny_all",
                    "sdk_model": model,
                    "codex_bin": codex_bin,
                    "worktree_path": request.worktree_path,
                }
            )
        with Codex(config=config) as codex:
            thread = codex.thread_start(**kwargs)
            thread_id = str(getattr(thread, "id", "") or "sdk-thread")
            if state_update is not None:
                state_update({"thread_id": thread_id})
            result = thread.run(_host_codex_prompt(request), output_schema=_provider_report_output_schema(), **kwargs)
        report = self._report_from_sdk_result(result)
        self._commit_worktree_changes(worktree, request)
        return {"thread_id": thread_id, "turn_id": "sdk-turn", "report": report}

    def _report_from_sdk_result(self, result: Any) -> dict[str, Any]:
        value = result
        for attr in ["final_response", "output", "content", "text"]:
            if hasattr(result, attr):
                value = getattr(result, attr)
                if value is not None:
                    break
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            return _json_object_from_text(value)
        raise ValueError("missing final JSON object")

    def _commit_worktree_changes(self, worktree: Path, request: AgentJobRequest) -> None:
        current = subprocess.run(["git", "branch", "--show-current"], cwd=worktree, text=True, capture_output=True, check=False)
        if current.returncode != 0:
            raise RuntimeError(f"worktree branch check failed: {current.stderr.strip() or current.stdout.strip()}")
        if current.stdout.strip() != request.branch_name:
            raise RuntimeError(f"host-codex worktree branch mismatch: expected {request.branch_name}, actual {current.stdout.strip()}")
        status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=worktree, text=True, capture_output=True, check=False)
        if status.returncode != 0:
            raise RuntimeError(f"worktree status failed: {status.stderr.strip() or status.stdout.strip()}")
        changed: list[str] = []
        for line in status.stdout.splitlines():
            relpath = line[3:] if len(line) > 3 else ""
            if " -> " in relpath:
                relpath = relpath.split(" -> ", 1)[1]
            if relpath and not relpath.startswith(".ai-team/"):
                changed.append(relpath)
        if not changed:
            return
        add = subprocess.run(["git", "add", "--", *sorted(set(changed))], cwd=worktree, text=True, capture_output=True, check=False)
        if add.returncode != 0:
            raise RuntimeError(f"worktree add failed: {add.stderr.strip() or add.stdout.strip()}")
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=worktree, text=True, capture_output=True, check=False)
        if diff.returncode == 0:
            return
        commit = subprocess.run(
            [
                "git",
                "-c",
                "user.name=Codex Harness",
                "-c",
                "user.email=harness@example.invalid",
                "commit",
                "-m",
                f"Host Codex task {request.task_id}",
            ],
            cwd=worktree,
            text=True,
            capture_output=True,
            check=False,
        )
        if commit.returncode != 0:
            raise RuntimeError(f"worktree commit failed: {commit.stderr.strip() or commit.stdout.strip()}")

    def _report_path(self, root: Path, run_id: str, task_id: str) -> Path:
        return root / ".ai-team" / "runtime" / "host-codex" / run_id / f"{task_id}.json"

    def _job_path(self, root: Path, run_id: str, task_id: str) -> Path:
        return root / ".ai-team" / "runtime" / "host-codex" / run_id / f"{task_id}.job.json"


def provider_for(name: str) -> AgentProvider:
    if name == "manual-csv":
        return ManualCsvProvider()
    if name == "fixture":
        return FixtureAgentProvider()
    if name == "host-codex":
        return HostCodexProvider()
    raise ValueError(f"unknown agent provider: {name}")


def _host_codex_worker(job_path: Path) -> int:
    job = _read_json_object(job_path)
    provider = HostCodexProvider()
    request = _request_from_job(job)
    codex_bin = str(job.get("codex_bin", os.environ.get("HARNESS_CODEX_BIN", "")))
    model = str(job.get("model", os.environ.get("HARNESS_CODEX_MODEL", "")))
    timeout = float(job.get("timeout", os.environ.get("HARNESS_CODEX_TURN_TIMEOUT_SECONDS", "1800")))
    report_path = Path(str(job.get("report_path") or provider._report_path(request.root, request.run_id, request.task_id)))
    started = time.monotonic()
    base_metadata = {
        "worker_pid": os.getpid(),
        "sdk": "openai-codex",
        "sdk_sandbox": "workspace_write",
        "sdk_approval_mode": "deny_all",
        "sdk_model": model,
        "codex_bin": codex_bin,
        "worktree_path": request.worktree_path,
        "job_path": str(job_path),
        "report_path": str(report_path),
        "timeout_seconds": timeout,
    }
    _write_json_atomic(
        report_path,
        {
            "status": "running",
            "last_error": "",
            "result_json": "",
            "metadata": base_metadata,
        },
    )

    def state_update(values: dict[str, Any]) -> None:
        _update_host_codex_report(report_path, dict(values))

    try:
        result = provider._run_sdk_turn(request, codex_bin=codex_bin, model=model, state_update=state_update)
    except Exception as exc:  # noqa: BLE001 - worker failure is serialized for collect.
        status = "cancelled" if "cancelled" in str(exc).lower() else "failed"
        _update_host_codex_report(
            report_path,
            {
                "status": status,
                "last_error": str(exc),
                "result_json": "",
                "duration_seconds": round(time.monotonic() - started, 6),
            },
        )
        return 1 if status != "cancelled" else 0
    _update_host_codex_report(
        report_path,
        {
            "status": "success",
            "last_error": "",
            "result_json": json.dumps(result["report"], sort_keys=True),
            "thread_id": result["thread_id"],
            "turn_id": result["turn_id"],
            "duration_seconds": round(time.monotonic() - started, 6),
        },
    )
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "host-codex-worker":
        raise SystemExit(_host_codex_worker(Path(sys.argv[2])))
    raise SystemExit("usage: agent_provider.py host-codex-worker <job-path>")

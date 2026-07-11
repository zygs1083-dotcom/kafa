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


def _host_codex_model_selection(request: AgentJobRequest, explicit_model: str, policy: str, spark_model: str) -> dict[str, Any]:
    normalized_policy = (policy or "default").strip() or "default"
    spark_eligible = bool(request.input_json.get("spark_eligible"))
    base_reason = str(request.input_json.get("model_selection_reason") or "")
    if explicit_model:
        return {
            "model_policy": normalized_policy,
            "selected_model": explicit_model,
            "model_selection_reason": "HARNESS_CODEX_MODEL override",
            "spark_eligible": spark_eligible,
        }
    if normalized_policy == "default":
        return {
            "model_policy": normalized_policy,
            "selected_model": "",
            "model_selection_reason": "SDK default model",
            "spark_eligible": spark_eligible,
        }
    if normalized_policy != "spark-deterministic":
        raise RuntimeError(f"unsupported HARNESS_CODEX_MODEL_POLICY: {normalized_policy}")
    if not spark_model:
        raise RuntimeError(
            "HARNESS_CODEX_MODEL_POLICY=spark-deterministic requires explicit HARNESS_CODEX_SPARK_MODEL"
        )
    if spark_eligible:
        return {
            "model_policy": normalized_policy,
            "selected_model": spark_model,
            "model_selection_reason": base_reason or "spark eligible deterministic developer task",
            "spark_eligible": True,
        }
    return {
        "model_policy": normalized_policy,
        "selected_model": "",
        "model_selection_reason": base_reason or "spark ineligible; SDK default model",
        "spark_eligible": False,
    }


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


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and exit_code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        text=True,
        capture_output=True,
        check=False,
    )
    if state.returncode == 0 and (not state.stdout.strip() or state.stdout.strip().startswith("Z")):
        return False
    return True


def _descendant_pids(root_pid: int) -> list[int]:
    if os.name == "nt":
        return []
    result = subprocess.run(
        ["ps", "-eo", "pid=,ppid="],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    children: dict[int, list[int]] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pid, parent_pid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children.setdefault(parent_pid, []).append(pid)
    descendants: list[int] = []
    pending = list(children.get(root_pid, []))
    while pending:
        pid = pending.pop()
        descendants.append(pid)
        pending.extend(children.get(pid, []))
    return descendants


def _terminate_process_tree(pid: int, *, expected_pgid: int = 0, grace_seconds: float = 1.0) -> bool:
    if not _process_alive(pid):
        return True
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            text=True,
            capture_output=True,
            check=False,
        )
        deadline = time.monotonic() + max(grace_seconds, 0.0) + 1.0
        while time.monotonic() < deadline:
            if not _process_alive(pid):
                return True
            time.sleep(0.02)
        return not _process_alive(pid)
    try:
        process_group = os.getpgid(pid)
        if expected_pgid and process_group != expected_pgid:
            return False
        os.killpg(process_group, signal.SIGSTOP)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    descendants: set[int] = set()
    stable_scans = 0
    for _ in range(20):
        current = set(_descendant_pids(pid))
        new_descendants = current - descendants
        descendants.update(current)
        for child_pid in new_descendants:
            try:
                os.kill(child_pid, signal.SIGSTOP)
            except ProcessLookupError:
                pass
        if new_descendants:
            stable_scans = 0
        else:
            stable_scans += 1
            if stable_scans >= 2:
                break
        time.sleep(0.01)
    for child_pid in sorted(descendants, reverse=True):
        if _process_alive(child_pid):
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    if _process_alive(pid):
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and (
        _process_alive(pid) or any(_process_alive(child_pid) for child_pid in descendants)
    ):
        time.sleep(0.02)
    return not _process_alive(pid) and all(not _process_alive(child_pid) for child_pid in descendants)


def _update_host_codex_report(path: Path, updates: dict[str, Any], *, expected_status: str = "") -> bool:
    lock_path = path.with_name(f"{path.name}.lock")
    deadline = time.monotonic() + 2.0
    while True:
        try:
            lock_path.mkdir()
            break
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > 1.0:
                    lock_path.rmdir()
                    continue
            except (FileNotFoundError, OSError):
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError(f"host-codex report lock timeout: {path}")
            time.sleep(0.01)
    try:
        data = _read_json_object(path)
        if expected_status and str(data.get("status", "")) != expected_status:
            return False
        metadata = data.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        update_metadata = updates.pop("metadata", {})
        if isinstance(update_metadata, dict):
            metadata.update(update_metadata)
        for key in [
            "worker_pid",
            "worker_pgid",
            "watchdog_pid",
            "deadline_epoch",
            "app_server_pid",
            "thread_id",
            "turn_id",
            "app_server_command",
            "sdk",
            "sdk_sandbox",
            "sdk_approval_mode",
            "sdk_model",
            "codex_bin",
            "model_policy",
            "selected_model",
            "model_selection_reason",
            "spark_eligible",
            "legacy_host_policy",
            "worktree_path",
            "duration_seconds",
            "job_path",
            "report_path",
            "report_path_absolute",
            "timeout_seconds",
        ]:
            if key in updates:
                metadata[key] = updates.pop(key)
        data.update(updates)
        data["metadata"] = metadata
        _write_json_atomic(path, data)
        return True
    finally:
        try:
            lock_path.rmdir()
        except FileNotFoundError:
            pass


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
        explicit_model = os.environ.get("HARNESS_CODEX_MODEL", "").strip()
        model_policy = os.environ.get("HARNESS_CODEX_MODEL_POLICY", "default").strip()
        spark_model = os.environ.get("HARNESS_CODEX_SPARK_MODEL", "").strip()
        legacy_host_policy = os.environ.get("HARNESS_CODEX_LEGACY_HOST_POLICY", "").strip()
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
        if legacy_host_policy != "isolated-deny-all":
            return AgentJobHandle(
                provider=self.name,
                provider_session_id=provider_session_id,
                provider_job_id=request.task_id,
                status="spawn_failed",
                message=(
                    "legacy host-codex cannot inherit native task permissions; "
                    "requires explicit HARNESS_CODEX_LEGACY_HOST_POLICY=isolated-deny-all"
                ),
            )
        try:
            model_selection = _host_codex_model_selection(request, explicit_model, model_policy, spark_model)
        except RuntimeError as exc:
            return AgentJobHandle(
                provider=self.name,
                provider_session_id=provider_session_id,
                provider_job_id=request.task_id,
                status="spawn_failed",
                message=str(exc),
            )
        selected_model = str(model_selection["selected_model"])
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
            "model": selected_model,
            "model_selection": model_selection,
            "legacy_host_policy": legacy_host_policy,
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
                    "sdk_model": selected_model,
                    "codex_bin": codex_bin,
                    **model_selection,
                    "legacy_host_policy": legacy_host_policy,
                    "worktree_path": request.worktree_path,
                    "job_path": job_path.relative_to(request.root).as_posix(),
                    "report_path": report_path.relative_to(request.root).as_posix(),
                    "timeout_seconds": timeout,
                },
            },
        )
        worker_command = [sys.executable, str(Path(__file__).resolve()), "host-codex-worker", str(job_path)]
        try:
            _write_json_atomic(job_path, job)
            worker = subprocess.Popen(
                worker_command,
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
                message=json.dumps({"error": str(exc), "worker_command": worker_command}, sort_keys=True),
            )
        metadata = {
            "worker_pid": worker.pid,
            "worker_pgid": worker.pid if os.name != "nt" else 0,
            "sdk": "openai-codex",
            "sdk_sandbox": "workspace_write",
            "sdk_approval_mode": "deny_all",
            "sdk_model": selected_model,
            "codex_bin": codex_bin,
            **model_selection,
            "legacy_host_policy": legacy_host_policy,
            "worktree_path": request.worktree_path,
            "job_path": job_path.relative_to(request.root).as_posix(),
            "report_path": report_path.relative_to(request.root).as_posix(),
            "report_path_absolute": str(report_path.resolve()),
            "timeout_seconds": timeout,
            "deadline_epoch": time.time() + timeout,
        }
        _update_host_codex_report(report_path, dict(metadata))
        watchdog_command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "host-codex-watchdog",
            str(report_path),
            str(worker.pid),
            str(timeout),
        ]
        try:
            watchdog = subprocess.Popen(
                watchdog_command,
                cwd=request.root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as exc:  # noqa: BLE001 - watchdog startup must fail closed.
            _terminate_process_tree(worker.pid, expected_pgid=worker.pid if os.name != "nt" else 0)
            _update_host_codex_report(
                report_path,
                {
                    "status": "failed",
                    "last_error": f"host-codex watchdog failed to start: {exc}",
                    "result_json": "",
                },
                expected_status="running",
            )
            return AgentJobHandle(
                provider=self.name,
                provider_session_id=provider_session_id,
                provider_job_id=request.task_id,
                status="spawn_failed",
                message=f"host-codex watchdog failed to start: {exc}",
            )
        metadata["watchdog_pid"] = watchdog.pid
        _update_host_codex_report(report_path, {"watchdog_pid": watchdog.pid})
        return AgentJobHandle(
            provider=self.name,
            provider_session_id=provider_session_id,
            provider_job_id=f"worker:{worker.pid}",
            status="running",
            message=json.dumps(metadata, sort_keys=True),
        )

    def status(self, handle: AgentJobHandle) -> AgentJobHandle:
        metadata = self._handle_metadata(handle)
        report_path = Path(str(metadata.get("report_path_absolute", ""))) if metadata.get("report_path_absolute") else None
        if report_path is not None:
            report = _read_json_object(report_path)
            report_status = str(report.get("status", ""))
            if report_status and report_status != "running":
                return AgentJobHandle(handle.provider, handle.provider_session_id, handle.provider_job_id, report_status, str(report.get("last_error", "")))
        worker_pid = int(metadata.get("worker_pid") or 0)
        if worker_pid and not _process_alive(worker_pid):
            return AgentJobHandle(handle.provider, handle.provider_session_id, handle.provider_job_id, "failed", "host-codex worker exited without terminal report")
        return handle

    def heartbeat(self, handle: AgentJobHandle) -> AgentJobHandle:
        return self.status(handle)

    def collect(self, handle: AgentJobHandle, *, root: Path, run_id: str, task_id: str) -> AgentJobReport | None:
        path = self._report_path(root, run_id, task_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        status = str(data.get("status", "running"))
        if status == "running":
            metadata = data.get("metadata", {})
            worker_pid = int(metadata.get("worker_pid") or 0) if isinstance(metadata, dict) else 0
            worker_pgid = int(metadata.get("worker_pgid") or 0) if isinstance(metadata, dict) else 0
            deadline_epoch = float(metadata.get("deadline_epoch") or 0) if isinstance(metadata, dict) else 0.0
            if worker_pid and deadline_epoch and time.time() >= deadline_epoch:
                terminated = _terminate_process_tree(worker_pid, expected_pgid=worker_pgid)
                _update_host_codex_report(
                    path,
                    {
                        "status": "failed",
                        "last_error": (
                            "host-codex turn timeout; known process tree terminated but detached helper termination unconfirmed"
                            if terminated
                            else "host-codex turn timeout; process tree termination unconfirmed"
                        ),
                        "result_json": "",
                    },
                    expected_status="running",
                )
            elif worker_pid and _process_alive(worker_pid):
                return None
            else:
                _update_host_codex_report(
                    path,
                    {
                        "status": "failed",
                        "last_error": "host-codex worker exited without terminal report",
                        "result_json": "",
                    },
                    expected_status="running",
                )
            data = _read_json_object(path)
            status = str(data.get("status", "failed"))
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
        metadata = self._handle_metadata(handle)
        report_path = Path(str(metadata.get("report_path_absolute", ""))) if metadata.get("report_path_absolute") else None
        if report_path is not None:
            report = _read_json_object(report_path)
            report_metadata = report.get("metadata", {})
            if isinstance(report_metadata, dict):
                metadata.update(report_metadata)
            if str(report.get("status", "running")) == "running":
                _update_host_codex_report(
                    report_path,
                    {"status": "cancelled", "last_error": reason, "result_json": ""},
                    expected_status="running",
                )
        worker_pid = int(metadata.get("worker_pid") or 0)
        worker_pgid = int(metadata.get("worker_pgid") or 0)
        watchdog_pid = int(metadata.get("watchdog_pid") or 0)
        worker_stopped = _terminate_process_tree(worker_pid, expected_pgid=worker_pgid) if worker_pid else True
        watchdog_stopped = _terminate_process_tree(watchdog_pid, expected_pgid=watchdog_pid if os.name != "nt" else 0) if watchdog_pid else True
        termination_detail = (
            "known process tree terminated but detached helper termination cannot be independently confirmed"
            if worker_stopped and watchdog_stopped
            else "process tree termination not confirmed"
        )
        if report_path is not None:
            _update_host_codex_report(
                report_path,
                {
                    "status": "failed",
                    "last_error": f"{reason}; {termination_detail}",
                    "result_json": "",
                },
                expected_status="cancelled",
            )
        return AgentJobHandle(
            handle.provider,
            handle.provider_session_id,
            handle.provider_job_id,
            "cancel_failed",
            f"{reason}; {termination_detail}",
        )

    @staticmethod
    def _handle_metadata(handle: AgentJobHandle) -> dict[str, Any]:
        try:
            metadata = json.loads(handle.message or "{}")
        except json.JSONDecodeError:
            return {}
        return metadata if isinstance(metadata, dict) else {}

    def _run_sdk_turn(
        self,
        request: AgentJobRequest,
        *,
        codex_bin: str,
        model: str,
        state_update: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        if os.environ.get("HARNESS_CODEX_LEGACY_HOST_POLICY", "").strip() != "isolated-deny-all":
            raise RuntimeError(
                "legacy host-codex SDK execution requires explicit "
                "HARNESS_CODEX_LEGACY_HOST_POLICY=isolated-deny-all"
            )
        if not request.worktree_path:
            raise RuntimeError("host-codex job missing worktree_path")
        worktree = request.root / request.worktree_path
        if not worktree.exists():
            raise RuntimeError(f"host-codex worktree missing: {request.worktree_path}")
        worktree = worktree.resolve()
        try:
            from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox
        except ImportError as exc:
            raise RuntimeError(
                "Host Codex Provider requires the optional SDK; install kafa[host-codex] "
                f"before starting this provider ({exc})"
            ) from exc

        config = CodexConfig(
            codex_bin=codex_bin or None,
            client_name="codex_project_harness",
            client_title="Codex Project Harness",
            client_version="1.23.0-beta.1",
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
    model_selection = job.get("model_selection", {})
    if not isinstance(model_selection, dict):
        model_selection = {}
    legacy_host_policy = str(job.get("legacy_host_policy", ""))
    runtime_legacy_host_policy = os.environ.get("HARNESS_CODEX_LEGACY_HOST_POLICY", "").strip()
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
        **model_selection,
        "legacy_host_policy": legacy_host_policy,
        "worktree_path": request.worktree_path,
        "job_path": str(job_path),
        "report_path": str(report_path),
        "timeout_seconds": timeout,
    }
    if legacy_host_policy != "isolated-deny-all" or runtime_legacy_host_policy != "isolated-deny-all":
        _write_json_atomic(
            report_path,
            {
                "status": "failed",
                "last_error": (
                    "legacy host-codex worker requires matching job and environment "
                    "HARNESS_CODEX_LEGACY_HOST_POLICY=isolated-deny-all"
                ),
                "result_json": "",
                "metadata": base_metadata,
            },
        )
        return 1
    initialized = _update_host_codex_report(
        report_path,
        {
            "status": "running",
            "last_error": "",
            "result_json": "",
            "metadata": base_metadata,
        },
        expected_status="running",
    )
    if not initialized:
        return 0

    def state_update(values: dict[str, Any]) -> None:
        _update_host_codex_report(report_path, dict(values))

    try:
        result = provider._run_sdk_turn(request, codex_bin=codex_bin, model=model, state_update=state_update)
    except Exception as exc:  # noqa: BLE001 - worker failure is serialized for collect.
        if str(_read_json_object(report_path).get("status", "running")) != "running":
            return 0
        status = "cancelled" if "cancelled" in str(exc).lower() else "failed"
        _update_host_codex_report(
            report_path,
            {
                "status": status,
                "last_error": str(exc),
                "result_json": "",
                "duration_seconds": round(time.monotonic() - started, 6),
            },
            expected_status="running",
        )
        return 1 if status != "cancelled" else 0
    if str(_read_json_object(report_path).get("status", "running")) != "running":
        return 0
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
        expected_status="running",
    )
    return 0


def _host_codex_watchdog(report_path: Path, worker_pid: int, timeout: float) -> int:
    started = time.monotonic()
    deadline = started + max(timeout, 0.0)
    while True:
        report = _read_json_object(report_path)
        status = str(report.get("status", "running"))
        if status != "running":
            return 0
        if not _process_alive(worker_pid):
            _update_host_codex_report(
                report_path,
                {
                    "status": "failed",
                    "last_error": "host-codex worker exited without terminal report",
                    "result_json": "",
                    "duration_seconds": round(time.monotonic() - started, 6),
                },
                expected_status="running",
            )
            return 1
        if time.monotonic() >= deadline:
            terminated = _terminate_process_tree(worker_pid, expected_pgid=worker_pid if os.name != "nt" else 0)
            _update_host_codex_report(
                report_path,
                {
                    "status": "failed",
                    "last_error": (
                        f"host-codex turn timeout after {timeout:g} seconds; known process tree terminated but detached helper termination unconfirmed"
                        if terminated
                        else f"host-codex turn timeout after {timeout:g} seconds; process tree termination unconfirmed"
                    ),
                    "result_json": "",
                    "duration_seconds": round(time.monotonic() - started, 6),
                },
                expected_status="running",
            )
            return 0 if terminated else 1
        time.sleep(0.05)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "host-codex-worker":
        raise SystemExit(_host_codex_worker(Path(sys.argv[2])))
    if len(sys.argv) == 5 and sys.argv[1] == "host-codex-watchdog":
        raise SystemExit(_host_codex_watchdog(Path(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4])))
    raise SystemExit("usage: agent_provider.py host-codex-worker <job-path> | host-codex-watchdog <report-path> <worker-pid> <timeout>")

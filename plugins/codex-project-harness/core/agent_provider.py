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
import selectors
import shlex
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


def _message_text(message: dict[str, Any]) -> str:
    params = message.get("params")
    if not isinstance(params, dict):
        return ""
    chunks: list[str] = []
    for key in ["delta", "text", "content"]:
        value = params.get(key)
        if isinstance(value, str):
            chunks.append(value)
    item = params.get("item")
    if isinstance(item, dict):
        for key in ["text", "content"]:
            value = item.get(key)
            if isinstance(value, str):
                chunks.append(value)
            elif isinstance(value, list):
                for part in value:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        chunks.append(part["text"])
                    elif isinstance(part, str):
                        chunks.append(part)
    return "".join(chunks)


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
    for key in ["worker_pid", "app_server_pid", "thread_id", "turn_id", "app_server_command", "duration_seconds", "job_path", "report_path", "timeout_seconds"]:
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
    )


class HostCodexProvider:
    name = "host-codex"

    def spawn(self, request: AgentJobRequest) -> AgentJobHandle:
        command = os.environ.get("HARNESS_CODEX_APP_SERVER_CMD", "codex app-server")
        timeout = float(os.environ.get("HARNESS_CODEX_TURN_TIMEOUT_SECONDS", "1800"))
        provider_session_id = request.provider_session_id or f"host-codex:{request.run_id}:{request.task_id}"
        job_path = self._job_path(request.root, request.run_id, request.task_id)
        report_path = self._report_path(request.root, request.run_id, request.task_id)
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
            },
            "provider": self.name,
            "provider_session_id": provider_session_id,
            "command": command,
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
                    "app_server_command": command,
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
            "app_server_command": command,
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

    def _run_turn(
        self,
        request: AgentJobRequest,
        command: str,
        timeout: float,
        *,
        state_update: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        cancel_requested = False

        def request_cancel(_signum: int, _frame: Any) -> None:
            nonlocal cancel_requested
            cancel_requested = True

        previous_sigterm = signal.signal(signal.SIGTERM, request_cancel)
        try:
            proc = subprocess.Popen(
                shlex.split(command),
                cwd=request.root,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except Exception:
            signal.signal(signal.SIGTERM, previous_sigterm)
            raise
        if state_update is not None:
            state_update({"app_server_pid": proc.pid})
        next_id = 0

        def send(method: str, params: dict[str, Any] | None = None, *, notify: bool = False) -> int:
            nonlocal next_id
            message: dict[str, Any] = {"method": method, "params": params or {}}
            if not notify:
                next_id += 1
                message["id"] = next_id
            assert proc.stdin is not None
            proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            proc.stdin.flush()
            return next_id

        initialize_id = send(
            "initialize",
            {"clientInfo": {"name": "codex_project_harness", "title": "Codex Project Harness", "version": "1.17.0-beta.1"}},
        )
        send("initialized", {}, notify=True)
        thread_id_request = send("thread/start", {})
        thread_id = ""
        turn_id = ""
        agent_text = ""
        turn_started = False
        pending = b""
        completed = False
        deadline = time.monotonic() + timeout
        assert proc.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ)
        try:
            while time.monotonic() < deadline:
                if cancel_requested:
                    if turn_started and thread_id:
                        try:
                            send("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, notify=True)
                            time.sleep(float(os.environ.get("HARNESS_CODEX_INTERRUPT_GRACE_SECONDS", "0.2")))
                        except Exception:
                            pass
                    raise RuntimeError("host codex worker cancelled")
                if b"\n" not in pending:
                    ready = selector.select(timeout=min(0.1, max(deadline - time.monotonic(), 0)))
                    if not ready:
                        if proc.poll() is not None:
                            break
                        continue
                    chunk = os.read(proc.stdout.fileno(), 4096)
                    if not chunk:
                        if proc.poll() is not None:
                            break
                        continue
                    pending += chunk
                line_bytes, pending = pending.split(b"\n", 1)
                line = line_bytes.decode("utf-8")
                message = json.loads(line)
                if "error" in message:
                    raise RuntimeError(message["error"].get("message", "app-server JSON-RPC error"))
                if message.get("id") == initialize_id:
                    continue
                if message.get("id") == thread_id_request:
                    thread = message.get("result", {}).get("thread", {})
                    thread_id = str(thread.get("id", ""))
                    if not thread_id:
                        raise RuntimeError("thread/start response missing thread.id")
                    if state_update is not None:
                        state_update({"thread_id": thread_id})
                    send(
                        "turn/start",
                        {
                            "threadId": thread_id,
                            "input": [{"type": "text", "text": _host_codex_prompt(request)}],
                            "cwd": str(request.root),
                        },
                    )
                    continue
                method = message.get("method")
                if method == "turn/started":
                    turn = message.get("params", {}).get("turn", {})
                    turn_id = str(turn.get("id", turn_id))
                    turn_started = True
                    if state_update is not None:
                        state_update({"turn_id": turn_id})
                elif method in {"item/agentMessage/delta", "item/completed"}:
                    agent_text += _message_text(message)
                elif method == "turn/completed":
                    turn = message.get("params", {}).get("turn", {})
                    turn_id = str(turn.get("id", turn_id))
                    report = _json_object_from_text(agent_text)
                    completed = True
                    return {"thread_id": thread_id, "turn_id": turn_id or "turn-completed", "report": report}
            raise TimeoutError("codex app-server turn timed out" if turn_started else "codex app-server did not start turn")
        finally:
            selector.close()
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
            signal.signal(signal.SIGTERM, previous_sigterm)
            if not completed and proc.returncode not in (0, None):
                stderr = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
                if stderr:
                    raise RuntimeError(stderr.strip())

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
    command = str(job.get("command", os.environ.get("HARNESS_CODEX_APP_SERVER_CMD", "codex app-server")))
    timeout = float(job.get("timeout", os.environ.get("HARNESS_CODEX_TURN_TIMEOUT_SECONDS", "1800")))
    report_path = Path(str(job.get("report_path") or provider._report_path(request.root, request.run_id, request.task_id)))
    started = time.monotonic()
    base_metadata = {
        "worker_pid": os.getpid(),
        "app_server_command": command,
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
        result = provider._run_turn(request, command, timeout, state_update=state_update)
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

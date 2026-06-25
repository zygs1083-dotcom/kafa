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
import selectors
import shlex
import subprocess
import time
from typing import Any, Protocol


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
            provider_session_id=f"manual-csv:{request.run_id}:{request.task_id}",
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
            provider_session_id=f"fixture:{request.run_id}:{request.task_id}",
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


class HostCodexProvider:
    name = "host-codex"

    def spawn(self, request: AgentJobRequest) -> AgentJobHandle:
        command = os.environ.get("HARNESS_CODEX_APP_SERVER_CMD", "codex app-server")
        timeout = float(os.environ.get("HARNESS_CODEX_TURN_TIMEOUT_SECONDS", "1800"))
        started = time.monotonic()
        try:
            result = self._run_turn(request, command, timeout)
        except Exception as exc:  # noqa: BLE001 - provider errors are recorded as session failures.
            return AgentJobHandle(
                provider=self.name,
                provider_session_id=f"host-codex:{request.run_id}:{request.task_id}:failed",
                provider_job_id=request.task_id,
                status="spawn_failed",
                message=json.dumps({"error": str(exc), "app_server_command": command}, sort_keys=True),
            )
        path = self._report_path(request.root, request.run_id, request.task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "status": "success",
                    "last_error": "",
                    "result_json": json.dumps(result["report"], sort_keys=True),
                    "metadata": {
                        "thread_id": result["thread_id"],
                        "turn_id": result["turn_id"],
                        "app_server_command": command,
                        "duration_seconds": round(time.monotonic() - started, 6),
                    },
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return AgentJobHandle(
            provider=self.name,
            provider_session_id=f"host-codex:{result['thread_id']}",
            provider_job_id=str(result["turn_id"]),
            status="running",
            message=json.dumps(
                {
                    "thread_id": result["thread_id"],
                    "turn_id": result["turn_id"],
                    "app_server_command": command,
                    "report_path": path.relative_to(request.root).as_posix(),
                },
                sort_keys=True,
            ),
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
        return AgentJobReport(
            provider=self.name,
            provider_session_id=handle.provider_session_id,
            provider_job_id=handle.provider_job_id,
            status=str(data.get("status", "success")),
            last_error=str(data.get("last_error", "")),
            result_json=str(data.get("result_json", "")),
        )

    def cancel(self, handle: AgentJobHandle, reason: str) -> AgentJobHandle:
        return AgentJobHandle(handle.provider, handle.provider_session_id, handle.provider_job_id, "cancelled", reason)

    def _run_turn(self, request: AgentJobRequest, command: str, timeout: float) -> dict[str, Any]:
        proc = subprocess.Popen(
            shlex.split(command),
            cwd=request.root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
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
            {"clientInfo": {"name": "codex_project_harness", "title": "Codex Project Harness", "version": "1.9.0-beta.1"}},
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
            if not completed and proc.returncode not in (0, None):
                stderr = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
                if stderr:
                    raise RuntimeError(stderr.strip())

    def _report_path(self, root: Path, run_id: str, task_id: str) -> Path:
        return root / ".ai-team" / "runtime" / "host-codex" / run_id / f"{task_id}.json"


def provider_for(name: str) -> AgentProvider:
    if name == "manual-csv":
        return ManualCsvProvider()
    if name == "fixture":
        return FixtureAgentProvider()
    if name == "host-codex":
        return HostCodexProvider()
    raise ValueError(f"unknown agent provider: {name}")

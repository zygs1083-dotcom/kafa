"""AgentProvider lifecycle contracts.

Providers manage external or fixture-backed agent sessions. Provider reports
are raw inputs only; controller verification remains responsible for trusted
delivery evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
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


class HostCodexProvider(ManualCsvProvider):
    name = "host-codex"


def provider_for(name: str) -> AgentProvider:
    if name == "manual-csv":
        return ManualCsvProvider()
    if name == "fixture":
        return FixtureAgentProvider()
    if name == "host-codex":
        return HostCodexProvider()
    raise ValueError(f"unknown agent provider: {name}")

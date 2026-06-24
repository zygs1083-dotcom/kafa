#!/usr/bin/env python3
"""SQLite-backed runtime for Codex Project Harness."""

from __future__ import annotations

import json
import contextvars
import csv
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tomllib
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from harness_lib import ensure_parent, git_base_commit, git_dirty, git_head_sha, git_source_tree_hash, git_tracked_diff_hash, markdown_row, now_iso, source_tree_hash_for_mode
from core.connector_trust import (
    ConnectorTrustError,
    agent_session_payload,
    ci_payload,
    configured_key_path,
    external_session_payload,
    prepare_connector_record,
    verify_connector_record,
)
from core.schema_guard import ADAPTER_MODES, ANCHOR_ORIGINS, CI_CONCLUSIONS, EXTERNAL_SESSION_CONCLUSIONS, FAILURE_MODE_STATUSES, TASK_STATUSES, TEST_TARGET_KINDS
from core.store import DB_PATH, SqliteStore, Store


SCHEMA_VERSION = 22
RUNTIME_VERSION = "4.1.1"
LEASE_TTL_SECONDS = 3600
DEFAULT_CONTAINER_IMAGE = "python:3.12-slim"
RUNTIME_GITIGNORE_PATTERNS = [
    ".ai-team/state/",
    ".ai-team/backups/",
    ".ai-team/runtime/",
]

PHASES = [
    "intake",
    "project_bootstrap",
    "requirement_baseline",
    "confirmation",
    "team_architecture",
    "planning",
    "implementation",
    "qa",
    "delivery_readiness",
    "retrospective",
    "archived",
]

PHASE_TRANSITIONS = {
    "intake": {"project_bootstrap"},
    "project_bootstrap": {"requirement_baseline"},
    "requirement_baseline": {"confirmation"},
    "confirmation": {"team_architecture", "planning"},
    "team_architecture": {"planning"},
    "planning": {"implementation"},
    "implementation": {"qa"},
    "qa": {"delivery_readiness", "implementation"},
    "delivery_readiness": {"retrospective"},
    "retrospective": {"archived"},
    "archived": set(),
}

ADAPTER_ACTION_STATUSES = {"planned", "draft", "confirmed", "completed", "blocked"}
DISPATCH_STATUSES = {"planned", "claimed", "reported", "completed", "failed", "stale", "integration_conflict", "verification_failed", "integrated"}
SESSION_ROLES = {"developer", "qa-reviewer", "reviewer", "architect", "product", "security"}
REVIEWER_SESSION_ROLES = {"qa-reviewer", "reviewer", "architect", "security"}
ACTIVE_SESSION_STATUSES = {"active", "running", "reported", "verified"}
SESSION_TRUST_LEVELS = {"local-only", "human-confirmed", "connector"}
CODEX_FANOUT_INPUT_FIELDS = [
    "item_id",
    "task",
    "acceptance",
    "failure_modes",
    "target_id",
    "command_template",
    "branch_name",
    "fence",
    "agent_id",
]
CODEX_FANOUT_OUTPUT_FIELDS = [
    "command",
    "exit_code",
    "stdout_sha256",
    "artifact_path",
    "executed_count",
    "executed_count_source",
    "source_tree_hash",
    "branch_name",
    "status",
    "target_id",
]
CODEX_FANOUT_RESULT_COLUMNS = ["job_id", "item_id", "status", "last_error", "result_json"]
CODEX_AGENT_REQUIRED_FIELDS = {"name", "description", "developer_instructions"}
CODEX_AGENT_ALLOWED_FIELDS = {
    "name",
    "description",
    "developer_instructions",
    "model",
    "model_reasoning_effort",
    "sandbox_mode",
    "mcp_servers",
    "skills",
}
DEFAULT_EXECUTOR_PREFIXES = [
    "python3 -m unittest",
    "python3 -B -m unittest",
    "python3 -m pytest",
    "pytest",
    "npm test",
    "pnpm test",
    "yarn test",
    "make test",
    "make lint",
    "go test",
    "cargo test",
    "dotnet test",
]
GATEABLE_TEST_PREFIXES = [
    "python3 -m unittest",
    "python3 -B -m unittest",
    "python -m unittest",
    "python3 -m pytest",
    "python -m pytest",
    "pytest",
    "npm test",
    "npm run test",
    "pnpm test",
    "pnpm run test",
    "yarn test",
    "yarn run test",
    "jest",
    "npx jest",
    "go test",
    "cargo test",
    "dotnet test",
    "make test",
]
DUMB_COMMAND_PREFIXES = ["echo", "true", "false", "cat", "pwd", "ls", "printf"]

SNAPSHOT_TABLES = [
    "project",
    "requirements",
    "acceptance",
    "requirement_acceptance",
    "failure_modes",
    "failure_mode_acceptance",
    "tasks",
    "task_acceptance",
    "task_failure_modes",
    "task_dependencies",
    "task_test_targets",
    "task_attempts",
    "validations",
    "validation_failure_modes",
    "validation_tests",
    "validation_evidence",
    "test_targets",
    "quality_gates",
    "quality_gate_findings",
    "deliveries",
    "delivery_acceptance",
    "evidence",
    "tests",
    "findings",
    "decisions",
    "adapters",
    "adapter_actions",
    "ci_verifications",
    "external_session_verifications",
    "invalidations",
    "agents",
    "agent_sessions",
    "session_attestations",
    "agent_capabilities",
    "executor_allowlist",
    "dispatch_runs",
    "dispatch_assignments",
    "dispatch_worktrees",
    "task_file_claims",
    "agent_reports",
    "agent_provider_sessions",
    "agent_provider_events",
    "sandbox_executions",
    "integration_attempts",
    "codex_fanout_exports",
    "runtime_snapshots",
    "command_log",
    "migrations",
    "events",
]


class HarnessError(Exception):
    """User-facing runtime error."""


_active_request: "contextvars.ContextVar[dict[str, Any] | None]" = contextvars.ContextVar("active_request", default=None)


_store_factory: Callable[[Path], Store] = SqliteStore


def set_store_factory(factory: Callable[[Path], Store]) -> None:
    """Test seam: override how stores are created."""
    global _store_factory
    _store_factory = factory


def get_store(root: Path) -> Store:
    return _store_factory(Path(root))


def db_file(root: Path) -> Path:
    return root / DB_PATH


@contextmanager
def connection(root: Path) -> Iterator[sqlite3.Connection]:
    with get_store(root).connection() as conn:
        yield conn


def transaction_invariant_issues(conn: sqlite3.Connection, root: Path, touched: list[tuple[str, str]] | None = None) -> list[object]:
    exists = conn.execute("select 1 from sqlite_master where type='table' and name = 'project'").fetchone()
    if not exists:
        return []
    from core.invariant_checker import check_runtime_invariants

    return check_runtime_invariants(conn, root, scope=touched or [], full=False)


def full_invariant_issues(conn: sqlite3.Connection, root: Path) -> list[object]:
    exists = conn.execute("select 1 from sqlite_master where type='table' and name = 'project'").fetchone()
    if not exists:
        return []
    from core.invariant_checker import check_runtime_invariants

    return check_runtime_invariants(conn, root)


def require_full_invariants(conn: sqlite3.Connection, root: Path, label: str) -> None:
    issues = full_invariant_issues(conn, root)
    if issues:
        raise HarnessError(f"{label} invariant failed: " + "; ".join(str(issue) for issue in issues))


def stable_args_hash(command: str, args: dict[str, Any]) -> str:
    payload_json = json.dumps({"command": command, "args": args}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def insert_active_command_log(conn: sqlite3.Connection) -> None:
    req = _active_request.get()
    if not req or not req.get("request_id") or req.get("inserted"):
        return
    conn.execute(
        """
        insert into command_log (request_id, command, args_hash, result_json, created_at)
        values (?, ?, ?, '', ?)
        """,
        (req["request_id"], req["command"], req["args_hash"], now_iso()),
    )
    req["inserted"] = True
    fail_request_id = os.environ.get("HARNESS_TEST_FAIL_AFTER_COMMAND_LOG", "")
    if fail_request_id and fail_request_id == req["request_id"]:
        raise HarnessError(f"test command_log rollback: {fail_request_id}")


@contextmanager
def transaction(root: Path, *, validate_invariants: bool = True, touched: list[tuple[str, str]] | None = None) -> Iterator[sqlite3.Connection]:
    def before_commit(conn: sqlite3.Connection) -> None:
        insert_active_command_log(conn)
        if validate_invariants:
            issues = transaction_invariant_issues(conn, root, touched)
            if issues:
                raise HarnessError("; ".join(str(issue) for issue in issues))

    with get_store(root).transaction(before_commit=before_commit) as conn:
        yield conn


def run_idempotent(root: Path, request_id: str | None, command: str, args: dict[str, Any], fn: Callable[[], str]) -> str:
    if not request_id:
        return fn()
    args_hash = stable_args_hash(command, args)
    with connection(root) as conn:
        existing = conn.execute("select args_hash, result_json from command_log where request_id = ?", (request_id,)).fetchone()
    if existing:
        if existing["args_hash"] != args_hash:
            raise HarnessError(f"idempotency-conflict: {request_id}")
        return existing["result_json"] if existing["result_json"] else f"already-applied: {request_id}"

    token = _active_request.set({"request_id": request_id, "command": command, "args_hash": args_hash, "inserted": False})
    try:
        result = fn()
    except sqlite3.IntegrityError:
        _active_request.reset(token)
        return run_idempotent(root, request_id, command, args, fn)
    except Exception:
        _active_request.reset(token)
        raise
    else:
        _active_request.reset(token)

    try:
        with transaction(root, validate_invariants=False) as conn:
            conn.execute("update command_log set result_json = ? where request_id = ?", (result, request_id))
    except Exception:
        pass
    return result


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        create table if not exists project (
            id integer primary key check (id = 1),
            project_id text not null,
            schema_version integer not null,
            runtime_version text not null,
            phase text not null,
            status text not null,
            scope_status text not null,
            current_owner text not null,
            revision integer not null,
            updated_at text not null
        );
        create table if not exists acceptance (
            id text primary key,
            criterion text not null,
            priority text not null default '',
            tool_link text not null default '',
            status text not null default 'active',
            revision integer not null default 1
        );
        create table if not exists requirements (
            id text primary key,
            kind text not null,
            body text not null,
            priority text not null default '',
            status text not null default 'active',
            tool_link text not null default '',
            revision integer not null default 1,
            updated_at text not null
        );
        create table if not exists failure_modes (
            id text primary key,
            feature text not null,
            scenario text not null,
            trigger text not null,
            expected_behavior text not null,
            recovery text not null default '',
            data_safety text not null default '',
            risk text not null,
            status text not null,
            accepted_by text,
            acceptance_reason text,
            acceptance_scope text not null default '',
            accepted_revision integer,
            expires_at text,
            revision integer not null default 1
        );
        create table if not exists requirement_acceptance (
            requirement_id text not null references requirements(id) on delete cascade,
            acceptance_id text not null references acceptance(id) on delete cascade,
            primary key (requirement_id, acceptance_id)
        );
        create table if not exists failure_mode_acceptance (
            failure_mode_id text not null references failure_modes(id) on delete cascade,
            acceptance_id text not null references acceptance(id) on delete cascade,
            primary key (failure_mode_id, acceptance_id)
        );
        create table if not exists baselines (
            id text primary key,
            summary text not null,
            snapshot_json text not null,
            digest text not null,
            project_revision integer not null,
            created_by text not null default '',
            created_at text not null
        );
        create table if not exists tasks (
            id text primary key,
            task text not null,
            owner text not null,
            status text not null,
            evidence text not null default '',
            tool_link text not null default '',
            submitted_by text not null default '',
            submitted_session_id text not null default '',
            accepted_by text not null default '',
            accepted_session_id text not null default '',
            lease_agent text,
            lease_token text,
            lease_heartbeat_at text,
            lease_expires_at text,
            retry_count integer not null default 0,
            retry_budget integer not null default 2,
            fence integer not null default 0,
            revision integer not null default 1,
            updated_at text not null
        );
        create table if not exists task_acceptance (
            task_id text not null references tasks(id) on delete cascade,
            acceptance_id text not null references acceptance(id) on delete cascade,
            primary key (task_id, acceptance_id)
        );
        create table if not exists task_failure_modes (
            task_id text not null references tasks(id) on delete cascade,
            failure_mode_id text not null references failure_modes(id) on delete cascade,
            primary key (task_id, failure_mode_id)
        );
        create table if not exists task_dependencies (
            task_id text not null references tasks(id) on delete cascade,
            depends_on text not null references tasks(id) on delete restrict,
            primary key (task_id, depends_on),
            check (task_id != depends_on)
        );
        create table if not exists task_test_targets (
            task_id text not null references tasks(id) on delete cascade,
            target_id text not null references test_targets(id) on delete cascade,
            primary key (task_id, target_id)
        );
        create table if not exists task_attempts (
            id text primary key,
            run_id text not null,
            task_id text not null references tasks(id) on delete cascade,
            agent_id text not null,
            fence integer not null default 0,
            base_commit_sha text not null default '',
            head_commit_sha text not null default '',
            tree_sha text not null default '',
            branch_name text not null default '',
            target_id text not null default '',
            status text not null,
            provider_session_id text not null default '',
            agent_session_id text not null default '',
            report_id text not null default '',
            evidence_id text not null default '',
            started_at text not null default '',
            finished_at text not null default ''
        );
        create table if not exists validations (
            id text primary key,
            surface text not null,
            acceptance_id text not null default '',
            commands text not null default '',
            command text not null default '',
            exit_code integer,
            stdout_sha256 text not null default '',
            artifact_path text not null default '',
            target_id text not null default '',
            executed_count integer not null default 0,
            executed_count_source text not null default '',
            allow_unlisted integer not null default 0,
            no_network integer not null default 0,
            sandbox_profile text not null default 'none',
            sandbox_status text not null default '',
            sandbox_execution_id text not null default '',
            sandbox_engine text not null default '',
            container_image text not null default '',
            allow_unlisted_reason text not null default '',
            trust_anchor text not null default 'local-only',
            trust_anchor_id text not null default '',
            policy_status text not null default '',
            policy_reason text not null default '',
            findings text not null,
            result text not null,
            residual_risk text not null default '',
            head_commit text not null default '',
            source_tree_hash text not null default '',
            attempt_id text not null default '',
            tree_sha text not null default '',
            code_ref text not null default '',
            verified_by text not null default '',
            tracked_diff_hash text not null default '',
            project_revision integer not null default 0,
            created_at text not null
        );
        create table if not exists validation_failure_modes (
            validation_id text not null references validations(id) on delete cascade,
            failure_mode_id text not null references failure_modes(id) on delete cascade,
            primary key (validation_id, failure_mode_id)
        );
        create table if not exists validation_tests (
            validation_id text not null references validations(id) on delete cascade,
            test_id text not null references tests(id) on delete cascade,
            primary key (validation_id, test_id)
        );
        create table if not exists validation_evidence (
            validation_id text not null references validations(id) on delete cascade,
            evidence_id text not null references evidence(id) on delete cascade,
            primary key (validation_id, evidence_id)
        );
        create table if not exists test_targets (
            id text primary key,
            kind text not null,
            command_template text not null,
            description text not null default '',
            gateable integer not null default 1,
            gate_block_reason text not null default '',
            created_at text not null,
            updated_at text not null
        );
        create table if not exists quality_gates (
            id text primary key,
            gate text not null,
            reviewed_commit text not null,
            evidence_commit text not null default '',
            diff_hash text not null default '',
            base_commit text not null default '',
            head_commit text not null default '',
            tracked_diff_hash text not null default '',
            project_revision integer not null default 0,
            reviewer_context text not null,
            result text not null,
            blocking_findings text not null default '',
            commands text not null default '',
            evidence text not null default '',
            residual_risk text not null default '',
            reviewer_session_id text not null default '',
            reviewer_attestation_id text not null default '',
            review_trust_level text not null default 'local-only',
            created_at text not null
        );
        create table if not exists quality_gate_findings (
            gate_id text not null references quality_gates(id) on delete cascade,
            finding_id text not null references findings(id) on delete cascade,
            primary key (gate_id, finding_id)
        );
        create table if not exists deliveries (
            id text primary key,
            scope text not null,
            acceptance text not null default '',
            changed_files text not null default '',
            validation text not null default '',
            qa text not null default '',
            failure_mode_coverage text not null default '',
            quality_gate text not null default '',
            data_config_notes text not null default '',
            collaboration_links text not null default '',
            known_gaps text not null default '',
            handoff text not null default '',
            created_at text not null
        );
        create table if not exists delivery_acceptance (
            delivery_id text not null references deliveries(id) on delete cascade,
            acceptance_id text not null references acceptance(id) on delete cascade,
            primary key (delivery_id, acceptance_id)
        );
        create table if not exists evidence (
            id text primary key,
            kind text not null,
            summary text not null,
            uri text not null default '',
            hash text not null default '',
            command text not null default '',
            exit_code integer,
            stdout_sha256 text not null default '',
            artifact_path text not null default '',
            source_tree_hash text not null default '',
            target_id text not null default '',
            executed_count integer not null default 0,
            executed_count_source text not null default '',
            allow_unlisted integer not null default 0,
            no_network integer not null default 0,
            sandbox_profile text not null default 'none',
            sandbox_status text not null default '',
            sandbox_execution_id text not null default '',
            sandbox_engine text not null default '',
            container_image text not null default '',
            allow_unlisted_reason text not null default '',
            trust_anchor text not null default 'local-only',
            trust_anchor_id text not null default '',
            policy_status text not null default '',
            policy_reason text not null default '',
            attempt_id text not null default '',
            tree_sha text not null default '',
            code_ref text not null default '',
            verified_by text not null default '',
            created_at text not null
        );
        create table if not exists tests (
            id text primary key,
            surface text not null,
            command text not null default '',
            result text not null,
            evidence_id text not null default '',
            created_at text not null
        );
        create table if not exists findings (
            id text primary key,
            surface text not null,
            severity text not null,
            status text not null,
            summary text not null,
            evidence_id text not null default '',
            created_at text not null
        );
        create table if not exists decisions (
            id text primary key,
            decision text not null,
            reason text not null,
            created_at text not null
        );
        create table if not exists adapters (
            id text primary key,
            tool text not null,
            mode text not null,
            artifact text not null,
            external_id text not null default '',
            external_link text not null default '',
            idempotency_key text not null,
            evidence text not null default '',
            fallback text not null default '',
            confirmation_needed text not null default 'no',
            updated_at text not null,
            unique(tool, idempotency_key)
        );
        create table if not exists adapter_actions (
            id text primary key,
            tool text not null,
            mode text not null,
            artifact text not null,
            action text not null,
            payload_json text not null default '{}',
            status text not null,
            confirmation text not null default '',
            external_id text not null default '',
            external_link text not null default '',
            idempotency_key text not null,
            created_at text not null,
            updated_at text not null,
            unique(tool, idempotency_key)
        );
        create table if not exists invalidations (
            id text primary key,
            source_type text not null,
            source_id text not null,
            target_type text not null,
            target_id text not null,
            reason text not null,
            resolved_at text,
            created_at text not null
        );
        create table if not exists agents (
            id text primary key,
            role text not null,
            template_path text not null,
            status text not null,
            tool_permissions text not null default '',
            session_id text not null default '',
            lease_task_id text not null default '',
            updated_at text not null
        );
        create table if not exists agent_sessions (
            session_id text primary key,
            agent_id text not null,
            role text not null,
            context_id text not null,
            provider_session_id text not null default '',
            origin text not null default 'manual',
            trust_level text not null default 'local-only',
            status text not null default 'active',
            started_at text not null,
            ended_at text not null default ''
        );
        create table if not exists session_attestations (
            id text primary key,
            session_id text not null,
            agent_id text not null,
            role text not null,
            context_id text not null,
            provider_session_id text not null default '',
            origin text not null default 'manual',
            verification_token text not null default '',
            token_status text not null default 'unchecked',
            token_reason text not null default '',
            trust_level text not null default 'local-only',
            created_at text not null
        );
        create table if not exists ci_verifications (
            id text primary key,
            provider text not null,
            run_id text not null,
            conclusion text not null,
            commit_sha text not null,
            origin text not null default 'manual',
            verification_token text not null default '',
            token_status text not null default 'unchecked',
            token_reason text not null default '',
            external_link text not null default '',
            created_at text not null,
            unique(provider, run_id)
        );
        create table if not exists external_session_verifications (
            id text primary key,
            session_id text not null,
            verifier text not null,
            conclusion text not null,
            commit_sha text not null,
            origin text not null default 'manual',
            verification_token text not null default '',
            token_status text not null default 'unchecked',
            token_reason text not null default '',
            external_link text not null default '',
            created_at text not null,
            unique(session_id, verifier)
        );
        create table if not exists agent_capabilities (
            agent_id text not null references agents(id) on delete cascade,
            capability text not null,
            primary key (agent_id, capability)
        );
        create table if not exists executor_allowlist (
            id text primary key,
            prefix text not null unique,
            reason text not null,
            created_at text not null
        );
        create table if not exists dispatch_runs (
            id text primary key,
            scope text not null,
            status text not null,
            created_at text not null,
            updated_at text not null
        );
        create table if not exists dispatch_assignments (
            run_id text not null references dispatch_runs(id) on delete cascade,
            task_id text not null references tasks(id) on delete cascade,
            agent_id text not null default '',
            capability text not null default '',
            status text not null,
            evidence text not null default '',
            provider_session_id text not null default '',
            claimed_at text,
            heartbeat_at text,
            lease_expires_at text,
            updated_at text not null,
            primary key (run_id, task_id)
        );
        create table if not exists dispatch_worktrees (
            id text primary key,
            run_id text not null,
            task_id text not null,
            agent_id text not null,
            branch_name text not null,
            worktree_path text not null,
            status text not null,
            created_at text not null,
            cleaned_at text not null default ''
        );
        create table if not exists task_file_claims (
            id text primary key,
            run_id text not null,
            task_id text not null,
            agent_id text not null,
            path text not null,
            worktree_path text not null default '',
            branch_name text not null default '',
            status text not null,
            created_at text not null,
            released_at text not null default ''
        );
        create unique index if not exists task_file_claims_active_path
            on task_file_claims(path) where status = 'active';
        create table if not exists agent_reports (
            id text primary key,
            run_id text not null,
            task_id text not null,
            provider_session_id text not null default '',
            job_id text not null default '',
            status text not null,
            last_error text not null default '',
            result_json text not null,
            created_at text not null
        );
        create table if not exists agent_provider_sessions (
            id text primary key,
            run_id text not null,
            task_id text not null,
            provider text not null,
            provider_session_id text not null default '',
            provider_job_id text not null default '',
            agent_id text not null default '',
            status text not null,
            fence integer not null default 0,
            agent_session_id text not null default '',
            branch_name text not null default '',
            worktree_path text not null default '',
            input_json text not null default '',
            report_id text not null default '',
            attempt_id text not null default '',
            last_error text not null default '',
            spawned_at text not null default '',
            heartbeat_at text not null default '',
            lease_expires_at text not null default '',
            collected_at text not null default '',
            cancelled_at text not null default '',
            finished_at text not null default '',
            unique(run_id, task_id, provider)
        );
        create table if not exists agent_provider_events (
            id text primary key,
            session_id text not null,
            run_id text not null,
            task_id text not null,
            provider text not null,
            event_type text not null,
            payload_json text not null default '',
            created_at text not null
        );
        create table if not exists sandbox_executions (
            id text primary key,
            runner text not null,
            engine text not null default '',
            image text not null default '',
            command text not null,
            target_id text not null default '',
            source_ref text not null default '',
            tree_sha text not null default '',
            network_mode text not null default '',
            timeout_seconds integer not null default 0,
            resource_limits text not null default '',
            exit_code integer,
            artifact_path text not null default '',
            artifact_sha256 text not null default '',
            sandbox_status text not null,
            started_at text not null,
            finished_at text not null default ''
        );
        create table if not exists integration_attempts (
            id text primary key,
            run_id text not null,
            target_branch text not null,
            integration_worktree text not null default '',
            base_ref text not null default '',
            merged_branches text not null default '',
            status text not null,
            validation_result text not null default '',
            finding_id text not null default '',
            started_at text not null,
            finished_at text not null default ''
        );
        create table if not exists codex_fanout_exports (
            id text primary key,
            run_id text not null,
            input_csv_path text not null,
            instruction_path text not null,
            output_schema_path text not null,
            spawn_config_path text not null,
            max_concurrency integer not null,
            max_runtime_seconds integer not null,
            status text not null,
            created_at text not null,
            imported_at text not null default ''
        );
        create table if not exists runtime_snapshots (
            id text primary key,
            label text not null,
            event_sequence integer not null,
            snapshot_json text not null,
            created_at text not null
        );
        create table if not exists command_log (
            request_id text primary key,
            command text not null,
            args_hash text not null,
            result_json text not null default '',
            created_at text not null
        );
        create table if not exists migrations (
            id integer primary key autoincrement,
            from_version integer not null,
            to_version integer not null,
            applied_at text not null
        );
        create table if not exists events (
            sequence integer primary key autoincrement,
            id text not null unique,
            schema_version integer not null,
            type text not null,
            source text not null,
            target text not null,
            correlation_id text not null default '',
            causation_id text not null default '',
            idempotency_key text not null default '',
            payload_json text not null,
            created_at text not null
        );
        """
    )
    ensure_column(conn, "failure_modes", "acceptance_scope", "text not null default ''")
    ensure_column(conn, "failure_modes", "accepted_revision", "integer")
    ensure_column(conn, "tasks", "submitted_by", "text not null default ''")
    ensure_column(conn, "tasks", "submitted_session_id", "text not null default ''")
    ensure_column(conn, "tasks", "accepted_by", "text not null default ''")
    ensure_column(conn, "tasks", "accepted_session_id", "text not null default ''")
    ensure_column(conn, "tasks", "lease_heartbeat_at", "text")
    ensure_column(conn, "tasks", "lease_expires_at", "text")
    ensure_column(conn, "tasks", "fence", "integer not null default 0")
    ensure_column(conn, "quality_gates", "base_commit", "text not null default ''")
    ensure_column(conn, "quality_gates", "head_commit", "text not null default ''")
    ensure_column(conn, "quality_gates", "tracked_diff_hash", "text not null default ''")
    ensure_column(conn, "quality_gates", "project_revision", "integer not null default 0")
    ensure_column(conn, "quality_gates", "reviewer_session_id", "text not null default ''")
    ensure_column(conn, "quality_gates", "reviewer_attestation_id", "text not null default ''")
    ensure_column(conn, "quality_gates", "review_trust_level", "text not null default 'local-only'")
    ensure_column(conn, "validations", "head_commit", "text not null default ''")
    ensure_column(conn, "validations", "source_tree_hash", "text not null default ''")
    ensure_column(conn, "validations", "attempt_id", "text not null default ''")
    ensure_column(conn, "validations", "tree_sha", "text not null default ''")
    ensure_column(conn, "validations", "code_ref", "text not null default ''")
    ensure_column(conn, "validations", "verified_by", "text not null default ''")
    ensure_column(conn, "validations", "tracked_diff_hash", "text not null default ''")
    ensure_column(conn, "validations", "project_revision", "integer not null default 0")
    ensure_column(conn, "validations", "command", "text not null default ''")
    ensure_column(conn, "validations", "exit_code", "integer")
    ensure_column(conn, "validations", "stdout_sha256", "text not null default ''")
    ensure_column(conn, "validations", "artifact_path", "text not null default ''")
    ensure_column(conn, "validations", "target_id", "text not null default ''")
    ensure_column(conn, "validations", "executed_count", "integer not null default 0")
    ensure_column(conn, "validations", "executed_count_source", "text not null default ''")
    ensure_column(conn, "validations", "allow_unlisted", "integer not null default 0")
    ensure_column(conn, "validations", "no_network", "integer not null default 0")
    ensure_column(conn, "validations", "sandbox_profile", "text not null default 'none'")
    ensure_column(conn, "validations", "sandbox_status", "text not null default ''")
    ensure_column(conn, "validations", "sandbox_execution_id", "text not null default ''")
    ensure_column(conn, "validations", "sandbox_engine", "text not null default ''")
    ensure_column(conn, "validations", "container_image", "text not null default ''")
    ensure_column(conn, "validations", "allow_unlisted_reason", "text not null default ''")
    ensure_column(conn, "validations", "trust_anchor", "text not null default 'local-only'")
    ensure_column(conn, "validations", "trust_anchor_id", "text not null default ''")
    ensure_column(conn, "validations", "policy_status", "text not null default ''")
    ensure_column(conn, "validations", "policy_reason", "text not null default ''")
    ensure_column(conn, "test_targets", "gateable", "integer not null default 1")
    ensure_column(conn, "test_targets", "gate_block_reason", "text not null default ''")
    ensure_column(conn, "evidence", "command", "text not null default ''")
    ensure_column(conn, "evidence", "exit_code", "integer")
    ensure_column(conn, "evidence", "stdout_sha256", "text not null default ''")
    ensure_column(conn, "evidence", "artifact_path", "text not null default ''")
    ensure_column(conn, "evidence", "source_tree_hash", "text not null default ''")
    ensure_column(conn, "evidence", "attempt_id", "text not null default ''")
    ensure_column(conn, "evidence", "tree_sha", "text not null default ''")
    ensure_column(conn, "evidence", "code_ref", "text not null default ''")
    ensure_column(conn, "evidence", "verified_by", "text not null default ''")
    ensure_column(conn, "evidence", "target_id", "text not null default ''")
    ensure_column(conn, "evidence", "executed_count", "integer not null default 0")
    ensure_column(conn, "evidence", "executed_count_source", "text not null default ''")
    ensure_column(conn, "evidence", "allow_unlisted", "integer not null default 0")
    ensure_column(conn, "evidence", "no_network", "integer not null default 0")
    ensure_column(conn, "evidence", "sandbox_profile", "text not null default 'none'")
    ensure_column(conn, "evidence", "sandbox_status", "text not null default ''")
    ensure_column(conn, "evidence", "sandbox_execution_id", "text not null default ''")
    ensure_column(conn, "evidence", "sandbox_engine", "text not null default ''")
    ensure_column(conn, "evidence", "container_image", "text not null default ''")
    ensure_column(conn, "evidence", "allow_unlisted_reason", "text not null default ''")
    ensure_column(conn, "evidence", "trust_anchor", "text not null default 'local-only'")
    ensure_column(conn, "evidence", "trust_anchor_id", "text not null default ''")
    ensure_column(conn, "evidence", "policy_status", "text not null default ''")
    ensure_column(conn, "evidence", "policy_reason", "text not null default ''")
    ensure_column(conn, "ci_verifications", "origin", "text not null default 'manual'")
    ensure_column(conn, "ci_verifications", "verification_token", "text not null default ''")
    ensure_column(conn, "ci_verifications", "token_status", "text not null default 'unchecked'")
    ensure_column(conn, "ci_verifications", "token_reason", "text not null default ''")
    ensure_column(conn, "external_session_verifications", "origin", "text not null default 'manual'")
    ensure_column(conn, "external_session_verifications", "verification_token", "text not null default ''")
    ensure_column(conn, "external_session_verifications", "token_status", "text not null default 'unchecked'")
    ensure_column(conn, "external_session_verifications", "token_reason", "text not null default ''")
    ensure_column(conn, "dispatch_assignments", "heartbeat_at", "text")
    ensure_column(conn, "dispatch_assignments", "lease_expires_at", "text")
    ensure_column(conn, "dispatch_assignments", "provider_session_id", "text not null default ''")
    ensure_column(conn, "task_attempts", "provider_session_id", "text not null default ''")
    ensure_column(conn, "task_attempts", "agent_session_id", "text not null default ''")
    ensure_column(conn, "agent_reports", "provider_session_id", "text not null default ''")
    ensure_column(conn, "agent_provider_sessions", "agent_session_id", "text not null default ''")
    ensure_default_executor_allowlist(conn)


def ensure_runtime_gitignore(root: Path) -> None:
    path = root / ".gitignore"
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    normalized = {line.strip() for line in existing}
    missing = [pattern for pattern in RUNTIME_GITIGNORE_PATTERNS if pattern not in normalized]
    if not missing:
        return
    ensure_parent(path)
    lines = existing[:]
    if lines and lines[-1].strip():
        lines.append("")
    lines.append("# Codex Project Harness runtime state")
    lines.extend(missing)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def git_tracked_runtime_paths(root: Path) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "ls-files", ".ai-team/state", ".ai-team/backups", ".ai-team/runtime"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def gitignore_runtime_issues(root: Path) -> list[str]:
    issues: list[str] = []
    path = root / ".gitignore"
    lines = {line.strip() for line in path.read_text(encoding="utf-8").splitlines()} if path.exists() else set()
    for pattern in RUNTIME_GITIGNORE_PATTERNS:
        if pattern not in lines:
            issues.append(f"missing .gitignore runtime pattern: {pattern}")
    tracked = git_tracked_runtime_paths(root)
    if tracked:
        issues.append(
            "runtime state is tracked by git: "
            + ", ".join(tracked)
            + " (fix with: git rm --cached "
            + " ".join(tracked)
            + ")"
        )
    key_path = configured_key_path(root)
    if key_path is not None:
        try:
            rel_key_path = key_path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            rel_key_path = ""
        if rel_key_path:
            try:
                tracked_key = subprocess.run(
                    ["git", "ls-files", "--error-unmatch", rel_key_path],
                    cwd=root,
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except (OSError, subprocess.CalledProcessError):
                tracked_key = None
            if tracked_key is not None:
                issues.append(
                    f"connector key file is tracked by git: {rel_key_path} "
                    f"(fix with: git rm --cached {rel_key_path})"
                )
    return issues


def ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    columns = {row["name"] for row in conn.execute(f"pragma table_info({table})")}
    if column not in columns:
        conn.execute(f"alter table {table} add column {column} {ddl}")


def ensure_default_executor_allowlist(conn: sqlite3.Connection) -> None:
    for prefix in DEFAULT_EXECUTOR_PREFIXES:
        conn.execute(
            """
            insert into executor_allowlist (id, prefix, reason, created_at)
            values (?, ?, ?, ?)
            on conflict(prefix) do nothing
            """,
            (f"default-{hashlib.sha256(prefix.encode('utf-8')).hexdigest()[:12]}", prefix, "default safe test prefix", now_iso()),
        )


def initialize_project(conn: sqlite3.Connection) -> None:
    existing = conn.execute("select id from project where id = 1").fetchone()
    if existing:
        return
    conn.execute(
        """
        insert into project
        (id, project_id, schema_version, runtime_version, phase, status, scope_status, current_owner, revision, updated_at)
        values (1, ?, ?, ?, 'intake', 'draft', 'unconfirmed', 'project-manager', 1, ?)
        """,
        (str(uuid.uuid4()), SCHEMA_VERSION, RUNTIME_VERSION, now_iso()),
    )


def emit_event(
    conn: sqlite3.Connection,
    event_type: str,
    payload_json: str,
    *,
    source: str = "harness-runtime",
    target: str = "project",
    idempotency_key: str = "",
    correlation_id: str = "",
    causation_id: str = "",
) -> None:
    from core.event_bus import emit

    emit(
        conn,
        SCHEMA_VERSION,
        event_type,
        payload_json,
        source=source,
        target=target,
        idempotency_key=idempotency_key,
        correlation_id=correlation_id,
        causation_id=causation_id,
    )


def payload(**values: object) -> str:
    from core.event_bus import payload as event_payload

    return event_payload(**values)


def stable_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_digest(value: object) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def row_snapshot(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def table_rows(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    return [row_snapshot(row) or {} for row in conn.execute(f"select * from {table} order by 1")]


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row["name"] for row in conn.execute(f"pragma table_info({table})")]


def runtime_snapshot(conn: sqlite3.Connection, *, include_events: bool = True) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for table in SNAPSHOT_TABLES:
        if table == "events" and not include_events:
            continue
        exists = conn.execute("select 1 from sqlite_master where type='table' and name = ?", (table,)).fetchone()
        if exists:
            data[table] = table_rows(conn, table)
    return data


def restore_snapshot(conn: sqlite3.Connection, snapshot: dict[str, Any]) -> None:
    create_schema(conn)
    conn.execute("pragma foreign_keys = off")
    try:
        for table in reversed(SNAPSHOT_TABLES):
            if table in snapshot:
                conn.execute(f"delete from {table}")
        for table in SNAPSHOT_TABLES:
            rows = snapshot.get(table, [])
            if not rows:
                continue
            columns = table_columns(conn, table)
            writable = [column for column in columns if column in rows[0]]
            placeholders = ",".join("?" for _ in writable)
            column_sql = ",".join(writable)
            for row in rows:
                conn.execute(
                    f"insert into {table} ({column_sql}) values ({placeholders})",
                    [row.get(column) for column in writable],
                )
    finally:
        conn.execute("pragma foreign_keys = on")


def baseline_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        "requirements": table_rows(conn, "requirements"),
        "acceptance": table_rows(conn, "acceptance"),
        "requirement_acceptance": table_rows(conn, "requirement_acceptance"),
        "failure_modes": table_rows(conn, "failure_modes"),
        "failure_mode_acceptance": table_rows(conn, "failure_mode_acceptance"),
    }


def baseline_digest(conn: sqlite3.Connection) -> str:
    return stable_digest(baseline_snapshot(conn))


def latest_baseline(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("select * from baselines order by created_at desc, id desc limit 1").fetchone()


def baseline_issues(conn: sqlite3.Connection) -> list[str]:
    row = latest_baseline(conn)
    if not row:
        return ["missing frozen baseline"]
    current = baseline_digest(conn)
    if row["digest"] != current:
        return [f"frozen baseline is stale: {row['id']}"]
    return []


def validation_has_test_or_evidence(conn: sqlite3.Connection, validation_id: str) -> bool:
    return bool(
        conn.execute(
            """
            select 1 from validation_tests vt
            join tests t on t.id = vt.test_id
            where vt.validation_id = ? and t.result = 'pass'
            union
            select 1 from validation_evidence ve
            join evidence e on e.id = ve.evidence_id
            where ve.validation_id = ?
            limit 1
            """,
            (validation_id, validation_id),
        ).fetchone()
    )


def trace_snapshot(conn: sqlite3.Connection, requirement_id: str) -> dict[str, Any]:
    return {
        "requirement_id": requirement_id,
        "acceptance_ids": [
            row["acceptance_id"]
            for row in conn.execute(
                "select acceptance_id from requirement_acceptance where requirement_id = ? order by acceptance_id",
                (requirement_id,),
            )
        ],
    }


def emit_audit_event(
    conn: sqlite3.Connection,
    event_type: str,
    *,
    entity_type: str,
    entity_id: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    actor: str = "",
    command: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    from core.event_bus import emit_audit

    emit_audit(
        conn,
        SCHEMA_VERSION,
        event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        before=before,
        after=after,
        actor=actor,
        command=command,
        extra=extra,
    )


def parse_time(value: str | None) -> datetime | None:
    from core.lock_manager import parse_time as core_parse_time

    return core_parse_time(value)


def lease_deadline() -> str:
    from core.lock_manager import lease_deadline as core_lease_deadline

    return core_lease_deadline()


def is_expired(value: str | None) -> bool:
    from core.lock_manager import is_expired as core_is_expired

    return core_is_expired(value)


def normalize_failure_mode_status(status: str) -> str:
    return status if status in FAILURE_MODE_STATUSES else "identified"


def guard_schema(callable_name: str, *args: object) -> None:
    from core import schema_guard

    try:
        getattr(schema_guard, callable_name)(*args)
    except schema_guard.SchemaGuardError as exc:
        raise HarnessError(str(exc)) from exc


def normalize_artifact_path(root: Path, artifact_path: str) -> str:
    if not artifact_path:
        return ""
    candidate = Path(artifact_path)
    resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise HarnessError(f"artifact path must be inside project root: {artifact_path}") from exc


def bool_int(value: bool) -> int:
    return 1 if value else 0


def command_has_prefix(command: str, prefixes: list[str]) -> bool:
    from core.executor import command_matches_prefix

    return any(command_matches_prefix(command, prefix) for prefix in prefixes)


def target_gateability(kind: str, command_template: str) -> tuple[int, str]:
    if command_has_prefix(command_template, DUMB_COMMAND_PREFIXES):
        return 0, "not a gateable test target: command is a shell utility or placeholder"
    if kind in {"unit", "integration"} and not command_has_prefix(command_template, GATEABLE_TEST_PREFIXES):
        return 0, "not a gateable test target: unit/integration command must use a known test runner"
    return 1, ""


def normalize_manual_execution_fields(
    executed_count: int | None,
    command: str,
    *,
    sandbox_profile: str = "none",
    no_network: bool = False,
    allow_unlisted_reason: str = "",
) -> tuple[int, str, str, str, str, str, str]:
    profile = "no-network" if no_network else sandbox_profile
    sandbox_status = "unavailable" if profile == "no-network" else ""
    if executed_count is None:
        return 0, "", "manual" if command else "", "recorded without executor", profile, sandbox_status, allow_unlisted_reason
    return int(executed_count), "manual", "manual", "recorded via CLI", profile, sandbox_status, allow_unlisted_reason


def test_target_command(conn: sqlite3.Connection, target_id: str) -> str:
    if not target_id:
        return ""
    row = conn.execute("select command_template from test_targets where id = ?", (target_id,)).fetchone()
    if not row:
        raise HarnessError(f"missing test target: {target_id}")
    return row["command_template"]


def executor_prefixes(conn: sqlite3.Connection) -> list[str]:
    return [row["prefix"] for row in conn.execute("select prefix from executor_allowlist order by prefix")]


def bump_project(conn: sqlite3.Connection, **updates: str) -> None:
    project = conn.execute("select revision from project where id = 1").fetchone()
    revision = int(project["revision"]) + 1
    assignments = ["revision = ?", "updated_at = ?"]
    values: list[object] = [revision, now_iso()]
    for key, value in updates.items():
        assignments.append(f"{key} = ?")
        values.append(value)
    values.append(1)
    conn.execute(f"update project set {', '.join(assignments)} where id = ?", values)


def invalidate_downstream(conn: sqlite3.Connection, source_type: str, source_id: str, reason: str) -> None:
    targets: list[tuple[str, str]] = []
    if source_type == "acceptance":
        targets.extend(("task", row["task_id"]) for row in conn.execute("select task_id from task_acceptance where acceptance_id = ?", (source_id,)))
        targets.extend(("validation", row["id"]) for row in conn.execute("select id from validations where acceptance_id = ?", (source_id,)))
        targets.extend(("quality_gate", row["id"]) for row in conn.execute("select id from quality_gates"))
    elif source_type == "failure_mode":
        targets.extend(("task", row["task_id"]) for row in conn.execute("select task_id from task_failure_modes where failure_mode_id = ?", (source_id,)))
        targets.extend(
            ("validation", row["validation_id"])
            for row in conn.execute("select validation_id from validation_failure_modes where failure_mode_id = ?", (source_id,))
        )
        targets.extend(("quality_gate", row["id"]) for row in conn.execute("select id from quality_gates"))
    elif source_type == "requirement":
        acceptance_ids = [
            row["acceptance_id"]
            for row in conn.execute("select acceptance_id from requirement_acceptance where requirement_id = ?", (source_id,))
        ]
        for acceptance_id in acceptance_ids:
            targets.append(("acceptance", acceptance_id))
            targets.extend(("task", row["task_id"]) for row in conn.execute("select task_id from task_acceptance where acceptance_id = ?", (acceptance_id,)))
            targets.extend(("validation", row["id"]) for row in conn.execute("select id from validations where acceptance_id = ?", (acceptance_id,)))
        targets.extend(("quality_gate", row["id"]) for row in conn.execute("select id from quality_gates"))
    for target_type, target_id in targets:
        exists = conn.execute(
            """
            select 1 from invalidations
            where source_type = ? and source_id = ? and target_type = ? and target_id = ? and resolved_at is null
            """,
            (source_type, source_id, target_type, target_id),
        ).fetchone()
        if exists:
            continue
        conn.execute(
            """
            insert into invalidations (id, source_type, source_id, target_type, target_id, reason, created_at)
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            (str(uuid.uuid4()), source_type, source_id, target_type, target_id, reason, now_iso()),
        )


def resolve_invalidations(conn: sqlite3.Connection, *, source_type: str | None = None, source_id: str | None = None, target_type: str | None = None) -> None:
    clauses = ["resolved_at is null"]
    values: list[object] = []
    if source_type:
        clauses.append("source_type = ?")
        values.append(source_type)
    if source_id:
        clauses.append("source_id = ?")
        values.append(source_id)
    if target_type:
        clauses.append("target_type = ?")
        values.append(target_type)
    values.append(now_iso())
    conn.execute(f"update invalidations set resolved_at = ? where {' and '.join(clauses)}", [values[-1], *values[:-1]])


def init_runtime(root: Path) -> None:
    ensure_runtime_gitignore(root)
    if not db_file(root).exists() and has_legacy_markdown_data(root):
        migrate_markdown_v1(root)
        return
    with transaction(root, validate_invariants=False) as conn:
        create_schema(conn)
        initialize_project(conn)
        emit_event(conn, "runtime_initialized", payload())
        require_full_invariants(conn, root, "init")
    render_all(root)
    install_agents(root)


def backup_runtime(root: Path, reason: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = root / ".ai-team" / "backups" / f"{stamp}-{reason}-{uuid.uuid4().hex[:8]}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    for relpath in [
        ".ai-team/state",
        ".ai-team/control",
        ".ai-team/requirements",
        ".ai-team/planning",
        "docs/harness",
    ]:
        source = root / relpath
        if not source.exists():
            continue
        target = backup_dir / relpath
        ensure_parent(target)
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            shutil.copy2(source, target)
    return backup_dir


def restore_runtime_backup(root: Path, backup_dir: Path) -> None:
    for relpath in [
        ".ai-team/state",
        ".ai-team/control",
        ".ai-team/requirements",
        ".ai-team/planning",
        "docs/harness",
    ]:
        source = backup_dir / relpath
        target = root / relpath
        if not source.exists():
            continue
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        ensure_parent(target)
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            shutil.copy2(source, target)


def has_legacy_markdown_data(root: Path) -> bool:
    for relpath in [
        ".ai-team/requirements/acceptance.md",
        ".ai-team/requirements/failure-modes.md",
        ".ai-team/planning/task-board.md",
        "docs/harness/validation.md",
        "docs/harness/quality-gates.md",
        "docs/harness/delivery.md",
    ]:
        path = root / relpath
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        data_lines = [
            line
            for line in text.splitlines()
            if line.startswith("|") and "---" not in line and not line.lower().startswith("| id ")
        ]
        if data_lines:
            return True
    return False


def markdown_table_rows(path: Path) -> list[list[str]]:
    if not path.exists():
        return []
    rows: list[list[str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or "---" in stripped:
            continue
        cells = [cell.strip().replace("\\|", "|") for cell in stripped.strip("|").split("|")]
        if not cells or cells[0].lower() in {"id", "surface", "gate", "date"}:
            continue
        rows.append(cells)
    return rows


def empty_migration_report(dry_run: bool) -> dict[str, Any]:
    return {
        "dry_run": dry_run,
        "imported": {},
        "skipped": {},
        "unrecognized": [],
    }


def report_count(report: dict[str, Any], bucket: str, entity: str, count: int = 1) -> None:
    report[bucket][entity] = int(report[bucket].get(entity, 0)) + count


def write_migration_report(root: Path, report: dict[str, Any]) -> None:
    lines = [
        "# Migration Report",
        "",
        f"dry_run: {'yes' if report['dry_run'] else 'no'}",
        "",
        "## Imported",
        "",
        "| Entity | Count |",
        "| --- | --- |",
    ]
    for entity, count in sorted(report["imported"].items()):
        lines.append(markdown_row([entity, count]))
    lines.extend(["", "## Skipped", "", "| Entity | Count |", "| --- | --- |"])
    for entity, count in sorted(report["skipped"].items()):
        lines.append(markdown_row([entity, count]))
    lines.extend(["", "## Unrecognized", ""])
    if report["unrecognized"]:
        lines.extend(f"- {item}" for item in report["unrecognized"])
    else:
        lines.append("- none")
    write_view(root, "docs/harness/migration-report.md", "\n".join(lines))


def migrate_markdown_v1(root: Path, *, dry_run: bool = False) -> dict[str, Any]:
    report = empty_migration_report(dry_run)
    acceptance_rows = markdown_table_rows(root / ".ai-team/requirements/acceptance.md")
    requirement_rows = markdown_table_rows(root / ".ai-team/requirements/requirements.md")
    failure_mode_rows = markdown_table_rows(root / ".ai-team/requirements/failure-modes.md")
    task_rows = markdown_table_rows(root / ".ai-team/planning/task-board.md")
    validation_rows = markdown_table_rows(root / "docs/harness/validation.md")
    gate_rows = markdown_table_rows(root / "docs/harness/quality-gates.md")
    decision_rows = markdown_table_rows(root / ".ai-team/control/decision-log.md")
    delivery_path = root / "docs/harness/delivery.md"
    delivery_text = delivery_path.read_text(encoding="utf-8") if delivery_path.exists() else ""

    for entity, rows in [
        ("acceptance", acceptance_rows),
        ("requirement", requirement_rows),
        ("failure_mode", failure_mode_rows),
        ("task", task_rows),
        ("validation", validation_rows),
        ("quality_gate", gate_rows),
        ("decision", decision_rows),
    ]:
        report_count(report, "imported", entity, len(rows))
    if delivery_text.strip():
        report_count(report, "imported", "delivery", delivery_text.count("## Delivery Record") or 1)
    if dry_run:
        return report

    backup_dir = backup_runtime(root, "markdown-v1")
    try:
        with transaction(root, validate_invariants=False) as conn:
            create_schema(conn)
            initialize_project(conn)
            for cells in acceptance_rows:
                if len(cells) < 2:
                    report_count(report, "skipped", "acceptance")
                    continue
                conn.execute(
                    """
                    insert into acceptance (id, criterion, priority, tool_link, status)
                    values (?, ?, ?, ?, ?)
                    on conflict(id) do nothing
                    """,
                    (cells[0], cells[1], cells[2] if len(cells) > 2 else "", cells[3] if len(cells) > 3 else "", cells[4] if len(cells) > 4 else "active"),
                )
            for cells in requirement_rows:
                if len(cells) < 3:
                    report_count(report, "skipped", "requirement")
                    continue
                conn.execute(
                    """
                    insert into requirements (id, kind, body, priority, status, tool_link, revision, updated_at)
                    values (?, ?, ?, ?, ?, ?, ?, ?)
                    on conflict(id) do nothing
                    """,
                    (
                        cells[0],
                        cells[1],
                        cells[2],
                        cells[3] if len(cells) > 3 else "",
                        cells[4] if len(cells) > 4 else "active",
                        cells[5] if len(cells) > 5 else "",
                        int(cells[6]) if len(cells) > 6 and cells[6].isdigit() else 1,
                        now_iso(),
                    ),
                )
            for cells in failure_mode_rows:
                if len(cells) < 8:
                    report_count(report, "skipped", "failure_mode")
                    continue
                conn.execute(
                    """
                    insert into failure_modes
                    (id, feature, scenario, trigger, expected_behavior, recovery, data_safety, risk, status)
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    on conflict(id) do nothing
                    """,
                    (
                        cells[0],
                        cells[1],
                        cells[2],
                        cells[3],
                        cells[4],
                        cells[5] if len(cells) > 5 else "",
                        cells[6] if len(cells) > 6 else "",
                        cells[7] if len(cells) > 7 else "medium",
                        normalize_failure_mode_status(cells[9] if len(cells) > 9 else "identified"),
                    ),
                )
                if len(cells) > 8:
                    for acceptance_id in parse_ids(cells[8]):
                        if conn.execute("select id from acceptance where id = ?", (acceptance_id,)).fetchone():
                            conn.execute(
                                "insert or ignore into failure_mode_acceptance (failure_mode_id, acceptance_id) values (?, ?)",
                                (cells[0], acceptance_id),
                            )
            for cells in task_rows:
                if len(cells) < 4:
                    report_count(report, "skipped", "task")
                    continue
                status = cells[3] if cells[3] in TASK_STATUSES else "ready"
                conn.execute(
                    """
                    insert into tasks (id, task, owner, status, tool_link, evidence, updated_at)
                    values (?, ?, ?, ?, ?, ?, ?)
                    on conflict(id) do nothing
                    """,
                    (
                        cells[0],
                        cells[1],
                        cells[2] if len(cells) > 2 else "unassigned",
                        status,
                        cells[7] if len(cells) > 7 else "",
                        cells[8] if len(cells) > 8 else "",
                        now_iso(),
                    ),
                )
                for acceptance_id in parse_ids(cells[4] if len(cells) > 4 else ""):
                    if conn.execute("select id from acceptance where id = ?", (acceptance_id,)).fetchone():
                        conn.execute("insert or ignore into task_acceptance (task_id, acceptance_id) values (?, ?)", (cells[0], acceptance_id))
                for fm_id in parse_ids(cells[5] if len(cells) > 5 else ""):
                    if conn.execute("select id from failure_modes where id = ?", (fm_id,)).fetchone():
                        conn.execute("insert or ignore into task_failure_modes (task_id, failure_mode_id) values (?, ?)", (cells[0], fm_id))
            for cells in validation_rows:
                if len(cells) < 10:
                    report_count(report, "skipped", "validation")
                    continue
                validation_id = str(uuid.uuid4())
                conn.execute(
                    """
                    insert into validations
                    (id, surface, acceptance_id, commands, findings, result, residual_risk, head_commit,
                     source_tree_hash, tracked_diff_hash, project_revision, created_at)
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        validation_id,
                        cells[0],
                        cells[1] if len(cells) > 1 else "",
                        cells[8] if len(cells) > 8 else "",
                        cells[9] if len(cells) > 9 else "",
                        cells[10] if len(cells) > 10 else "partial",
                        cells[11] if len(cells) > 11 else "",
                        cells[3] if len(cells) > 3 else "",
                        cells[4] if len(cells) > 4 else "",
                        cells[5] if len(cells) > 5 else "",
                        int(cells[6]) if len(cells) > 6 and cells[6].isdigit() else 0,
                        now_iso(),
                    ),
                )
                for fm_id in parse_ids(cells[2] if len(cells) > 2 else ""):
                    if conn.execute("select id from failure_modes where id = ?", (fm_id,)).fetchone():
                        conn.execute(
                            "insert or ignore into validation_failure_modes (validation_id, failure_mode_id) values (?, ?)",
                            (validation_id, fm_id),
                        )
            for cells in gate_rows:
                if len(cells) < 9:
                    report_count(report, "skipped", "quality_gate")
                    continue
                conn.execute(
                    """
                    insert into quality_gates
                    (id, gate, reviewed_commit, evidence_commit, diff_hash, base_commit, head_commit, tracked_diff_hash,
                     project_revision, reviewer_context, result, blocking_findings, commands, evidence, residual_risk, created_at)
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        cells[0],
                        cells[1],
                        cells[1],
                        cells[4] if len(cells) > 4 else "",
                        cells[2] if len(cells) > 2 else "",
                        cells[3] if len(cells) > 3 else "",
                        cells[5] if len(cells) > 5 else "",
                        int(cells[6]) if len(cells) > 6 and cells[6].isdigit() else 0,
                        cells[7] if len(cells) > 7 else "external",
                        cells[8] if len(cells) > 8 else "blocked",
                        cells[9] if len(cells) > 9 else "",
                        cells[10] if len(cells) > 10 else "",
                        cells[11] if len(cells) > 11 else "",
                        cells[12] if len(cells) > 12 else "",
                        now_iso(),
                    ),
                )
            for cells in decision_rows:
                if len(cells) < 3:
                    report_count(report, "skipped", "decision")
                    continue
                conn.execute(
                    "insert into decisions (id, decision, reason, created_at) values (?, ?, ?, ?)",
                    (str(uuid.uuid4()), cells[1], cells[2], cells[0] or now_iso()),
                )
            if delivery_text.strip():
                conn.execute(
                    """
                    insert into deliveries (id, scope, handoff, created_at)
                    values (?, ?, ?, ?)
                    """,
                    (str(uuid.uuid4()), "Imported markdown-v1 delivery history", delivery_text.strip(), now_iso()),
                )
            conn.execute(
                "insert into migrations (from_version, to_version, applied_at) values (?, ?, ?)",
                (1, SCHEMA_VERSION, now_iso()),
            )
            emit_event(conn, "markdown_v1_migrated", payload(to=SCHEMA_VERSION))
            require_full_invariants(conn, root, "migration")
    except Exception:
        restore_runtime_backup(root, backup_dir)
        raise
    render_all(root)
    write_migration_report(root, report)
    install_agents(root)
    return report


def validate_codex_agent_template(path: Path) -> dict[str, Any]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HarnessError(f"invalid agent template {path.name}: {exc}") from exc
    missing = sorted(field for field in CODEX_AGENT_REQUIRED_FIELDS if not str(data.get(field, "")).strip())
    if missing:
        raise HarnessError(f"invalid agent template {path.name}: missing {', '.join(missing)}")
    extra = sorted(set(data) - CODEX_AGENT_ALLOWED_FIELDS)
    if extra:
        raise HarnessError(f"invalid agent template {path.name}: unsupported fields {', '.join(extra)}")
    expected_name = path.stem
    if data["name"] != expected_name:
        raise HarnessError(f"invalid agent template {path.name}: name must be {expected_name}")
    return data


def install_agents(root: Path, *, target_dir: str = ".codex/agents", force: bool = False, strict_no_overwrite: bool = False) -> int:
    template_dir = Path(__file__).resolve().parents[1] / "templates" / "agents"
    agent_dir = root / target_dir
    agent_dir.mkdir(parents=True, exist_ok=True)
    installed = 0
    with transaction(root) as conn:
        for template in sorted(template_dir.glob("*.toml")):
            data = validate_codex_agent_template(template)
            target = agent_dir / f"{data['name']}.toml"
            if target.exists() and strict_no_overwrite and not force:
                raise HarnessError(f"agent already exists: {target.relative_to(root)}")
            if force or not target.exists():
                shutil.copyfile(template, target)
                installed += 1
            role = data["name"]
            conn.execute(
                """
                insert into agents (id, role, template_path, status, updated_at)
                values (?, ?, ?, 'available', ?)
                on conflict(id) do update set template_path=excluded.template_path, status='available', updated_at=excluded.updated_at
                """,
                (role, role, str(target), now_iso()),
            )
        emit_event(conn, "agents_installed", payload(target_dir=target_dir, force=force, installed=installed))
    return installed


def project_row(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute("select * from project where id = 1").fetchone()
    if not row:
        raise HarnessError("project is not initialized")
    return row


def transition_phase(root: Path, phase: str, *, status: str | None = None, owner: str | None = None) -> None:
    with transaction(root, touched=[("project", "1")]) as conn:
        row = project_row(conn)
        current = row["phase"]
        if phase not in PHASES:
            raise HarnessError(f"unknown phase: {phase}")
        if phase != current and phase not in PHASE_TRANSITIONS[current]:
            raise HarnessError(f"illegal phase transition: {current} -> {phase}")
        issues = phase_prerequisite_issues(conn, phase)
        if issues:
            raise HarnessError(f"phase prerequisites blocked: {'; '.join(issues)}")
        if phase == "delivery_readiness":
            delivery_issues = validate_delivery(conn, root, require_phase=False)
            if delivery_issues:
                raise HarnessError("delivery readiness blocked: " + "; ".join(delivery_issues))
        updates: dict[str, str] = {"phase": phase}
        if status:
            updates["status"] = status
        if owner:
            updates["current_owner"] = owner
        bump_project(conn, **updates)
        after = project_row(conn)
        emit_audit_event(
            conn,
            "phase_updated",
            entity_type="project",
            entity_id=str(row["project_id"]),
            before=row_snapshot(row),
            after=row_snapshot(after),
            actor=owner or "",
            command="phase",
            extra={"from": current, "to": phase},
        )
    render_all(root)


def phase_prerequisite_issues(conn: sqlite3.Connection, phase: str) -> list[str]:
    issues: list[str] = []
    project = project_row(conn)
    requirement_count = conn.execute("select count(*) from requirements where status != 'cancelled'").fetchone()[0]
    acceptance_count = conn.execute("select count(*) from acceptance").fetchone()[0]
    task_count = conn.execute("select count(*) from tasks").fetchone()[0]
    if phase in {"confirmation", "team_architecture", "planning"} and requirement_count == 0:
        issues.append(f"{phase} requires at least one requirement baseline record")
    if phase in {"confirmation", "team_architecture", "planning"} and acceptance_count == 0:
        issues.append(f"{phase} requires at least one acceptance criterion")
    if phase in {"implementation", "qa"} and task_count == 0:
        issues.append(f"{phase} requires at least one task")
    if phase in {"planning", "implementation", "qa", "delivery_readiness"}:
        if project["scope_status"] != "confirmed":
            issues.append(f"{phase} requires confirmed scope")
        if baseline_issues(conn):
            issues.extend(f"{phase} requires current frozen baseline: {issue}" for issue in baseline_issues(conn))
    if phase == "qa":
        active = conn.execute(
            "select id, status from tasks where status in ('ready', 'claimed', 'in_progress', 'blocked') order by id"
        ).fetchall()
        for task in active:
            issues.append(f"qa requires implementation task submitted or accepted: {task['id']} status={task['status']}")
    return issues


def confirm_scope(root: Path, by: str, summary: str) -> None:
    with transaction(root, touched=[("project", "1")]) as conn:
        before = project_row(conn)
        bump_project(conn, scope_status="confirmed", current_owner=by, status="scope-confirmed")
        after = project_row(conn)
        emit_audit_event(
            conn,
            "scope_confirmed",
            entity_type="project",
            entity_id=str(before["project_id"]),
            before=row_snapshot(before),
            after=row_snapshot(after),
            actor=by,
            command="scope confirm",
            extra={"summary": summary},
        )
    render_all(root)


def freeze_baseline(root: Path, baseline_id: str, summary: str, *, by: str = "") -> None:
    with transaction(root, touched=[("baseline", baseline_id)]) as conn:
        snapshot = baseline_snapshot(conn)
        digest = stable_digest(snapshot)
        conn.execute(
            """
            insert into baselines (id, summary, snapshot_json, digest, project_revision, created_by, created_at)
            values (?, ?, ?, ?, ?, ?, ?)
            on conflict(id) do update set summary=excluded.summary, snapshot_json=excluded.snapshot_json,
              digest=excluded.digest, project_revision=excluded.project_revision, created_by=excluded.created_by,
              created_at=excluded.created_at
            """,
            (baseline_id, summary, stable_json(snapshot), digest, int(project_row(conn)["revision"]), by, now_iso()),
        )
        emit_audit_event(
            conn,
            "baseline_frozen",
            entity_type="baseline",
            entity_id=baseline_id,
            before=None,
            after={"id": baseline_id, "summary": summary, "digest": digest},
            actor=by,
            command="baseline freeze",
        )
    render_all(root)


def baseline_validate(root: Path) -> list[str]:
    with connection(root) as conn:
        return baseline_issues(conn)


def baseline_diff(root: Path, from_id: str, to: str = "current") -> list[str]:
    with connection(root) as conn:
        baseline = conn.execute("select * from baselines where id = ?", (from_id,)).fetchone()
        if not baseline:
            raise HarnessError(f"missing baseline: {from_id}")
        before = json.loads(baseline["snapshot_json"])
        after = baseline_snapshot(conn) if to == "current" else json.loads(conn.execute("select snapshot_json from baselines where id = ?", (to,)).fetchone()["snapshot_json"])
    lines = [f"# Baseline Diff {from_id} -> {to}", ""]
    for table in ["requirements", "acceptance", "requirement_acceptance", "failure_modes", "failure_mode_acceptance"]:
        before_rows = stable_digest(before.get(table, []))
        after_rows = stable_digest(after.get(table, []))
        status = "same" if before_rows == after_rows else "changed"
        lines.append(f"- {table}: {status}")
    return lines


def add_requirement(root: Path, requirement_id: str, kind: str, body: str, priority: str = "", status: str = "active", tool_link: str = "") -> None:
    guard_schema("validate_requirement", requirement_id, kind, body, status)
    with transaction(root, touched=[("requirement", requirement_id)]) as conn:
        existing = conn.execute("select * from requirements where id = ?", (requirement_id,)).fetchone()
        conn.execute(
            """
            insert into requirements (id, kind, body, priority, status, tool_link, updated_at)
            values (?, ?, ?, ?, ?, ?, ?)
            on conflict(id) do update set kind=excluded.kind, body=excluded.body, priority=excluded.priority,
              status=excluded.status, tool_link=excluded.tool_link, revision=requirements.revision+1, updated_at=excluded.updated_at
            """,
            (requirement_id, kind, body, priority, status, tool_link, now_iso()),
        )
        if existing and (existing["kind"], existing["body"], existing["priority"], existing["status"], existing["tool_link"]) != (kind, body, priority, status, tool_link):
            invalidate_downstream(conn, "requirement", requirement_id, "requirement changed")
        after = conn.execute("select * from requirements where id = ?", (requirement_id,)).fetchone()
        emit_audit_event(
            conn,
            "requirement_recorded",
            entity_type="requirement",
            entity_id=requirement_id,
            before=row_snapshot(existing),
            after=row_snapshot(after),
            command="requirement add",
            extra={"kind": kind},
        )
    render_all(root)


def add_acceptance(root: Path, acceptance_id: str, criterion: str, priority: str = "", tool_link: str = "") -> None:
    guard_schema("validate_acceptance", acceptance_id, criterion)
    with transaction(root, touched=[("acceptance", acceptance_id)]) as conn:
        existing = conn.execute("select * from acceptance where id = ?", (acceptance_id,)).fetchone()
        conn.execute(
            """
            insert into acceptance (id, criterion, priority, tool_link)
            values (?, ?, ?, ?)
            on conflict(id) do update set criterion=excluded.criterion, priority=excluded.priority, tool_link=excluded.tool_link,
                revision=acceptance.revision+1
            """,
            (acceptance_id, criterion, priority, tool_link),
        )
        if existing and (existing["criterion"], existing["priority"], existing["tool_link"]) != (criterion, priority, tool_link):
            invalidate_downstream(conn, "acceptance", acceptance_id, "acceptance criterion changed")
        after = conn.execute("select * from acceptance where id = ?", (acceptance_id,)).fetchone()
        emit_audit_event(
            conn,
            "acceptance_recorded",
            entity_type="acceptance",
            entity_id=acceptance_id,
            before=row_snapshot(existing),
            after=row_snapshot(after),
            command="acceptance add",
        )
    render_all(root)


def add_failure_mode(
    root: Path,
    fm_id: str,
    feature: str,
    scenario: str,
    trigger: str,
    expected: str,
    *,
    risk: str = "medium",
    status: str = "identified",
    acceptance: str = "",
    recovery: str = "",
    data_safety: str = "",
    accepted_by: str = "",
    acceptance_reason: str = "",
    acceptance_scope: str = "",
    expires_at: str = "",
) -> None:
    guard_schema("validate_failure_mode", fm_id, risk, status)
    with transaction(root, touched=[("failure_mode", fm_id)]) as conn:
        existing = conn.execute("select * from failure_modes where id = ?", (fm_id,)).fetchone()
        accepted_revision = None
        if status not in FAILURE_MODE_STATUSES:
            raise HarnessError("failure mode status must be identified, accepted, or exempt; coverage is derived from passing validation")
        if status in {"accepted", "exempt"}:
            if not accepted_by or not acceptance_reason or not acceptance_scope or not expires_at:
                raise HarnessError("accepted or exempt failure modes require accepted-by, acceptance-reason, acceptance-scope, and expires-at")
            accepted_revision = int(project_row(conn)["revision"])
        conn.execute(
            """
            insert into failure_modes
            (id, feature, scenario, trigger, expected_behavior, recovery, data_safety, risk, status,
             accepted_by, acceptance_reason, acceptance_scope, accepted_revision, expires_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(id) do update set feature=excluded.feature, scenario=excluded.scenario, trigger=excluded.trigger,
              expected_behavior=excluded.expected_behavior, recovery=excluded.recovery, data_safety=excluded.data_safety,
              risk=excluded.risk, status=excluded.status, accepted_by=excluded.accepted_by,
              acceptance_reason=excluded.acceptance_reason, acceptance_scope=excluded.acceptance_scope,
              accepted_revision=excluded.accepted_revision, expires_at=excluded.expires_at, revision=failure_modes.revision+1
            """,
            (
                fm_id,
                feature,
                scenario,
                trigger,
                expected,
                recovery,
                data_safety,
                risk,
                status,
                accepted_by or None,
                acceptance_reason or None,
                acceptance_scope,
                accepted_revision,
                expires_at or None,
            ),
        )
        if acceptance:
            require_acceptance(conn, acceptance)
            conn.execute(
                "insert or ignore into failure_mode_acceptance (failure_mode_id, acceptance_id) values (?, ?)",
                (fm_id, acceptance),
            )
        if existing and (
            existing["feature"],
            existing["scenario"],
            existing["trigger"],
            existing["expected_behavior"],
            existing["risk"],
            existing["status"],
        ) != (feature, scenario, trigger, expected, risk, status):
            invalidate_downstream(conn, "failure_mode", fm_id, "failure mode changed")
        after = conn.execute("select * from failure_modes where id = ?", (fm_id,)).fetchone()
        emit_audit_event(
            conn,
            "failure_mode_recorded",
            entity_type="failure_mode",
            entity_id=fm_id,
            before=row_snapshot(existing),
            after=row_snapshot(after),
            command="failure-mode add",
            extra={"risk": risk},
        )
    render_all(root)


def require_acceptance(conn: sqlite3.Connection, acceptance_id: str) -> None:
    if not conn.execute("select id from acceptance where id = ?", (acceptance_id,)).fetchone():
        raise HarnessError(f"missing acceptance: {acceptance_id}")


def require_requirement(conn: sqlite3.Connection, requirement_id: str) -> None:
    if not conn.execute("select id from requirements where id = ?", (requirement_id,)).fetchone():
        raise HarnessError(f"missing requirement: {requirement_id}")


def link_requirement_acceptance(root: Path, requirement_id: str, acceptance_id: str) -> None:
    with transaction(root, touched=[("requirement", requirement_id), ("acceptance", acceptance_id)]) as conn:
        require_requirement(conn, requirement_id)
        require_acceptance(conn, acceptance_id)
        before = trace_snapshot(conn, requirement_id)
        conn.execute(
            "insert or ignore into requirement_acceptance (requirement_id, acceptance_id) values (?, ?)",
            (requirement_id, acceptance_id),
        )
        after = trace_snapshot(conn, requirement_id)
        emit_audit_event(
            conn,
            "requirement_acceptance_linked",
            entity_type="requirement",
            entity_id=requirement_id,
            before=before,
            after=after,
            command="requirement link",
        )
    render_all(root)


def require_task(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    row = conn.execute("select * from tasks where id = ?", (task_id,)).fetchone()
    if not row:
        raise HarnessError(f"missing task: {task_id}")
    return row


def require_agent(conn: sqlite3.Connection, agent: str) -> sqlite3.Row:
    row = conn.execute("select * from agents where id = ?", (agent,)).fetchone()
    if not row:
        raise HarnessError(f"unknown agent: {agent}")
    return row


def session_trust_level(origin: str, token_status: str) -> str:
    if origin == "connector" and token_status == "hmac-valid":
        return "connector"
    if origin == "manual" or token_status.startswith("downgraded"):
        return "human-confirmed"
    return "local-only"


def require_agent_session(
    conn: sqlite3.Connection,
    session_id: str,
    agent: str,
    *,
    allowed_roles: set[str] | None = None,
) -> sqlite3.Row:
    row = conn.execute("select * from agent_sessions where session_id = ?", (session_id,)).fetchone()
    if not row:
        raise HarnessError(f"missing agent session: {session_id}")
    if row["agent_id"] != agent:
        raise HarnessError(f"agent-session-mismatch: {session_id} agent={row['agent_id']} expected={agent}")
    if allowed_roles is not None and row["role"] not in allowed_roles:
        raise HarnessError(f"agent-session-role-invalid: {session_id} role={row['role']}")
    if row["status"] not in ACTIVE_SESSION_STATUSES:
        raise HarnessError(f"agent-session-inactive: {session_id} status={row['status']}")
    return row


def latest_session_attestation(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "select * from session_attestations where session_id = ? order by created_at desc, id desc limit 1",
        (session_id,),
    ).fetchone()


def require_session_attestation(conn: sqlite3.Connection, attestation_id: str, session_id: str = "") -> sqlite3.Row:
    row = conn.execute("select * from session_attestations where id = ?", (attestation_id,)).fetchone()
    if not row:
        raise HarnessError(f"missing session attestation: {attestation_id}")
    if session_id and row["session_id"] != session_id:
        raise HarnessError(f"session-attestation-mismatch: {attestation_id} session={row['session_id']} expected={session_id}")
    return row


def require_revision(row: sqlite3.Row, expected_revision: int | None) -> None:
    from core.lock_manager import require_revision as core_require_revision

    core_require_revision(row, expected_revision, error_factory=HarnessError)


def require_lease(row: sqlite3.Row, agent: str, lease_token: str | None) -> None:
    from core.lock_manager import require_lease as core_require_lease

    core_require_lease(row, agent, lease_token, error_factory=HarnessError)


def require_fence(row: sqlite3.Row, expected_fence: int | None) -> None:
    if expected_fence is not None and int(row["fence"]) != int(expected_fence):
        raise HarnessError(f"fence-stale: {row['id']} expected={expected_fence} actual={row['fence']}")


def parse_ids(value: str) -> list[str]:
    return [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]


def record_session_attestation(
    root: Path,
    session_id: str,
    agent: str,
    role: str,
    context_id: str,
    *,
    provider_session_id: str = "",
    origin: str = "manual",
    verification_token: str = "",
) -> str:
    if role not in SESSION_ROLES:
        raise HarnessError(f"invalid session role: {role}")
    if origin not in ANCHOR_ORIGINS:
        raise HarnessError(f"invalid attestation origin: {origin}")
    payload_value = agent_session_payload(session_id, agent, role, context_id)
    try:
        stored_origin, stored_token, token_status, token_reason = prepare_connector_record(root, origin, verification_token, payload_value)
    except ConnectorTrustError as exc:
        raise HarnessError(str(exc)) from exc
    trust = session_trust_level(stored_origin, token_status)
    attestation_id = f"SESSION-ATTEST-{uuid.uuid4().hex[:12]}"
    now = now_iso()
    with transaction(root, touched=[("agent_session", session_id), ("session_attestation", attestation_id)]) as conn:
        require_agent(conn, agent)
        conn.execute(
            """
            insert into agent_sessions
            (session_id, agent_id, role, context_id, provider_session_id, origin, trust_level, status, started_at, ended_at)
            values (?, ?, ?, ?, ?, ?, ?, 'active', ?, '')
            on conflict(session_id) do update set
              agent_id=excluded.agent_id, role=excluded.role, context_id=excluded.context_id,
              provider_session_id=excluded.provider_session_id, origin=excluded.origin,
              trust_level=excluded.trust_level, status='active', ended_at=''
            """,
            (session_id, agent, role, context_id, provider_session_id, stored_origin, trust, now),
        )
        conn.execute(
            """
            insert into session_attestations
            (id, session_id, agent_id, role, context_id, provider_session_id, origin, verification_token,
             token_status, token_reason, trust_level, created_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attestation_id,
                session_id,
                agent,
                role,
                context_id,
                provider_session_id,
                stored_origin,
                stored_token,
                token_status,
                token_reason,
                trust,
                now,
            ),
        )
        emit_event(
            conn,
            "session_attested",
            payload(session_id=session_id, agent=agent, role=role, context_id=context_id, origin=stored_origin, token_status=token_status, trust_level=trust),
        )
    return attestation_id


def session_status_lines(root: Path, *, agent: str = "") -> list[str]:
    clauses: list[str] = []
    params: list[str] = []
    if agent:
        clauses.append("agent_id = ?")
        params.append(agent)
    where = f" where {' and '.join(clauses)}" if clauses else ""
    with connection(root) as conn:
        rows = conn.execute(
            f"select session_id, agent_id, role, context_id, origin, trust_level, status, provider_session_id from agent_sessions{where} order by started_at, session_id",
            tuple(params),
        ).fetchall()
    lines = ["| Session | Agent | Role | Context | Origin | Trust | Status | Provider Session |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    lines.extend(markdown_row([row["session_id"], row["agent_id"], row["role"], row["context_id"], row["origin"], row["trust_level"], row["status"], row["provider_session_id"]]) for row in rows)
    return lines


def close_agent_session(root: Path, session_id: str) -> None:
    with transaction(root, touched=[("agent_session", session_id)]) as conn:
        row = conn.execute("select * from agent_sessions where session_id = ?", (session_id,)).fetchone()
        if not row:
            raise HarnessError(f"missing agent session: {session_id}")
        conn.execute("update agent_sessions set status = 'closed', ended_at = ? where session_id = ?", (now_iso(), session_id))
        emit_event(conn, "session_closed", payload(session_id=session_id))


def assert_no_dependency_cycle(conn: sqlite3.Connection, task_id: str, depends_on: str) -> None:
    from core.scheduler import assert_no_dependency_cycle as core_assert_no_dependency_cycle

    core_assert_no_dependency_cycle(conn, task_id, depends_on, error_factory=HarnessError)


def add_task(
    root: Path,
    task_id: str,
    task: str,
    *,
    owner: str = "unassigned",
    acceptance: str = "",
    failure_modes: str = "",
    depends_on: str = "",
    status: str = "ready",
    evidence: str = "",
    tool_link: str = "",
) -> None:
    guard_schema("validate_task", task_id, task, status)
    with transaction(root, touched=[("task", task_id)]) as conn:
        if status not in TASK_STATUSES:
            raise HarnessError(f"invalid task status: {status}")
        if status == "accepted":
            raise HarnessError("new tasks cannot be created as accepted; use task complete with evidence")
        if conn.execute("select id from tasks where id = ?", (task_id,)).fetchone():
            raise HarnessError(f"duplicate task id: {task_id}")
        conn.execute(
            """
            insert into tasks (id, task, owner, status, evidence, tool_link, updated_at)
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            (task_id, task, owner, status, evidence, tool_link, now_iso()),
        )
        conn.execute("delete from task_acceptance where task_id = ?", (task_id,))
        for acceptance_id in parse_ids(acceptance):
            require_acceptance(conn, acceptance_id)
            conn.execute("insert into task_acceptance (task_id, acceptance_id) values (?, ?)", (task_id, acceptance_id))
        conn.execute("delete from task_failure_modes where task_id = ?", (task_id,))
        for fm_id in parse_ids(failure_modes):
            if not conn.execute("select id from failure_modes where id = ?", (fm_id,)).fetchone():
                raise HarnessError(f"missing failure mode: {fm_id}")
            conn.execute("insert into task_failure_modes (task_id, failure_mode_id) values (?, ?)", (task_id, fm_id))
        conn.execute("delete from task_dependencies where task_id = ?", (task_id,))
        for dep in parse_ids(depends_on):
            require_task(conn, dep)
            assert_no_dependency_cycle(conn, task_id, dep)
            conn.execute("insert into task_dependencies (task_id, depends_on) values (?, ?)", (task_id, dep))
        after = conn.execute("select * from tasks where id = ?", (task_id,)).fetchone()
        emit_audit_event(
            conn,
            "task_created",
            entity_type="task",
            entity_id=task_id,
            before=None,
            after=row_snapshot(after),
            actor=owner,
            command="task add",
        )
    render_all(root)


def update_task(root: Path, task_id: str, *, depends_on: str | None = None, status: str | None = None) -> None:
    with transaction(root, touched=[("task", task_id)]) as conn:
        row = require_task(conn, task_id)
        if status and status not in TASK_STATUSES:
            raise HarnessError(f"invalid task status: {status}")
        if status == "accepted":
            raise HarnessError("task acceptance must use task complete with evidence")
        if depends_on is not None:
            conn.execute("delete from task_dependencies where task_id = ?", (task_id,))
            for dep in parse_ids(depends_on):
                require_task(conn, dep)
                assert_no_dependency_cycle(conn, task_id, dep)
                conn.execute("insert into task_dependencies (task_id, depends_on) values (?, ?)", (task_id, dep))
        if status:
            conn.execute(
                "update tasks set status = ?, revision = revision + 1, updated_at = ? where id = ?",
                (status, now_iso(), task_id),
            )
        else:
            conn.execute("update tasks set revision = revision + 1, updated_at = ? where id = ?", (now_iso(), task_id))
        after = conn.execute("select * from tasks where id = ?", (task_id,)).fetchone()
        emit_audit_event(
            conn,
            "task_updated",
            entity_type="task",
            entity_id=row["id"],
            before=row_snapshot(row),
            after=row_snapshot(after),
            command="task update",
        )
    render_all(root)


def ready_tasks(root: Path) -> list[str]:
    from core.scheduler import ready_queue

    with connection(root) as conn:
        return ready_queue(conn)


def dependency_blockers(conn: sqlite3.Connection, task_id: str) -> list[str]:
    from core.scheduler import dependency_blockers as core_dependency_blockers

    return core_dependency_blockers(conn, task_id)


def require_task_runnable(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    from core.scheduler import require_task_runnable as core_require_task_runnable

    core_require_task_runnable(conn, row, error_factory=HarnessError)


def claim_task(root: Path, task_id: str, agent: str, expected_revision: int) -> tuple[str, int]:
    with transaction(root, touched=[("task", task_id)]) as conn:
        row = require_task(conn, task_id)
        require_revision(row, expected_revision)
        active_lease = require_agent(conn, agent)
        if active_lease and active_lease["lease_task_id"] and active_lease["lease_task_id"] != task_id:
            raise HarnessError(f"agent already leased to {active_lease['lease_task_id']}")
        if row["lease_agent"]:
            raise HarnessError(f"task already leased by {row['lease_agent']}")
        if row["status"] != "ready":
            raise HarnessError(f"task status is not ready: {task_id} status={row['status']}")
        require_task_runnable(conn, row)
        fence = int(row["fence"])
        token = str(uuid.uuid4())
        conn.execute(
            """
            update tasks set lease_agent = ?, lease_token = ?, lease_heartbeat_at = ?, lease_expires_at = ?, status = 'claimed',
              revision = revision + 1, updated_at = ? where id = ?
            """,
            (agent, token, now_iso(), lease_deadline(), now_iso(), task_id),
        )
        conn.execute(
            """
            update agents set lease_task_id = ?, status = 'leased', updated_at = ?
            where id = ?
            """,
            (task_id, now_iso(), agent),
        )
        after = conn.execute("select * from tasks where id = ?", (task_id,)).fetchone()
        emit_audit_event(
            conn,
            "task_claimed",
            entity_type="task",
            entity_id=task_id,
            before=row_snapshot(row),
            after=row_snapshot(after),
            actor=agent,
            command="task claim",
        )
    render_all(root)
    return token, fence


def heartbeat_task(root: Path, task_id: str, agent: str, lease_token: str, expected_revision: int, *, expected_fence: int | None = None) -> None:
    with transaction(root, touched=[("task", task_id)]) as conn:
        row = require_task(conn, task_id)
        require_agent(conn, agent)
        require_revision(row, expected_revision)
        require_lease(row, agent, lease_token)
        require_fence(row, expected_fence)
        conn.execute(
            "update tasks set lease_heartbeat_at = ?, lease_expires_at = ?, revision = revision + 1, updated_at = ? where id = ?",
            (now_iso(), lease_deadline(), now_iso(), task_id),
        )
        after = conn.execute("select * from tasks where id = ?", (task_id,)).fetchone()
        emit_audit_event(
            conn,
            "task_heartbeat",
            entity_type="task",
            entity_id=task_id,
            before=row_snapshot(row),
            after=row_snapshot(after),
            actor=agent,
            command="task heartbeat",
        )
    render_all(root)


def recover_stale_leases(root: Path) -> int:
    recovered = 0
    with transaction(root, touched=[]) as conn:
        rows = conn.execute(
            """
            select id, status, lease_agent from tasks
            where lease_expires_at is not null and lease_expires_at <= ? and lease_agent is not null
            order by id
            """,
            (now_iso(),),
        ).fetchall()
        for row in rows:
            next_status = "submitted" if row["status"] == "review" else "ready"
            conn.execute(
                """
                update tasks set status = ?, lease_agent = null, lease_token = null, lease_heartbeat_at = null,
                  lease_expires_at = null, revision = revision + 1, fence = fence + 1, updated_at = ? where id = ?
                """,
                (next_status, now_iso(), row["id"]),
            )
            if row["lease_agent"]:
                conn.execute(
                    "update agents set lease_task_id = '', status = 'available', updated_at = ? where id = ?",
                    (now_iso(), row["lease_agent"]),
                )
            recovered += 1
        if recovered:
            emit_event(conn, "stale_leases_recovered", payload(count=recovered))
    render_all(root)
    return recovered


def release_task(root: Path, task_id: str, agent: str, *, lease_token: str | None = None, expected_revision: int | None = None, expected_fence: int | None = None) -> None:
    with transaction(root, touched=[("task", task_id)]) as conn:
        row = require_task(conn, task_id)
        require_agent(conn, agent)
        require_revision(row, expected_revision)
        require_lease(row, agent, lease_token)
        require_fence(row, expected_fence)
        conn.execute(
            """
            update tasks set lease_agent = null, lease_token = null, lease_heartbeat_at = null, lease_expires_at = null,
              status = 'ready', revision = revision + 1, fence = fence + 1, updated_at = ? where id = ?
            """,
            (now_iso(), task_id),
        )
        conn.execute("update agents set lease_task_id = '', status = 'available', updated_at = ? where id = ?", (now_iso(), agent))
        after = conn.execute("select * from tasks where id = ?", (task_id,)).fetchone()
        emit_audit_event(
            conn,
            "task_released",
            entity_type="task",
            entity_id=task_id,
            before=row_snapshot(row),
            after=row_snapshot(after),
            actor=agent,
            command="task release",
        )
    render_all(root)


def start_task(root: Path, task_id: str, agent: str, *, lease_token: str | None = None, expected_revision: int | None = None, expected_fence: int | None = None) -> None:
    with transaction(root, touched=[("task", task_id)]) as conn:
        row = require_task(conn, task_id)
        require_agent(conn, agent)
        require_revision(row, expected_revision)
        if row["status"] != "claimed":
            raise HarnessError(f"task status is not startable: {task_id} status={row['status']}")
        require_lease(row, agent, lease_token)
        require_fence(row, expected_fence)
        require_task_runnable(conn, row)
        conn.execute(
            """
            update tasks set status = 'in_progress', owner = ?, revision = revision + 1, updated_at = ? where id = ?
            """,
            (agent, now_iso(), task_id),
        )
        after = conn.execute("select * from tasks where id = ?", (task_id,)).fetchone()
        emit_audit_event(
            conn,
            "task_started",
            entity_type="task",
            entity_id=task_id,
            before=row_snapshot(row),
            after=row_snapshot(after),
            actor=agent,
            command="task start",
        )
    render_all(root)


def submit_task(root: Path, task_id: str, evidence: str, *, agent: str, lease_token: str | None = None, expected_revision: int | None = None, expected_fence: int | None = None, session_id: str = "") -> None:
    with transaction(root, touched=[("task", task_id)]) as conn:
        row = require_task(conn, task_id)
        require_agent(conn, agent)
        require_revision(row, expected_revision)
        require_lease(row, agent, lease_token)
        require_fence(row, expected_fence)
        if session_id:
            require_agent_session(conn, session_id, agent, allowed_roles=SESSION_ROLES)
        if row["status"] != "in_progress":
            raise HarnessError(f"task status is not submittable: {task_id} status={row['status']}")
        conn.execute(
            """
            update tasks set status = 'submitted', evidence = ?, submitted_by = ?, lease_agent = null, lease_token = null,
              lease_heartbeat_at = null, lease_expires_at = null,
              submitted_session_id = ?, revision = revision + 1, updated_at = ? where id = ?
            """,
            (evidence, agent, session_id, now_iso(), task_id),
        )
        conn.execute(
            "update agents set lease_task_id = '', status = 'available', updated_at = ? where id = ?",
            (now_iso(), agent),
        )
        after = conn.execute("select * from tasks where id = ?", (task_id,)).fetchone()
        emit_audit_event(
            conn,
            "task_submitted",
            entity_type="task",
            entity_id=task_id,
            before=row_snapshot(row),
            after=row_snapshot(after),
            actor=agent,
            command="task submit",
        )
    render_all(root)


def complete_task(root: Path, task_id: str, evidence: str, *, agent: str, lease_token: str | None = None, expected_revision: int | None = None, expected_fence: int | None = None, session_id: str = "") -> None:
    submit_task(root, task_id, evidence, agent=agent, lease_token=lease_token, expected_revision=expected_revision, expected_fence=expected_fence, session_id=session_id)


def review_task(root: Path, task_id: str, agent: str, expected_revision: int, *, session_id: str = "") -> tuple[str, int]:
    with transaction(root, touched=[("task", task_id)]) as conn:
        row = require_task(conn, task_id)
        active_lease = require_agent(conn, agent)
        require_revision(row, expected_revision)
        if session_id:
            require_agent_session(conn, session_id, agent, allowed_roles=REVIEWER_SESSION_ROLES)
            if row["submitted_session_id"] and row["submitted_session_id"] == session_id:
                raise HarnessError(f"review-session-not-independent: {task_id} session={session_id}")
        if row["status"] != "submitted":
            raise HarnessError(f"task status is not reviewable: {task_id} status={row['status']}")
        if row["owner"] == agent:
            raise HarnessError("producer cannot review own task")
        if active_lease["lease_task_id"] and active_lease["lease_task_id"] != task_id:
            raise HarnessError(f"agent already leased to {active_lease['lease_task_id']}")
        token = str(uuid.uuid4())
        conn.execute(
            """
            update tasks set status = 'review', lease_agent = ?, lease_token = ?, lease_heartbeat_at = ?, lease_expires_at = ?,
              revision = revision + 1, fence = fence + 1, updated_at = ? where id = ?
            """,
            (agent, token, now_iso(), lease_deadline(), now_iso(), task_id),
        )
        conn.execute(
            "update agents set lease_task_id = ?, status = 'leased', updated_at = ? where id = ?",
            (task_id, now_iso(), agent),
        )
        after = conn.execute("select * from tasks where id = ?", (task_id,)).fetchone()
        emit_audit_event(
            conn,
            "task_review_started",
            entity_type="task",
            entity_id=task_id,
            before=row_snapshot(row),
            after=row_snapshot(after),
            actor=agent,
            command="task review",
        )
        fence = int(after["fence"])
    render_all(root)
    return token, fence


def accept_task(root: Path, task_id: str, evidence: str, *, agent: str, lease_token: str | None = None, expected_revision: int | None = None, expected_fence: int | None = None, session_id: str = "") -> None:
    with transaction(root, touched=[("task", task_id)]) as conn:
        row = require_task(conn, task_id)
        require_agent(conn, agent)
        require_revision(row, expected_revision)
        require_lease(row, agent, lease_token)
        require_fence(row, expected_fence)
        if session_id:
            require_agent_session(conn, session_id, agent, allowed_roles=REVIEWER_SESSION_ROLES)
            if row["submitted_session_id"] and row["submitted_session_id"] == session_id:
                raise HarnessError(f"review-session-not-independent: {task_id} session={session_id}")
        if row["status"] != "review":
            raise HarnessError(f"task status is not acceptable: {task_id} status={row['status']}")
        conn.execute(
            """
            update tasks set status = 'accepted', evidence = ?, accepted_by = ?, lease_agent = null, lease_token = null,
              lease_heartbeat_at = null, lease_expires_at = null,
              accepted_session_id = ?, revision = revision + 1, updated_at = ? where id = ?
            """,
            (evidence, agent, session_id, now_iso(), task_id),
        )
        conn.execute(
            "update agents set lease_task_id = '', status = 'available', updated_at = ? where id = ?",
            (now_iso(), agent),
        )
        after = conn.execute("select * from tasks where id = ?", (task_id,)).fetchone()
        emit_audit_event(
            conn,
            "task_accepted",
            entity_type="task",
            entity_id=task_id,
            before=row_snapshot(row),
            after=row_snapshot(after),
            actor=agent,
            command="task accept",
        )
    render_all(root)


def block_task(root: Path, task_id: str, reason: str, *, agent: str, lease_token: str | None = None, expected_revision: int | None = None, expected_fence: int | None = None) -> None:
    with transaction(root, touched=[("task", task_id)]) as conn:
        row = require_task(conn, task_id)
        require_agent(conn, agent)
        require_revision(row, expected_revision)
        require_lease(row, agent, lease_token)
        require_fence(row, expected_fence)
        conn.execute(
            """
            update tasks set status = 'blocked', evidence = ?, lease_agent = null, lease_token = null,
              lease_heartbeat_at = null, lease_expires_at = null, revision = revision + 1, updated_at = ? where id = ?
            """,
            (reason, now_iso(), task_id),
        )
        conn.execute(
            "update agents set lease_task_id = '', status = 'available', updated_at = ? where id = ?",
            (now_iso(), agent),
        )
        after = conn.execute("select * from tasks where id = ?", (task_id,)).fetchone()
        emit_audit_event(
            conn,
            "task_blocked",
            entity_type="task",
            entity_id=task_id,
            before=row_snapshot(row),
            after=row_snapshot(after),
            actor=agent,
            command="task block",
        )
    render_all(root)


def record_decision(root: Path, decision: str, reason: str) -> None:
    with transaction(root) as conn:
        conn.execute(
            "insert into decisions (id, decision, reason, created_at) values (?, ?, ?, ?)",
            (str(uuid.uuid4()), decision, reason, now_iso()),
        )
        emit_event(conn, "decision_recorded", payload(decision=decision, reason=reason))
    render_all(root)


def add_test_target(root: Path, target_id: str, kind: str, command_template: str, description: str = "") -> None:
    guard_schema("validate_test_target", target_id, kind, command_template)
    gateable, gate_block_reason = target_gateability(kind, command_template)
    with transaction(root, touched=[("test_target", target_id)]) as conn:
        conn.execute(
            """
            insert into test_targets (id, kind, command_template, description, gateable, gate_block_reason, created_at, updated_at)
            values (?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(id) do update set kind=excluded.kind, command_template=excluded.command_template,
              description=excluded.description, gateable=excluded.gateable, gate_block_reason=excluded.gate_block_reason,
              updated_at=excluded.updated_at
            """,
            (target_id, kind, command_template, description, gateable, gate_block_reason, now_iso(), now_iso()),
        )
        emit_event(conn, "test_target_recorded", payload(id=target_id, kind=kind, gateable=gateable))
    render_all(root)


def link_task_test_target(root: Path, task_id: str, target_id: str) -> None:
    with transaction(root, touched=[("task", task_id)]) as conn:
        if not conn.execute("select id from tasks where id = ?", (task_id,)).fetchone():
            raise HarnessError(f"missing task: {task_id}")
        if not conn.execute("select id from test_targets where id = ?", (target_id,)).fetchone():
            raise HarnessError(f"missing test target: {target_id}")
        conn.execute("insert or ignore into task_test_targets (task_id, target_id) values (?, ?)", (task_id, target_id))
        emit_event(conn, "task_test_target_linked", payload(task_id=task_id, target_id=target_id))
    render_all(root)


def task_target(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        select tt.* from task_test_targets link
        join test_targets tt on tt.id = link.target_id
        where link.task_id = ?
        order by tt.id
        limit 1
        """,
        (task_id,),
    ).fetchone()


def list_test_targets(root: Path) -> list[str]:
    with connection(root) as conn:
        rows = conn.execute("select id, kind, command_template, description, gateable, gate_block_reason from test_targets order by id").fetchall()
    return [markdown_row([row["id"], row["kind"], row["command_template"], row["description"], str(row["gateable"]), row["gate_block_reason"]]) for row in rows]


def add_executor_prefix(root: Path, prefix: str, reason: str) -> None:
    if not prefix.strip():
        raise HarnessError("executor allow prefix is required")
    if not reason.strip():
        raise HarnessError("executor allow prefix reason is required")
    with transaction(root, touched=[("executor_allowlist", prefix)]) as conn:
        conn.execute(
            """
            insert into executor_allowlist (id, prefix, reason, created_at)
            values (?, ?, ?, ?)
            on conflict(prefix) do update set reason=excluded.reason
            """,
            (f"user-{hashlib.sha256(prefix.encode('utf-8')).hexdigest()[:12]}", prefix, reason, now_iso()),
        )
        emit_event(conn, "executor_prefix_allowed", payload(prefix=prefix, reason=reason))
    render_all(root)


def list_executor_prefixes(root: Path) -> list[str]:
    with connection(root) as conn:
        rows = conn.execute("select prefix, reason from executor_allowlist order by prefix").fetchall()
    return [markdown_row([row["prefix"], row["reason"]]) for row in rows]


def record_validation(
    root: Path,
    surface: str,
    findings: str,
    result: str,
    *,
    acceptance: str = "",
    commands: str = "",
    risk: str = "",
    failure_modes: str = "",
    tests: str = "",
    evidence: str = "",
    command: str = "",
    exit_code: int | None = None,
    stdout_sha256: str = "",
    artifact_path: str = "",
    target_id: str = "",
    executed_count: int | None = None,
    allow_unlisted: bool = False,
    no_network: bool = False,
    sandbox_profile: str = "none",
    trust_anchor: str = "local-only",
    trust_anchor_id: str = "",
    allow_unlisted_reason: str = "",
    code_identity: str = "auto",
) -> None:
    guard_schema("validate_validation", surface, findings, result)
    guard_schema("validate_trust_anchor", trust_anchor)
    guard_schema("validate_sandbox_profile", "no-network" if no_network else sandbox_profile)
    guard_schema("validate_code_identity_mode", code_identity)
    current_sha = git_head_sha(root) or "no-git"
    source_hash = source_tree_hash_for_mode(root, code_identity)
    tracked_diff_hash = git_tracked_diff_hash(root) or ""
    artifact_path = normalize_artifact_path(root, artifact_path)
    (
        executed_count_value,
        executed_count_source,
        policy_status,
        policy_reason,
        sandbox_profile_value,
        sandbox_status,
        allow_unlisted_reason_value,
    ) = normalize_manual_execution_fields(
        executed_count,
        command,
        sandbox_profile=sandbox_profile,
        no_network=no_network,
        allow_unlisted_reason=allow_unlisted_reason,
    )
    with transaction(root, touched=[("validation", "")]) as conn:
        if target_id:
            test_target_command(conn, target_id)
        evidence_ids = parse_ids(evidence)
        if not command and evidence_ids:
            source_evidence = conn.execute("select * from evidence where id = ? and command != ''", (evidence_ids[0],)).fetchone()
            if source_evidence:
                command = source_evidence["command"]
                exit_code = source_evidence["exit_code"]
                stdout_sha256 = source_evidence["stdout_sha256"]
                artifact_path = source_evidence["artifact_path"]
                target_id = target_id or source_evidence["target_id"]
                executed_count_value = int(source_evidence["executed_count"] or 0)
                executed_count_source = source_evidence["executed_count_source"]
                allow_unlisted = bool(source_evidence["allow_unlisted"])
                no_network = bool(source_evidence["no_network"])
                sandbox_profile_value = source_evidence["sandbox_profile"] if "sandbox_profile" in source_evidence.keys() else sandbox_profile_value
                sandbox_status = source_evidence["sandbox_status"] if "sandbox_status" in source_evidence.keys() else sandbox_status
                allow_unlisted_reason_value = source_evidence["allow_unlisted_reason"] if "allow_unlisted_reason" in source_evidence.keys() else allow_unlisted_reason_value
                policy_status = source_evidence["policy_status"]
                policy_reason = source_evidence["policy_reason"]
                if trust_anchor == "local-only":
                    trust_anchor = source_evidence["trust_anchor"] if "trust_anchor" in source_evidence.keys() else trust_anchor
                    trust_anchor_id = source_evidence["trust_anchor_id"] if "trust_anchor_id" in source_evidence.keys() else trust_anchor_id
        validation_id = str(uuid.uuid4())
        project_revision = int(project_row(conn)["revision"])
        conn.execute(
            """
            insert into validations
            (id, surface, acceptance_id, commands, command, exit_code, stdout_sha256, artifact_path,
             target_id, executed_count, executed_count_source, allow_unlisted, no_network, policy_status,
             policy_reason, sandbox_profile, sandbox_status, allow_unlisted_reason, trust_anchor, trust_anchor_id,
             findings, result, residual_risk, head_commit, source_tree_hash, tracked_diff_hash,
             project_revision, created_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                validation_id,
                surface,
                acceptance,
                commands,
                command,
                exit_code,
                stdout_sha256,
                artifact_path,
                target_id,
                executed_count_value,
                executed_count_source,
                bool_int(allow_unlisted),
                bool_int(no_network),
                policy_status,
                policy_reason,
                sandbox_profile_value,
                sandbox_status,
                allow_unlisted_reason_value,
                trust_anchor,
                trust_anchor_id,
                findings,
                result,
                risk,
                current_sha,
                source_hash,
                tracked_diff_hash,
                project_revision,
                now_iso(),
            ),
        )
        if acceptance:
            resolve_invalidations(conn, source_type="acceptance", source_id=acceptance)
        for test_id in parse_ids(tests):
            test_row = conn.execute("select id, result from tests where id = ?", (test_id,)).fetchone()
            if not test_row:
                raise HarnessError(f"missing test: {test_id}")
            conn.execute("insert or ignore into validation_tests (validation_id, test_id) values (?, ?)", (validation_id, test_id))
        for evidence_id in evidence_ids:
            if not conn.execute("select id from evidence where id = ?", (evidence_id,)).fetchone():
                raise HarnessError(f"missing evidence: {evidence_id}")
            conn.execute("insert or ignore into validation_evidence (validation_id, evidence_id) values (?, ?)", (validation_id, evidence_id))
        for fm_id in parse_ids(failure_modes):
            if not conn.execute("select id from failure_modes where id = ?", (fm_id,)).fetchone():
                raise HarnessError(f"missing failure mode: {fm_id}")
            conn.execute(
                "insert into validation_failure_modes (validation_id, failure_mode_id) values (?, ?)",
                (validation_id, fm_id),
            )
            resolve_invalidations(conn, source_type="failure_mode", source_id=fm_id)
        after = conn.execute("select * from validations where id = ?", (validation_id,)).fetchone()
        emit_audit_event(
            conn,
            "validation_recorded",
            entity_type="validation",
            entity_id=validation_id,
            before=None,
            after=row_snapshot(after),
            command="validation record",
            extra={"surface": surface, "result": result},
        )
    render_all(root)


def record_evidence(
    root: Path,
    evidence_id: str,
    kind: str,
    summary: str,
    *,
    uri: str = "",
    artifact_hash: str = "",
    command: str = "",
    exit_code: int | None = None,
    stdout_sha256: str = "",
    artifact_path: str = "",
    target_id: str = "",
    executed_count: int | None = None,
    allow_unlisted: bool = False,
    no_network: bool = False,
    sandbox_profile: str = "none",
    trust_anchor: str = "local-only",
    trust_anchor_id: str = "",
    allow_unlisted_reason: str = "",
    code_identity: str = "auto",
) -> None:
    guard_schema("validate_trust_anchor", trust_anchor)
    guard_schema("validate_sandbox_profile", "no-network" if no_network else sandbox_profile)
    guard_schema("validate_code_identity_mode", code_identity)
    artifact_path = normalize_artifact_path(root, artifact_path)
    source_hash = source_tree_hash_for_mode(root, code_identity)
    (
        executed_count_value,
        executed_count_source,
        policy_status,
        policy_reason,
        sandbox_profile_value,
        sandbox_status,
        allow_unlisted_reason_value,
    ) = normalize_manual_execution_fields(
        executed_count,
        command,
        sandbox_profile=sandbox_profile,
        no_network=no_network,
        allow_unlisted_reason=allow_unlisted_reason,
    )
    with transaction(root, touched=[("evidence", evidence_id)]) as conn:
        if target_id:
            test_target_command(conn, target_id)
        conn.execute(
            """
            insert into evidence
            (id, kind, summary, uri, hash, command, exit_code, stdout_sha256, artifact_path, source_tree_hash,
             target_id, executed_count, executed_count_source, allow_unlisted, no_network, policy_status, policy_reason,
             sandbox_profile, sandbox_status, allow_unlisted_reason, trust_anchor, trust_anchor_id,
             created_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(id) do update set kind=excluded.kind, summary=excluded.summary, uri=excluded.uri,
              hash=excluded.hash, command=excluded.command, exit_code=excluded.exit_code,
              stdout_sha256=excluded.stdout_sha256, artifact_path=excluded.artifact_path,
              source_tree_hash=excluded.source_tree_hash, target_id=excluded.target_id,
              executed_count=excluded.executed_count, executed_count_source=excluded.executed_count_source,
              allow_unlisted=excluded.allow_unlisted, no_network=excluded.no_network,
              policy_status=excluded.policy_status, policy_reason=excluded.policy_reason,
              sandbox_profile=excluded.sandbox_profile, sandbox_status=excluded.sandbox_status,
              allow_unlisted_reason=excluded.allow_unlisted_reason, trust_anchor=excluded.trust_anchor,
              trust_anchor_id=excluded.trust_anchor_id,
              created_at=excluded.created_at
            """,
            (
                evidence_id,
                kind,
                summary,
                uri,
                artifact_hash,
                command,
                exit_code,
                stdout_sha256,
                artifact_path,
                source_hash,
                target_id,
                executed_count_value,
                executed_count_source,
                bool_int(allow_unlisted),
                bool_int(no_network),
                policy_status,
                policy_reason,
                sandbox_profile_value,
                sandbox_status,
                allow_unlisted_reason_value,
                trust_anchor,
                trust_anchor_id,
                now_iso(),
            ),
        )
        emit_event(conn, "evidence_recorded", payload(id=evidence_id, kind=kind))
    render_all(root)


def record_test(root: Path, test_id: str, surface: str, command: str, result: str, *, evidence_id: str = "") -> None:
    with transaction(root) as conn:
        if evidence_id and not conn.execute("select id from evidence where id = ?", (evidence_id,)).fetchone():
            raise HarnessError(f"missing evidence: {evidence_id}")
        conn.execute(
            """
            insert into tests (id, surface, command, result, evidence_id, created_at)
            values (?, ?, ?, ?, ?, ?)
            on conflict(id) do update set surface=excluded.surface, command=excluded.command, result=excluded.result,
              evidence_id=excluded.evidence_id, created_at=excluded.created_at
            """,
            (test_id, surface, command, result, evidence_id, now_iso()),
        )
        emit_event(conn, "test_recorded", payload(id=test_id, result=result))
    render_all(root)


def record_finding(root: Path, finding_id: str, surface: str, severity: str, status: str, summary: str, *, evidence_id: str = "") -> None:
    with transaction(root) as conn:
        if evidence_id and not conn.execute("select id from evidence where id = ?", (evidence_id,)).fetchone():
            raise HarnessError(f"missing evidence: {evidence_id}")
        conn.execute(
            """
            insert into findings (id, surface, severity, status, summary, evidence_id, created_at)
            values (?, ?, ?, ?, ?, ?, ?)
            on conflict(id) do update set surface=excluded.surface, severity=excluded.severity, status=excluded.status,
              summary=excluded.summary, evidence_id=excluded.evidence_id, created_at=excluded.created_at
            """,
            (finding_id, surface, severity, status, summary, evidence_id, now_iso()),
        )
        emit_event(conn, "finding_recorded", payload(id=finding_id, severity=severity, status=status))
    render_all(root)


def sweep_expired_risks(root: Path) -> int:
    swept = 0
    with transaction(root) as conn:
        rows = conn.execute(
            "select * from failure_modes where status in ('accepted', 'exempt') and expires_at is not null order by id"
        ).fetchall()
        for row in rows:
            if not is_expired(row["expires_at"]):
                continue
            conn.execute(
                """
                update failure_modes set status = 'identified', accepted_by = null, acceptance_reason = null,
                  accepted_revision = null, revision = revision + 1
                where id = ?
                """,
                (row["id"],),
            )
            after = conn.execute("select * from failure_modes where id = ?", (row["id"],)).fetchone()
            emit_audit_event(
                conn,
                "risk_acceptance_expired",
                entity_type="failure_mode",
                entity_id=row["id"],
                before=row_snapshot(row),
                after=row_snapshot(after),
                command="risk sweep-expired",
            )
            swept += 1
    if swept:
        render_all(root)
    return swept


def record_gate(
    root: Path,
    reviewer_context: str,
    result: str,
    *,
    gate: str = "independent_qa",
    commands: str = "",
    evidence: str = "",
    blocking_findings: str = "",
    residual_risk: str = "",
    findings: str = "",
    reviewer_session_id: str = "",
    reviewer_attestation_id: str = "",
) -> None:
    guard_schema("validate_gate", reviewer_context, result, gate)
    current_sha = git_head_sha(root) or "no-git"
    base_commit = git_base_commit(root) or current_sha
    source_hash = git_source_tree_hash(root) or ""
    tracked_diff_hash = git_tracked_diff_hash(root) or ""
    if result == "pass" and git_dirty(root):
        raise HarnessError("cannot record a passing quality gate with a dirty git worktree")
    with transaction(root) as conn:
        project_revision = int(project_row(conn)["revision"])
        review_trust_level = "local-only"
        if reviewer_session_id:
            session = conn.execute("select * from agent_sessions where session_id = ?", (reviewer_session_id,)).fetchone()
            if not session:
                raise HarnessError(f"missing agent session: {reviewer_session_id}")
            if session["status"] not in ACTIVE_SESSION_STATUSES:
                raise HarnessError(f"agent-session-inactive: {reviewer_session_id} status={session['status']}")
            if session["role"] not in REVIEWER_SESSION_ROLES:
                raise HarnessError(f"agent-session-role-invalid: {reviewer_session_id} role={session['role']}")
            review_trust_level = session["trust_level"]
        if reviewer_attestation_id:
            attestation = require_session_attestation(conn, reviewer_attestation_id, reviewer_session_id)
            review_trust_level = attestation["trust_level"]
            if attestation["origin"] == "connector":
                ok, reason = verify_connector_record(
                    root,
                    attestation["verification_token"],
                    agent_session_payload(attestation["session_id"], attestation["agent_id"], attestation["role"], attestation["context_id"]),
                )
                if not ok:
                    raise HarnessError(f"session connector HMAC invalid: {reason}")
        gate_id = str(uuid.uuid4())
        conn.execute(
            """
            insert into quality_gates
            (id, gate, reviewed_commit, evidence_commit, diff_hash, base_commit, head_commit, tracked_diff_hash,
             project_revision, reviewer_context, result, blocking_findings, commands, evidence, residual_risk,
             reviewer_session_id, reviewer_attestation_id, review_trust_level, created_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                gate_id,
                gate,
                current_sha,
                current_sha,
                source_hash,
                base_commit,
                current_sha,
                tracked_diff_hash,
                project_revision,
                reviewer_context,
                result,
                blocking_findings,
                commands,
                evidence,
                residual_risk,
                reviewer_session_id,
                reviewer_attestation_id,
                review_trust_level,
                now_iso(),
            ),
        )
        for finding_id in parse_ids(findings):
            if not conn.execute("select id from findings where id = ?", (finding_id,)).fetchone():
                raise HarnessError(f"missing finding: {finding_id}")
            conn.execute("insert or ignore into quality_gate_findings (gate_id, finding_id) values (?, ?)", (gate_id, finding_id))
        if result == "pass":
            resolve_invalidations(conn, target_type="quality_gate")
        after = conn.execute("select * from quality_gates where id = ?", (gate_id,)).fetchone()
        emit_audit_event(
            conn,
            "quality_gate_recorded",
            entity_type="quality_gate",
            entity_id=gate_id,
            before=None,
            after=row_snapshot(after),
            command="gate record",
            extra={"gate": gate, "result": result},
        )
    render_all(root)


def record_delivery(
    root: Path,
    scope: str,
    *,
    acceptance: str = "",
    changed_files: str = "",
    validation: str = "",
    qa: str = "",
    failure_mode_coverage: str = "",
    quality_gate: str = "",
    data_config_notes: str = "",
    collaboration_links: str = "",
    known_gaps: str = "",
    handoff: str = "",
) -> None:
    guard_schema("validate_delivery", scope)
    with transaction(root) as conn:
        project = project_row(conn)
        if project["phase"] not in {"delivery_readiness", "retrospective"}:
            raise HarnessError(f"delivery record requires phase delivery_readiness or retrospective, current={project['phase']}")
        issues = validate_delivery(conn, root)
        if issues:
            raise HarnessError("delivery record blocked: " + "; ".join(issues))
        delivery_id = str(uuid.uuid4())
        conn.execute(
            """
            insert into deliveries
            (id, scope, acceptance, changed_files, validation, qa, failure_mode_coverage, quality_gate,
             data_config_notes, collaboration_links, known_gaps, handoff, created_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                delivery_id,
                scope,
                acceptance,
                changed_files,
                validation,
                qa,
                failure_mode_coverage,
                quality_gate,
                data_config_notes,
                collaboration_links,
                known_gaps,
                handoff,
                now_iso(),
            ),
        )
        for acceptance_id in parse_ids(acceptance):
            if not conn.execute("select id from acceptance where id = ?", (acceptance_id,)).fetchone():
                continue
            conn.execute(
                "insert or ignore into delivery_acceptance (delivery_id, acceptance_id) values (?, ?)",
                (delivery_id, acceptance_id),
            )
        after = conn.execute("select * from deliveries where id = ?", (delivery_id,)).fetchone()
        emit_audit_event(
            conn,
            "delivery_recorded",
            entity_type="delivery",
            entity_id=delivery_id,
            before=None,
            after=row_snapshot(after),
            command="delivery record",
            extra={"scope": scope},
        )
    render_all(root)


def record_adapter(root: Path, tool: str, mode: str, artifact: str, external_id: str, idempotency_key: str, *, external_link: str = "", evidence: str = "", fallback: str = "", confirmation_needed: str = "no") -> None:
    if mode not in ADAPTER_MODES:
        raise HarnessError(f"invalid adapter mode: {mode}")
    with transaction(root) as conn:
        conn.execute(
            """
            insert into adapters
            (id, tool, mode, artifact, external_id, external_link, idempotency_key, evidence, fallback, confirmation_needed, updated_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(tool, idempotency_key) do update set mode=excluded.mode, artifact=excluded.artifact,
              external_id=excluded.external_id, external_link=excluded.external_link, evidence=excluded.evidence,
              fallback=excluded.fallback, confirmation_needed=excluded.confirmation_needed, updated_at=excluded.updated_at
            """,
            (
                str(uuid.uuid4()),
                tool,
                mode,
                artifact,
                external_id,
                external_link,
                idempotency_key,
                evidence,
                fallback,
                confirmation_needed,
                now_iso(),
            ),
        )
        emit_event(conn, "adapter_recorded", payload(tool=tool, mode=mode), idempotency_key=idempotency_key)
    render_tooling_map(root)


def record_ci_verification(
    root: Path,
    provider: str,
    run_id: str,
    conclusion: str,
    commit_sha: str,
    *,
    external_link: str = "",
    origin: str = "manual",
    verification_token: str = "",
) -> str:
    guard_schema("validate_ci_verification", provider, run_id, conclusion, commit_sha, origin)
    try:
        stored_origin, stored_token, token_status, token_reason = prepare_connector_record(
            root,
            origin,
            verification_token,
            ci_payload(provider, run_id, commit_sha, conclusion),
        )
    except ConnectorTrustError as exc:
        raise HarnessError(str(exc)) from exc
    verification_id = f"{provider}:{run_id}"
    with transaction(root, touched=[("ci_verification", verification_id)]) as conn:
        conn.execute(
            """
            insert into ci_verifications
            (id, provider, run_id, conclusion, commit_sha, origin, verification_token, token_status, token_reason, external_link, created_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(provider, run_id) do update set conclusion=excluded.conclusion,
              commit_sha=excluded.commit_sha, origin=excluded.origin, verification_token=excluded.verification_token,
              token_status=excluded.token_status, token_reason=excluded.token_reason,
              external_link=excluded.external_link, created_at=excluded.created_at
            """,
            (verification_id, provider, run_id, conclusion, commit_sha, stored_origin, stored_token, token_status, token_reason, external_link, now_iso()),
        )
        emit_event(
            conn,
            "ci_verification_recorded",
            payload(
                id=verification_id,
                provider=provider,
                run_id=run_id,
                conclusion=conclusion,
                commit_sha=commit_sha,
                origin=stored_origin,
                token_status=token_status,
                token_reason=token_reason,
            ),
        )
    render_tooling_map(root)
    return verification_id


def record_external_session_verification(
    root: Path,
    session_id: str,
    verifier: str,
    conclusion: str,
    commit_sha: str,
    *,
    external_link: str = "",
    origin: str = "manual",
    verification_token: str = "",
) -> str:
    guard_schema("validate_external_session_verification", session_id, verifier, conclusion, commit_sha, origin)
    try:
        stored_origin, stored_token, token_status, token_reason = prepare_connector_record(
            root,
            origin,
            verification_token,
            external_session_payload(session_id, verifier, commit_sha, conclusion),
        )
    except ConnectorTrustError as exc:
        raise HarnessError(str(exc)) from exc
    verification_id = f"{session_id}:{verifier}"
    with transaction(root, touched=[("external_session_verification", verification_id)]) as conn:
        conn.execute(
            """
            insert into external_session_verifications
            (id, session_id, verifier, conclusion, commit_sha, origin, verification_token, token_status, token_reason, external_link, created_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(session_id, verifier) do update set conclusion=excluded.conclusion,
              commit_sha=excluded.commit_sha, origin=excluded.origin, verification_token=excluded.verification_token,
              token_status=excluded.token_status, token_reason=excluded.token_reason,
              external_link=excluded.external_link, created_at=excluded.created_at
            """,
            (verification_id, session_id, verifier, conclusion, commit_sha, stored_origin, stored_token, token_status, token_reason, external_link, now_iso()),
        )
        emit_event(
            conn,
            "external_session_verification_recorded",
            payload(
                id=verification_id,
                session_id=session_id,
                verifier=verifier,
                conclusion=conclusion,
                commit_sha=commit_sha,
                origin=stored_origin,
                token_status=token_status,
                token_reason=token_reason,
            ),
        )
    render_tooling_map(root)
    return verification_id


def adapter_plan(root: Path, tool: str, mode: str, artifact: str, action: str, *, payload_json: str = "{}", idempotency_key: str = "") -> str:
    guard_schema("validate_adapter_action", tool, mode, artifact, action, payload_json, "planned")
    action_id = str(uuid.uuid4())
    key = idempotency_key or f"codex-project-harness:adapter-action:{tool}:{artifact}:{action}"
    with transaction(root) as conn:
        conn.execute(
            """
            insert into adapter_actions
            (id, tool, mode, artifact, action, payload_json, status, idempotency_key, created_at, updated_at)
            values (?, ?, ?, ?, ?, ?, 'planned', ?, ?, ?)
            on conflict(tool, idempotency_key) do update set mode=excluded.mode, artifact=excluded.artifact,
              action=excluded.action, payload_json=excluded.payload_json, updated_at=excluded.updated_at
            """,
            (action_id, tool, mode, artifact, action, payload_json, key, now_iso(), now_iso()),
        )
        row = conn.execute("select id from adapter_actions where tool = ? and idempotency_key = ?", (tool, key)).fetchone()
        action_id = row["id"]
        emit_event(conn, "adapter_action_planned", payload(id=action_id, tool=tool, mode=mode), idempotency_key=key)
    render_tooling_map(root)
    return action_id


def adapter_transition(root: Path, action_id: str, status: str, *, confirmation: str = "", external_id: str = "", external_link: str = "") -> None:
    if status not in ADAPTER_ACTION_STATUSES:
        raise HarnessError(f"invalid adapter action status: {status}")
    with transaction(root) as conn:
        row = conn.execute("select * from adapter_actions where id = ?", (action_id,)).fetchone()
        if not row:
            raise HarnessError(f"missing adapter action: {action_id}")
        conn.execute(
            """
            update adapter_actions set status = ?, confirmation = coalesce(nullif(?, ''), confirmation),
              external_id = coalesce(nullif(?, ''), external_id), external_link = coalesce(nullif(?, ''), external_link),
              updated_at = ? where id = ?
            """,
            (status, confirmation, external_id, external_link, now_iso(), action_id),
        )
        if status == "completed":
            conn.execute(
                """
                insert into adapters
                (id, tool, mode, artifact, external_id, external_link, idempotency_key, evidence, fallback, confirmation_needed, updated_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, '', 'no', ?)
                on conflict(tool, idempotency_key) do update set mode=excluded.mode, artifact=excluded.artifact,
                  external_id=excluded.external_id, external_link=excluded.external_link, evidence=excluded.evidence,
                  updated_at=excluded.updated_at
                """,
                (str(uuid.uuid4()), row["tool"], row["mode"], row["artifact"], external_id or row["external_id"], external_link or row["external_link"], row["idempotency_key"], f"adapter action {action_id}", now_iso()),
            )
        emit_event(conn, "adapter_action_updated", payload(id=action_id, status=status), idempotency_key=row["idempotency_key"])
    render_tooling_map(root)


def adapter_reconcile(root: Path) -> list[str]:
    issues: list[str] = []
    with connection(root) as conn:
        completed = conn.execute("select * from adapter_actions where status = 'completed' order by tool, artifact").fetchall()
        for action in completed:
            adapter = conn.execute(
                "select * from adapters where tool = ? and idempotency_key = ?",
                (action["tool"], action["idempotency_key"]),
            ).fetchone()
            if not adapter:
                issues.append(f"completed adapter action has no adapter record: {action['id']}")
            elif adapter["external_id"] != action["external_id"] or adapter["external_link"] != action["external_link"]:
                issues.append(f"adapter action drift: {action['id']}")
    return issues


def create_checkpoint(root: Path, label: str) -> str:
    checkpoint_id = str(uuid.uuid4())
    with transaction(root) as conn:
        sequence = conn.execute("select coalesce(max(sequence), 0) from events").fetchone()[0]
        snapshot = runtime_snapshot(conn, include_events=True)
        conn.execute(
            "insert into runtime_snapshots (id, label, event_sequence, snapshot_json, created_at) values (?, ?, ?, ?, ?)",
            (checkpoint_id, label, sequence, stable_json(snapshot), now_iso()),
        )
        emit_event(conn, "checkpoint_created", payload(id=checkpoint_id, label=label, event_sequence=sequence))
    render_all(root)
    return checkpoint_id


def list_checkpoints(root: Path) -> list[str]:
    with connection(root) as conn:
        rows = conn.execute("select id, label, event_sequence, created_at from runtime_snapshots order by created_at, id").fetchall()
    return [markdown_row([row["id"], row["label"], row["event_sequence"], row["created_at"]]) for row in rows]


def export_checkpoint(root: Path, out: Path) -> None:
    with connection(root) as conn:
        row = conn.execute("select * from runtime_snapshots order by created_at desc, id desc limit 1").fetchone()
        if not row:
            raise HarnessError("missing checkpoint")
        package = {"schema_version": SCHEMA_VERSION, "runtime_version": RUNTIME_VERSION, "checkpoint": row_snapshot(row)}
    ensure_parent(out)
    out.write_text(json.dumps(package, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def import_checkpoint(root: Path, file_path: Path, *, apply: bool = False) -> list[str]:
    package = json.loads(file_path.read_text(encoding="utf-8"))
    checkpoint = package.get("checkpoint", {})
    snapshot = json.loads(checkpoint.get("snapshot_json", "{}"))
    issues = []
    if package.get("schema_version") != SCHEMA_VERSION:
        issues.append(f"schema version differs: package={package.get('schema_version')} runtime={SCHEMA_VERSION}")
    if not snapshot:
        issues.append("checkpoint package has no snapshot")
    if issues or not apply:
        return issues
    backup_runtime(root, "checkpoint-import")
    with transaction(root) as conn:
        restore_snapshot(conn, snapshot)
        emit_event(conn, "checkpoint_imported", payload(id=checkpoint.get("id", "")))
    render_all(root)
    return []


def export_events(root: Path, out: Path) -> None:
    with connection(root) as conn:
        rows = [row_snapshot(row) or {} for row in conn.execute("select * from events order by sequence")]
    ensure_parent(out)
    out.write_text("\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n", encoding="utf-8")


def validate_events(root: Path) -> list[str]:
    from core.event_bus import validate_replay_compatible_events

    with connection(root) as conn:
        return validate_replay_compatible_events(conn)


def apply_event_after(conn: sqlite3.Connection, event: sqlite3.Row) -> None:
    from core.event_bus import apply_event_after as core_apply_event_after

    core_apply_event_after(conn, event)


def replay_events(root: Path, to_sequence: int, out: Path) -> None:
    from core.event_bus import rebuild_state_from_events

    try:
        rebuild_state_from_events(root, to_sequence, out)
    except ValueError as exc:
        raise HarnessError(str(exc)) from exc


def add_agent_capability(root: Path, agent: str, capability: str) -> None:
    with transaction(root) as conn:
        require_agent(conn, agent)
        conn.execute("insert or ignore into agent_capabilities (agent_id, capability) values (?, ?)", (agent, capability))
        emit_event(conn, "agent_capability_added", payload(agent=agent, capability=capability))


def dispatch_plan(root: Path, scope: str) -> str:
    from core.scheduler import ready_queue

    run_id = str(uuid.uuid4())
    with transaction(root) as conn:
        conn.execute(
            "insert into dispatch_runs (id, scope, status, created_at, updated_at) values (?, ?, 'planned', ?, ?)",
            (run_id, scope, now_iso(), now_iso()),
        )
        ready_ids = ready_queue(conn)
        for task_id in ready_ids:
            task = conn.execute("select id, owner from tasks where id = ?", (task_id,)).fetchone()
            capability = task["owner"] if task["owner"] and task["owner"] != "unassigned" else "developer"
            conn.execute(
                "insert into dispatch_assignments (run_id, task_id, capability, status, updated_at) values (?, ?, ?, 'planned', ?)",
                (run_id, task["id"], capability, now_iso()),
            )
        emit_event(conn, "dispatch_planned", payload(id=run_id, scope=scope))
    return run_id


def default_codex_fanout_dir(root: Path, run_id: str) -> Path:
    return root / ".ai-team" / "runtime" / "codex-fanout" / safe_branch_part(run_id)


def codex_output_schema() -> dict[str, Any]:
    properties: dict[str, Any] = {
        "command": {"type": "string"},
        "exit_code": {"type": "integer"},
        "stdout_sha256": {"type": "string"},
        "artifact_path": {"type": "string"},
        "executed_count": {"type": "integer"},
        "executed_count_source": {"type": "string", "enum": ["parsed"]},
        "source_tree_hash": {"type": "string"},
        "branch_name": {"type": "string"},
        "status": {"type": "string", "enum": ["success", "failed"]},
        "target_id": {"type": "string"},
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": CODEX_FANOUT_OUTPUT_FIELDS,
        "properties": properties,
        "additionalProperties": False,
    }


def dispatch_export_csv(
    root: Path,
    run_id: str,
    *,
    out_dir: Path | None = None,
    max_concurrency: int = 6,
    max_runtime_seconds: int = 1800,
) -> Path:
    from core.scheduler import ready_queue

    if max_concurrency < 1 or max_concurrency > 6:
        raise HarnessError("max concurrency must be between 1 and 6")
    if max_runtime_seconds < 1 or max_runtime_seconds > 1800:
        raise HarnessError("max runtime seconds must be between 1 and 1800")
    output_dir = out_dir if out_dir else default_codex_fanout_dir(root, run_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    input_csv = output_dir / "input.csv"
    instruction_path = output_dir / "instruction.md"
    output_schema_path = output_dir / "output_schema.json"
    spawn_config_path = output_dir / "spawn_config.json"

    with connection(root) as conn:
        ready_ids = set(ready_queue(conn))
        assignments = conn.execute(
            """
            select da.run_id, da.task_id, da.agent_id, da.capability, da.status as assignment_status,
                   t.task, t.owner, t.fence
            from dispatch_assignments da
            join tasks t on t.id = da.task_id
            where da.run_id = ? and t.status = 'ready' and da.status in ('planned', 'claimed')
            order by da.task_id
            """,
            (run_id,),
        ).fetchall()
        assignments = [assignment for assignment in assignments if assignment["task_id"] in ready_ids]
        if not assignments:
            raise HarnessError(f"no ready dispatch assignments for run: {run_id}")
        rows: list[dict[str, str]] = []
        for assignment in assignments:
            acceptance = grouped(conn, "task_acceptance", "task_id", "acceptance_id").get(assignment["task_id"], "")
            failure_modes = grouped(conn, "task_failure_modes", "task_id", "failure_mode_id").get(assignment["task_id"], "")
            agent_id = assignment["agent_id"] or assignment["capability"] or assignment["owner"] or "developer"
            branch_name = f"agent/{safe_branch_part(run_id)}/{safe_branch_part(assignment['task_id'])}/{safe_branch_part(agent_id)}"
            target = task_target(conn, assignment["task_id"])
            rows.append(
                {
                    "item_id": assignment["task_id"],
                    "task": assignment["task"],
                    "acceptance": acceptance,
                    "failure_modes": failure_modes,
                    "target_id": target["id"] if target else "",
                    "command_template": target["command_template"] if target else "",
                    "branch_name": branch_name,
                    "fence": str(assignment["fence"]),
                    "agent_id": agent_id,
                }
            )

    with input_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CODEX_FANOUT_INPUT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    instruction = "\n".join(
        [
            "# Codex Harness Dispatch Worker",
            "",
            "You are the assigned Codex worker for task {item_id}.",
            "Task: {task}",
            "Acceptance: {acceptance}",
            "Failure modes: {failure_modes}",
            "Run the registered target `{target_id}` using `{command_template}` when available.",
            "Work on branch `{branch_name}` and report exactly one result via report_agent_job_result.",
            "Return JSON matching output_schema.json with parsed command evidence and status.",
            "",
        ]
    )
    instruction_path.write_text(instruction, encoding="utf-8")
    output_schema_path.write_text(json.dumps(codex_output_schema(), indent=2, sort_keys=True), encoding="utf-8")
    spawn_config = {
        "csv_path": input_csv.as_posix(),
        "instruction_template_path": instruction_path.as_posix(),
        "id_column": "item_id",
        "output_schema": output_schema_path.as_posix(),
        "output_csv_path": (output_dir / "output.csv").as_posix(),
        "max_concurrency": max_concurrency,
        "max_runtime_seconds": max_runtime_seconds,
        "max_depth": 1,
        "sqlite_home": (root / ".ai-team" / "state").as_posix(),
    }
    spawn_config_path.write_text(json.dumps(spawn_config, indent=2, sort_keys=True), encoding="utf-8")
    with transaction(root, touched=[("codex_fanout_export", run_id)]) as conn:
        conn.execute(
            """
            insert into codex_fanout_exports
            (id, run_id, input_csv_path, instruction_path, output_schema_path, spawn_config_path,
             max_concurrency, max_runtime_seconds, status, created_at, imported_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, 'exported', ?, '')
            """,
            (
                str(uuid.uuid4()),
                run_id,
                input_csv.relative_to(root).as_posix() if input_csv.is_relative_to(root) else input_csv.as_posix(),
                instruction_path.relative_to(root).as_posix() if instruction_path.is_relative_to(root) else instruction_path.as_posix(),
                output_schema_path.relative_to(root).as_posix() if output_schema_path.is_relative_to(root) else output_schema_path.as_posix(),
                spawn_config_path.relative_to(root).as_posix() if spawn_config_path.is_relative_to(root) else spawn_config_path.as_posix(),
                max_concurrency,
                max_runtime_seconds,
                now_iso(),
            ),
        )
        emit_event(conn, "codex_fanout_exported", payload(run_id=run_id, input_csv=input_csv.as_posix(), count=len(rows)))
    return output_dir


def dispatch_claim_next(root: Path, agent: str) -> str:
    from core.scheduler import ready_queue

    with transaction(root) as conn:
        require_agent(conn, agent)
        active = conn.execute(
            "select task_id from dispatch_assignments where agent_id = ? and status = 'claimed' limit 1",
            (agent,),
        ).fetchone()
        if active:
            raise HarnessError(f"agent already has dispatch assignment: {agent} -> {active['task_id']}")
        capabilities = {
            row["capability"]
            for row in conn.execute("select capability from agent_capabilities where agent_id = ?", (agent,))
        }
        capabilities.add(agent)
        ready_ids = ready_queue(conn)
        if not ready_ids:
            raise HarnessError(f"no dispatch assignment for agent: {agent}")
        assignment = conn.execute(
            f"""
            select da.* from dispatch_assignments da
            join tasks t on t.id = da.task_id
            where da.status = 'planned' and t.status = 'ready'
              and da.capability in ({','.join('?' for _ in capabilities)})
              and da.task_id in ({','.join('?' for _ in ready_ids)})
            order by da.updated_at, da.task_id
            limit 1
            """,
            tuple(capabilities) + tuple(ready_ids),
        ).fetchone()
        if not assignment:
            raise HarnessError(f"no dispatch assignment for agent: {agent}")
        conn.execute(
            "update dispatch_assignments set agent_id = ?, status = 'claimed', claimed_at = ?, heartbeat_at = ?, lease_expires_at = ?, updated_at = ? where run_id = ? and task_id = ?",
            (agent, now_iso(), now_iso(), lease_deadline(), now_iso(), assignment["run_id"], assignment["task_id"]),
        )
        emit_event(conn, "dispatch_assignment_claimed", payload(run_id=assignment["run_id"], task_id=assignment["task_id"], agent=agent))
        return assignment["task_id"]


def provider_event(conn: sqlite3.Connection, session: sqlite3.Row | dict[str, Any], event_type: str, values: dict[str, Any] | None = None) -> None:
    data = dict(values or {})
    if isinstance(session, sqlite3.Row):
        session_id = session["id"]
        run_id = session["run_id"]
        task_id = session["task_id"]
        provider = session["provider"]
    else:
        session_id = session.get("id", "")
        run_id = session.get("run_id", "")
        task_id = session.get("task_id", "")
        provider = session.get("provider", "")
    conn.execute(
        """
        insert into agent_provider_events
        (id, session_id, run_id, task_id, provider, event_type, payload_json, created_at)
        values (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (str(uuid.uuid4()), session_id, run_id, task_id, provider, event_type, stable_json(data), now_iso()),
    )


def provider_assignment_rows(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    from core.scheduler import ready_queue

    ready_ids = set(ready_queue(conn))
    if not ready_ids:
        return []
    placeholders = ",".join("?" for _ in ready_ids)
    return conn.execute(
        f"""
        select da.*, t.task, t.owner, t.fence
        from dispatch_assignments da
        join tasks t on t.id = da.task_id
        where da.run_id = ? and da.status in ('planned', 'claimed') and t.status = 'ready'
          and da.task_id in ({placeholders})
        order by da.updated_at, da.task_id
        """,
        (run_id, *tuple(ready_ids)),
    ).fetchall()


def dispatch_provider_start(root: Path, run_id: str, provider_name: str, *, max_concurrency: int = 6) -> int:
    from core.agent_provider import AgentJobRequest, provider_for

    if max_concurrency < 1 or max_concurrency > 6:
        raise HarnessError("max concurrency must be between 1 and 6")
    provider = provider_for(provider_name)
    started = 0
    with transaction(root, touched=[("dispatch_run", run_id)]) as conn:
        run = conn.execute("select * from dispatch_runs where id = ?", (run_id,)).fetchone()
        if not run:
            raise HarnessError(f"missing dispatch run: {run_id}")
        rows = provider_assignment_rows(conn, run_id)[:max_concurrency]
        for row in rows:
            existing = conn.execute(
                """
                select * from agent_provider_sessions
                where run_id = ? and task_id = ? and provider = ? and status in ('spawning', 'running', 'reported')
                """,
                (run_id, row["task_id"], provider.name),
            ).fetchone()
            if existing:
                continue
            agent_id = row["agent_id"] or row["capability"] or row["owner"] or "developer"
            branch_name = f"agent/{safe_branch_part(run_id)}/{safe_branch_part(row['task_id'])}/{safe_branch_part(agent_id)}"
            target = task_target(conn, row["task_id"])
            request = AgentJobRequest(
                root=root,
                run_id=run_id,
                task_id=row["task_id"],
                agent_id=agent_id,
                branch_name=branch_name,
                fence=int(row["fence"]),
                target_id=target["id"] if target else "",
                command_template=target["command_template"] if target else "",
                instruction=f"Work on task {row['task_id']}: {row['task']}",
                input_json={
                    "run_id": run_id,
                    "task_id": row["task_id"],
                    "task": row["task"],
                    "branch_name": branch_name,
                    "target_id": target["id"] if target else "",
                },
            )
            handle = provider.spawn(request)
            session_id = str(uuid.uuid4())
            agent_session_id = f"AGENT-SESSION-{uuid.uuid4().hex[:12]}"
            role = agent_id if agent_id in SESSION_ROLES else "developer"
            context_id = f"{run_id}:{row['task_id']}"
            now = now_iso()
            conn.execute(
                """
                insert into agent_sessions
                (session_id, agent_id, role, context_id, provider_session_id, origin, trust_level, status, started_at, ended_at)
                values (?, ?, ?, ?, ?, 'manual', 'local-only', 'running', ?, '')
                """,
                (agent_session_id, agent_id, role, context_id, handle.provider_session_id, now),
            )
            conn.execute(
                """
                insert into agent_provider_sessions
                (id, run_id, task_id, provider, provider_session_id, provider_job_id, agent_id, status, fence,
                 agent_session_id, branch_name, worktree_path, input_json, report_id, attempt_id, last_error, spawned_at,
                 heartbeat_at, lease_expires_at, collected_at, cancelled_at, finished_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, '', '', ?, ?, ?, ?, '', '', '')
                """,
                (
                    session_id,
                    run_id,
                    row["task_id"],
                    provider.name,
                    handle.provider_session_id,
                    handle.provider_job_id,
                    agent_id,
                    handle.status,
                    int(row["fence"]),
                    agent_session_id,
                    branch_name,
                    stable_json(request.input_json),
                    handle.message,
                    now,
                    now,
                    lease_deadline(),
                ),
            )
            conn.execute(
                """
                update dispatch_assignments
                set agent_id = ?, status = 'claimed', provider_session_id = ?, claimed_at = coalesce(claimed_at, ?),
                    heartbeat_at = ?, lease_expires_at = ?, updated_at = ?
                where run_id = ? and task_id = ?
                """,
                (agent_id, handle.provider_session_id, now, now, lease_deadline(), now, run_id, row["task_id"]),
            )
            session = conn.execute("select * from agent_provider_sessions where id = ?", (session_id,)).fetchone()
            provider_event(conn, session, "started", {"provider_status": handle.status})
            emit_event(conn, "agent_provider_session_started", payload(run_id=run_id, task_id=row["task_id"], provider=provider.name, provider_session_id=handle.provider_session_id))
            started += 1
        if started:
            conn.execute("update dispatch_runs set status = 'claimed', updated_at = ? where id = ?", (now_iso(), run_id))
    return started


def normalize_claim_path(path: str) -> str:
    value = path.strip()
    if not value:
        raise HarnessError("file claim path is required")
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise HarnessError(f"invalid file claim path: {path}")
    return candidate.as_posix()


def safe_branch_part(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in value).strip("-") or "item"


def git_ref_commit(root: Path, ref: str) -> str:
    result = subprocess.run(["git", "rev-parse", "--verify", ref], cwd=root, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise HarnessError(f"branch is missing: {ref}")
    return result.stdout.strip()


def git_ref_tree(root: Path, ref: str) -> str:
    result = subprocess.run(["git", "rev-parse", "--verify", f"{ref}^{{tree}}"], cwd=root, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise HarnessError(f"tree is unavailable for ref: {ref}")
    return result.stdout.strip()


def ensure_verification_worktree(root: Path, branch_name: str, run_id: str, task_id: str) -> Path:
    worktree = root / ".ai-team" / "runtime" / "controller-verifications" / safe_branch_part(run_id) / safe_branch_part(task_id)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    if worktree.exists():
        subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=root, text=True, capture_output=True, check=False)
        shutil.rmtree(worktree, ignore_errors=True)
    result = subprocess.run(["git", "worktree", "add", str(worktree), branch_name], cwd=root, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise HarnessError(f"verification worktree create failed: {result.stderr.strip() or result.stdout.strip()}")
    return worktree


def ensure_integration_runtime_link(root: Path, worktree: Path) -> None:
    source = root / ".ai-team"
    target = worktree / ".ai-team"
    if target.exists() or target.is_symlink():
        return
    if source.exists():
        os.symlink(source, target, target_is_directory=True)


def assignment_for_agent(root: Path, agent: str) -> dict[str, Any]:
    with connection(root) as conn:
        active = conn.execute(
            "select * from dispatch_assignments where agent_id = ? and status = 'claimed' order by claimed_at, task_id limit 1",
            (agent,),
        ).fetchone()
    if not active:
        try:
            dispatch_claim_next(root, agent)
        except HarnessError:
            pass
    with connection(root) as conn:
        assignment = conn.execute(
            "select * from dispatch_assignments where agent_id = ? and status = 'claimed' order by claimed_at, task_id limit 1",
            (agent,),
        ).fetchone()
    if assignment:
        return row_snapshot(assignment) or {}
    return {"run_id": "", "task_id": "local-execution", "agent_id": agent}


def ensure_dispatch_worktree(root: Path, run_id: str, task_id: str, agent: str) -> tuple[str, str]:
    run_part = safe_branch_part(run_id or "local")
    task_part = safe_branch_part(task_id)
    agent_part = safe_branch_part(agent)
    branch = f"agent/{run_part}/{task_part}/{agent_part}"
    worktree = root / ".ai-team" / "runtime" / "worktrees" / run_part / task_part / agent_part
    with connection(root) as conn:
        existing = conn.execute(
            "select * from dispatch_worktrees where run_id = ? and task_id = ? and agent_id = ? and status = 'active' order by created_at desc limit 1",
            (run_id, task_id, agent),
        ).fetchone()
        if existing and (root / existing["worktree_path"]).exists():
            return existing["branch_name"], existing["worktree_path"]
    worktree.parent.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        raise HarnessError("local-process runner requires a git repository")
    subprocess.run(["git", "worktree", "prune"], cwd=root, text=True, capture_output=True, check=False)
    result = subprocess.run(
        ["git", "worktree", "add", "-B", branch, str(worktree), "HEAD"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise HarnessError(f"worktree create failed: {result.stderr.strip() or result.stdout.strip()}")
    rel = worktree.relative_to(root).as_posix()
    with transaction(root, touched=[("dispatch_worktree", task_id)]) as conn:
        conn.execute(
            """
            insert into dispatch_worktrees
            (id, run_id, task_id, agent_id, branch_name, worktree_path, status, created_at, cleaned_at)
            values (?, ?, ?, ?, ?, ?, 'active', ?, '')
            """,
            (str(uuid.uuid4()), run_id, task_id, agent, branch, rel, now_iso()),
        )
        emit_event(conn, "dispatch_worktree_created", payload(run_id=run_id, task_id=task_id, agent=agent, branch=branch, worktree_path=rel))
    return branch, rel


def changed_worktree_files(work_dir: Path) -> list[str]:
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=work_dir,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise HarnessError(f"worktree status failed: {result.stderr.strip() or result.stdout.strip()}")
    changed: list[str] = []
    for line in result.stdout.splitlines():
        relpath = line[3:] if len(line) > 3 else ""
        if " -> " in relpath:
            relpath = relpath.split(" -> ", 1)[1]
        if relpath and not relpath.startswith(".ai-team/"):
            changed.append(relpath)
    return sorted(set(changed))


def committed_files_since(work_dir: Path, base_commit: str) -> list[str]:
    if not base_commit:
        return []
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base_commit}..HEAD"],
        cwd=work_dir,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise HarnessError(f"worktree diff failed: {result.stderr.strip() or result.stdout.strip()}")
    return sorted(path for path in result.stdout.splitlines() if path and not path.startswith(".ai-team/"))


def reset_worktree_to_base(work_dir: Path, base_commit: str) -> None:
    if base_commit:
        subprocess.run(["git", "reset", "--hard", base_commit], cwd=work_dir, text=True, capture_output=True, check=False)
    subprocess.run(["git", "clean", "-fd"], cwd=work_dir, text=True, capture_output=True, check=False)


def commit_worktree_claims(work_dir: Path, agent: str, task_id: str, claim_files: list[str], *, base_commit: str = "") -> None:
    changed = sorted(set(changed_worktree_files(work_dir) + committed_files_since(work_dir, base_commit)))
    allowed = set(claim_files)
    unexpected = sorted(path for path in changed if path not in allowed)
    if unexpected:
        reset_worktree_to_base(work_dir, base_commit)
        raise HarnessError(f"file-claim-violation: {', '.join(unexpected)}")
    if not claim_files or not changed:
        return
    subprocess.run(["git", "add", "--", *claim_files], cwd=work_dir, text=True, capture_output=True, check=False)
    diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=work_dir, text=True, capture_output=True, check=False)
    if diff.returncode == 0:
        return
    result = subprocess.run(
        ["git", "-c", "user.name=Codex Harness", "-c", "user.email=harness@example.invalid", "commit", "-m", f"Agent {agent} task {task_id}"],
        cwd=work_dir,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise HarnessError(f"worktree commit failed: {result.stderr.strip() or result.stdout.strip()}")


def dispatch_file_claim_add(root: Path, task_id: str, agent: str, path: str, *, run_id: str = "", worktree_path: str = "", branch_name: str = "") -> str:
    normalized = normalize_claim_path(path)
    with transaction(root, touched=[("task_file_claim", normalized)]) as conn:
        existing = conn.execute(
            "select id from task_file_claims where task_id = ? and agent_id = ? and path = ? and status = 'active'",
            (task_id, agent, normalized),
        ).fetchone()
        if existing:
            return existing["id"]
        try:
            claim_id = str(uuid.uuid4())
            conn.execute(
                """
                insert into task_file_claims
                (id, run_id, task_id, agent_id, path, worktree_path, branch_name, status, created_at, released_at)
                values (?, ?, ?, ?, ?, ?, ?, 'active', ?, '')
                """,
                (claim_id, run_id, task_id, agent, normalized, worktree_path, branch_name, now_iso()),
            )
        except sqlite3.IntegrityError as exc:
            raise HarnessError(f"file-claim-conflict: {normalized}") from exc
        emit_event(conn, "task_file_claimed", payload(id=claim_id, run_id=run_id, task_id=task_id, agent=agent, path=normalized))
        return claim_id


def dispatch_file_claim_list(root: Path, *, task_id: str = "", agent: str = "") -> list[str]:
    clauses = ["status = 'active'"]
    params: list[str] = []
    if task_id:
        clauses.append("task_id = ?")
        params.append(task_id)
    if agent:
        clauses.append("agent_id = ?")
        params.append(agent)
    query = f"select * from task_file_claims where {' and '.join(clauses)} order by path"
    with connection(root) as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    lines = ["| Path | Task | Agent | Run | Worktree |", "| --- | --- | --- | --- | --- |"]
    lines.extend(markdown_row([row["path"], row["task_id"], row["agent_id"], row["run_id"], row["worktree_path"]]) for row in rows)
    return lines


def dispatch_file_claim_release(root: Path, task_id: str, agent: str, *, path: str = "") -> int:
    normalized = normalize_claim_path(path) if path else ""
    clauses = ["task_id = ?", "agent_id = ?", "status = 'active'"]
    params: list[str] = [task_id, agent]
    if normalized:
        clauses.append("path = ?")
        params.append(normalized)
    with transaction(root, touched=[("task_file_claim", normalized or task_id)]) as conn:
        rows = conn.execute(f"select id from task_file_claims where {' and '.join(clauses)}", tuple(params)).fetchall()
        for row in rows:
            conn.execute("update task_file_claims set status = 'released', released_at = ? where id = ?", (now_iso(), row["id"]))
        if rows:
            emit_event(conn, "task_file_claim_released", payload(task_id=task_id, agent=agent, path=normalized, count=len(rows)))
    return len(rows)


def dispatch_run(
    root: Path,
    agent: str,
    command: str,
    *,
    timeout: int = 120,
    target_id: str = "",
    allow_unlisted: bool = False,
    no_network: bool = False,
    sandbox_profile: str = "none",
    allow_unlisted_reason: str = "",
    executed_count: int | None = None,
    code_identity: str = "auto",
    runner: str = "null",
    claim_files: list[str] | None = None,
) -> str:
    from dataclasses import replace
    from core.agent_runner import RunnerRequest, runner_for
    guard_schema("validate_code_identity_mode", code_identity)

    assignment = assignment_for_agent(root, agent)
    run_id = assignment["run_id"]
    task_id = assignment["task_id"]
    branch_name = ""
    worktree_path = ""
    work_dir = root
    base_commit = git_head_sha(root) or ""
    if runner == "local-process":
        branch_name, worktree_path = ensure_dispatch_worktree(root, run_id, task_id, agent)
        work_dir = root / worktree_path
    elif runner != "null":
        raise HarnessError(f"unknown runner: {runner}")
    for claim_file in claim_files or []:
        dispatch_file_claim_add(root, task_id, agent, claim_file, run_id=run_id, worktree_path=worktree_path, branch_name=branch_name)

    with connection(root) as conn:
        target_command = test_target_command(conn, target_id) if target_id else ""
        prefixes = executor_prefixes(conn)

    runner_result = runner_for(runner).run(RunnerRequest(
        root=root,
        work_dir=work_dir,
        command=command,
        timeout=timeout,
        target_id=target_id,
        target_command_template=target_command,
        allowed_prefixes=prefixes,
        allow_unlisted=allow_unlisted,
        no_network=no_network,
        sandbox_profile="no-network" if no_network else sandbox_profile,
        allow_unlisted_reason=allow_unlisted_reason,
        executed_count=executed_count,
    ))
    result = runner_result.evidence
    normalized_claims = [normalize_claim_path(path) for path in (claim_files or [])]
    if runner == "local-process" and result.exit_code == 0:
        try:
            commit_worktree_claims(work_dir, agent, task_id, normalized_claims, base_commit=base_commit)
        except HarnessError:
            if run_id:
                with transaction(root, touched=[("dispatch_assignment", task_id)]) as conn:
                    conn.execute("update dispatch_assignments set status = 'failed', updated_at = ? where run_id = ? and task_id = ?", (now_iso(), run_id, task_id))
                    conn.execute("update dispatch_runs set status = 'failed', updated_at = ? where id = ?", (now_iso(), run_id))
                    emit_event(conn, "dispatch_command_failed", payload(run_id=run_id, task_id=task_id, agent=agent, reason="file-claim-violation"))
            raise
    if runner == "local-process":
        source_artifact = work_dir / result.artifact_path
        stdout = source_artifact.read_bytes()
        artifact = root / ".ai-team" / "runtime" / "executions" / uuid.uuid4().hex / "stdout.txt"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(stdout)
        result = replace(result, artifact_path=artifact.relative_to(root).as_posix(), stdout_sha256=hashlib.sha256(stdout).hexdigest())
    evidence_id = f"EXEC-{uuid.uuid4().hex[:12]}"
    source_hash = source_tree_hash_for_mode(work_dir, code_identity)
    code_ref = branch_name if runner == "local-process" else ""
    tree_sha = git_ref_tree(root, branch_name) if branch_name else ""
    status = "completed" if result.exit_code == 0 else "failed"
    with transaction(root, touched=[("dispatch_assignment", task_id), ("evidence", evidence_id)]) as conn:
        conn.execute(
            """
            insert into evidence
            (id, kind, summary, uri, hash, command, exit_code, stdout_sha256, artifact_path, source_tree_hash,
             target_id, executed_count, executed_count_source, allow_unlisted, no_network, policy_status, policy_reason,
             sandbox_profile, sandbox_status, allow_unlisted_reason, trust_anchor, trust_anchor_id,
             attempt_id, tree_sha, code_ref, verified_by,
             created_at)
            values (?, 'command', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'local-only', '', ?, ?, ?, 'controller-local', ?)
            """,
            (
                evidence_id,
                f"dispatch {task_id} command exit {result.exit_code}",
                f"local://{result.artifact_path}",
                result.stdout_sha256,
                result.command,
                result.exit_code,
                result.stdout_sha256,
                result.artifact_path,
                source_hash,
                result.target_id,
                result.executed_count,
                result.executed_count_source,
                bool_int(result.allow_unlisted),
                bool_int(result.no_network),
                result.policy_status,
                result.policy_reason,
                result.sandbox_profile,
                result.sandbox_status,
                result.allow_unlisted_reason,
                "",
                tree_sha,
                code_ref,
                now_iso(),
            ),
        )
        if run_id:
            conn.execute(
                """
                update dispatch_assignments
                set status = ?, evidence = ?, updated_at = ?
                where run_id = ? and task_id = ?
                """,
                (status, evidence_id, now_iso(), run_id, task_id),
            )
            conn.execute("update dispatch_runs set status = ?, updated_at = ? where id = ?", (status, now_iso(), run_id))
            if result.exit_code == 0:
                task = conn.execute("select status from tasks where id = ?", (task_id,)).fetchone()
                if task and task["status"] in {"ready", "claimed", "in_progress"}:
                    conn.execute(
                        """
                        update tasks set status = 'submitted', evidence = ?, submitted_by = ?, lease_agent = null,
                          lease_token = null, lease_heartbeat_at = null, lease_expires_at = null,
                          revision = revision + 1, updated_at = ? where id = ?
                        """,
                        (evidence_id, agent, now_iso(), task_id),
                    )
        emit_event(
            conn,
            "dispatch_command_executed",
            payload(
                run_id=run_id,
                task_id=task_id,
                agent=agent,
                evidence_id=evidence_id,
                exit_code=result.exit_code,
                timed_out=result.timed_out,
                runner=runner,
                file_claims=normalized_claims,
                allow_unlisted_reason=result.allow_unlisted_reason,
                sandbox_profile=result.sandbox_profile,
                sandbox_status=result.sandbox_status,
            ),
        )
    if result.exit_code != 0:
        detail = f" {result.policy_reason}" if result.policy_reason else ""
        raise HarnessError(f"dispatch command failed: {task_id} exit_code={result.exit_code} evidence={evidence_id}{detail}")
    return evidence_id


def dispatch_recover_stale(root: Path) -> int:
    recovered = 0
    with transaction(root) as conn:
        rows = conn.execute(
            """
            select da.run_id, da.task_id from dispatch_assignments da
            join tasks t on t.id = da.task_id
            where da.status = 'claimed' and t.status = 'ready'
              and da.lease_expires_at is not null and da.lease_expires_at <= ?
            order by da.updated_at
            """,
            (now_iso(),),
        ).fetchall()
        for row in rows:
            conn.execute(
                "update dispatch_assignments set agent_id = '', status = 'planned', claimed_at = null, heartbeat_at = null, lease_expires_at = null, updated_at = ? where run_id = ? and task_id = ?",
                (now_iso(), row["run_id"], row["task_id"]),
            )
            recovered += 1
        if recovered:
            emit_event(conn, "dispatch_stale_recovered", payload(count=recovered))
    return recovered


def record_integration_finding(conn: sqlite3.Connection, run_id: str, summary: str) -> str:
    finding_id = f"INT-{uuid.uuid4().hex[:12]}"
    conn.execute(
        """
        insert into findings
        (id, surface, severity, status, summary, evidence_id, created_at)
        values (?, 'dispatch-integration', 'high', 'open', ?, '', ?)
        """,
        (finding_id, summary[:1000], now_iso()),
    )
    emit_event(conn, "dispatch_integration_finding_recorded", payload(run_id=run_id, finding_id=finding_id, summary=summary[:500]))
    return finding_id


def git_changed_files_between(root: Path, base_ref: str, branch_name: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base_ref}..{branch_name}"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise HarnessError(f"branch diff failed: {result.stderr.strip() or result.stdout.strip()}")
    return sorted(path for path in result.stdout.splitlines() if path and not path.startswith(".ai-team/"))


def record_integration_attempt_start(conn: sqlite3.Connection, run_id: str, target_branch: str, integration_worktree: Path, base_ref: str) -> str:
    attempt_id = f"INTEGRATION-{uuid.uuid4().hex[:12]}"
    conn.execute(
        """
        insert into integration_attempts
        (id, run_id, target_branch, integration_worktree, base_ref, merged_branches, status,
         validation_result, finding_id, started_at, finished_at)
        values (?, ?, ?, ?, ?, '', 'running', '', '', ?, '')
        """,
        (attempt_id, run_id, target_branch, integration_worktree.as_posix(), base_ref, now_iso()),
    )
    return attempt_id


def finish_integration_attempt(
    conn: sqlite3.Connection,
    attempt_id: str,
    status: str,
    *,
    merged_branches: list[str] | None = None,
    validation_result: str = "",
    finding_id: str = "",
) -> None:
    conn.execute(
        """
        update integration_attempts
        set status = ?, merged_branches = ?, validation_result = ?, finding_id = ?, finished_at = ?
        where id = ?
        """,
        (
            status,
            stable_json(merged_branches or []),
            validation_result[:2000],
            finding_id,
            now_iso(),
            attempt_id,
        ),
    )


def fail_integration_precheck(root: Path, run_id: str, attempt_id: str, status: str, message: str) -> None:
    with transaction(root, touched=[("dispatch_run", run_id), ("finding", run_id)]) as conn:
        finding_id = record_integration_finding(conn, run_id, message)
        finish_integration_attempt(conn, attempt_id, status, validation_result=message, finding_id=finding_id)
        conn.execute("update dispatch_runs set status = 'verification_failed', updated_at = ? where id = ?", (now_iso(), run_id))
        conn.execute("update dispatch_assignments set status = 'verification_failed', updated_at = ? where run_id = ?", (now_iso(), run_id))
        emit_event(conn, "dispatch_integration_precheck_failed", payload(run_id=run_id, status=status, message=message[:500]))


def integration_verified_attempt(conn: sqlite3.Connection, run_id: str, task_id: str, branch_name: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        select * from task_attempts
        where run_id = ? and task_id = ? and branch_name = ? and status = 'verified' and evidence_id != ''
        order by finished_at desc, started_at desc, id desc
        limit 1
        """,
        (run_id, task_id, branch_name),
    ).fetchone()


def verify_integration_inputs(root: Path, run_id: str, rows: list[sqlite3.Row], attempt_id: str) -> None:
    with connection(root) as conn:
        for row in rows:
            attempt = integration_verified_attempt(conn, run_id, row["task_id"], row["branch_name"])
            if not attempt:
                message = f"integration-unverified-branch: {row['task_id']} {row['branch_name']}"
                fail_integration_precheck(root, run_id, attempt_id, "integration_unverified_branch", message)
                raise HarnessError(message)
            actual_head = git_ref_commit(root, row["branch_name"])
            actual_tree = git_ref_tree(root, row["branch_name"])
            if actual_head != attempt["head_commit_sha"] or actual_tree != attempt["tree_sha"]:
                message = (
                    f"integration-branch-drift: {row['task_id']} {row['branch_name']} "
                    f"expected_head={attempt['head_commit_sha']} actual_head={actual_head} "
                    f"expected_tree={attempt['tree_sha']} actual_tree={actual_tree}"
                )
                fail_integration_precheck(root, run_id, attempt_id, "integration_branch_drift", message)
                raise HarnessError(message)
            changed_files = git_changed_files_between(root, attempt["base_commit_sha"] or "HEAD", row["branch_name"])
            claim_rows = conn.execute(
                """
                select path from task_file_claims
                where run_id = ? and task_id = ? and agent_id = ? and status = 'active'
                """,
                (run_id, row["task_id"], row["agent_id"]),
            ).fetchall()
            allowed = {claim["path"] for claim in claim_rows}
            unexpected = sorted(path for path in changed_files if path not in allowed)
            if unexpected:
                message = f"file-claim-violation: {row['task_id']} {', '.join(unexpected)}"
                fail_integration_precheck(root, run_id, attempt_id, "file_claim_violation", message)
                raise HarnessError(message)


def dispatch_integrate(root: Path, run_id: str, *, target_branch: str = "") -> str:
    if not (root / ".git").exists():
        raise HarnessError("dispatch integrate requires a git repository")
    target = target_branch or f"integration/{safe_branch_part(run_id)}"
    current = subprocess.run(["git", "branch", "--show-current"], cwd=root, text=True, capture_output=True, check=False).stdout.strip()
    if not current:
        raise HarnessError("dispatch integrate requires a named current branch")
    with connection(root) as conn:
        rows = conn.execute(
            "select * from dispatch_worktrees where run_id = ? and status = 'active' order by created_at",
            (run_id,),
        ).fetchall()
    if not rows:
        raise HarnessError(f"no active dispatch worktrees for run: {run_id}")
    integration_worktree = root / ".ai-team" / "runtime" / "integration-worktrees" / safe_branch_part(run_id)
    with transaction(root, touched=[("dispatch_run", run_id)]) as conn:
        integration_attempt_id = record_integration_attempt_start(conn, run_id, target, integration_worktree, current)
    verify_integration_inputs(root, run_id, rows, integration_attempt_id)
    integration_worktree.parent.mkdir(parents=True, exist_ok=True)
    if integration_worktree.exists():
        subprocess.run(["git", "worktree", "remove", "--force", str(integration_worktree)], cwd=root, text=True, capture_output=True, check=False)
        shutil.rmtree(integration_worktree, ignore_errors=True)
    create = subprocess.run(["git", "worktree", "add", "-B", target, str(integration_worktree), current], cwd=root, text=True, capture_output=True, check=False)
    if create.returncode != 0:
        raise HarnessError(f"integration worktree create failed: {create.stderr.strip() or create.stdout.strip()}")
    ensure_integration_runtime_link(root, integration_worktree)
    try:
        for row in rows:
            merge = subprocess.run(["git", "merge", "--no-ff", "--no-edit", row["branch_name"]], cwd=integration_worktree, text=True, capture_output=True, check=False)
            if merge.returncode != 0:
                subprocess.run(["git", "merge", "--abort"], cwd=integration_worktree, text=True, capture_output=True, check=False)
                summary = f"merge conflict for {row['task_id']} from {row['branch_name']}: {merge.stderr.strip() or merge.stdout.strip()}"
                with transaction(root, touched=[("dispatch_run", run_id), ("finding", run_id)]) as conn:
                    finding_id = record_integration_finding(conn, run_id, summary)
                    finish_integration_attempt(conn, integration_attempt_id, "integration_conflict", merged_branches=[r["branch_name"] for r in rows], validation_result=summary, finding_id=finding_id)
                    conn.execute("update dispatch_runs set status = 'integration_conflict', updated_at = ? where id = ?", (now_iso(), run_id))
                    conn.execute("update dispatch_assignments set status = 'integration_conflict', updated_at = ? where run_id = ?", (now_iso(), run_id))
                    emit_event(conn, "dispatch_integration_conflict", payload(run_id=run_id, branch=row["branch_name"]))
                raise HarnessError(f"integration conflict: {row['task_id']}")
        issues = validate_runtime(integration_worktree, delivery=True)
        if issues:
            issue_text = [str(issue) for issue in issues]
            summary = "; ".join(issue_text[:5])
            with transaction(root, touched=[("dispatch_run", run_id), ("finding", run_id)]) as conn:
                finding_id = record_integration_finding(conn, run_id, f"delivery validation failed after integration: {summary}")
                finish_integration_attempt(conn, integration_attempt_id, "verification_failed", merged_branches=[r["branch_name"] for r in rows], validation_result=summary, finding_id=finding_id)
                conn.execute("update dispatch_runs set status = 'verification_failed', updated_at = ? where id = ?", (now_iso(), run_id))
                conn.execute("update dispatch_assignments set status = 'verification_failed', updated_at = ? where run_id = ?", (now_iso(), run_id))
                emit_event(conn, "dispatch_integration_verification_failed", payload(run_id=run_id, issues=issue_text[:10]))
            raise HarnessError(f"integration verification failed: {summary}")
        with transaction(root, touched=[("dispatch_run", run_id)]) as conn:
            finish_integration_attempt(conn, integration_attempt_id, "integrated", merged_branches=[r["branch_name"] for r in rows], validation_result="pass")
            conn.execute("update dispatch_runs set status = 'integrated', updated_at = ? where id = ?", (now_iso(), run_id))
            conn.execute("update dispatch_assignments set status = 'integrated', updated_at = ? where run_id = ?", (now_iso(), run_id))
            for row in rows:
                if row["worktree_path"]:
                    worktree = root / row["worktree_path"]
                    subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=root, text=True, capture_output=True, check=False)
                conn.execute("update dispatch_worktrees set status = 'cleaned', cleaned_at = ? where id = ?", (now_iso(), row["id"]))
            emit_event(conn, "dispatch_integrated", payload(run_id=run_id, target_branch=target))
        subprocess.run(["git", "worktree", "remove", "--force", str(integration_worktree)], cwd=root, text=True, capture_output=True, check=False)
        return target
    finally:
        if integration_worktree.exists():
            subprocess.run(["git", "worktree", "remove", "--force", str(integration_worktree)], cwd=root, text=True, capture_output=True, check=False)


def resolve_runtime_path(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def resolve_container_image(root: Path, container_image: str = "") -> str:
    if container_image.strip():
        return container_image.strip()
    config = root / ".ai-team" / "control" / "container-image.txt"
    if config.exists():
        configured = config.read_text(encoding="utf-8").strip()
        if configured:
            return configured
    return DEFAULT_CONTAINER_IMAGE


def read_csv_dicts(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def latest_fanout_export(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
    row = conn.execute(
        "select * from codex_fanout_exports where run_id = ? order by created_at desc limit 1",
        (run_id,),
    ).fetchone()
    if not row:
        raise HarnessError(f"missing codex fanout export for run: {run_id}")
    return row


def codex_import_failure(conn: sqlite3.Connection, run_id: str, task_id: str, message: str) -> None:
    record_integration_finding(conn, run_id, f"codex fanout import failed for {task_id}: {message}")
    conn.execute("update dispatch_runs set status = 'verification_failed', updated_at = ? where id = ?", (now_iso(), run_id))
    conn.execute("update dispatch_assignments set status = 'verification_failed', updated_at = ? where run_id = ? and task_id = ?", (now_iso(), run_id, task_id))


def codex_report_issues(root: Path, conn: sqlite3.Connection, expected: dict[str, str], result: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    for field in CODEX_FANOUT_OUTPUT_FIELDS:
        if field not in result:
            issues.append(f"missing result field: {field}")
    if issues:
        return issues
    if result["status"] != "success":
        issues.append(f"result status is not success: {result['status']}")
    target = conn.execute("select * from test_targets where id = ?", (expected.get("target_id") or result["target_id"],)).fetchone()
    if expected.get("target_id") and result["target_id"] != expected["target_id"]:
        issues.append(f"target differs from export: expected={expected['target_id']} actual={result['target_id']}")
    elif expected.get("target_id") and not target:
        issues.append(f"missing test target: {result['target_id']}")
    if result["branch_name"] != expected["branch_name"]:
        issues.append(f"branch differs from export: expected={expected['branch_name']} actual={result['branch_name']}")
    branch = subprocess.run(["git", "rev-parse", "--verify", str(result["branch_name"])], cwd=root, text=True, capture_output=True, check=False)
    if branch.returncode != 0:
        issues.append(f"branch is missing: {result['branch_name']}")
    task = conn.execute("select fence from tasks where id = ?", (expected["item_id"],)).fetchone()
    if not task:
        issues.append(f"task is missing: {expected['item_id']}")
    elif int(task["fence"]) != int(expected["fence"]):
        issues.append(f"fence-stale: {expected['item_id']} expected={expected['fence']} actual={task['fence']}")
    return issues


def dispatch_import_csv(root: Path, run_id: str, result_csv: Path) -> str:
    with connection(root) as conn:
        export = latest_fanout_export(conn, run_id)
        input_csv = resolve_runtime_path(root, export["input_csv_path"])
    expected_rows = {row["item_id"]: row for row in read_csv_dicts(input_csv)}
    result_rows = read_csv_dicts(result_csv)
    seen: set[str] = set()
    imported = 0
    failed = False
    for row in result_rows:
        for column in CODEX_FANOUT_RESULT_COLUMNS:
            if column not in row:
                raise HarnessError(f"result CSV missing column: {column}")
        item_id = row["item_id"]
        seen.add(item_id)
        expected = expected_rows.get(item_id)
        if not expected:
            with transaction(root, touched=[("dispatch_run", run_id), ("finding", item_id)]) as conn:
                codex_import_failure(conn, run_id, item_id or "unknown", "unexpected item_id")
            failed = True
            continue
        if row["status"] != "success" or row["last_error"]:
            with transaction(root, touched=[("dispatch_run", run_id), ("finding", item_id)]) as conn:
                codex_import_failure(conn, run_id, item_id, row["last_error"] or f"worker status {row['status']}")
            failed = True
            continue
        try:
            result = json.loads(row["result_json"])
        except json.JSONDecodeError as exc:
            with transaction(root, touched=[("dispatch_run", run_id), ("finding", item_id)]) as conn:
                codex_import_failure(conn, run_id, item_id, f"invalid result_json: {exc.msg}")
            failed = True
            continue
        with connection(root) as conn:
            issues = codex_report_issues(root, conn, expected, result)
        if issues:
            with transaction(root, touched=[("dispatch_run", run_id), ("finding", item_id)]) as conn:
                codex_import_failure(conn, run_id, item_id, "; ".join(issues[:5]))
            failed = True
            continue
        report_id = f"REPORT-{uuid.uuid4().hex[:12]}"
        attempt_id = f"ATTEMPT-{uuid.uuid4().hex[:12]}"
        head_commit = git_ref_commit(root, result["branch_name"])
        tree_sha = git_ref_tree(root, result["branch_name"])
        with transaction(root, touched=[("dispatch_assignment", item_id), ("task_attempt", attempt_id)]) as conn:
            conn.execute(
                """
                insert into agent_reports
                (id, run_id, task_id, provider_session_id, job_id, status, last_error, result_json, created_at)
                values (?, ?, ?, '', ?, ?, ?, ?, ?)
                """,
                (report_id, run_id, item_id, row["job_id"], row["status"], row["last_error"], row["result_json"], now_iso()),
            )
            conn.execute(
                """
                insert into task_attempts
                (id, run_id, task_id, agent_id, fence, base_commit_sha, head_commit_sha, tree_sha,
                 branch_name, target_id, status, provider_session_id, agent_session_id, report_id, evidence_id, started_at, finished_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reported', '', '', ?, '', ?, '')
                """,
                (
                    attempt_id,
                    run_id,
                    item_id,
                    expected["agent_id"],
                    int(expected["fence"]),
                    git_base_commit(root) or "",
                    head_commit,
                    tree_sha,
                    result["branch_name"],
                    result["target_id"],
                    report_id,
                    now_iso(),
                ),
            )
            conn.execute(
                "update dispatch_assignments set status = 'reported', updated_at = ? where run_id = ? and task_id = ?",
                (now_iso(), run_id, item_id),
            )
            conn.execute(
                """
                insert into dispatch_worktrees
                (id, run_id, task_id, agent_id, branch_name, worktree_path, status, created_at, cleaned_at)
                values (?, ?, ?, ?, ?, '', 'active', ?, '')
                """,
                (str(uuid.uuid4()), run_id, item_id, expected["agent_id"], result["branch_name"], now_iso()),
            )
            emit_event(conn, "codex_fanout_report_imported", payload(run_id=run_id, item_id=item_id, report_id=report_id, attempt_id=attempt_id))
        imported += 1
    missing = sorted(set(expected_rows) - seen)
    for item_id in missing:
        with transaction(root, touched=[("dispatch_run", run_id), ("finding", item_id)]) as conn:
            codex_import_failure(conn, run_id, item_id, "worker did not report result")
        failed = True
    with transaction(root, touched=[("codex_fanout_export", run_id), ("dispatch_run", run_id)]) as conn:
        conn.execute(
            "update codex_fanout_exports set status = ?, imported_at = ? where run_id = ? and imported_at = ''",
            ("verification_failed" if failed else "imported", now_iso(), run_id),
        )
        if not failed:
            conn.execute("update dispatch_runs set status = 'reported', updated_at = ? where id = ?", (now_iso(), run_id))
    if failed:
        raise HarnessError(f"codex fanout import failed: {run_id}")
    return f"imported {imported} report(s)"


def provider_handle_from_row(row: sqlite3.Row) -> Any:
    from core.agent_provider import AgentJobHandle

    return AgentJobHandle(
        provider=row["provider"],
        provider_session_id=row["provider_session_id"],
        provider_job_id=row["provider_job_id"],
        status=row["status"],
        message=row["last_error"],
    )


def expected_from_provider_session(session: sqlite3.Row) -> dict[str, str]:
    data = json.loads(session["input_json"] or "{}")
    return {
        "item_id": session["task_id"],
        "target_id": str(data.get("target_id", "")),
        "branch_name": session["branch_name"],
        "fence": str(session["fence"]),
        "agent_id": session["agent_id"],
    }


def dispatch_provider_collect(root: Path, run_id: str) -> int:
    from core.agent_provider import provider_for

    collected = 0
    with connection(root) as conn:
        sessions = conn.execute(
            "select * from agent_provider_sessions where run_id = ? and status = 'running' order by spawned_at, task_id",
            (run_id,),
        ).fetchall()
    for session in sessions:
        provider = provider_for(session["provider"])
        report = provider.collect(provider_handle_from_row(session), root=root, run_id=run_id, task_id=session["task_id"])
        if report is None:
            with transaction(root, touched=[("agent_provider_session", session["id"])]) as conn:
                conn.execute(
                    "update agent_provider_sessions set heartbeat_at = ?, lease_expires_at = ? where id = ?",
                    (now_iso(), lease_deadline(), session["id"]),
                )
            continue
        if report.status != "success" or report.last_error:
            with transaction(root, touched=[("agent_provider_session", session["id"]), ("finding", session["task_id"])]) as conn:
                record_integration_finding(conn, run_id, f"provider report failed for {session['task_id']}: {report.last_error or report.status}")
                conn.execute(
                    "update agent_provider_sessions set status = 'verification_failed', last_error = ?, collected_at = ?, finished_at = ? where id = ?",
                    (report.last_error or report.status, now_iso(), now_iso(), session["id"]),
                )
                conn.execute("update dispatch_assignments set status = 'verification_failed', updated_at = ? where run_id = ? and task_id = ?", (now_iso(), run_id, session["task_id"]))
                conn.execute("update dispatch_runs set status = 'verification_failed', updated_at = ? where id = ?", (now_iso(), run_id))
                provider_event(conn, session, "collect_failed", {"status": report.status, "last_error": report.last_error})
            continue
        try:
            result = json.loads(report.result_json)
        except json.JSONDecodeError as exc:
            with transaction(root, touched=[("agent_provider_session", session["id"]), ("finding", session["task_id"])]) as conn:
                record_integration_finding(conn, run_id, f"provider report invalid for {session['task_id']}: {exc.msg}")
                conn.execute("update agent_provider_sessions set status = 'verification_failed', last_error = ?, finished_at = ? where id = ?", (exc.msg, now_iso(), session["id"]))
            continue
        with connection(root) as conn:
            issues = codex_report_issues(root, conn, expected_from_provider_session(session), result)
        if issues:
            with transaction(root, touched=[("agent_provider_session", session["id"]), ("finding", session["task_id"])]) as conn:
                record_integration_finding(conn, run_id, f"provider report rejected for {session['task_id']}: {'; '.join(issues[:5])}")
                conn.execute("update agent_provider_sessions set status = 'verification_failed', last_error = ?, finished_at = ? where id = ?", ("; ".join(issues[:5]), now_iso(), session["id"]))
                conn.execute("update dispatch_assignments set status = 'verification_failed', updated_at = ? where run_id = ? and task_id = ?", (now_iso(), run_id, session["task_id"]))
                conn.execute("update dispatch_runs set status = 'verification_failed', updated_at = ? where id = ?", (now_iso(), run_id))
                provider_event(conn, session, "collect_rejected", {"issues": issues[:5]})
            continue
        report_id = f"REPORT-{uuid.uuid4().hex[:12]}"
        attempt_id = f"ATTEMPT-{uuid.uuid4().hex[:12]}"
        head_commit = git_ref_commit(root, result["branch_name"])
        tree_sha = git_ref_tree(root, result["branch_name"])
        with transaction(root, touched=[("agent_provider_session", session["id"]), ("task_attempt", attempt_id)]) as conn:
            conn.execute(
                """
                insert into agent_reports
                (id, run_id, task_id, provider_session_id, job_id, status, last_error, result_json, created_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (report_id, run_id, session["task_id"], session["provider_session_id"], report.provider_job_id, report.status, report.last_error, report.result_json, now_iso()),
            )
            conn.execute(
                """
                insert into task_attempts
                (id, run_id, task_id, agent_id, fence, base_commit_sha, head_commit_sha, tree_sha,
                 branch_name, target_id, status, provider_session_id, agent_session_id, report_id, evidence_id, started_at, finished_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reported', ?, ?, ?, '', ?, '')
                """,
                (
                    attempt_id,
                    run_id,
                    session["task_id"],
                    session["agent_id"],
                    int(session["fence"]),
                    git_base_commit(root) or "",
                    head_commit,
                    tree_sha,
                    result["branch_name"],
                    result["target_id"],
                    session["provider_session_id"],
                    session["agent_session_id"],
                    report_id,
                    now_iso(),
                ),
            )
            conn.execute(
                "update agent_provider_sessions set status = 'reported', report_id = ?, attempt_id = ?, collected_at = ? where id = ?",
                (report_id, attempt_id, now_iso(), session["id"]),
            )
            conn.execute(
                "update dispatch_assignments set status = 'reported', provider_session_id = ?, updated_at = ? where run_id = ? and task_id = ?",
                (session["provider_session_id"], now_iso(), run_id, session["task_id"]),
            )
            conn.execute(
                """
                insert into dispatch_worktrees
                (id, run_id, task_id, agent_id, branch_name, worktree_path, status, created_at, cleaned_at)
                values (?, ?, ?, ?, ?, '', 'active', ?, '')
                """,
                (str(uuid.uuid4()), run_id, session["task_id"], session["agent_id"], result["branch_name"], now_iso()),
            )
            provider_event(conn, session, "collected", {"report_id": report_id, "attempt_id": attempt_id})
            emit_event(conn, "agent_provider_report_collected", payload(run_id=run_id, task_id=session["task_id"], provider=session["provider"], report_id=report_id, attempt_id=attempt_id))
        collected += 1
    if collected:
        with transaction(root, touched=[("dispatch_run", run_id)]) as conn:
            conn.execute("update dispatch_runs set status = 'reported', updated_at = ? where id = ?", (now_iso(), run_id))
    return collected


def dispatch_provider_cancel(root: Path, run_id: str, *, task_id: str = "", reason: str = "") -> int:
    from core.agent_provider import provider_for

    clauses = ["run_id = ?", "status in ('spawning', 'running', 'reported')"]
    params: list[str] = [run_id]
    if task_id:
        clauses.append("task_id = ?")
        params.append(task_id)
    with connection(root) as conn:
        sessions = conn.execute(f"select * from agent_provider_sessions where {' and '.join(clauses)}", tuple(params)).fetchall()
    cancelled = 0
    for session in sessions:
        provider = provider_for(session["provider"])
        provider.cancel(provider_handle_from_row(session), reason)
        with transaction(root, touched=[("agent_provider_session", session["id"])]) as conn:
            if reason:
                record_integration_finding(conn, run_id, f"provider session cancelled for {session['task_id']}: {reason}")
            conn.execute(
                "update agent_provider_sessions set status = 'cancelled', last_error = ?, cancelled_at = ?, finished_at = ? where id = ?",
                (reason, now_iso(), now_iso(), session["id"]),
            )
            if session["agent_session_id"]:
                conn.execute(
                    "update agent_sessions set status = 'cancelled', ended_at = ? where session_id = ?",
                    (now_iso(), session["agent_session_id"]),
                )
            conn.execute(
                """
                update dispatch_assignments
                set status = 'planned', agent_id = '', provider_session_id = '', heartbeat_at = null,
                    lease_expires_at = null, updated_at = ?
                where run_id = ? and task_id = ? and status != 'completed'
                """,
                (now_iso(), run_id, session["task_id"]),
            )
            provider_event(conn, session, "cancelled", {"reason": reason})
        cancelled += 1
    return cancelled


def dispatch_provider_reconcile(root: Path, run_id: str) -> int:
    with transaction(root, touched=[("dispatch_run", run_id)]) as conn:
        rows = conn.execute(
            """
            select * from agent_provider_sessions
            where run_id = ? and status in ('spawning', 'running') and lease_expires_at is not null
              and lease_expires_at != '' and lease_expires_at <= ?
            """,
            (run_id, now_iso()),
        ).fetchall()
        for row in rows:
            conn.execute(
                "update agent_provider_sessions set status = 'timed_out', last_error = 'provider session timed out', finished_at = ? where id = ?",
                (now_iso(), row["id"]),
            )
            if row["agent_session_id"]:
                conn.execute(
                    "update agent_sessions set status = 'timed_out', ended_at = ? where session_id = ?",
                    (now_iso(), row["agent_session_id"]),
                )
            conn.execute(
                """
                update dispatch_assignments
                set status = 'planned', agent_id = '', provider_session_id = '', heartbeat_at = null,
                    lease_expires_at = null, updated_at = ?
                where run_id = ? and task_id = ? and status != 'completed'
                """,
                (now_iso(), run_id, row["task_id"]),
            )
            provider_event(conn, row, "timed_out", {})
        if rows:
            emit_event(conn, "agent_provider_sessions_reconciled", payload(run_id=run_id, count=len(rows)))
        return len(rows)


def dispatch_provider_status(root: Path, run_id: str) -> list[str]:
    with connection(root) as conn:
        rows = conn.execute(
            """
            select task_id, provider, provider_session_id, agent_id, status, heartbeat_at, lease_expires_at, last_error
            from agent_provider_sessions where run_id = ? order by task_id, provider
            """,
            (run_id,),
        ).fetchall()
    lines = ["| Task | Provider | Session | Agent | Status | Heartbeat | Expires | Error |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    lines.extend(markdown_row([row["task_id"], row["provider"], row["provider_session_id"], row["agent_id"], row["status"], row["heartbeat_at"], row["lease_expires_at"], row["last_error"]]) for row in rows)
    return lines


def record_attempt_failure(conn: sqlite3.Connection, run_id: str, task_id: str, attempt_id: str, message: str) -> None:
    record_integration_finding(conn, run_id, f"dispatch attempt verification failed for {task_id}: {message}")
    conn.execute("update task_attempts set status = 'verification_failed', finished_at = ? where id = ?", (now_iso(), attempt_id))
    conn.execute("update dispatch_assignments set status = 'verification_failed', updated_at = ? where run_id = ? and task_id = ?", (now_iso(), run_id, task_id))
    conn.execute("update dispatch_runs set status = 'verification_failed', updated_at = ? where id = ?", (now_iso(), run_id))


def dispatch_verify_attempt(root: Path, run_id: str, task_id: str, *, runner: str = "local", container_image: str = "") -> str:
    from core.executor import LocalExecutor
    from core.agent_runner import RunnerRequest, runner_for

    if runner not in {"local", "container"}:
        raise HarnessError(f"unknown verification runner: {runner}")
    with connection(root) as conn:
        attempt = conn.execute(
            "select * from task_attempts where run_id = ? and task_id = ? order by started_at desc, id desc limit 1",
            (run_id, task_id),
        ).fetchone()
        if not attempt:
            raise HarnessError(f"missing task attempt: {run_id}/{task_id}")
        target = conn.execute("select * from test_targets where id = ?", (attempt["target_id"],)).fetchone()
        acceptance = conn.execute("select acceptance_id from task_acceptance where task_id = ? order by acceptance_id limit 1", (task_id,)).fetchone()
        task = conn.execute("select fence from tasks where id = ?", (task_id,)).fetchone()
        assignment = conn.execute("select provider_session_id from dispatch_assignments where run_id = ? and task_id = ?", (run_id, task_id)).fetchone()
        provider_session = None
        if attempt["provider_session_id"]:
            provider_session = conn.execute(
                "select * from agent_provider_sessions where provider_session_id = ? and run_id = ? and task_id = ?",
                (attempt["provider_session_id"], run_id, task_id),
            ).fetchone()
    if task and int(task["fence"]) != int(attempt["fence"]):
        raise HarnessError(f"fence-stale: {task_id} expected={attempt['fence']} actual={task['fence']}")
    if attempt["provider_session_id"]:
        if not provider_session:
            raise HarnessError(f"provider-session-stale: {attempt['provider_session_id']}")
        if provider_session["status"] in {"cancelled", "timed_out", "verification_failed"}:
            raise HarnessError(f"provider-session-stale: {attempt['provider_session_id']} status={provider_session['status']}")
        if assignment and assignment["provider_session_id"] and assignment["provider_session_id"] != attempt["provider_session_id"]:
            raise HarnessError(f"provider-session-stale: {attempt['provider_session_id']}")
    if not target:
        raise HarnessError(f"missing test target: {attempt['target_id']}")
    if not int(target["gateable"]):
        raise HarnessError(f"test target is not gateable: {attempt['target_id']}")

    worktree = ensure_verification_worktree(root, attempt["branch_name"], run_id, task_id)
    with connection(root) as conn:
        prefixes = executor_prefixes(conn)
    if runner == "container":
        image = resolve_container_image(root, container_image)
        try:
            runner_result = runner_for("container").run(
                RunnerRequest(
                    root=root,
                    work_dir=worktree,
                    command=target["command_template"],
                    target_id=target["id"],
                    target_command_template=target["command_template"],
                    allowed_prefixes=prefixes,
                    no_network=True,
                    sandbox_profile="no-network",
                    container_image=image,
                )
            )
        except RuntimeError as exc:
            if str(exc).startswith("sandbox-unavailable"):
                raise HarnessError(str(exc)) from exc
            raise
        result = runner_result.evidence
    else:
        executor = LocalExecutor(worktree)
        result = executor.run(
            target["command_template"],
            target_id=target["id"],
            target_command_template=target["command_template"],
            allowed_prefixes=prefixes,
        )
        runner_result = None
    if runner == "container" and result.sandbox_status == "available":
        verified_by = "controller-container"
    elif runner == "container":
        verified_by = "controller-container-unavailable"
    else:
        verified_by = "controller-local"
    head_commit = git_ref_commit(root, attempt["branch_name"])
    tree_sha = git_ref_tree(root, attempt["branch_name"])
    source_hash = source_tree_hash_for_mode(worktree, "auto")
    source_artifact = (root / result.artifact_path) if runner == "container" else (worktree / result.artifact_path)
    artifact = root / ".ai-team" / "runtime" / "executions" / uuid.uuid4().hex / "stdout.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(source_artifact.read_bytes())
    artifact_rel = artifact.relative_to(root).as_posix()
    artifact_hash = hashlib.sha256(artifact.read_bytes()).hexdigest()
    sandbox_execution_id = ""
    sandbox_engine = ""
    sandbox_image = ""
    if runner == "container":
        sandbox_execution_id = f"SANDBOX-{uuid.uuid4().hex[:12]}"
        sandbox_engine = str((runner_result.sandbox_execution or {}).get("engine", "")) if runner_result else ""
        sandbox_image = str((runner_result.sandbox_execution or {}).get("image", "")) if runner_result else ""
    issues: list[str] = []
    if result.exit_code != 0:
        issues.append(f"exit_code={result.exit_code}")
    if result.executed_count_source != "parsed":
        issues.append(f"executed_count_source={result.executed_count_source}")
    if result.executed_count <= 0:
        issues.append("executed_count must be > 0")
    if result.policy_status == "rejected":
        issues.append(f"policy rejected: {result.policy_reason}")
    if not source_hash:
        issues.append("source tree hash unavailable")
    if issues:
        with transaction(root, touched=[("task_attempt", attempt["id"]), ("finding", task_id)]) as conn:
            if sandbox_execution_id:
                conn.execute(
                    """
                    insert into sandbox_executions
                    (id, runner, engine, image, command, target_id, source_ref, tree_sha, network_mode,
                     timeout_seconds, resource_limits, exit_code, artifact_path, artifact_sha256,
                     sandbox_status, started_at, finished_at)
                    values (?, ?, ?, ?, ?, ?, ?, ?, 'none', ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sandbox_execution_id,
                        runner,
                        sandbox_engine,
                        sandbox_image,
                        result.command,
                        target["id"],
                        attempt["branch_name"],
                        tree_sha,
                        120,
                        stable_json({"cpus": "1", "memory": "512m", "pids_limit": "256"}),
                        result.exit_code,
                        artifact_rel,
                        artifact_hash,
                        result.sandbox_status,
                        now_iso(),
                        now_iso(),
                    ),
                )
            record_attempt_failure(conn, run_id, task_id, attempt["id"], "; ".join(issues))
        raise HarnessError(f"dispatch attempt verification failed: {'; '.join(issues)}")

    evidence_id = f"CODEX-{uuid.uuid4().hex[:12]}"
    validation_id = f"CODEX-VAL-{uuid.uuid4().hex[:8]}"
    acceptance_id = acceptance["acceptance_id"] if acceptance else ""
    with transaction(root, touched=[("task_attempt", attempt["id"]), ("evidence", evidence_id), ("validation", validation_id), ("task", task_id)]) as conn:
        conn.execute(
            """
            insert into evidence
            (id, kind, summary, uri, hash, command, exit_code, stdout_sha256, artifact_path, source_tree_hash,
             target_id, executed_count, executed_count_source, allow_unlisted, no_network, policy_status, policy_reason,
             sandbox_profile, sandbox_status, sandbox_execution_id, sandbox_engine, container_image, allow_unlisted_reason, trust_anchor, trust_anchor_id,
             attempt_id, tree_sha, code_ref, verified_by, created_at)
            values (?, 'command', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, '', 'local-only', '',
                    ?, ?, ?, ?, ?)
            """,
            (
                evidence_id,
                f"controller verified {task_id} command exit {result.exit_code}",
                f"local://{artifact_rel}",
                artifact_hash,
                result.command,
                result.exit_code,
                artifact_hash,
                artifact_rel,
                source_hash,
                target["id"],
                result.executed_count,
                result.executed_count_source,
                bool_int(runner == "container"),
                result.policy_status,
                result.policy_reason,
                result.sandbox_profile,
                result.sandbox_status,
                sandbox_execution_id,
                sandbox_engine,
                sandbox_image,
                attempt["id"],
                tree_sha,
                attempt["branch_name"],
                verified_by,
                now_iso(),
            ),
        )
        project_revision = int(project_row(conn)["revision"])
        conn.execute(
            """
            insert into validations
            (id, surface, acceptance_id, commands, command, exit_code, stdout_sha256, artifact_path,
             target_id, executed_count, executed_count_source, allow_unlisted, no_network, policy_status,
             policy_reason, sandbox_profile, sandbox_status, sandbox_execution_id, sandbox_engine, container_image, allow_unlisted_reason, trust_anchor, trust_anchor_id,
             findings, result, residual_risk, head_commit, source_tree_hash, attempt_id, tree_sha, code_ref,
             verified_by, tracked_diff_hash, project_revision, created_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, '', 'local-only', '',
                    'controller verification passed', 'pass', '', ?, ?, ?, ?, ?, ?, '', ?, ?)
            """,
            (
                validation_id,
                f"dispatch {task_id}",
                acceptance_id,
                result.command,
                result.command,
                result.exit_code,
                artifact_hash,
                artifact_rel,
                target["id"],
                result.executed_count,
                result.executed_count_source,
                bool_int(runner == "container"),
                result.policy_status,
                result.policy_reason,
                result.sandbox_profile,
                result.sandbox_status,
                sandbox_execution_id,
                sandbox_engine,
                sandbox_image,
                head_commit,
                source_hash,
                attempt["id"],
                tree_sha,
                attempt["branch_name"],
                verified_by,
                project_revision,
                now_iso(),
            ),
        )
        conn.execute("insert into validation_evidence (validation_id, evidence_id) values (?, ?)", (validation_id, evidence_id))
        if sandbox_execution_id:
            conn.execute(
                """
                insert into sandbox_executions
                (id, runner, engine, image, command, target_id, source_ref, tree_sha, network_mode,
                 timeout_seconds, resource_limits, exit_code, artifact_path, artifact_sha256,
                 sandbox_status, started_at, finished_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, 'none', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sandbox_execution_id,
                    runner,
                    sandbox_engine,
                    sandbox_image,
                    result.command,
                    target["id"],
                    attempt["branch_name"],
                    tree_sha,
                    120,
                    stable_json({"cpus": "1", "memory": "512m", "pids_limit": "256"}),
                    result.exit_code,
                    artifact_rel,
                    artifact_hash,
                    result.sandbox_status,
                    now_iso(),
                    now_iso(),
                ),
            )
        conn.execute(
            "update task_attempts set status = 'verified', evidence_id = ?, head_commit_sha = ?, tree_sha = ?, finished_at = ? where id = ?",
            (evidence_id, head_commit, tree_sha, now_iso(), attempt["id"]),
        )
        if attempt["provider_session_id"]:
            conn.execute(
                "update agent_provider_sessions set status = 'verified', attempt_id = ?, finished_at = ? where provider_session_id = ? and run_id = ? and task_id = ?",
                (attempt["id"], now_iso(), attempt["provider_session_id"], run_id, task_id),
            )
        if attempt["agent_session_id"]:
            conn.execute(
                "update agent_sessions set status = 'verified' where session_id = ?",
                (attempt["agent_session_id"],),
            )
        conn.execute(
            "update dispatch_assignments set status = 'completed', evidence = ?, updated_at = ? where run_id = ? and task_id = ?",
            (evidence_id, now_iso(), run_id, task_id),
        )
        conn.execute("update dispatch_runs set status = 'completed', updated_at = ? where id = ?", (now_iso(), run_id))
        task = conn.execute("select status from tasks where id = ?", (task_id,)).fetchone()
        if task and task["status"] in {"ready", "claimed", "in_progress"}:
            conn.execute(
                """
                update tasks set status = 'submitted', evidence = ?, submitted_by = ?, submitted_session_id = ?, lease_agent = null,
                  lease_token = null, lease_heartbeat_at = null, lease_expires_at = null,
                  revision = revision + 1, updated_at = ? where id = ?
                """,
                (evidence_id, attempt["agent_id"], attempt["agent_session_id"], now_iso(), task_id),
            )
        emit_event(conn, "dispatch_attempt_verified", payload(run_id=run_id, task_id=task_id, attempt_id=attempt["id"], evidence_id=evidence_id, verified_by=verified_by))
    render_all(root)
    return evidence_id


def dispatch_status(root: Path) -> list[str]:
    with connection(root) as conn:
        rows = conn.execute(
            """
            select dr.id as run_id, dr.scope, da.task_id, da.agent_id, da.capability, da.status
            from dispatch_runs dr
            left join dispatch_assignments da on da.run_id = dr.id
            order by dr.created_at, da.task_id
            """
        ).fetchall()
    lines = ["| Run | Scope | Task | Agent | Capability | Status |", "| --- | --- | --- | --- | --- | --- |"]
    lines.extend(markdown_row([row["run_id"], row["scope"], row["task_id"] or "", row["agent_id"] or "", row["capability"] or "", row["status"] or ""]) for row in rows)
    return lines


def migrate(root: Path, from_version: str, to_version: int, *, dry_run: bool = False) -> dict[str, Any] | None:
    if from_version == "markdown-v1":
        return migrate_markdown_v1(root, dry_run=dry_run)
    if dry_run:
        return {
            "dry_run": True,
            "imported": {"schema_migration": 1},
            "skipped": {},
            "unrecognized": [],
        }
    backup_dir = backup_runtime(root, "migrate")
    from_version_int = int(from_version)
    try:
        with transaction(root, validate_invariants=False) as conn:
            create_schema(conn)
            initialize_project(conn)
            conn.execute(
                "insert into migrations (from_version, to_version, applied_at) values (?, ?, ?)",
                (from_version_int, to_version, now_iso()),
            )
            conn.execute("update project set schema_version = ?, runtime_version = ?, revision = revision + 1, updated_at = ? where id = 1", (to_version, RUNTIME_VERSION, now_iso()))
            emit_event(conn, "migration_applied", payload(**{"from": from_version_int, "to": to_version}))
            require_full_invariants(conn, root, "migration")
    except Exception:
        restore_runtime_backup(root, backup_dir)
        raise
    render_all(root)
    return None


def doctor(root: Path) -> list[str]:
    issues: list[str] = []
    issues.extend(gitignore_runtime_issues(root))
    path = db_file(root)
    if not path.exists():
        return ["missing sqlite state: .ai-team/state/harness.db"]
    with connection(root) as conn:
        try:
            project = project_row(conn)
        except HarnessError as exc:
            issues.append(str(exc))
        else:
            if int(project["schema_version"]) != SCHEMA_VERSION:
                issues.append(f"schema version mismatch: expected {SCHEMA_VERSION}, actual {project['schema_version']}")
            if project["runtime_version"] != RUNTIME_VERSION:
                issues.append(f"runtime version mismatch: expected {RUNTIME_VERSION}, actual {project['runtime_version']}")
        integrity = conn.execute("pragma integrity_check").fetchone()[0]
        if integrity != "ok":
            issues.append(f"sqlite integrity check failed: {integrity}")
        foreign_key_errors = conn.execute("pragma foreign_key_check").fetchall()
        if foreign_key_errors:
            issues.append(f"sqlite foreign key check failed: {len(foreign_key_errors)} issue(s)")
        issues.extend(runtime_schema_issues(conn))
        from core.invariant_checker import check_runtime_invariants

        issues.extend(check_runtime_invariants(conn, root))
        for relpath in [
            ".ai-team/control/project-state.yaml",
            ".ai-team/planning/task-board.md",
            ".ai-team/requirements/acceptance.md",
            ".ai-team/requirements/failure-modes.md",
            "docs/harness/quality-gates.md",
        ]:
            if not (root / relpath).exists():
                issues.append(f"missing view: {relpath}")
    return issues


def runtime_schema_issues(conn: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    enum_checks = [
        ("tasks", "status", TASK_STATUSES, "task status"),
        ("failure_modes", "risk", {"low", "medium", "high", "critical"}, "failure mode risk"),
        ("failure_modes", "status", FAILURE_MODE_STATUSES, "failure mode status"),
        ("validations", "result", {"pass", "fail", "blocked", "partial"}, "validation result"),
        ("quality_gates", "reviewer_context", {"fresh", "same-context-degraded", "external"}, "quality gate reviewer context"),
        ("quality_gates", "result", {"pass", "fail", "conditional", "blocked"}, "quality gate result"),
        ("adapters", "mode", ADAPTER_MODES, "adapter mode"),
        ("adapter_actions", "mode", ADAPTER_MODES, "adapter action mode"),
        ("adapter_actions", "status", ADAPTER_ACTION_STATUSES, "adapter action status"),
        ("agents", "status", {"available", "leased", "disabled"}, "agent status"),
        ("dispatch_runs", "status", DISPATCH_STATUSES, "dispatch run status"),
        ("dispatch_assignments", "status", DISPATCH_STATUSES, "dispatch assignment status"),
        ("test_targets", "kind", TEST_TARGET_KINDS, "test target kind"),
        ("validations", "trust_anchor", {"local-only", "human-confirmed", "external-session", "ci"}, "validation trust anchor"),
        ("evidence", "trust_anchor", {"local-only", "human-confirmed", "external-session", "ci"}, "evidence trust anchor"),
        ("validations", "sandbox_profile", {"none", "no-network"}, "validation sandbox profile"),
        ("evidence", "sandbox_profile", {"none", "no-network"}, "evidence sandbox profile"),
        ("ci_verifications", "conclusion", CI_CONCLUSIONS, "ci conclusion"),
        ("ci_verifications", "origin", ANCHOR_ORIGINS, "ci origin"),
        ("external_session_verifications", "conclusion", EXTERNAL_SESSION_CONCLUSIONS, "external session conclusion"),
        ("external_session_verifications", "origin", ANCHOR_ORIGINS, "external session origin"),
        ("agent_sessions", "role", SESSION_ROLES, "agent session role"),
        ("agent_sessions", "origin", ANCHOR_ORIGINS, "agent session origin"),
        ("agent_sessions", "trust_level", SESSION_TRUST_LEVELS, "agent session trust level"),
        ("agent_sessions", "status", {"active", "running", "reported", "verified", "closed", "cancelled", "timed_out", "verification_failed"}, "agent session status"),
        ("session_attestations", "role", SESSION_ROLES, "session attestation role"),
        ("session_attestations", "origin", ANCHOR_ORIGINS, "session attestation origin"),
        ("session_attestations", "trust_level", SESSION_TRUST_LEVELS, "session attestation trust level"),
        ("sandbox_executions", "sandbox_status", {"", "available", "unavailable"}, "sandbox execution status"),
        ("integration_attempts", "status", {"running", "integrated", "integration_conflict", "verification_failed", "integration_unverified_branch", "integration_branch_drift", "file_claim_violation"}, "integration attempt status"),
    ]
    for table, column, allowed, label in enum_checks:
        if table == "dispatch_assignments":
            id_column = "task_id"
        elif table == "agent_sessions":
            id_column = "session_id"
        else:
            id_column = "id"
        for row in conn.execute(f"select {id_column} as id, {column} as value from {table} where {column} not in ({','.join('?' for _ in allowed)})", tuple(allowed)):
            issues.append(f"invalid {label}: {table}.{row['id']}={row['value']}")
    for row in conn.execute("select id, payload_json from events"):
        try:
            json.loads(row["payload_json"])
        except json.JSONDecodeError as exc:
            issues.append(f"invalid event payload_json: {row['id']} {exc.msg}")
    issues.extend(schema_contract_issues(conn))
    return issues


def schema_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "schemas"


def load_schema(name: str) -> dict[str, Any]:
    return json.loads((schema_dir() / name).read_text(encoding="utf-8"))


def json_type_matches(value: Any, expected: str | list[str]) -> bool:
    options = expected if isinstance(expected, list) else [expected]
    for option in options:
        if option == "null" and value is None:
            return True
        if option == "string" and isinstance(value, str):
            return True
        if option == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if option == "array" and isinstance(value, list):
            return True
        if option == "object" and isinstance(value, dict):
            return True
        if option == "boolean" and isinstance(value, bool):
            return True
    return False


def validate_object_against_schema(label: str, data: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    for field in schema.get("required", []):
        if field not in data or data[field] is None:
            issues.append(f"schema contract failed: {label}.{field} is required")
    properties = schema.get("properties", {})
    if schema.get("additionalProperties") is False:
        for field in data:
            if field not in properties:
                issues.append(f"schema contract failed: {label}.{field} is not declared")
    for field, definition in properties.items():
        if field not in data:
            continue
        value = data[field]
        expected_type = definition.get("type")
        if expected_type is not None and not json_type_matches(value, expected_type):
            issues.append(f"schema contract failed: {label}.{field} expected {expected_type}, got {type(value).__name__}")
            continue
        if "enum" in definition and value not in definition["enum"]:
            issues.append(f"schema contract failed: {label}.{field}={value} not in {definition['enum']}")
        if definition.get("type") == "array" and isinstance(value, list):
            item_type = definition.get("items", {}).get("type")
            if item_type:
                for index, item in enumerate(value):
                    if not json_type_matches(item, item_type):
                        issues.append(f"schema contract failed: {label}.{field}[{index}] expected {item_type}")
    return issues


def schema_entity_rows(conn: sqlite3.Connection) -> list[tuple[str, str, list[dict[str, Any]]]]:
    tasks = []
    task_acceptance = grouped(conn, "task_acceptance", "task_id", "acceptance_id")
    task_failure_modes = grouped(conn, "task_failure_modes", "task_id", "failure_mode_id")
    task_dependencies = grouped(conn, "task_dependencies", "task_id", "depends_on")
    for row in conn.execute("select * from tasks order by id"):
        data = row_snapshot(row) or {}
        data["acceptance_ids"] = parse_ids(task_acceptance.get(row["id"], ""))
        data["failure_mode_ids"] = parse_ids(task_failure_modes.get(row["id"], ""))
        data["dependencies"] = parse_ids(task_dependencies.get(row["id"], ""))
        tasks.append(data)

    validations = []
    validation_failure_modes = grouped(conn, "validation_failure_modes", "validation_id", "failure_mode_id")
    for row in conn.execute("select * from validations order by id"):
        data = row_snapshot(row) or {}
        data["failure_mode_ids"] = parse_ids(validation_failure_modes.get(row["id"], ""))
        validations.append(data)

    project = conn.execute(
        """
        select status, phase, scope_status, current_owner, schema_version, runtime_version, project_id, revision, updated_at
        from project where id = 1
        """
    ).fetchall()

    return [
        ("project-state.schema.json", "project", [row_snapshot(row) or {} for row in project]),
        ("acceptance.schema.json", "acceptance", [row_snapshot(row) or {} for row in conn.execute("select * from acceptance")]),
        ("requirement.schema.json", "requirements", [row_snapshot(row) or {} for row in conn.execute("select * from requirements")]),
        ("failure-mode.schema.json", "failure_modes", [row_snapshot(row) or {} for row in conn.execute("select * from failure_modes")]),
        ("task.schema.json", "tasks", tasks),
        ("task-test-target.schema.json", "task_test_targets", [row_snapshot(row) or {} for row in conn.execute("select * from task_test_targets")]),
        ("task-attempt.schema.json", "task_attempts", [row_snapshot(row) or {} for row in conn.execute("select * from task_attempts")]),
        ("validation.schema.json", "validations", validations),
        ("test-target.schema.json", "test_targets", [row_snapshot(row) or {} for row in conn.execute("select * from test_targets")]),
        ("quality-gate.schema.json", "quality_gates", [row_snapshot(row) or {} for row in conn.execute("select * from quality_gates")]),
        ("delivery.schema.json", "deliveries", [row_snapshot(row) or {} for row in conn.execute("select * from deliveries")]),
        ("evidence.schema.json", "evidence", [row_snapshot(row) or {} for row in conn.execute("select * from evidence")]),
        ("test.schema.json", "tests", [row_snapshot(row) or {} for row in conn.execute("select * from tests")]),
        ("finding.schema.json", "findings", [row_snapshot(row) or {} for row in conn.execute("select * from findings")]),
        ("adapter.schema.json", "adapters", [row_snapshot(row) or {} for row in conn.execute("select * from adapters")]),
        ("adapter-action.schema.json", "adapter_actions", [row_snapshot(row) or {} for row in conn.execute("select * from adapter_actions")]),
        ("ci-verification.schema.json", "ci_verifications", [row_snapshot(row) or {} for row in conn.execute("select * from ci_verifications")]),
        ("command-log.schema.json", "command_log", [row_snapshot(row) or {} for row in conn.execute("select * from command_log")]),
        ("external-session-verification.schema.json", "external_session_verifications", [row_snapshot(row) or {} for row in conn.execute("select * from external_session_verifications")]),
        ("agent.schema.json", "agents", [row_snapshot(row) or {} for row in conn.execute("select * from agents")]),
        ("agent-session.schema.json", "agent_sessions", [row_snapshot(row) or {} for row in conn.execute("select * from agent_sessions")]),
        ("session-attestation.schema.json", "session_attestations", [row_snapshot(row) or {} for row in conn.execute("select * from session_attestations")]),
        ("baseline.schema.json", "baselines", [row_snapshot(row) or {} for row in conn.execute("select * from baselines")]),
        ("dispatch-run.schema.json", "dispatch_runs", [row_snapshot(row) or {} for row in conn.execute("select * from dispatch_runs")]),
        ("dispatch-assignment.schema.json", "dispatch_assignments", [row_snapshot(row) or {} for row in conn.execute("select * from dispatch_assignments")]),
        ("dispatch-worktree.schema.json", "dispatch_worktrees", [row_snapshot(row) or {} for row in conn.execute("select * from dispatch_worktrees")]),
        ("task-file-claim.schema.json", "task_file_claims", [row_snapshot(row) or {} for row in conn.execute("select * from task_file_claims")]),
        ("agent-report.schema.json", "agent_reports", [row_snapshot(row) or {} for row in conn.execute("select * from agent_reports")]),
        ("agent-provider-session.schema.json", "agent_provider_sessions", [row_snapshot(row) or {} for row in conn.execute("select * from agent_provider_sessions")]),
        ("agent-provider-event.schema.json", "agent_provider_events", [row_snapshot(row) or {} for row in conn.execute("select * from agent_provider_events")]),
        ("sandbox-execution.schema.json", "sandbox_executions", [row_snapshot(row) or {} for row in conn.execute("select * from sandbox_executions")]),
        ("integration-attempt.schema.json", "integration_attempts", [row_snapshot(row) or {} for row in conn.execute("select * from integration_attempts")]),
        ("codex-fanout-export.schema.json", "codex_fanout_exports", [row_snapshot(row) or {} for row in conn.execute("select * from codex_fanout_exports")]),
        ("runtime-snapshot.schema.json", "runtime_snapshots", [row_snapshot(row) or {} for row in conn.execute("select * from runtime_snapshots")]),
        ("invalidation.schema.json", "invalidations", [row_snapshot(row) or {} for row in conn.execute("select * from invalidations")]),
        ("event.schema.json", "events", [row_snapshot(row) or {} for row in conn.execute("select * from events")]),
    ]


def schema_contract_issues(conn: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    for schema_name, entity, rows in schema_entity_rows(conn):
        schema = load_schema(schema_name)
        for row in rows:
            label = f"{entity}.{row.get('id', row.get('sequence', 'row'))}"
            issues.extend(validate_object_against_schema(label, row, schema))
    return issues


def trace_rows(conn: sqlite3.Connection, requirement_id: str | None = None) -> list[dict[str, str]]:
    clauses = ["r.status != 'cancelled'"]
    values: list[object] = []
    if requirement_id:
        clauses.append("r.id = ?")
        values.append(requirement_id)
    rows = conn.execute(
        f"""
        select
          r.id as requirement_id,
          r.kind as requirement_kind,
          r.body as requirement_body,
          ra.acceptance_id,
          a.criterion as acceptance_criterion,
          group_concat(distinct ta.task_id) as task_ids,
          group_concat(distinct v.id) as validation_ids,
          group_concat(distinct vfm.failure_mode_id) as failure_mode_ids,
          group_concat(distinct da.delivery_id) as delivery_ids
        from requirements r
        left join requirement_acceptance ra on ra.requirement_id = r.id
        left join acceptance a on a.id = ra.acceptance_id
        left join task_acceptance ta on ta.acceptance_id = ra.acceptance_id
        left join validations v on v.acceptance_id = ra.acceptance_id and v.result = 'pass'
        left join validation_failure_modes vfm on vfm.validation_id = v.id
        left join delivery_acceptance da on da.acceptance_id = ra.acceptance_id
        where {' and '.join(clauses)}
        group by r.id, ra.acceptance_id
        order by r.id, ra.acceptance_id
        """,
        values,
    ).fetchall()
    return [{key: row[key] or "" for key in row.keys()} for row in rows]


def traceability_issues(conn: sqlite3.Connection, requirement_id: str | None = None) -> list[str]:
    issues: list[str] = []
    req_clause = "where status != 'cancelled'"
    values: list[object] = []
    if requirement_id:
        req_clause += " and id = ?"
        values.append(requirement_id)
    requirements = conn.execute(f"select id from requirements {req_clause} order by id", values).fetchall()
    for requirement in requirements:
        links = conn.execute(
            "select acceptance_id from requirement_acceptance where requirement_id = ? order by acceptance_id",
            (requirement["id"],),
        ).fetchall()
        if not links:
            issues.append(f"requirement has no acceptance link: {requirement['id']}")
            continue
        for link in links:
            acceptance_id = link["acceptance_id"]
            accepted_task = conn.execute(
                """
                select 1 from task_acceptance ta
                join tasks t on t.id = ta.task_id
                where ta.acceptance_id = ? and t.status in ('accepted', 'cancelled', 'skipped')
                limit 1
                """,
                (acceptance_id,),
            ).fetchone()
            if not accepted_task:
                issues.append(f"acceptance has no completed task in trace: {requirement['id']} -> {acceptance_id}")
            passing_validation = conn.execute(
                "select 1 from validations where acceptance_id = ? and result = 'pass' limit 1",
                (acceptance_id,),
            ).fetchone()
            if not passing_validation:
                issues.append(f"acceptance has no passing validation in trace: {requirement['id']} -> {acceptance_id}")
    return issues


def trace_show(root: Path, requirement_id: str | None = None) -> list[str]:
    with connection(root) as conn:
        rows = trace_rows(conn, requirement_id)
        issues = traceability_issues(conn, requirement_id)
    lines = ["# Traceability", "", "| Requirement | Kind | Acceptance | Tasks | Validations | Failure Modes | Deliveries |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for row in rows:
        lines.append(
            markdown_row(
                [
                    row["requirement_id"],
                    row["requirement_kind"],
                    row["acceptance_id"],
                    row["task_ids"],
                    row["validation_ids"],
                    row["failure_mode_ids"],
                    row["delivery_ids"],
                ]
            )
        )
    lines.extend(["", "## Issues", ""])
    lines.extend(f"- {issue}" for issue in issues) if issues else lines.append("- none")
    return lines


def trace_validate(root: Path) -> list[str]:
    with connection(root) as conn:
        return traceability_issues(conn)


def validate_delivery(conn: sqlite3.Connection, root: Path, *, require_phase: bool = False) -> list[str]:
    from core.gate_engine import evaluate_delivery_readiness

    return evaluate_delivery_readiness(conn, root)


def validate_runtime(root: Path, *, delivery: bool = False) -> list[str]:
    issues = doctor(root)
    if issues:
        return issues
    with connection(root) as conn:
        project = project_row(conn)
        if delivery or project["phase"] in {"delivery_readiness", "retrospective"}:
            issues.extend(validate_delivery(conn, root))
    return issues


def status_lines(root: Path) -> list[str]:
    with connection(root) as conn:
        row = project_row(conn)
        task_count = conn.execute("select count(*) from tasks").fetchone()[0]
        ready_count = conn.execute("select count(*) from tasks where status = 'ready'").fetchone()[0]
        event_count = conn.execute("select count(*) from events").fetchone()[0]
    return [
        "# Harness Status",
        f"status: {row['status']}",
        f"phase: {row['phase']}",
        f"scope_status: {row['scope_status']}",
        f"current_owner: {row['current_owner']}",
        f"schema_version: {row['schema_version']}",
        f"runtime_version: {row['runtime_version']}",
        f"revision: {row['revision']}",
        f"tasks: {task_count}",
        f"ready_tasks: {ready_count}",
        f"events: {event_count}",
    ]


def repair(root: Path, *, dry_run: bool = False, clear_invariant: str = "", confirm: str = "") -> list[str]:
    if clear_invariant:
        supported = {"expired-lease", "producer-self-accepted"}
        plan = [f"clear invariant: {clear_invariant}"]
        if clear_invariant not in supported:
            return [f"unsupported invariant repair code: {clear_invariant}"]
        if dry_run:
            return [f"repair action: {item}" for item in plan]
        if confirm != clear_invariant:
            return [f"repair requires --confirm {clear_invariant}", *[f"repair action: {item}" for item in plan]]
        backup_runtime(root, f"repair-{clear_invariant}")
        with transaction(root, validate_invariants=False) as conn:
            if clear_invariant == "expired-lease":
                rows = conn.execute(
                    """
                    select id, lease_agent from tasks
                    where lease_expires_at is not null and lease_expires_at <= ? and lease_agent is not null
                    order by id
                    """,
                    (now_iso(),),
                ).fetchall()
                for row in rows:
                    conn.execute(
                        """
                        update tasks set lease_agent = null, lease_token = null, lease_heartbeat_at = null,
                          lease_expires_at = null, revision = revision + 1, updated_at = ? where id = ?
                        """,
                        (now_iso(), row["id"]),
                    )
                    if row["lease_agent"]:
                        conn.execute(
                            "update agents set lease_task_id = '', status = 'available', updated_at = ? where id = ?",
                            (now_iso(), row["lease_agent"]),
                        )
            elif clear_invariant == "producer-self-accepted":
                conn.execute(
                    """
                    update tasks
                    set status = 'review', accepted_by = '', revision = revision + 1, updated_at = ?
                    where status = 'accepted' and accepted_by != '' and accepted_by = owner
                    """,
                    (now_iso(),),
                )
            emit_event(conn, "invariant_repaired", payload(code=clear_invariant))
        render_all(root)
        return []
    if dry_run:
        issues = doctor(root)
        plan = [
            "ensure runtime .gitignore patterns",
            "initialize missing sqlite state",
            f"migrate schema to {SCHEMA_VERSION}",
            "render generated harness views",
        ]
        return issues + [f"repair action: {item}" for item in plan]
    backup_runtime(root, "repair")
    init_runtime(root)
    migrate(root, str(SCHEMA_VERSION), SCHEMA_VERSION)
    render_all(root)
    return []


def render_all(root: Path) -> None:
    from core.projections import render_all as core_render_all

    core_render_all(root)


def render_project_state(root: Path) -> None:
    from core.projections import render_project_state as core_render_project_state

    core_render_project_state(root)


def write_view(root: Path, relpath: str, content: str) -> None:
    from core.projections import write_view as core_write_view

    core_write_view(root, relpath, content)


def render_requirements(root: Path) -> None:
    from core.projections import render_requirements as core_render_requirements

    core_render_requirements(root)


def render_traceability(root: Path) -> None:
    from core.projections import render_traceability as core_render_traceability

    core_render_traceability(root)


def render_acceptance(root: Path) -> None:
    from core.projections import render_acceptance as core_render_acceptance

    core_render_acceptance(root)


def render_failure_modes(root: Path) -> None:
    from core.projections import render_failure_modes as core_render_failure_modes

    core_render_failure_modes(root)


def render_tasks(root: Path) -> None:
    from core.projections import render_tasks as core_render_tasks

    core_render_tasks(root)


def grouped(conn: sqlite3.Connection, table: str, key: str, value: str) -> dict[str, str]:
    return {
        row[key]: row["ids"]
        for row in conn.execute(f"select {key}, group_concat({value}, ', ') as ids from {table} group by {key}")
    }


def render_validation(root: Path) -> None:
    from core.projections import render_validation as core_render_validation

    core_render_validation(root)


def render_evidence(root: Path) -> None:
    from core.projections import render_evidence as core_render_evidence

    core_render_evidence(root)


def render_findings(root: Path) -> None:
    from core.projections import render_findings as core_render_findings

    core_render_findings(root)


def render_gates(root: Path) -> None:
    from core.projections import render_gates as core_render_gates

    core_render_gates(root)


def render_deliveries(root: Path) -> None:
    from core.projections import render_deliveries as core_render_deliveries

    core_render_deliveries(root)


def render_decisions(root: Path) -> None:
    from core.projections import render_decisions as core_render_decisions

    core_render_decisions(root)


def render_tooling_map(root: Path) -> None:
    from core.projections import render_tooling_map as core_render_tooling_map

    core_render_tooling_map(root)

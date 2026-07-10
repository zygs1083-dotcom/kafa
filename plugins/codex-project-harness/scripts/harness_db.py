#!/usr/bin/env python3
"""SQLite-backed runtime for Codex Project Harness."""

from __future__ import annotations

import json
import contextvars
import csv
import hashlib
import http.client
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import time
from typing import Any, Callable, Iterator

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from harness_lib import content_source_tree_hash, ensure_parent, git_base_commit, git_dirty, git_head_sha, git_source_tree_hash, git_tracked_diff_hash, markdown_row, now_iso, source_tree_hash_for_mode
from core.connector_trust import (
    ConnectorTrustError,
    agent_session_payload,
    ci_payload,
    configured_key_path,
    external_session_payload,
    prepare_connector_record,
    verify_connector_record,
)
from core.schema_guard import (
    ADAPTER_MODES,
    ANCHOR_ORIGINS,
    CI_CONCLUSIONS,
    EXECUTED_COUNT_SOURCES,
    EXTERNAL_SESSION_CONCLUSIONS,
    FAILURE_MODE_STATUSES,
    RESULT_FORMATS,
    SANDBOX_PROFILES,
    SANDBOX_STATUSES,
    SEMANTIC_STATUSES,
    STACK_PROFILES,
    TASK_STATUSES,
    TEST_TARGET_KINDS,
    adapter_action_payload_hash,
)
from core.store import DB_PATH, SqliteStore, Store


SCHEMA_VERSION = 29
RUNTIME_VERSION = "4.18.0"
MIN_MIGRATABLE_SCHEMA_VERSION = 6
REGISTERED_SCHEMA_SOURCES = frozenset(range(MIN_MIGRATABLE_SCHEMA_VERSION, SCHEMA_VERSION))
DEFAULT_CYCLE_ID = "CYCLE-current"
LEGACY_CYCLE_ID = "CYCLE-legacy"
LEASE_TTL_SECONDS = 3600
DEFAULT_CONTAINER_IMAGE = "python:3.12-slim"
STACK_PROFILE_IMAGES = {
    "python": DEFAULT_CONTAINER_IMAGE,
    "node": "node:22-bookworm-slim",
    "go": "golang:1.23-bookworm",
    "rust": "rust:1.83-bookworm",
    "java": "eclipse-temurin:21-jdk",
    "browser-e2e": "mcr.microsoft.com/playwright:v1.49.0-noble",
    "data-integration": DEFAULT_CONTAINER_IMAGE,
}
RUNTIME_GITIGNORE_PATTERNS = [
    ".ai-team/state/",
    ".ai-team/backups/",
    ".ai-team/runtime/",
    "__pycache__/",
    "*.pyc",
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

ADAPTER_ACTION_STATUSES = {"planned", "draft", "confirmed", "executing", "completed", "retryable_failed", "unknown", "blocked"}
CONNECTOR_STATUSES = {"available", "degraded", "blocked"}
CONNECTOR_PROFILE_STATUSES = {"bound", "disabled"}
ADVISORY_FALLBACK_STATUSES = {"generated", "superseded", "stale"}
DELIVERY_CYCLE_STATUSES = {"active", "delivered", "archived"}
VALIDATION_STATUSES = {"active", "superseded", "invalidated"}
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
    "delivery_cycles",
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
    "connector_budgets",
    "advisory_fallbacks",
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


class _MigrationAlreadyApplied(Exception):
    """Internal control flow for idempotent post-migration recovery."""


@dataclass
class ConnectorStats:
    tool: str
    operation: str
    scope_key: str
    status: str = "available"
    attempt_count: int = 0
    retry_after_at: str = ""
    rate_limit_remaining: int | None = None
    rate_limit_reset_at: str = ""
    last_status_code: int | None = None
    last_error: str = ""
    free_plan_risk: str = ""


class ConnectorFailure(HarnessError):
    """Connector failure with budget/retry metadata."""

    def __init__(self, message: str, stats: ConnectorStats, *, ambiguous: bool = False) -> None:
        super().__init__(message)
        self.stats = stats
        self.ambiguous = ambiguous


@dataclass
class ConnectorOutcome:
    external_id: str
    external_link: str
    stats: ConnectorStats


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


def runtime_initialized(root: Path) -> bool:
    store = get_store(root)
    if isinstance(store, SqliteStore) and not db_file(root).exists():
        return False
    try:
        with store.connection() as conn:
            exists = conn.execute("select 1 from sqlite_master where type='table' and name = 'project'").fetchone()
            if not exists:
                return False
            return conn.execute("select 1 from project where id = 1").fetchone() is not None
    except sqlite3.Error:
        return False


def uninitialized_lines(root: Path) -> list[str]:
    harness_py = Path(__file__).resolve().with_name("harness.py")
    return [
        f"ERROR: harness is not initialized in this project: {root}",
        f"NEXT: python3 {harness_py} --root {root} init",
        f"NEXT: python3 {harness_py} --root {root} quickstart status",
    ]


def require_initialized(root: Path) -> None:
    if not runtime_initialized(root):
        raise HarnessError("\n".join(uninitialized_lines(root)))


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
    replay_before: dict[str, list[dict[str, Any]]] = {}
    event_sequence_before = 0

    def before_commit(conn: sqlite3.Connection) -> None:
        insert_active_command_log(conn)
        attach_transaction_replay_mutations(conn, replay_before, event_sequence_before)
        if validate_invariants:
            issues = transaction_invariant_issues(conn, root, touched)
            if issues:
                raise HarnessError("; ".join(str(issue) for issue in issues))

    with get_store(root).transaction(before_commit=before_commit) as conn:
        replay_before = replay_mutation_snapshot(conn)
        if conn.execute("select 1 from sqlite_master where type = 'table' and name = 'events'").fetchone():
            event_sequence_before = int(conn.execute("select coalesce(max(sequence), 0) from events").fetchone()[0])
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


def execute_transactional_script(conn: sqlite3.Connection, script: str) -> None:
    if not conn.in_transaction:
        raise HarnessError("schema SQL requires an active transaction")
    statement = ""
    for character in script:
        statement += character
        if character != ";" or not sqlite3.complete_statement(statement):
            continue
        sql = statement.strip()
        if sql:
            conn.execute(sql)
        statement = ""
    if statement.strip():
        raise HarnessError("incomplete schema SQL statement")


def create_schema(conn: sqlite3.Connection) -> None:
    adapter_action_columns_before = {
        str(row[1]) for row in conn.execute("pragma table_info(adapter_actions)").fetchall()
    }
    execute_transactional_script(
        conn,
        """
        create table if not exists project (
            id integer primary key check (id = 1),
            project_id text not null,
            schema_version integer not null,
            runtime_version text not null,
            phase text not null,
            current_cycle_id text not null default '',
            connector_project_key text not null default '',
            status text not null,
            scope_status text not null,
            current_owner text not null,
            revision integer not null,
            updated_at text not null
        );
        create table if not exists delivery_cycles (
            id text primary key,
            name text not null,
            goal text not null,
            status text not null,
            phase text not null,
            base_ref text not null default '',
            candidate_sha text not null default '',
            started_at text not null,
            closed_at text not null default '',
            created_at text not null,
            updated_at text not null
        );
        create table if not exists acceptance (
            uid text primary key default (lower(hex(randomblob(16)))),
            id text not null,
            cycle_id text not null default '',
            criterion text not null,
            priority text not null default '',
            tool_link text not null default '',
            status text not null default 'active',
            revision integer not null default 1,
            unique(cycle_id, id)
        );
        create table if not exists requirements (
            uid text primary key default (lower(hex(randomblob(16)))),
            id text not null,
            cycle_id text not null default '',
            kind text not null,
            body text not null,
            priority text not null default '',
            status text not null default 'active',
            tool_link text not null default '',
            revision integer not null default 1,
            updated_at text not null,
            unique(cycle_id, id)
        );
        create table if not exists failure_modes (
            uid text primary key default (lower(hex(randomblob(16)))),
            id text not null,
            cycle_id text not null default '',
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
            revision integer not null default 1,
            unique(cycle_id, id)
        );
        create table if not exists requirement_acceptance (
            cycle_id text not null,
            requirement_id text not null,
            acceptance_id text not null,
            primary key (cycle_id, requirement_id, acceptance_id),
            foreign key (cycle_id, requirement_id) references requirements(cycle_id, id) on delete cascade,
            foreign key (cycle_id, acceptance_id) references acceptance(cycle_id, id) on delete cascade
        );
        create table if not exists failure_mode_acceptance (
            cycle_id text not null,
            failure_mode_id text not null,
            acceptance_id text not null,
            primary key (cycle_id, failure_mode_id, acceptance_id),
            foreign key (cycle_id, failure_mode_id) references failure_modes(cycle_id, id) on delete cascade,
            foreign key (cycle_id, acceptance_id) references acceptance(cycle_id, id) on delete cascade
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
            uid text primary key default (lower(hex(randomblob(16)))),
            id text not null,
            cycle_id text not null default '',
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
            updated_at text not null,
            unique(cycle_id, id)
        );
        create table if not exists task_acceptance (
            cycle_id text not null,
            task_id text not null,
            acceptance_id text not null,
            primary key (cycle_id, task_id, acceptance_id),
            foreign key (cycle_id, task_id) references tasks(cycle_id, id) on delete cascade,
            foreign key (cycle_id, acceptance_id) references acceptance(cycle_id, id) on delete cascade
        );
        create table if not exists task_failure_modes (
            cycle_id text not null,
            task_id text not null,
            failure_mode_id text not null,
            primary key (cycle_id, task_id, failure_mode_id),
            foreign key (cycle_id, task_id) references tasks(cycle_id, id) on delete cascade,
            foreign key (cycle_id, failure_mode_id) references failure_modes(cycle_id, id) on delete cascade
        );
        create table if not exists task_dependencies (
            cycle_id text not null,
            task_id text not null,
            depends_on text not null,
            primary key (cycle_id, task_id, depends_on),
            foreign key (cycle_id, task_id) references tasks(cycle_id, id) on delete cascade,
            foreign key (cycle_id, depends_on) references tasks(cycle_id, id) on delete restrict,
            check (task_id != depends_on)
        );
        create table if not exists task_test_targets (
            cycle_id text not null,
            task_id text not null,
            target_id text not null references test_targets(id) on delete cascade,
            primary key (cycle_id, task_id, target_id),
            foreign key (cycle_id, task_id) references tasks(cycle_id, id) on delete cascade
        );
        create table if not exists task_attempts (
            id text primary key,
            run_id text not null,
            cycle_id text not null default '',
            task_id text not null,
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
            finished_at text not null default '',
            foreign key (cycle_id, task_id) references tasks(cycle_id, id) on delete cascade
        );
        create table if not exists validations (
            id text primary key,
            cycle_id text not null default '',
            candidate_sha text not null default '',
            validation_status text not null default 'active',
            superseded_by text not null default '',
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
            result_format text not null default 'regex',
            result_path text not null default '',
            semantic_status text not null default '',
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
            cycle_id text not null,
            failure_mode_id text not null,
            primary key (validation_id, cycle_id, failure_mode_id),
            foreign key (cycle_id, failure_mode_id) references failure_modes(cycle_id, id) on delete cascade
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
            stack_profile text not null default 'python',
            container_image text not null default '',
            requires_sandbox integer not null default 0,
            requires_no_network integer not null default 0,
            result_format text not null default 'regex',
            result_path text not null default '',
            created_at text not null,
            updated_at text not null
        );
        create table if not exists quality_gates (
            id text primary key,
            sequence integer not null default 0 unique,
            cycle_id text not null default '',
            candidate_sha text not null default '',
            gate_status text not null default 'active',
            superseded_by text not null default '',
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
            cycle_id text not null default '',
            candidate_sha text not null default '',
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
            cycle_id text not null,
            acceptance_id text not null,
            primary key (delivery_id, cycle_id, acceptance_id),
            foreign key (cycle_id, acceptance_id) references acceptance(cycle_id, id) on delete cascade
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
            result_format text not null default 'regex',
            result_path text not null default '',
            semantic_status text not null default '',
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
            cycle_id text not null default '',
            candidate_sha text not null default '',
            surface text not null,
            severity text not null,
            status text not null,
            summary text not null,
            evidence_id text not null default '',
            waived_by text not null default '',
            waiver_reason text not null default '',
            waiver_scope text not null default '',
            waived_revision integer,
            waiver_expires_at text not null default '',
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
            payload_hash text not null default '',
            status text not null,
            confirmation text not null default '',
            external_id text not null default '',
            external_link text not null default '',
            idempotency_key text not null,
            attempt_count integer not null default 0,
            next_retry_at text not null default '',
            connector_status text not null default 'available',
            blocked_reason text not null default '',
            execution_fence integer not null default 0,
            claimed_at text not null default '',
            claim_expires_at text not null default '',
            last_recovery_at text not null default '',
            remote_recovery_count integer not null default 0,
            created_at text not null,
            updated_at text not null,
            unique(tool, idempotency_key)
        );
        create table if not exists connector_budgets (
            id text primary key,
            tool text not null,
            operation text not null,
            scope_key text not null default '',
            status text not null,
            retry_after_at text not null default '',
            rate_limit_remaining integer,
            rate_limit_reset_at text not null default '',
            last_status_code integer,
            last_error text not null default '',
            free_plan_risk text not null default '',
            updated_at text not null,
            unique(tool, operation, scope_key)
        );
        create table if not exists connector_profiles (
            id text primary key,
            tool text not null,
            project_key text not null,
            status text not null,
            scope_json text not null default '{}',
            created_at text not null,
            updated_at text not null,
            unique(tool)
        );
        create table if not exists advisory_fallbacks (
            id text primary key,
            action_id text not null,
            tool text not null,
            operation text not null,
            scope_key text not null default '',
            source_status text not null default '',
            fallback_kind text not null,
            official_capability text not null,
            artifact_path text not null,
            summary text not null,
            status text not null,
            delivery_eligible integer not null default 0,
            generated_at text not null,
            updated_at text not null,
            unique(action_id)
        );
        create table if not exists invalidations (
            id text primary key,
            cycle_id text not null default '',
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
            effective_trust text not null default 'local-only',
            receipt_provenance text not null default '',
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
            effective_trust text not null default 'local-only',
            receipt_provenance text not null default '',
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
            effective_trust text not null default 'local-only',
            receipt_provenance text not null default '',
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
            effective_trust text not null default 'local-only',
            receipt_provenance text not null default '',
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
            cycle_id text not null default '',
            scope text not null,
            status text not null,
            created_at text not null,
            updated_at text not null
        );
        create table if not exists dispatch_assignments (
            run_id text not null references dispatch_runs(id) on delete cascade,
            cycle_id text not null default '',
            task_id text not null,
            agent_id text not null default '',
            capability text not null default '',
            status text not null,
            evidence text not null default '',
            provider_session_id text not null default '',
            claimed_at text,
            heartbeat_at text,
            lease_expires_at text,
            updated_at text not null,
            primary key (run_id, task_id),
            foreign key (cycle_id, task_id) references tasks(cycle_id, id) on delete cascade
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
    ensure_column(conn, "project", "current_cycle_id", "text not null default ''")
    ensure_column(conn, "acceptance", "cycle_id", "text not null default ''")
    ensure_column(conn, "requirements", "cycle_id", "text not null default ''")
    ensure_column(conn, "failure_modes", "cycle_id", "text not null default ''")
    ensure_column(conn, "tasks", "cycle_id", "text not null default ''")
    for relation in [
        "requirement_acceptance",
        "failure_mode_acceptance",
        "task_acceptance",
        "task_failure_modes",
        "task_dependencies",
        "task_test_targets",
        "validation_failure_modes",
        "delivery_acceptance",
    ]:
        ensure_column(conn, relation, "cycle_id", "text not null default ''")
    ensure_column(conn, "task_attempts", "cycle_id", "text not null default ''")
    ensure_column(conn, "dispatch_assignments", "cycle_id", "text not null default ''")
    ensure_column(conn, "invalidations", "cycle_id", "text not null default ''")
    ensure_column(conn, "dispatch_runs", "cycle_id", "text not null default ''")
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
    ensure_column(conn, "quality_gates", "cycle_id", "text not null default ''")
    ensure_column(conn, "quality_gates", "candidate_sha", "text not null default ''")
    ensure_column(conn, "quality_gates", "sequence", "integer not null default 0")
    ensure_column(conn, "quality_gates", "gate_status", "text not null default 'active'")
    ensure_column(conn, "quality_gates", "superseded_by", "text not null default ''")
    ensure_column(conn, "quality_gates", "head_commit", "text not null default ''")
    ensure_column(conn, "quality_gates", "tracked_diff_hash", "text not null default ''")
    ensure_column(conn, "quality_gates", "project_revision", "integer not null default 0")
    ensure_column(conn, "quality_gates", "reviewer_session_id", "text not null default ''")
    ensure_column(conn, "quality_gates", "reviewer_attestation_id", "text not null default ''")
    ensure_column(conn, "quality_gates", "review_trust_level", "text not null default 'local-only'")
    ensure_column(conn, "findings", "cycle_id", "text not null default ''")
    ensure_column(conn, "findings", "candidate_sha", "text not null default ''")
    ensure_column(conn, "findings", "waived_by", "text not null default ''")
    ensure_column(conn, "findings", "waiver_reason", "text not null default ''")
    ensure_column(conn, "findings", "waiver_scope", "text not null default ''")
    ensure_column(conn, "findings", "waived_revision", "integer")
    ensure_column(conn, "findings", "waiver_expires_at", "text not null default ''")
    ensure_column(conn, "validations", "head_commit", "text not null default ''")
    ensure_column(conn, "validations", "cycle_id", "text not null default ''")
    ensure_column(conn, "validations", "candidate_sha", "text not null default ''")
    ensure_column(conn, "validations", "validation_status", "text not null default 'active'")
    ensure_column(conn, "validations", "superseded_by", "text not null default ''")
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
    ensure_column(conn, "validations", "result_format", "text not null default 'regex'")
    ensure_column(conn, "validations", "result_path", "text not null default ''")
    ensure_column(conn, "validations", "semantic_status", "text not null default ''")
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
    ensure_column(conn, "test_targets", "stack_profile", "text not null default 'python'")
    ensure_column(conn, "test_targets", "container_image", "text not null default ''")
    ensure_column(conn, "test_targets", "requires_sandbox", "integer not null default 0")
    ensure_column(conn, "test_targets", "requires_no_network", "integer not null default 0")
    ensure_column(conn, "test_targets", "result_format", "text not null default 'regex'")
    ensure_column(conn, "test_targets", "result_path", "text not null default ''")
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
    ensure_column(conn, "evidence", "result_format", "text not null default 'regex'")
    ensure_column(conn, "evidence", "result_path", "text not null default ''")
    ensure_column(conn, "evidence", "semantic_status", "text not null default ''")
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
    ensure_column(conn, "deliveries", "cycle_id", "text not null default ''")
    ensure_column(conn, "deliveries", "candidate_sha", "text not null default ''")
    ensure_column(conn, "project", "connector_project_key", "text not null default ''")
    ensure_column(conn, "ci_verifications", "origin", "text not null default 'manual'")
    ensure_column(conn, "ci_verifications", "verification_token", "text not null default ''")
    ensure_column(conn, "ci_verifications", "token_status", "text not null default 'unchecked'")
    ensure_column(conn, "ci_verifications", "token_reason", "text not null default ''")
    ensure_column(conn, "ci_verifications", "effective_trust", "text not null default 'local-only'")
    ensure_column(conn, "ci_verifications", "receipt_provenance", "text not null default ''")
    ensure_column(conn, "external_session_verifications", "origin", "text not null default 'manual'")
    ensure_column(conn, "external_session_verifications", "verification_token", "text not null default ''")
    ensure_column(conn, "external_session_verifications", "token_status", "text not null default 'unchecked'")
    ensure_column(conn, "external_session_verifications", "token_reason", "text not null default ''")
    ensure_column(conn, "external_session_verifications", "effective_trust", "text not null default 'local-only'")
    ensure_column(conn, "external_session_verifications", "receipt_provenance", "text not null default ''")
    ensure_column(conn, "agent_sessions", "effective_trust", "text not null default 'local-only'")
    ensure_column(conn, "agent_sessions", "receipt_provenance", "text not null default ''")
    ensure_column(conn, "session_attestations", "effective_trust", "text not null default 'local-only'")
    ensure_column(conn, "session_attestations", "receipt_provenance", "text not null default ''")
    ensure_column(conn, "dispatch_assignments", "heartbeat_at", "text")
    ensure_column(conn, "dispatch_assignments", "lease_expires_at", "text")
    ensure_column(conn, "dispatch_assignments", "provider_session_id", "text not null default ''")
    ensure_column(conn, "task_attempts", "provider_session_id", "text not null default ''")
    ensure_column(conn, "task_attempts", "agent_session_id", "text not null default ''")
    ensure_column(conn, "agent_reports", "provider_session_id", "text not null default ''")
    ensure_column(conn, "agent_provider_sessions", "agent_session_id", "text not null default ''")
    ensure_column(conn, "adapter_actions", "attempt_count", "integer not null default 0")
    ensure_column(conn, "adapter_actions", "payload_hash", "text not null default ''")
    if adapter_action_columns_before and "payload_hash" not in adapter_action_columns_before:
        backfill_adapter_action_payload_hashes(conn)
    ensure_column(conn, "adapter_actions", "next_retry_at", "text not null default ''")
    ensure_column(conn, "adapter_actions", "connector_status", "text not null default 'available'")
    ensure_column(conn, "adapter_actions", "blocked_reason", "text not null default ''")
    ensure_column(conn, "adapter_actions", "execution_fence", "integer not null default 0")
    ensure_column(conn, "adapter_actions", "claimed_at", "text not null default ''")
    ensure_column(conn, "adapter_actions", "claim_expires_at", "text not null default ''")
    ensure_column(conn, "adapter_actions", "last_recovery_at", "text not null default ''")
    ensure_column(conn, "adapter_actions", "remote_recovery_count", "integer not null default 0")
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


def current_candidate_sha(root: Path) -> str:
    return git_source_tree_hash(root) or content_source_tree_hash(root)


def ensure_delivery_cycles(conn: sqlite3.Connection) -> None:
    now = now_iso()
    project = conn.execute("select * from project where id = 1").fetchone()
    existing_cycle = conn.execute("select 1 from delivery_cycles limit 1").fetchone()
    if not existing_cycle:
        has_audit_rows = any(
            conn.execute(f"select 1 from {table} limit 1").fetchone()
            for table in [
                "requirements",
                "acceptance",
                "tasks",
                "failure_modes",
                "validations",
                "quality_gates",
                "deliveries",
                "invalidations",
                "dispatch_runs",
            ]
        )
        if has_audit_rows:
            conn.execute(
                """
                insert into delivery_cycles
                (id, name, goal, status, phase, base_ref, candidate_sha, started_at, closed_at, created_at, updated_at)
                values (?, 'Legacy Audit Cycle', 'Imported schema 24 runtime records for audit only.', 'archived', 'archived', '', '', ?, ?, ?, ?)
                """,
                (LEGACY_CYCLE_ID, now, now, now, now),
            )
        conn.execute(
            """
            insert into delivery_cycles
            (id, name, goal, status, phase, base_ref, candidate_sha, started_at, closed_at, created_at, updated_at)
            values (?, 'Current Delivery Cycle', 'Current active delivery candidate.', 'active', 'intake', '', '', ?, '', ?, ?)
            on conflict(id) do nothing
            """,
            (DEFAULT_CYCLE_ID, now, now, now),
        )
        legacy_target = LEGACY_CYCLE_ID if has_audit_rows else DEFAULT_CYCLE_ID
        for table in ["requirements", "acceptance", "tasks", "failure_modes", "validations", "quality_gates", "deliveries", "invalidations", "dispatch_runs"]:
            conn.execute(f"update {table} set cycle_id = ? where cycle_id = ''", (legacy_target,))
    if project:
        current_cycle_id = project["current_cycle_id"] if "current_cycle_id" in project.keys() else ""
        if not current_cycle_id or not conn.execute("select 1 from delivery_cycles where id = ?", (current_cycle_id,)).fetchone():
            current_cycle_id = DEFAULT_CYCLE_ID
            conn.execute(
                "update project set current_cycle_id = ?, phase = coalesce(nullif(phase, ''), 'intake'), updated_at = ? where id = 1",
                (DEFAULT_CYCLE_ID, now),
            )
        for table in ["requirements", "acceptance", "tasks", "failure_modes", "validations", "quality_gates", "deliveries", "invalidations", "dispatch_runs"]:
            conn.execute(f"update {table} set cycle_id = ? where cycle_id = ''", (current_cycle_id,))


def slugify_project_key(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:80] or "project"


def infer_connector_project_key(root: Path) -> str:
    remote = subprocess.run(["git", "remote", "get-url", "origin"], cwd=root, text=True, capture_output=True, check=False)
    if remote.returncode == 0:
        value = remote.stdout.strip()
        if value.startswith("git@github.com:"):
            value = value.removeprefix("git@github.com:")
        elif "github.com/" in value:
            value = value.split("github.com/", 1)[1]
        value = value.removesuffix(".git").strip("/")
        if value:
            return slugify_project_key(value)
    return slugify_project_key(root.name)


def ensure_connector_project_key(conn: sqlite3.Connection, root: Path) -> str:
    row = conn.execute("select connector_project_key from project where id = 1").fetchone()
    current = str(row["connector_project_key"] or "") if row else ""
    if current:
        return current
    project_key = infer_connector_project_key(root)
    conn.execute("update project set connector_project_key = ?, updated_at = ? where id = 1", (project_key, now_iso()))
    return project_key


def initialize_project(conn: sqlite3.Connection) -> None:
    existing = conn.execute("select id from project where id = 1").fetchone()
    if existing:
        ensure_delivery_cycles(conn)
        return
    now = now_iso()
    conn.execute(
        """
        insert into project
        (id, project_id, schema_version, runtime_version, phase, current_cycle_id, connector_project_key, status, scope_status, current_owner, revision, updated_at)
        values (1, ?, ?, ?, 'intake', ?, '', 'draft', 'unconfirmed', 'project-manager', 1, ?)
        """,
        (str(uuid.uuid4()), SCHEMA_VERSION, RUNTIME_VERSION, DEFAULT_CYCLE_ID, now),
    )
    ensure_delivery_cycles(conn)


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


def backfill_adapter_action_payload_hashes(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "select id, tool, mode, artifact, action, payload_json from adapter_actions where payload_hash = ''"
    ).fetchall()
    for row in rows:
        conn.execute(
            "update adapter_actions set payload_hash = ? where id = ? and payload_hash = ''",
            (
                adapter_action_payload_hash(
                    str(row["tool"]),
                    str(row["mode"]),
                    str(row["artifact"]),
                    str(row["action"]),
                    str(row["payload_json"]),
                ),
                row["id"],
            ),
        )


def ensure_adapter_action_payload_hash_state(root: Path) -> None:
    with transaction(root) as conn:
        ensure_column(conn, "adapter_actions", "payload_hash", "text not null default ''")


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


def replay_mutation_snapshot(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    return {
        table: table_rows(conn, table)
        for table in SNAPSHOT_TABLES
        if table != "events"
        and conn.execute("select 1 from sqlite_master where type = 'table' and name = ?", (table,)).fetchone()
    }


def replay_row_key(conn: sqlite3.Connection, table: str, row: dict[str, Any]) -> dict[str, Any]:
    primary_key = [
        item["name"]
        for item in sorted(conn.execute(f"pragma table_info({table})"), key=lambda item: int(item["pk"] or 0))
        if int(item["pk"] or 0) > 0
    ]
    if not primary_key or any(column not in row for column in primary_key):
        raise HarnessError(f"event replay requires a stable primary key: {table}")
    return {column: row[column] for column in primary_key}


def attach_transaction_replay_mutations(
    conn: sqlite3.Connection,
    before: dict[str, list[dict[str, Any]]],
    event_sequence_before: int,
) -> None:
    event = conn.execute(
        "select sequence, payload_json from events where sequence > ? order by sequence desc limit 1",
        (event_sequence_before,),
    ).fetchone()
    after = replay_mutation_snapshot(conn)
    if not event and before != after:
        emit_event(conn, "transaction_state_changed", payload())
        event = conn.execute(
            "select sequence, payload_json from events where sequence > ? order by sequence desc limit 1",
            (event_sequence_before,),
        ).fetchone()
    if event:
        for prior_event in conn.execute(
            "select sequence, payload_json from events where sequence > ? and sequence < ? order by sequence",
            (event_sequence_before, event["sequence"]),
        ):
            prior_payload = json.loads(prior_event["payload_json"])
            prior_payload.setdefault("canonical_mutations", [])
            conn.execute(
                "update events set payload_json = ? where sequence = ?",
                (stable_json(prior_payload), prior_event["sequence"]),
            )
    before_project = before.get("project", [])
    after_project = after.get("project", [])
    before_schema = before_project[0].get("schema_version") if before_project else None
    after_schema = after_project[0].get("schema_version") if after_project else None
    if before_schema is not None and before_schema != after_schema:
        if not event:
            raise HarnessError("schema mutation has no replay event")
        payload_data = json.loads(event["payload_json"])
        payload_data["canonical_mutations"] = []
        payload_data["replay_boundary"] = "checkpoint-required-after-schema-migration"
        conn.execute(
            "update events set payload_json = ? where sequence = ?",
            (stable_json(payload_data), event["sequence"]),
        )
        return
    mutations: list[dict[str, Any]] = []
    table_order = [table for table in SNAPSHOT_TABLES if table != "events"]
    indexed_before: dict[str, dict[str, dict[str, Any]]] = {}
    indexed_after: dict[str, dict[str, dict[str, Any]]] = {}
    for table in table_order:
        if table not in before and table not in after:
            continue
        indexed_before[table] = {
            stable_json(replay_row_key(conn, table, row)): row for row in before.get(table, [])
        }
        indexed_after[table] = {
            stable_json(replay_row_key(conn, table, row)): row for row in after.get(table, [])
        }
    for table in reversed(table_order):
        for key_json in sorted(set(indexed_before.get(table, {})) - set(indexed_after.get(table, {}))):
            row = indexed_before[table][key_json]
            mutations.append({"table": table, "op": "delete", "key": replay_row_key(conn, table, row)})
    for table in table_order:
        before_rows = indexed_before.get(table, {})
        for key_json, row in sorted(indexed_after.get(table, {}).items()):
            if before_rows.get(key_json) != row:
                mutations.append(
                    {"table": table, "op": "upsert", "key": replay_row_key(conn, table, row), "row": row}
                )
    if not mutations:
        if event:
            payload_data = json.loads(event["payload_json"])
            payload_data["canonical_mutations"] = []
            conn.execute(
                "update events set payload_json = ? where sequence = ?",
                (stable_json(payload_data), event["sequence"]),
            )
        return
    if not event:
        raise HarnessError("state mutation has no replay event")
    payload_data = json.loads(event["payload_json"])
    payload_data["canonical_mutations"] = mutations
    conn.execute(
        "update events set payload_json = ? where sequence = ?",
        (stable_json(payload_data), event["sequence"]),
    )


def restore_snapshot(conn: sqlite3.Connection, snapshot: dict[str, Any]) -> None:
    create_schema(conn)
    conn.execute("pragma defer_foreign_keys = on")
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


def baseline_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    cycle_id = current_cycle_id(conn)
    return {
        "requirements": [row_snapshot(row) or {} for row in conn.execute("select * from requirements where cycle_id = ? order by id", (cycle_id,))],
        "acceptance": [row_snapshot(row) or {} for row in conn.execute("select * from acceptance where cycle_id = ? order by id", (cycle_id,))],
        "requirement_acceptance": [
            row_snapshot(row) or {}
            for row in conn.execute(
                """
                select ra.* from requirement_acceptance ra
                join requirements r on r.cycle_id = ra.cycle_id and r.id = ra.requirement_id
                where ra.cycle_id = ?
                order by ra.requirement_id, ra.acceptance_id
                """,
                (cycle_id,),
            )
        ],
        "failure_modes": [row_snapshot(row) or {} for row in conn.execute("select * from failure_modes where cycle_id = ? order by id", (cycle_id,))],
        "failure_mode_acceptance": [
            row_snapshot(row) or {}
            for row in conn.execute(
                """
                select fma.* from failure_mode_acceptance fma
                join failure_modes fm on fm.cycle_id = fma.cycle_id and fm.id = fma.failure_mode_id
                where fma.cycle_id = ?
                order by fma.failure_mode_id, fma.acceptance_id
                """,
                (cycle_id,),
            )
        ],
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
    cycle_id = current_cycle_id(conn)
    return {
        "requirement_id": requirement_id,
        "acceptance_ids": [
            row["acceptance_id"]
            for row in conn.execute(
                "select acceptance_id from requirement_acceptance where cycle_id = ? and requirement_id = ? order by acceptance_id",
                (cycle_id, requirement_id),
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
    cycle_id = current_cycle_id(conn)
    targets: list[tuple[str, str]] = []
    if source_type == "acceptance":
        targets.extend(
            ("task", row["task_id"])
            for row in conn.execute(
                "select ta.task_id from task_acceptance ta join tasks t on t.cycle_id = ta.cycle_id and t.id = ta.task_id where ta.cycle_id = ? and ta.acceptance_id = ?",
                (cycle_id, source_id),
            )
        )
        targets.extend(("validation", row["id"]) for row in conn.execute("select id from validations where acceptance_id = ? and cycle_id = ?", (source_id, cycle_id)))
        targets.extend(("quality_gate", row["id"]) for row in conn.execute("select id from quality_gates where cycle_id = ?", (cycle_id,)))
    elif source_type == "failure_mode":
        targets.extend(
            ("task", row["task_id"])
            for row in conn.execute(
                "select tfm.task_id from task_failure_modes tfm join tasks t on t.cycle_id = tfm.cycle_id and t.id = tfm.task_id where tfm.cycle_id = ? and tfm.failure_mode_id = ?",
                (cycle_id, source_id),
            )
        )
        targets.extend(
            ("validation", row["validation_id"])
            for row in conn.execute(
                """
                select vfm.validation_id from validation_failure_modes vfm
                join validations v on v.id = vfm.validation_id
                where vfm.cycle_id = ? and vfm.failure_mode_id = ? and v.cycle_id = vfm.cycle_id
                """,
                (cycle_id, source_id),
            )
        )
        targets.extend(("quality_gate", row["id"]) for row in conn.execute("select id from quality_gates where cycle_id = ?", (cycle_id,)))
    elif source_type == "requirement":
        acceptance_ids = [
            row["acceptance_id"]
            for row in conn.execute(
                """
                select ra.acceptance_id from requirement_acceptance ra
                join acceptance a on a.cycle_id = ra.cycle_id and a.id = ra.acceptance_id
                where ra.cycle_id = ? and ra.requirement_id = ?
                """,
                (cycle_id, source_id),
            )
        ]
        for acceptance_id in acceptance_ids:
            targets.append(("acceptance", acceptance_id))
            targets.extend(
                ("task", row["task_id"])
                for row in conn.execute(
                    "select ta.task_id from task_acceptance ta join tasks t on t.cycle_id = ta.cycle_id and t.id = ta.task_id where ta.cycle_id = ? and ta.acceptance_id = ?",
                    (cycle_id, acceptance_id),
                )
            )
            targets.extend(("validation", row["id"]) for row in conn.execute("select id from validations where acceptance_id = ? and cycle_id = ?", (acceptance_id, cycle_id)))
        targets.extend(("quality_gate", row["id"]) for row in conn.execute("select id from quality_gates where cycle_id = ?", (cycle_id,)))
    for target_type, target_id in targets:
        exists = conn.execute(
            """
            select 1 from invalidations
            where cycle_id = ? and source_type = ? and source_id = ? and target_type = ? and target_id = ? and resolved_at is null
            """,
            (cycle_id, source_type, source_id, target_type, target_id),
        ).fetchone()
        if exists:
            continue
        conn.execute(
            """
            insert into invalidations (id, cycle_id, source_type, source_id, target_type, target_id, reason, created_at)
            values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (str(uuid.uuid4()), cycle_id, source_type, source_id, target_type, target_id, reason, now_iso()),
        )


def resolve_invalidations(conn: sqlite3.Connection, *, source_type: str | None = None, source_id: str | None = None, target_type: str | None = None) -> None:
    clauses = ["resolved_at is null"]
    values: list[object] = []
    try:
        clauses.append("cycle_id = ?")
        values.append(current_cycle_id(conn))
    except HarnessError:
        pass
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
        ensure_connector_project_key(conn, root)
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
        if relpath == ".ai-team/state" and source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            source_db = source / "harness.db"
            if source_db.exists():
                get_store(root).backup_to(target / "harness.db")
            for child in source.iterdir():
                if child.name in {"harness.db", "harness.db-wal", "harness.db-shm"}:
                    continue
                destination = target / child.name
                if child.is_dir():
                    shutil.copytree(child, destination, dirs_exist_ok=True)
                else:
                    shutil.copy2(child, destination)
            continue
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

    backup_runtime(root, "markdown-v1")
    try:
        with transaction(root, validate_invariants=False) as conn:
            create_schema(conn)
            already_applied = conn.execute(
                "select 1 from migrations where from_version = 1 and to_version = ? limit 1",
                (SCHEMA_VERSION,),
            ).fetchone()
            if already_applied:
                raise _MigrationAlreadyApplied
            initialize_project(conn)
            cycle_id = current_cycle_id(conn)
            for cells in acceptance_rows:
                if len(cells) < 2:
                    report_count(report, "skipped", "acceptance")
                    continue
                conn.execute(
                    """
                    insert into acceptance (id, cycle_id, criterion, priority, tool_link, status)
                    values (?, ?, ?, ?, ?, ?)
                    on conflict(cycle_id, id) do nothing
                    """,
                    (cells[0], cycle_id, cells[1], cells[2] if len(cells) > 2 else "", cells[3] if len(cells) > 3 else "", cells[4] if len(cells) > 4 else "active"),
                )
            for cells in requirement_rows:
                if len(cells) < 3:
                    report_count(report, "skipped", "requirement")
                    continue
                conn.execute(
                    """
                    insert into requirements (id, cycle_id, kind, body, priority, status, tool_link, revision, updated_at)
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    on conflict(cycle_id, id) do nothing
                    """,
                    (
                        cells[0],
                        cycle_id,
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
                    (id, cycle_id, feature, scenario, trigger, expected_behavior, recovery, data_safety, risk, status)
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    on conflict(cycle_id, id) do nothing
                    """,
                    (
                        cells[0],
                        cycle_id,
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
                        if conn.execute("select id from acceptance where cycle_id = ? and id = ?", (cycle_id, acceptance_id)).fetchone():
                            conn.execute(
                                "insert or ignore into failure_mode_acceptance (cycle_id, failure_mode_id, acceptance_id) values (?, ?, ?)",
                                (cycle_id, cells[0], acceptance_id),
                            )
            for cells in task_rows:
                if len(cells) < 4:
                    report_count(report, "skipped", "task")
                    continue
                status = cells[3] if cells[3] in TASK_STATUSES else "ready"
                conn.execute(
                    """
                    insert into tasks (id, cycle_id, task, owner, status, tool_link, evidence, updated_at)
                    values (?, ?, ?, ?, ?, ?, ?, ?)
                    on conflict(cycle_id, id) do nothing
                    """,
                    (
                        cells[0],
                        cycle_id,
                        cells[1],
                        cells[2] if len(cells) > 2 else "unassigned",
                        status,
                        cells[7] if len(cells) > 7 else "",
                        cells[8] if len(cells) > 8 else "",
                        now_iso(),
                    ),
                )
                for acceptance_id in parse_ids(cells[4] if len(cells) > 4 else ""):
                    if conn.execute("select id from acceptance where cycle_id = ? and id = ?", (cycle_id, acceptance_id)).fetchone():
                        conn.execute("insert or ignore into task_acceptance (cycle_id, task_id, acceptance_id) values (?, ?, ?)", (cycle_id, cells[0], acceptance_id))
                for fm_id in parse_ids(cells[5] if len(cells) > 5 else ""):
                    if conn.execute("select id from failure_modes where cycle_id = ? and id = ?", (cycle_id, fm_id)).fetchone():
                        conn.execute("insert or ignore into task_failure_modes (cycle_id, task_id, failure_mode_id) values (?, ?, ?)", (cycle_id, cells[0], fm_id))
            for cells in validation_rows:
                if len(cells) < 10:
                    report_count(report, "skipped", "validation")
                    continue
                validation_id = str(uuid.uuid4())
                conn.execute(
                    """
                    insert into validations
                    (id, cycle_id, surface, acceptance_id, commands, findings, result, residual_risk, head_commit,
                     source_tree_hash, tracked_diff_hash, project_revision, created_at)
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        validation_id,
                        cycle_id,
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
                    if conn.execute("select id from failure_modes where cycle_id = ? and id = ?", (cycle_id, fm_id)).fetchone():
                        conn.execute(
                            "insert or ignore into validation_failure_modes (validation_id, cycle_id, failure_mode_id) values (?, ?, ?)",
                            (validation_id, cycle_id, fm_id),
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
            ensure_delivery_cycles(conn)
            emit_event(conn, "markdown_v1_migrated", payload(to=SCHEMA_VERSION))
            require_full_invariants(conn, root, "migration")
    except _MigrationAlreadyApplied:
        pass
    except Exception:
        raise
    try:
        render_all(root)
        write_migration_report(root, report)
        install_agents(root)
    except Exception as exc:
        raise HarnessError(
            f"markdown migration committed but projection rebuild failed; rerun migrate --from-version markdown-v1 --to-version {SCHEMA_VERSION}: {exc}"
        ) from exc
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


def current_cycle_row(conn: sqlite3.Connection) -> sqlite3.Row:
    project = project_row(conn)
    cycle_id = project["current_cycle_id"]
    if not cycle_id:
        raise HarnessError("current delivery cycle is not configured")
    cycle = conn.execute("select * from delivery_cycles where id = ?", (cycle_id,)).fetchone()
    if not cycle:
        raise HarnessError(f"current delivery cycle is missing: {cycle_id}")
    return cycle


def current_cycle_id(conn: sqlite3.Connection) -> str:
    return current_cycle_row(conn)["id"]


def cycle_status(root: Path) -> dict[str, Any]:
    with connection(root) as conn:
        cycle = current_cycle_row(conn)
        return row_snapshot(cycle) or {}


def cycle_start(root: Path, cycle_id: str, name: str, goal: str, *, base_ref: str = "") -> None:
    if not cycle_id or not name or not goal:
        raise HarnessError("cycle start requires id, name, and goal")
    with transaction(root, touched=[("project", "1"), ("delivery_cycle", cycle_id)]) as conn:
        current = current_cycle_row(conn)
        if current["status"] not in {"delivered", "archived"}:
            raise HarnessError(f"current cycle is not closed: {current['id']} status={current['status']}")
        if conn.execute("select 1 from delivery_cycles where id = ?", (cycle_id,)).fetchone():
            raise HarnessError(f"duplicate cycle id: {cycle_id}")
        now = now_iso()
        conn.execute(
            """
            insert into delivery_cycles
            (id, name, goal, status, phase, base_ref, candidate_sha, started_at, closed_at, created_at, updated_at)
            values (?, ?, ?, 'active', 'intake', ?, '', ?, '', ?, ?)
            """,
            (cycle_id, name, goal, base_ref, now, now, now),
        )
        bump_project(conn, current_cycle_id=cycle_id, phase="intake", status="draft")
        emit_event(conn, "delivery_cycle_started", payload(id=cycle_id, name=name, base_ref=base_ref))
    render_all(root)


def cycle_close(root: Path, status: str) -> None:
    if status not in {"delivered", "archived"}:
        raise HarnessError("cycle close status must be delivered or archived")
    with transaction(root, touched=[("project", "1")]) as conn:
        cycle = current_cycle_row(conn)
        if cycle["status"] != "active":
            raise HarnessError(f"current cycle is already closed: {cycle['id']} status={cycle['status']}")
        now = now_iso()
        candidate_sha = cycle["candidate_sha"] or current_candidate_sha(root)
        conn.execute(
            """
            update delivery_cycles
            set status = ?, phase = case when ? = 'archived' then 'archived' else phase end,
                candidate_sha = ?, closed_at = ?, updated_at = ?
            where id = ?
            """,
            (status, status, candidate_sha, now, now, cycle["id"]),
        )
        if status == "archived":
            bump_project(conn, phase="archived", status="archived")
        emit_event(conn, "delivery_cycle_closed", payload(id=cycle["id"], status=status))
    render_all(root)


def transition_phase(root: Path, phase: str, *, status: str | None = None, owner: str | None = None) -> None:
    with transaction(root, touched=[("project", "1")]) as conn:
        row = project_row(conn)
        cycle = current_cycle_row(conn)
        if cycle["status"] != "active" and phase != row["phase"]:
            raise HarnessError(f"current cycle is not active: {cycle['id']} status={cycle['status']}")
        current = row["phase"]
        if phase not in PHASES:
            raise HarnessError(f"unknown phase: {phase}")
        if phase != current and phase not in PHASE_TRANSITIONS[current]:
            allowed = ", ".join(sorted(PHASE_TRANSITIONS[current])) or "none"
            order = " -> ".join(PHASES)
            raise HarnessError(f"illegal phase transition: {current} -> {phase}; allowed next: {allowed}; phase order: {order}")
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
        conn.execute("update delivery_cycles set phase = ?, updated_at = ? where id = ?", (phase, now_iso(), cycle["id"]))
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
    cycle_id = project["current_cycle_id"]
    requirement_count = conn.execute("select count(*) from requirements where cycle_id = ? and status != 'cancelled'", (cycle_id,)).fetchone()[0]
    acceptance_count = conn.execute("select count(*) from acceptance where cycle_id = ?", (cycle_id,)).fetchone()[0]
    task_count = conn.execute("select count(*) from tasks where cycle_id = ?", (cycle_id,)).fetchone()[0]
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
        cycle_id = current_cycle_id(conn)
        existing = conn.execute("select * from requirements where cycle_id = ? and id = ?", (cycle_id, requirement_id)).fetchone()
        conn.execute(
            """
            insert into requirements (id, cycle_id, kind, body, priority, status, tool_link, updated_at)
            values (?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(cycle_id, id) do update set kind=excluded.kind, body=excluded.body, priority=excluded.priority,
              status=excluded.status, tool_link=excluded.tool_link,
              revision=requirements.revision+1, updated_at=excluded.updated_at
            """,
            (requirement_id, cycle_id, kind, body, priority, status, tool_link, now_iso()),
        )
        if existing and (existing["kind"], existing["body"], existing["priority"], existing["status"], existing["tool_link"]) != (kind, body, priority, status, tool_link):
            invalidate_downstream(conn, "requirement", requirement_id, "requirement changed")
        after = conn.execute("select * from requirements where cycle_id = ? and id = ?", (cycle_id, requirement_id)).fetchone()
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
        cycle_id = current_cycle_id(conn)
        existing = conn.execute("select * from acceptance where cycle_id = ? and id = ?", (cycle_id, acceptance_id)).fetchone()
        conn.execute(
            """
            insert into acceptance (id, cycle_id, criterion, priority, tool_link)
            values (?, ?, ?, ?, ?)
            on conflict(cycle_id, id) do update set criterion=excluded.criterion, priority=excluded.priority,
                tool_link=excluded.tool_link, revision=acceptance.revision+1
            """,
            (acceptance_id, cycle_id, criterion, priority, tool_link),
        )
        if existing and (existing["criterion"], existing["priority"], existing["tool_link"]) != (criterion, priority, tool_link):
            invalidate_downstream(conn, "acceptance", acceptance_id, "acceptance criterion changed")
        after = conn.execute("select * from acceptance where cycle_id = ? and id = ?", (cycle_id, acceptance_id)).fetchone()
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
        cycle_id = current_cycle_id(conn)
        existing = conn.execute("select * from failure_modes where cycle_id = ? and id = ?", (cycle_id, fm_id)).fetchone()
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
            (id, cycle_id, feature, scenario, trigger, expected_behavior, recovery, data_safety, risk, status,
             accepted_by, acceptance_reason, acceptance_scope, accepted_revision, expires_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(cycle_id, id) do update set feature=excluded.feature, scenario=excluded.scenario, trigger=excluded.trigger,
              expected_behavior=excluded.expected_behavior, recovery=excluded.recovery, data_safety=excluded.data_safety,
              risk=excluded.risk, status=excluded.status, accepted_by=excluded.accepted_by,
              acceptance_reason=excluded.acceptance_reason, acceptance_scope=excluded.acceptance_scope,
              accepted_revision=excluded.accepted_revision, expires_at=excluded.expires_at, revision=failure_modes.revision+1
            """,
            (
                fm_id,
                cycle_id,
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
                "insert or ignore into failure_mode_acceptance (cycle_id, failure_mode_id, acceptance_id) values (?, ?, ?)",
                (cycle_id, fm_id, acceptance),
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
        after = conn.execute("select * from failure_modes where cycle_id = ? and id = ?", (cycle_id, fm_id)).fetchone()
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
    cycle_id = current_cycle_id(conn)
    if not conn.execute("select id from acceptance where cycle_id = ? and id = ?", (cycle_id, acceptance_id)).fetchone():
        raise HarnessError(f"missing acceptance: {acceptance_id}")


def require_requirement(conn: sqlite3.Connection, requirement_id: str) -> None:
    cycle_id = current_cycle_id(conn)
    if not conn.execute("select id from requirements where cycle_id = ? and id = ?", (cycle_id, requirement_id)).fetchone():
        raise HarnessError(f"missing requirement: {requirement_id}")


def link_requirement_acceptance(root: Path, requirement_id: str, acceptance_id: str) -> None:
    with transaction(root, touched=[("requirement", requirement_id), ("acceptance", acceptance_id)]) as conn:
        cycle_id = current_cycle_id(conn)
        require_requirement(conn, requirement_id)
        require_acceptance(conn, acceptance_id)
        before = trace_snapshot(conn, requirement_id)
        conn.execute(
            "insert or ignore into requirement_acceptance (cycle_id, requirement_id, acceptance_id) values (?, ?, ?)",
            (cycle_id, requirement_id, acceptance_id),
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
    cycle_id = current_cycle_id(conn)
    row = conn.execute("select * from tasks where cycle_id = ? and id = ?", (cycle_id, task_id)).fetchone()
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

    core_assert_no_dependency_cycle(
        conn,
        task_id,
        depends_on,
        cycle_id=current_cycle_id(conn),
        error_factory=HarnessError,
    )


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
        cycle_id = current_cycle_id(conn)
        if status not in TASK_STATUSES:
            raise HarnessError(f"invalid task status: {status}")
        if status == "accepted":
            raise HarnessError("new tasks cannot be created as accepted; use task complete with evidence")
        if conn.execute("select id from tasks where cycle_id = ? and id = ?", (cycle_id, task_id)).fetchone():
            raise HarnessError(f"duplicate task id: {task_id}")
        conn.execute(
            """
            insert into tasks (id, cycle_id, task, owner, status, evidence, tool_link, updated_at)
            values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (task_id, cycle_id, task, owner, status, evidence, tool_link, now_iso()),
        )
        conn.execute("delete from task_acceptance where cycle_id = ? and task_id = ?", (cycle_id, task_id))
        for acceptance_id in parse_ids(acceptance):
            require_acceptance(conn, acceptance_id)
            conn.execute("insert into task_acceptance (cycle_id, task_id, acceptance_id) values (?, ?, ?)", (cycle_id, task_id, acceptance_id))
        conn.execute("delete from task_failure_modes where cycle_id = ? and task_id = ?", (cycle_id, task_id))
        for fm_id in parse_ids(failure_modes):
            if not conn.execute("select id from failure_modes where cycle_id = ? and id = ?", (cycle_id, fm_id)).fetchone():
                raise HarnessError(f"missing failure mode: {fm_id}")
            conn.execute("insert into task_failure_modes (cycle_id, task_id, failure_mode_id) values (?, ?, ?)", (cycle_id, task_id, fm_id))
        conn.execute("delete from task_dependencies where cycle_id = ? and task_id = ?", (cycle_id, task_id))
        for dep in parse_ids(depends_on):
            require_task(conn, dep)
            assert_no_dependency_cycle(conn, task_id, dep)
            conn.execute("insert into task_dependencies (cycle_id, task_id, depends_on) values (?, ?, ?)", (cycle_id, task_id, dep))
        after = conn.execute("select * from tasks where cycle_id = ? and id = ?", (cycle_id, task_id)).fetchone()
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
        cycle_id = current_cycle_id(conn)
        row = require_task(conn, task_id)
        if status and status not in TASK_STATUSES:
            raise HarnessError(f"invalid task status: {status}")
        if status == "accepted":
            raise HarnessError("task acceptance must use task complete with evidence")
        if depends_on is not None:
            conn.execute("delete from task_dependencies where cycle_id = ? and task_id = ?", (cycle_id, task_id))
            for dep in parse_ids(depends_on):
                require_task(conn, dep)
                assert_no_dependency_cycle(conn, task_id, dep)
                conn.execute("insert into task_dependencies (cycle_id, task_id, depends_on) values (?, ?, ?)", (cycle_id, task_id, dep))
        if status:
            conn.execute(
                "update tasks set status = ?, revision = revision + 1, updated_at = ? where cycle_id = ? and id = ?",
                (status, now_iso(), cycle_id, task_id),
            )
        else:
            conn.execute("update tasks set revision = revision + 1, updated_at = ? where cycle_id = ? and id = ?", (now_iso(), cycle_id, task_id))
        after = conn.execute("select * from tasks where cycle_id = ? and id = ?", (cycle_id, task_id)).fetchone()
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
        return ready_queue(conn, current_cycle_id(conn))


def dependency_blockers(conn: sqlite3.Connection, task_id: str) -> list[str]:
    from core.scheduler import dependency_blockers as core_dependency_blockers

    return core_dependency_blockers(conn, task_id, current_cycle_id(conn))


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
              revision = revision + 1, updated_at = ? where uid = ?
            """,
            (agent, token, now_iso(), lease_deadline(), now_iso(), row["uid"]),
        )
        conn.execute(
            """
            update agents set lease_task_id = ?, status = 'leased', updated_at = ?
            where id = ?
            """,
            (task_id, now_iso(), agent),
        )
        after = conn.execute("select * from tasks where uid = ?", (row["uid"],)).fetchone()
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
            "update tasks set lease_heartbeat_at = ?, lease_expires_at = ?, revision = revision + 1, updated_at = ? where uid = ?",
            (now_iso(), lease_deadline(), now_iso(), row["uid"]),
        )
        after = conn.execute("select * from tasks where uid = ?", (row["uid"],)).fetchone()
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
            select uid, id, status, lease_agent from tasks
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
                  lease_expires_at = null, revision = revision + 1, fence = fence + 1, updated_at = ? where uid = ?
                """,
                (next_status, now_iso(), row["uid"]),
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
              status = 'ready', revision = revision + 1, fence = fence + 1, updated_at = ? where uid = ?
            """,
            (now_iso(), row["uid"]),
        )
        conn.execute("update agents set lease_task_id = '', status = 'available', updated_at = ? where id = ?", (now_iso(), agent))
        after = conn.execute("select * from tasks where uid = ?", (row["uid"],)).fetchone()
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
            update tasks set status = 'in_progress', owner = ?, revision = revision + 1, updated_at = ? where uid = ?
            """,
            (agent, now_iso(), row["uid"]),
        )
        after = conn.execute("select * from tasks where uid = ?", (row["uid"],)).fetchone()
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
              submitted_session_id = ?, revision = revision + 1, updated_at = ? where uid = ?
            """,
            (evidence, agent, session_id, now_iso(), row["uid"]),
        )
        conn.execute(
            "update agents set lease_task_id = '', status = 'available', updated_at = ? where id = ?",
            (now_iso(), agent),
        )
        after = conn.execute("select * from tasks where uid = ?", (row["uid"],)).fetchone()
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
              revision = revision + 1, fence = fence + 1, updated_at = ? where uid = ?
            """,
            (agent, token, now_iso(), lease_deadline(), now_iso(), row["uid"]),
        )
        conn.execute(
            "update agents set lease_task_id = ?, status = 'leased', updated_at = ? where id = ?",
            (task_id, now_iso(), agent),
        )
        after = conn.execute("select * from tasks where uid = ?", (row["uid"],)).fetchone()
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
              accepted_session_id = ?, revision = revision + 1, updated_at = ? where uid = ?
            """,
            (evidence, agent, session_id, now_iso(), row["uid"]),
        )
        conn.execute(
            "update agents set lease_task_id = '', status = 'available', updated_at = ? where id = ?",
            (now_iso(), agent),
        )
        after = conn.execute("select * from tasks where uid = ?", (row["uid"],)).fetchone()
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


def accept_ready_task(root: Path, task_id: str, agent: str, evidence: str, *, session_id: str = "") -> None:
    with connection(root) as conn:
        row = require_task(conn, task_id)
        status = row["status"]
        if status == "accepted":
            return
        if status not in {"submitted", "review"}:
            raise HarnessError(f"task accept-ready requires submitted or review status: {task_id} status={status}")
        revision = int(row["revision"])
    if status == "submitted":
        token, fence = review_task(root, task_id, agent, revision, session_id=session_id)
        with connection(root) as conn:
            reviewed = require_task(conn, task_id)
            revision = int(reviewed["revision"])
        accept_task(root, task_id, evidence, agent=agent, lease_token=token, expected_revision=revision, expected_fence=fence, session_id=session_id)
        return
    with connection(root) as conn:
        review_row = require_task(conn, task_id)
        token = review_row["lease_token"]
        fence = int(review_row["fence"])
        revision = int(review_row["revision"])
    accept_task(root, task_id, evidence, agent=agent, lease_token=token, expected_revision=revision, expected_fence=fence, session_id=session_id)


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
              lease_heartbeat_at = null, lease_expires_at = null, revision = revision + 1, updated_at = ? where uid = ?
            """,
            (reason, now_iso(), row["uid"]),
        )
        conn.execute(
            "update agents set lease_task_id = '', status = 'available', updated_at = ? where id = ?",
            (now_iso(), agent),
        )
        after = conn.execute("select * from tasks where uid = ?", (row["uid"],)).fetchone()
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


def add_test_target(
    root: Path,
    target_id: str,
    kind: str,
    command_template: str,
    description: str = "",
    *,
    stack_profile: str = "python",
    container_image: str = "",
    requires_sandbox: bool = False,
    requires_no_network: bool = False,
    result_format: str = "regex",
    result_path: str = "",
) -> None:
    guard_schema("validate_test_target", target_id, kind, command_template, stack_profile, result_format)
    gateable, gate_block_reason = target_gateability(kind, command_template)
    with transaction(root, touched=[("test_target", target_id)]) as conn:
        conn.execute(
            """
            insert into test_targets
            (id, kind, command_template, description, gateable, gate_block_reason, stack_profile, container_image,
             requires_sandbox, requires_no_network, result_format, result_path, created_at, updated_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(id) do update set kind=excluded.kind, command_template=excluded.command_template,
              description=excluded.description, gateable=excluded.gateable, gate_block_reason=excluded.gate_block_reason,
              stack_profile=excluded.stack_profile, container_image=excluded.container_image,
              requires_sandbox=excluded.requires_sandbox, requires_no_network=excluded.requires_no_network,
              result_format=excluded.result_format, result_path=excluded.result_path,
              updated_at=excluded.updated_at
            """,
            (
                target_id,
                kind,
                command_template,
                description,
                gateable,
                gate_block_reason,
                stack_profile,
                container_image,
                bool_int(requires_sandbox),
                bool_int(requires_no_network),
                result_format,
                result_path,
                now_iso(),
                now_iso(),
            ),
        )
        emit_event(
            conn,
            "test_target_recorded",
            payload(
                id=target_id,
                kind=kind,
                gateable=gateable,
                stack_profile=stack_profile,
                requires_sandbox=bool(requires_sandbox),
                requires_no_network=bool(requires_no_network),
                result_format=result_format,
            ),
        )
    render_all(root)


def link_task_test_target(root: Path, task_id: str, target_id: str) -> None:
    with transaction(root, touched=[("task", task_id)]) as conn:
        cycle_id = current_cycle_id(conn)
        if not conn.execute("select id from tasks where cycle_id = ? and id = ?", (cycle_id, task_id)).fetchone():
            raise HarnessError(f"missing task: {task_id}")
        if not conn.execute("select id from test_targets where id = ?", (target_id,)).fetchone():
            raise HarnessError(f"missing test target: {target_id}")
        conn.execute("insert or ignore into task_test_targets (cycle_id, task_id, target_id) values (?, ?, ?)", (cycle_id, task_id, target_id))
        emit_event(conn, "task_test_target_linked", payload(task_id=task_id, target_id=target_id))
    render_all(root)


def task_target(conn: sqlite3.Connection, task_id: str, cycle_id: str = "") -> sqlite3.Row | None:
    cycle_id = cycle_id or current_cycle_id(conn)
    return conn.execute(
        """
        select tt.* from task_test_targets link
        join test_targets tt on tt.id = link.target_id
        where link.cycle_id = ? and link.task_id = ?
        order by tt.id
        limit 1
        """,
        (cycle_id, task_id),
    ).fetchone()


def refresh_dispatch_run_status(conn: sqlite3.Connection, run_id: str) -> str:
    rows = conn.execute("select status from dispatch_assignments where run_id = ?", (run_id,)).fetchall()
    statuses = [str(row["status"]) for row in rows]
    if not statuses:
        aggregate = "planned"
    elif "verification_failed" in statuses:
        aggregate = "verification_failed"
    elif "integration_conflict" in statuses:
        aggregate = "integration_conflict"
    elif "failed" in statuses:
        aggregate = "failed"
    elif all(status == "integrated" for status in statuses):
        aggregate = "integrated"
    elif all(status in {"completed", "integrated"} for status in statuses):
        aggregate = "completed"
    elif any(status == "reported" for status in statuses):
        aggregate = "reported"
    elif any(status in {"claimed", "spawning", "running", "stale"} for status in statuses):
        aggregate = "claimed"
    else:
        aggregate = "planned"
    conn.execute("update dispatch_runs set status = ?, updated_at = ? where id = ?", (aggregate, now_iso(), run_id))
    return aggregate


def list_test_targets(root: Path) -> list[str]:
    with connection(root) as conn:
        rows = conn.execute(
            """
            select id, kind, command_template, description, gateable, gate_block_reason, stack_profile,
                   container_image, requires_sandbox, requires_no_network, result_format, result_path
            from test_targets order by id
            """
        ).fetchall()
    return [
        markdown_row(
            [
                row["id"],
                row["kind"],
                row["command_template"],
                row["description"],
                str(row["gateable"]),
                row["gate_block_reason"],
                row["stack_profile"],
                row["container_image"],
                str(row["requires_sandbox"]),
                str(row["requires_no_network"]),
                row["result_format"],
                row["result_path"],
            ]
        )
        for row in rows
    ]


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
        cycle_id = current_cycle_id(conn)
        candidate_sha = source_hash or current_candidate_sha(root)
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
            (id, cycle_id, candidate_sha, validation_status, superseded_by, surface, acceptance_id, commands, command, exit_code, stdout_sha256, artifact_path,
             target_id, executed_count, executed_count_source, allow_unlisted, no_network, policy_status,
             policy_reason, sandbox_profile, sandbox_status, allow_unlisted_reason, trust_anchor, trust_anchor_id,
             findings, result, residual_risk, head_commit, source_tree_hash, tracked_diff_hash,
             project_revision, created_at)
            values (?, ?, ?, 'active', '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                validation_id,
                cycle_id,
                candidate_sha,
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
        conn.execute(
            """
            update validations
            set validation_status = 'superseded', superseded_by = ?
            where id != ? and cycle_id = ? and acceptance_id = ? and candidate_sha = ? and validation_status = 'active'
            """,
            (validation_id, validation_id, cycle_id, acceptance, candidate_sha),
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
            if not conn.execute("select id from failure_modes where cycle_id = ? and id = ?", (cycle_id, fm_id)).fetchone():
                raise HarnessError(f"missing failure mode: {fm_id}")
            conn.execute(
                "insert into validation_failure_modes (validation_id, cycle_id, failure_mode_id) values (?, ?, ?)",
                (validation_id, cycle_id, fm_id),
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


def record_finding(
    root: Path,
    finding_id: str,
    surface: str,
    severity: str,
    status: str,
    summary: str,
    *,
    evidence_id: str = "",
    waived_by: str = "",
    waiver_reason: str = "",
    waiver_scope: str = "",
    waived_revision: int | None = None,
    waiver_expires_at: str = "",
) -> None:
    with transaction(root) as conn:
        if evidence_id and not conn.execute("select id from evidence where id = ?", (evidence_id,)).fetchone():
            raise HarnessError(f"missing evidence: {evidence_id}")
        cycle_id = current_cycle_id(conn)
        candidate_sha = current_candidate_sha(root)
        if status == "accepted" and not all(
            [waived_by, waiver_reason, waiver_scope, waived_revision, waiver_expires_at]
        ):
            raise HarnessError("accepted finding requires actor, reason, scope, revision, and expiry")
        conn.execute(
            """
            insert into findings
            (id, cycle_id, candidate_sha, surface, severity, status, summary, evidence_id,
             waived_by, waiver_reason, waiver_scope, waived_revision, waiver_expires_at, created_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(id) do update set surface=excluded.surface, severity=excluded.severity, status=excluded.status,
              cycle_id=excluded.cycle_id, candidate_sha=excluded.candidate_sha,
              summary=excluded.summary, evidence_id=excluded.evidence_id,
              waived_by=excluded.waived_by, waiver_reason=excluded.waiver_reason,
              waiver_scope=excluded.waiver_scope, waived_revision=excluded.waived_revision,
              waiver_expires_at=excluded.waiver_expires_at, created_at=excluded.created_at
            """,
            (finding_id, cycle_id, candidate_sha, surface, severity, status, summary, evidence_id,
             waived_by, waiver_reason, waiver_scope, waived_revision, waiver_expires_at, now_iso()),
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
                where uid = ?
                """,
                (row["uid"],),
            )
            after = conn.execute("select * from failure_modes where uid = ?", (row["uid"],)).fetchone()
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
        cycle_id = current_cycle_id(conn)
        candidate_sha = source_hash or current_candidate_sha(root)
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
        sequence = int(conn.execute("select coalesce(max(sequence), 0) + 1 from quality_gates").fetchone()[0])
        previous_gate = conn.execute(
            """
            select id from quality_gates
            where cycle_id = ? and candidate_sha = ? and gate_status = 'active'
            order by sequence desc limit 1
            """,
            (cycle_id, candidate_sha),
        ).fetchone()
        conn.execute(
            """
            insert into quality_gates
            (id, sequence, cycle_id, candidate_sha, gate_status, superseded_by,
             gate, reviewed_commit, evidence_commit, diff_hash, base_commit, head_commit, tracked_diff_hash,
             project_revision, reviewer_context, result, blocking_findings, commands, evidence, residual_risk,
             reviewer_session_id, reviewer_attestation_id, review_trust_level, created_at)
            values (?, ?, ?, ?, 'active', '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                gate_id,
                sequence,
                cycle_id,
                candidate_sha,
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
        if previous_gate:
            conn.execute(
                "update quality_gates set gate_status = 'superseded', superseded_by = ? where id = ? and gate_status = 'active'",
                (gate_id, previous_gate["id"]),
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
        cycle = current_cycle_row(conn)
        if cycle["status"] not in {"active", "delivered"}:
            raise HarnessError(f"delivery record requires active current cycle, current={cycle['status']}")
        if project["phase"] not in {"delivery_readiness", "retrospective"}:
            raise HarnessError(f"delivery record requires phase delivery_readiness or retrospective, current={project['phase']}")
        issues = validate_delivery(conn, root)
        if issues:
            raise HarnessError("delivery record blocked: " + "; ".join(issues))
        delivery_id = str(uuid.uuid4())
        candidate_sha = current_candidate_sha(root)
        conn.execute(
            """
            insert into deliveries
            (id, cycle_id, candidate_sha, scope, acceptance, changed_files, validation, qa, failure_mode_coverage, quality_gate,
             data_config_notes, collaboration_links, known_gaps, handoff, created_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                delivery_id,
                cycle["id"],
                candidate_sha,
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
            if not conn.execute(
                "select id from acceptance where cycle_id = ? and id = ?",
                (cycle["id"], acceptance_id),
            ).fetchone():
                continue
            conn.execute(
                "insert or ignore into delivery_acceptance (delivery_id, cycle_id, acceptance_id) values (?, ?, ?)",
                (delivery_id, cycle["id"], acceptance_id),
            )
        after = conn.execute("select * from deliveries where id = ?", (delivery_id,)).fetchone()
        now = now_iso()
        conn.execute(
            "update delivery_cycles set status = 'delivered', candidate_sha = ?, closed_at = coalesce(nullif(closed_at, ''), ?), updated_at = ? where id = ?",
            (candidate_sha, now, now, cycle["id"]),
        )
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


CONNECTOR_OPERATIONS = {
    "github": {"github.issue.create", "github.issue.comment", "github.pr.create", "github.probe"},
    "linear": {"linear.issue.create", "linear.issue.comment", "linear.issue.update", "linear.probe"},
    "notion": {"notion.page.create", "notion.page.update", "notion.probe"},
    "figma": {"figma.comment.create", "figma.probe"},
    "slack": {"slack.message.post", "slack.probe"},
}
PROBE_OPERATIONS = {"github.probe", "linear.probe", "notion.probe", "figma.probe", "slack.probe"}
WRITE_CONNECTOR_MODES = {"write-confirm", "write-auto"}
DEFAULT_CONNECTOR_MAX_ATTEMPTS = 3
RETRYABLE_CONNECTOR_CODES = {403, 429, 500, 502, 503, 504, 529}
_CONNECTOR_THROTTLE_LAST: dict[str, float] = {}
CONNECTOR_PROFILE_FIELDS = {
    "github": {"repo"},
    "linear": {"team_id", "project_id"},
    "notion": {"parent_page_id", "page_id"},
    "figma": {"file_key"},
    "slack": {"channel"},
}


def connector_project_marker(project_key: str) -> str:
    return f"codex-project-harness:project-key={project_key}"


def connector_idempotency_marker(idempotency_key: str) -> str:
    return f"codex-project-harness:idempotency-key={idempotency_key}"


def connector_marker(idempotency_key: str, project_key: str = "") -> str:
    if project_key:
        return f"{connector_project_marker(project_key)}\n{connector_idempotency_marker(idempotency_key)}"
    return connector_idempotency_marker(idempotency_key)


def with_connector_marker(text: str, idempotency_key: str, project_key: str = "") -> str:
    marker = connector_marker(idempotency_key, project_key)
    value = text or ""
    if marker in value:
        return value
    return f"{value}\n\n{marker}".strip()


def require_param(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if value is None or str(value) == "":
        raise HarnessError(f"connector payload missing param: {key}")
    return str(value)


def connector_scope_key(tool: str, operation: str, params: dict[str, Any], project_key: str = "") -> str:
    if tool == "github":
        scope = str(params.get("repo", ""))
    elif tool == "slack":
        scope = str(params.get("channel", ""))
    elif tool == "figma":
        scope = str(params.get("file_key", ""))
    elif tool == "notion":
        scope = str(params.get("page_id") or params.get("parent_page_id") or "")
    elif tool == "linear":
        scope = str(params.get("team_id") or params.get("project_id") or params.get("issue_id") or "")
    else:
        scope = operation
    return f"{project_key}:{scope}" if project_key else scope


def connector_stats(tool: str, operation: str, params: dict[str, Any], project_key: str = "") -> ConnectorStats:
    return ConnectorStats(tool=tool, operation=operation, scope_key=connector_scope_key(tool, operation, params, project_key))


def connector_budget_id(stats: ConnectorStats) -> str:
    digest = hashlib.sha256(f"{stats.tool}:{stats.operation}:{stats.scope_key}".encode("utf-8")).hexdigest()[:16]
    return f"connector-budget:{digest}"


def connector_max_attempts() -> int:
    try:
        value = int(os.environ.get("HARNESS_CONNECTOR_MAX_ATTEMPTS", str(DEFAULT_CONNECTOR_MAX_ATTEMPTS)))
    except ValueError:
        value = DEFAULT_CONNECTOR_MAX_ATTEMPTS
    return max(1, min(value, 10))


def parse_scope_json(value: str) -> dict[str, Any]:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError as exc:
        raise HarnessError(f"connector profile scope invalid JSON: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise HarnessError("connector profile scope must be a JSON object")
    return data


def connector_profile_issues_for_row(tool: str, project_key: str, status: str, scope_json: str) -> list[str]:
    issues: list[str] = []
    if tool not in CONNECTOR_PROFILE_FIELDS:
        issues.append(f"invalid connector profile tool: {tool}")
    if status not in CONNECTOR_PROFILE_STATUSES:
        issues.append(f"invalid connector profile status: {status}")
    if project_key != slugify_project_key(project_key):
        issues.append(f"invalid connector profile project_key: {project_key}")
    try:
        scope = parse_scope_json(scope_json)
    except HarnessError as exc:
        return [str(exc)]
    allowed = CONNECTOR_PROFILE_FIELDS.get(tool, set())
    for key, value in scope.items():
        if key not in allowed:
            issues.append(f"connector profile {tool} uses unsupported scope field: {key}")
        if not isinstance(value, str):
            issues.append(f"connector profile {tool}.{key} must be a string")
    return issues


def connector_profile_status(root: Path) -> dict[str, Any]:
    with transaction(root, validate_invariants=False) as conn:
        create_schema(conn)
        initialize_project(conn)
        project_key = ensure_connector_project_key(conn, root)
        rows = conn.execute("select tool, project_key, status, scope_json, updated_at from connector_profiles order by tool").fetchall()
    profiles = {
        tool: {"tool": tool, "project_key": project_key, "status": "unbound", "scope": {}, "updated_at": ""}
        for tool in sorted(CONNECTOR_PROFILE_FIELDS)
    }
    for row in rows:
        profiles[row["tool"]] = {
            "tool": row["tool"],
            "project_key": row["project_key"],
            "status": row["status"],
            "scope": parse_scope_json(row["scope_json"]),
            "updated_at": row["updated_at"],
        }
    return {"project_key": project_key, "profiles": profiles}


def set_connector_profile(root: Path, *, project_key: str, scopes: dict[str, dict[str, str]]) -> None:
    normalized_project_key = slugify_project_key(project_key)
    if not normalized_project_key:
        raise HarnessError("connector profile requires project key")
    now = now_iso()
    with transaction(root) as conn:
        create_schema(conn)
        initialize_project(conn)
        conn.execute("update project set connector_project_key = ?, updated_at = ? where id = 1", (normalized_project_key, now))
        for tool, scope in scopes.items():
            scope = {key: value for key, value in scope.items() if value}
            if not scope:
                continue
            scope_json = json.dumps(scope, ensure_ascii=False, sort_keys=True)
            issues = connector_profile_issues_for_row(tool, normalized_project_key, "bound", scope_json)
            if issues:
                raise HarnessError("; ".join(issues))
            conn.execute(
                """
                insert into connector_profiles (id, tool, project_key, status, scope_json, created_at, updated_at)
                values (?, ?, ?, 'bound', ?, ?, ?)
                on conflict(tool) do update set project_key=excluded.project_key, status='bound',
                  scope_json=excluded.scope_json, updated_at=excluded.updated_at
                """,
                (str(uuid.uuid4()), tool, normalized_project_key, scope_json, now, now),
            )
        emit_event(conn, "connector_profile_updated", payload(project_key=normalized_project_key, tools=sorted(scopes)), idempotency_key=f"connector-profile:{normalized_project_key}:{now}")
    render_all(root)


def unset_connector_profile(root: Path, tool: str) -> None:
    if tool not in CONNECTOR_PROFILE_FIELDS:
        raise HarnessError(f"unsupported connector profile tool: {tool}")
    now = now_iso()
    with transaction(root) as conn:
        create_schema(conn)
        initialize_project(conn)
        project_key = ensure_connector_project_key(conn, root)
        conn.execute(
            """
            insert into connector_profiles (id, tool, project_key, status, scope_json, created_at, updated_at)
            values (?, ?, ?, 'disabled', '{}', ?, ?)
            on conflict(tool) do update set project_key=excluded.project_key, status='disabled',
              scope_json='{}', updated_at=excluded.updated_at
            """,
            (str(uuid.uuid4()), tool, project_key, now, now),
        )
        emit_event(conn, "connector_profile_updated", payload(project_key=project_key, tool=tool, status="disabled"), idempotency_key=f"connector-profile:{project_key}:{tool}:disabled:{now}")
    render_all(root)


def connector_profile_for_tool(conn: sqlite3.Connection, root: Path, tool: str) -> tuple[str, sqlite3.Row | None, dict[str, Any]]:
    project_key = ensure_connector_project_key(conn, root)
    row = conn.execute("select * from connector_profiles where tool = ?", (tool,)).fetchone()
    if not row:
        return project_key, None, {}
    return project_key, row, parse_scope_json(row["scope_json"])


def parse_retry_after(value: str) -> int:
    value = value.strip()
    if not value:
        return 0
    if value.isdigit():
        return max(0, int(value))
    try:
        parsed = datetime.strptime(value, "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=timezone.utc)
    except ValueError:
        return 0
    return max(0, int((parsed - datetime.now(timezone.utc)).total_seconds()))


def iso_after_seconds(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=max(0, seconds))).replace(microsecond=0).isoformat()


def rate_limit_reset_iso(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).replace(microsecond=0).isoformat()
    except ValueError:
        return ""


def retry_delay_seconds(stats: ConnectorStats, attempt_index: int, retry_after: int) -> int:
    if retry_after > 0:
        return retry_after
    seed = int(hashlib.sha256(f"{stats.tool}:{stats.operation}:{attempt_index}".encode("utf-8")).hexdigest()[:2], 16)
    jitter = seed % 2
    return min(30, (2 ** attempt_index) + jitter)


def maybe_sleep_connector(delay_seconds: int) -> None:
    try:
        cap = float(os.environ.get("HARNESS_CONNECTOR_RETRY_SLEEP", "1"))
    except ValueError:
        cap = 1.0
    if cap <= 0:
        return
    time.sleep(min(float(delay_seconds), cap))


def throttle_connector(stats: ConnectorStats) -> None:
    interval = 0.0
    scope = stats.scope_key
    if stats.tool == "notion":
        interval = 0.5
        scope = "global"
    elif stats.operation == "slack.message.post" and stats.scope_key:
        interval = 1.0
    if interval <= 0:
        return
    key = f"{stats.tool}:{stats.operation}:{scope}"
    now = time.monotonic()
    previous = _CONNECTOR_THROTTLE_LAST.get(key)
    _CONNECTOR_THROTTLE_LAST[key] = now
    if previous is None:
        return
    wait = interval - (now - previous)
    if wait > 0:
        maybe_sleep_connector(max(0, int(wait + 0.999)))


def note_connector_headers(stats: ConnectorStats, headers: Any) -> None:
    get = headers.get if hasattr(headers, "get") else lambda _key, _default=None: None
    retry_after = parse_retry_after(str(get("Retry-After", "") or get("retry-after", "") or ""))
    if retry_after:
        stats.retry_after_at = iso_after_seconds(retry_after)
    remaining = str(get("x-ratelimit-remaining", "") or get("X-RateLimit-Remaining", "") or "")
    if remaining:
        try:
            stats.rate_limit_remaining = int(remaining)
        except ValueError:
            pass
    reset = str(get("x-ratelimit-reset", "") or get("X-RateLimit-Reset", "") or "")
    if reset:
        stats.rate_limit_reset_at = rate_limit_reset_iso(reset)
    figma_bits = []
    for header in ["x-figma-plan-tier", "x-figma-rate-limit-type", "x-figma-upgrade-link"]:
        value = str(get(header, "") or get(header.title(), "") or "")
        if value:
            figma_bits.append(f"{header}={value}")
    if figma_bits:
        stats.free_plan_risk = "; ".join(figma_bits)


def note_gh_rate_limit(stderr: str, stats: ConnectorStats) -> int:
    retry_after = 0
    match = re.search(r"retry-after[:=]\s*(\d+)", stderr, re.IGNORECASE)
    if match:
        retry_after = int(match.group(1))
        stats.retry_after_at = iso_after_seconds(retry_after)
    remaining = re.search(r"x-ratelimit-remaining[:=]\s*(\d+)", stderr, re.IGNORECASE)
    if remaining:
        stats.rate_limit_remaining = int(remaining.group(1))
    reset = re.search(r"x-ratelimit-reset[:=]\s*(\d+)", stderr, re.IGNORECASE)
    if reset:
        stats.rate_limit_reset_at = rate_limit_reset_iso(reset.group(1))
    return retry_after


def is_retryable_connector_status(status_code: int, detail: str = "") -> bool:
    if status_code in (RETRYABLE_CONNECTOR_CODES - {403}):
        return True
    if status_code == 403 and ("rate limit" in detail.lower() or "secondary rate" in detail.lower()):
        return True
    return False


def upsert_connector_budget(conn: sqlite3.Connection, stats: ConnectorStats) -> None:
    conn.execute(
        """
        insert into connector_budgets
        (id, tool, operation, scope_key, status, retry_after_at, rate_limit_remaining, rate_limit_reset_at,
         last_status_code, last_error, free_plan_risk, updated_at)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(tool, operation, scope_key) do update set status=excluded.status,
          retry_after_at=excluded.retry_after_at, rate_limit_remaining=excluded.rate_limit_remaining,
          rate_limit_reset_at=excluded.rate_limit_reset_at, last_status_code=excluded.last_status_code,
          last_error=excluded.last_error, free_plan_risk=excluded.free_plan_risk, updated_at=excluded.updated_at
        """,
        (
            connector_budget_id(stats),
            stats.tool,
            stats.operation,
            stats.scope_key,
            stats.status,
            stats.retry_after_at,
            stats.rate_limit_remaining,
            stats.rate_limit_reset_at,
            stats.last_status_code,
            stats.last_error,
            stats.free_plan_risk,
            now_iso(),
        ),
    )


def advisory_fallback_id(action_id: str) -> str:
    return f"advisory-fallback:{action_id}"


def sanitize_advisory_text(value: str) -> str:
    text = str(value or "").strip()
    replacements = [
        (r"HARNESS_CONNECTOR_KEY", "connector credential"),
        (r"[A-Z0-9_]*TOKEN[A-Z0-9_]*", "connector credential"),
        (r"[A-Z0-9_]*API_KEY[A-Z0-9_]*", "connector credential"),
        (r"token", "credential"),
        (r"key", "credential"),
    ]
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text[:1000] or "connector unavailable"


def fallback_policy(tool: str, operation: str) -> tuple[str, str, str, str]:
    if tool == "github":
        return (
            "GitHub draft",
            "GitHub plugin or gh CLI when available",
            "Prepare a PR/issue/comment draft, review checklist, and local delivery record.",
            "Use this as a copy-ready GitHub issue, PR, or review comment body once GitHub is available.",
        )
    if tool == "linear":
        return (
            "Linear task fallback",
            "Linear plugin task workflow when available",
            "Prepare task breakdown, priority, dependency, and risk notes for .ai-team/planning/task-board.md.",
            "Use this as a copy-ready Linear issue/update draft once Linear is available.",
        )
    if tool == "notion":
        return (
            "Notion document fallback",
            "Documents or Notion documentation workflow when available",
            "Prepare a structured PRD/spec/ADR/handoff draft under docs/harness/.",
            "Use this as a copy-ready Notion page/update draft once Notion is available.",
        )
    if tool == "figma":
        return (
            "Product Design fallback",
            "Product Design plugin for design brief, audit, and visual QA",
            "Prepare a Product Design brief, audit prompt, and visual QA checklist without claiming a Figma file/comment exists.",
            "Use this as a copy-ready Product Design or Figma comment brief once Figma is available.",
        )
    if tool == "slack":
        return (
            "Slack handoff fallback",
            "Slack plugin for post-ready summaries when available",
            "Prepare a post-ready summary and local handoff note without claiming it was sent.",
            "Use this as a copy-ready Slack message, email, or document handoff once Slack is available.",
        )
    return (
        "Connector advisory fallback",
        "Official connector workflow when available",
        "Prepare a local advisory draft while the external connector is unavailable.",
        "Use this as a copy-ready external update once the connector is available.",
    )


def fallback_draft(tool: str, operation: str, params: dict[str, Any], action_row: sqlite3.Row) -> str:
    title = str(params.get("title") or params.get("summary") or action_row["artifact"] or operation)
    body = str(params.get("body") or params.get("description") or params.get("content") or params.get("message") or params.get("text") or "")
    if tool == "github":
        return "\n".join(
            [
                f"Title: {title}",
                "",
                body or "Describe the change, verification, and reviewer asks here.",
                "",
                "Review checklist:",
                "- Link the local delivery record.",
                "- Confirm controller verification evidence before requesting merge.",
                "- Keep this draft separate from delivery evidence.",
            ]
        )
    if tool == "linear":
        return "\n".join(
            [
                f"Task: {title}",
                f"Priority: {params.get('priority', 'needs triage')}",
                f"Dependency: {params.get('depends_on', 'none recorded')}",
                f"Risk: {params.get('risk', 'needs review')}",
                "",
                body or "Break the task into acceptance, implementation, verification, and handoff steps.",
                "",
                "Local board target: .ai-team/planning/task-board.md",
            ]
        )
    if tool == "notion":
        return "\n".join(
            [
                f"# {title}",
                "",
                "## Context",
                body or "Capture the spec, decision, or handoff context here.",
                "",
                "## Acceptance",
                "- Link local harness acceptance criteria.",
                "- Link controller verification once available.",
                "",
                "## Decision / Handoff",
                "- Note owners, risks, and next steps.",
            ]
        )
    if tool == "figma":
        return "\n".join(
            [
                f"Design brief: {title}",
                "",
                body or "Describe the screen, state, or visual decision to review.",
                "",
                "Audit prompt:",
                "- Check hierarchy, interaction states, spacing, accessibility, and copy clarity.",
                "- Compare against implemented UI and acceptance criteria.",
                "",
                "Visual QA checklist:",
                "- No claim is made that a Figma comment or file was created.",
            ]
        )
    if tool == "slack":
        return "\n".join(
            [
                f"Subject: {title}",
                "",
                body or "Summarize status, blockers, verification, and asks.",
                "",
                "Handoff note:",
                "- Ready to paste into Slack when available.",
                "- This local note does not mean a message was sent.",
            ]
        )
    return body or f"Local advisory draft for {operation}."


def render_advisory_fallback_artifact(root: Path, fallback: dict[str, Any], action_row: sqlite3.Row, params: dict[str, Any]) -> None:
    artifact = root / str(fallback["artifact_path"])
    ensure_parent(artifact)
    reason = sanitize_advisory_text(str(fallback["source_status"]))
    draft = fallback_draft(str(fallback["tool"]), str(fallback["operation"]), params, action_row)
    lines = [
        f"# Advisory Fallback: {fallback['operation']}",
        "",
        "Not delivery evidence.",
        "",
        f"- Action: `{fallback['action_id']}`",
        f"- Connector: `{fallback['tool']}`",
        f"- Operation: `{fallback['operation']}`",
        f"- Fallback kind: {fallback['fallback_kind']}",
        f"- Official capability: {fallback['official_capability']}",
        f"- Source status: {reason}",
        "",
        "## Why This Exists",
        "",
        "The external connector is blocked or unavailable. This local artifact is an advisory draft so people and agents can continue planning, handoff, and review work without pretending an external write happened.",
        "",
        "## Local Guidance",
        "",
        str(fallback["summary"]),
        "",
        "## Copy-ready draft",
        "",
        draft,
        "",
        "## Boundary",
        "",
        "This artifact is advisory only. It cannot satisfy controller verification, HMAC or session attestation, integration hardening, or the delivery gate.",
    ]
    artifact.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def upsert_advisory_fallback(conn: sqlite3.Connection, root: Path, action_row: sqlite3.Row, stats: ConnectorStats, reason: str) -> dict[str, Any]:
    payload_value = load_connector_payload(action_row)
    operation = str(payload_value.get("operation") or stats.operation or action_row["action"])
    params = payload_value.get("params", {})
    if not isinstance(params, dict):
        params = {}
    scope_key = stats.scope_key or connector_scope_key(str(action_row["tool"]), operation, params)
    fallback_kind, official_capability, summary, _draft_hint = fallback_policy(str(action_row["tool"]), operation)
    now = now_iso()
    artifact_path = f"docs/harness/advisory-fallbacks/{action_row['id']}.md"
    row_id = advisory_fallback_id(str(action_row["id"]))
    source_status = sanitize_advisory_text(reason or stats.last_error)
    conn.execute(
        """
        insert into advisory_fallbacks
        (id, action_id, tool, operation, scope_key, source_status, fallback_kind, official_capability,
         artifact_path, summary, status, delivery_eligible, generated_at, updated_at)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'generated', 0, ?, ?)
        on conflict(action_id) do update set source_status=excluded.source_status,
          scope_key=excluded.scope_key, summary=excluded.summary, status='generated',
          delivery_eligible=0, updated_at=excluded.updated_at
        """,
        (
            row_id,
            action_row["id"],
            action_row["tool"],
            operation,
            scope_key,
            source_status,
            fallback_kind,
            official_capability,
            artifact_path,
            summary,
            now,
            now,
        ),
    )
    row = conn.execute("select * from advisory_fallbacks where action_id = ?", (action_row["id"],)).fetchone()
    data = row_snapshot(row) or {}
    render_advisory_fallback_artifact(root, data, action_row, params)
    return data


def mark_connector_blocked(root: Path, action_id: str, stats: ConnectorStats, reason: str) -> None:
    stats.status = "blocked"
    stats.last_error = reason[:1000]
    finding_id = f"connector:{action_id}:blocked"
    with transaction(root, validate_invariants=False) as conn:
        insert_active_command_log(conn)
        row = conn.execute("select * from adapter_actions where id = ?", (action_id,)).fetchone()
        if not row:
            raise HarnessError(f"missing adapter action: {action_id}")
        if row["status"] != "blocked":
            conn.execute(
                """
                update adapter_actions set status = 'blocked', connector_status = 'blocked',
                  blocked_reason = ?, attempt_count = ?, next_retry_at = ?, updated_at = ? where id = ?
                """,
                (stats.last_error, stats.attempt_count, stats.retry_after_at, now_iso(), action_id),
            )
            conn.execute(
                """
                insert into findings (id, surface, severity, status, summary, evidence_id, created_at)
                values (?, 'connector', 'medium', 'open', ?, '', ?)
                on conflict(id) do update set summary=excluded.summary, status=excluded.status, created_at=excluded.created_at
                """,
                (
                    finding_id,
                    f"Connector blocked for {stats.tool} {stats.operation}: {stats.last_error}. Local .ai-team fact source remains available.",
                    now_iso(),
                ),
            )
            emit_event(conn, "connector_action_blocked", payload(id=action_id, tool=stats.tool, operation=stats.operation, reason=stats.last_error), idempotency_key=row["idempotency_key"])
        fallback = upsert_advisory_fallback(conn, root, row, stats, reason)
        emit_event(
            conn,
            "advisory_fallback_generated",
            payload(id=fallback["id"], action_id=action_id, tool=fallback["tool"], operation=fallback["operation"]),
            idempotency_key=f"advisory-fallback:{row['idempotency_key']}",
        )
        upsert_connector_budget(conn, stats)
        req = _active_request.get()
        if req and req.get("request_id"):
            conn.execute(
                "update command_log set result_json = ? where request_id = ?",
                (f"ERROR: connector execution failed: {reason}", req["request_id"]),
            )
    render_tooling_map(root)
    render_all(root)


def load_connector_payload(row: sqlite3.Row) -> dict[str, Any]:
    expected_hash = adapter_action_payload_hash(
        str(row["tool"]),
        str(row["mode"]),
        str(row["artifact"]),
        str(row["action"]),
        str(row["payload_json"]),
    )
    stored_hash = str(row["payload_hash"] or "") if "payload_hash" in row.keys() else ""
    if stored_hash != expected_hash:
        raise HarnessError(f"adapter action payload hash mismatch: {row['id']}")
    try:
        payload_value = json.loads(row["payload_json"] or "{}")
    except json.JSONDecodeError as exc:
        raise HarnessError(f"connector payload invalid JSON: {exc.msg}") from exc
    if not isinstance(payload_value, dict):
        raise HarnessError("connector payload must be a JSON object")
    return payload_value


def should_execute_connector(row: sqlite3.Row) -> bool:
    return load_connector_payload(row).get("execute") is True


def validate_connector_request(row: sqlite3.Row, payload_value: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    operation = payload_value.get("operation")
    params = payload_value.get("params", {})
    if not isinstance(operation, str) or not operation:
        raise HarnessError("connector payload missing operation")
    if not isinstance(params, dict):
        raise HarnessError("connector payload params must be an object")
    tool = str(row["tool"])
    mode = str(row["mode"])
    allowed = CONNECTOR_OPERATIONS.get(tool)
    if not allowed:
        raise HarnessError(f"unsupported connector tool: {tool}")
    if operation not in allowed:
        raise HarnessError(f"connector operation does not match tool: {operation}")
    if mode == "disabled":
        raise HarnessError("connector tool is disabled")
    if operation in PROBE_OPERATIONS:
        return operation, params
    if mode not in WRITE_CONNECTOR_MODES:
        raise HarnessError(f"connector write requires write-confirm or write-auto mode: {mode}")
    return operation, params


def connector_scope_mismatch(tool: str, operation: str, params: dict[str, Any], scope: dict[str, Any]) -> str:
    if tool == "github":
        expected = str(scope.get("repo", ""))
        actual = str(params.get("repo", ""))
        return "" if expected and actual == expected else f"github repo mismatch: expected {expected or '<unbound>'}, got {actual or '<missing>'}"
    if tool == "slack":
        expected = str(scope.get("channel", ""))
        actual = str(params.get("channel", ""))
        return "" if expected and actual == expected else f"slack channel mismatch: expected {expected or '<unbound>'}, got {actual or '<missing>'}"
    if tool == "figma":
        expected = str(scope.get("file_key", ""))
        actual = str(params.get("file_key", ""))
        return "" if expected and actual == expected else f"figma file mismatch: expected {expected or '<unbound>'}, got {actual or '<missing>'}"
    if tool == "notion":
        if operation == "notion.page.create":
            expected = str(scope.get("parent_page_id", ""))
            actual = str(params.get("parent_page_id", ""))
            return "" if expected and actual == expected else f"notion parent mismatch: expected {expected or '<unbound>'}, got {actual or '<missing>'}"
        expected = str(scope.get("page_id") or scope.get("parent_page_id") or "")
        actual = str(params.get("page_id") or params.get("parent_page_id") or "")
        return "" if expected and actual == expected else f"notion page mismatch: expected {expected or '<unbound>'}, got {actual or '<missing>'}"
    if tool == "linear":
        expected_team = str(scope.get("team_id", ""))
        expected_project = str(scope.get("project_id", ""))
        actual_team = str(params.get("team_id", ""))
        actual_project = str(params.get("project_id", ""))
        if operation in {"linear.issue.comment", "linear.issue.update"}:
            return ""
        if expected_team and actual_team == expected_team:
            return ""
        if expected_project and actual_project == expected_project:
            return ""
        expected = expected_team or expected_project or "<unbound>"
        actual = actual_team or actual_project or str(params.get("issue_id", "")) or "<missing>"
        return f"linear scope mismatch: expected {expected}, got {actual}"
    return ""


def linear_issue_scope_mismatch(
    operation: str,
    params: dict[str, Any],
    scope: dict[str, Any],
    project_key: str,
) -> str:
    stats = connector_stats("linear", operation, params, project_key)
    token = env_token("LINEAR_API_KEY", stats)
    endpoint = f"{os.environ['HARNESS_LINEAR_API_URL'].rstrip('/')}/linear/graphql" if os.environ.get("HARNESS_LINEAR_API_URL") else "https://api.linear.app/graphql"
    data = http_json(
        endpoint,
        token,
        {
            "query": "query IssueScope($id: String!) { issue(id: $id) { id team { id } project { id } } }",
            "variables": {"id": require_param(params, "issue_id")},
        },
        stats=stats,
    )
    issue = data.get("data", {}).get("issue")
    if not isinstance(issue, dict) or not issue.get("id"):
        return "linear issue scope could not be confirmed"
    team = issue.get("team")
    project = issue.get("project")
    actual_team = str(team.get("id", "")) if isinstance(team, dict) else ""
    actual_project = str(project.get("id", "")) if isinstance(project, dict) else ""
    expected_team = str(scope.get("team_id", ""))
    expected_project = str(scope.get("project_id", ""))
    mismatches: list[str] = []
    if expected_team and actual_team != expected_team:
        mismatches.append(f"team expected {expected_team}, got {actual_team or '<missing>'}")
    if expected_project and actual_project != expected_project:
        mismatches.append(f"project expected {expected_project}, got {actual_project or '<missing>'}")
    if not expected_team and not expected_project:
        mismatches.append("linear profile has no team or project scope")
    return "; ".join(mismatches)


def audit_connector_scope_override(root: Path, row: sqlite3.Row, project_key: str, reason: str) -> None:
    with transaction(root) as conn:
        finding_id = f"connector-scope-override:{row['id']}"
        conn.execute(
            """
            insert into findings (id, surface, severity, status, summary, evidence_id, created_at)
            values (?, 'connector', 'medium', 'open', ?, '', ?)
            on conflict(id) do update set summary=excluded.summary, created_at=excluded.created_at
            """,
            (finding_id, f"connector scope override for project {project_key}: {reason}", now_iso()),
        )
        emit_event(conn, "connector_scope_override", payload(id=row["id"], tool=row["tool"], project_key=project_key, reason=reason), idempotency_key=f"connector-scope-override:{row['id']}")


def validate_connector_namespace(root: Path, row: sqlite3.Row, payload_value: dict[str, Any], operation: str, params: dict[str, Any]) -> str:
    if operation in PROBE_OPERATIONS:
        with transaction(root, validate_invariants=False) as conn:
            create_schema(conn)
            initialize_project(conn)
            return ensure_connector_project_key(conn, root)
    tool = str(row["tool"])
    with transaction(root, validate_invariants=False) as conn:
        create_schema(conn)
        initialize_project(conn)
        project_key, profile, scope = connector_profile_for_tool(conn, root, tool)
    if not profile:
        raise HarnessError(f"connector profile missing for {tool}; run connector profile set before external writes")
    if profile["status"] != "bound":
        raise HarnessError(f"connector profile is {profile['status']} for {tool}")
    mismatch = connector_scope_mismatch(tool, operation, params, scope)
    if not mismatch and tool == "linear" and operation in {"linear.issue.comment", "linear.issue.update"}:
        mismatch = linear_issue_scope_mismatch(operation, params, scope, project_key)
    if not mismatch:
        return project_key
    if payload_value.get("scope_override") is True:
        if row["mode"] != "write-confirm":
            raise HarnessError("connector scope_override requires write-confirm mode")
        audit_connector_scope_override(root, row, project_key, mismatch)
        return project_key
    audit_connector_scope_override(root, row, project_key, f"blocked mismatch: {mismatch}")
    raise HarnessError(f"connector scope mismatch: {mismatch}")


def infer_github_repo(root: Path) -> str:
    remote = subprocess.run(["git", "remote", "get-url", "origin"], cwd=root, text=True, capture_output=True, check=False)
    if remote.returncode != 0:
        raise HarnessError("github connector requires params.repo or git remote origin")
    value = remote.stdout.strip()
    if value.startswith("git@github.com:"):
        value = value.removeprefix("git@github.com:")
    elif "github.com/" in value:
        value = value.split("github.com/", 1)[1]
    value = value.removesuffix(".git").strip("/")
    if "/" not in value:
        raise HarnessError("could not infer GitHub repo from origin remote")
    return value


def run_gh_api(root: Path, endpoint: str, fields: dict[str, str], stats: ConnectorStats) -> dict[str, Any]:
    gh_override = os.environ.get("HARNESS_GH_BIN", "").strip()
    gh_command = shlex.split(gh_override, posix=(os.name != "nt")) if gh_override else ["gh"]
    if not gh_override and not shutil.which("gh"):
        raise ConnectorFailure("github connector requires gh CLI", stats)
    command = [*gh_command, "api", endpoint]
    for key, value in fields.items():
        command.extend(["-f", f"{key}={value}"])
    max_attempts = connector_max_attempts()
    for attempt_index in range(max_attempts):
        throttle_connector(stats)
        stats.attempt_count += 1
        try:
            result = subprocess.run(command, cwd=root, text=True, capture_output=True, check=False)
        except OSError as exc:
            stats.status = "blocked"
            stats.last_status_code = 500
            stats.last_error = str(exc)[:1000]
            raise ConnectorFailure(f"github connector failed to start: {exc}", stats) from exc
        if result.returncode == 0:
            stats.status = "available"
            stats.last_status_code = 200
            stats.last_error = ""
            try:
                data = json.loads(result.stdout or "{}")
            except json.JSONDecodeError as exc:
                raise ConnectorFailure(f"github connector returned invalid JSON: {exc.msg}", stats) from exc
            if not isinstance(data, dict):
                raise ConnectorFailure("github connector returned non-object JSON", stats)
            return data
        detail = result.stderr.strip() or result.stdout.strip() or str(result.returncode)
        stats.last_error = detail[:1000]
        retry_after = note_gh_rate_limit(detail, stats)
        stats.last_status_code = 429 if "429" in detail or "rate limit" in detail.lower() else 500
        retryable = "rate limit" in detail.lower() or "secondary rate" in detail.lower()
        if retryable and attempt_index < max_attempts - 1:
            stats.status = "degraded"
            maybe_sleep_connector(retry_delay_seconds(stats, attempt_index, retry_after))
            continue
        stats.status = "blocked"
        raise ConnectorFailure(f"github connector failed: {detail}", stats)
    stats.status = "blocked"
    raise ConnectorFailure("github connector failed: retry budget exhausted", stats)


def execute_github_connector(root: Path, operation: str, params: dict[str, Any], idempotency_key: str, project_key: str = "") -> ConnectorOutcome:
    stats = connector_stats("github", operation, params, project_key)
    if operation == "github.probe":
        data = run_gh_api(root, "user", {}, stats)
        login = data.get("login") or data.get("viewer", {}).get("login") or "ok"
        return ConnectorOutcome(f"github:probe:{login}", "", stats)
    repo = str(params.get("repo") or infer_github_repo(root))
    params = {**params, "repo": repo}
    stats.scope_key = connector_scope_key("github", operation, params, project_key)
    existing = find_existing_connector_object(root, operation, params, idempotency_key, stats, project_key)
    if existing:
        return ConnectorOutcome(existing[0], existing[1], stats)
    if operation == "github.issue.create":
        data = run_gh_api(
            root,
            f"repos/{repo}/issues",
            {"title": require_param(params, "title"), "body": with_connector_marker(str(params.get("body", "")), idempotency_key, project_key)},
            stats,
        )
        return ConnectorOutcome(f"github:issue:{data.get('number') or data.get('id')}", str(data.get("html_url", "")), stats)
    if operation == "github.issue.comment":
        issue_number = require_param(params, "issue_number")
        data = run_gh_api(root, f"repos/{repo}/issues/{issue_number}/comments", {"body": with_connector_marker(require_param(params, "body"), idempotency_key, project_key)}, stats)
        return ConnectorOutcome(f"github:comment:{data.get('id')}", str(data.get("html_url", "")), stats)
    if operation == "github.pr.create":
        fields = {
            "title": require_param(params, "title"),
            "body": with_connector_marker(str(params.get("body", "")), idempotency_key, project_key),
            "head": require_param(params, "head"),
            "base": require_param(params, "base"),
        }
        data = run_gh_api(root, f"repos/{repo}/pulls", fields, stats)
        return ConnectorOutcome(f"github:pr:{data.get('number') or data.get('id')}", str(data.get("html_url", "")), stats)
    raise ConnectorFailure(f"unsupported github connector operation: {operation}", stats)


def http_json(url: str, token: str, body: dict[str, Any] | None, *, stats: ConnectorStats, method: str = "POST", headers: dict[str, str] | None = None) -> dict[str, Any]:
    request_headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}", **(headers or {})}
    data_bytes = None if method == "GET" else json.dumps(body or {}).encode("utf-8")
    max_attempts = connector_max_attempts()
    for attempt_index in range(max_attempts):
        throttle_connector(stats)
        stats.attempt_count += 1
        request = urllib.request.Request(url, data=data_bytes, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - connector URL is explicit runtime configuration.
                stats.last_status_code = int(getattr(response, "status", 200))
                note_connector_headers(stats, response.headers)
                data = json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            stats.last_status_code = exc.code
            stats.last_error = f"{exc.code} {detail}"[:1000]
            note_connector_headers(stats, exc.headers)
            retry_after = parse_retry_after(str(exc.headers.get("Retry-After", "") if exc.headers else ""))
            if is_retryable_connector_status(exc.code, detail) and attempt_index < max_attempts - 1:
                stats.status = "degraded"
                maybe_sleep_connector(retry_delay_seconds(stats, attempt_index, retry_after))
                continue
            stats.status = "blocked"
            raise ConnectorFailure(f"connector HTTP failed: {exc.code} {detail}", stats) from exc
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            stats.last_error = str(reason)[:1000]
            if method != "GET":
                stats.status = "degraded"
                raise ConnectorFailure(f"connector HTTP failed: {reason}", stats, ambiguous=True) from exc
            if attempt_index < max_attempts - 1:
                stats.status = "degraded"
                maybe_sleep_connector(retry_delay_seconds(stats, attempt_index, 0))
                continue
            stats.status = "blocked"
            raise ConnectorFailure(f"connector HTTP failed: {reason}", stats) from exc
        except json.JSONDecodeError as exc:
            raise ConnectorFailure(f"connector returned invalid JSON: {exc.msg}", stats, ambiguous=method != "GET") from exc
        if not isinstance(data, dict):
            raise ConnectorFailure("connector returned non-object JSON", stats)
        stats.status = "available"
        stats.last_error = ""
        return data
    stats.status = "blocked"
    raise ConnectorFailure("connector HTTP failed: retry budget exhausted", stats)


def env_token(name: str, stats: ConnectorStats) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ConnectorFailure(f"connector token missing: {name}", stats)
    return value


def first_search_result(data: dict[str, Any]) -> dict[str, Any] | None:
    results = data.get("results")
    if isinstance(results, list) and results and isinstance(results[0], dict):
        return results[0]
    items = data.get("items")
    if isinstance(items, list) and items and isinstance(items[0], dict):
        return items[0]
    nodes = data.get("data", {}).get("search", {}).get("nodes")
    if isinstance(nodes, list) and nodes and isinstance(nodes[0], dict):
        return nodes[0]
    matches = data.get("messages", {}).get("matches")
    if isinstance(matches, list) and matches and isinstance(matches[0], dict):
        return matches[0]
    comments = data.get("comments")
    if isinstance(comments, list):
        for comment in comments:
            if isinstance(comment, dict):
                return comment
    return None


def find_existing_connector_object(root: Path, operation: str, params: dict[str, Any], idempotency_key: str, stats: ConnectorStats, project_key: str = "") -> tuple[str, str] | None:
    marker = connector_marker(idempotency_key, project_key)
    id_marker = connector_idempotency_marker(idempotency_key)
    project_marker = connector_project_marker(project_key) if project_key else ""
    if operation == "github.issue.create":
        repo = require_param(params, "repo")
        query = f"{id_marker} {project_marker} repo:{repo} type:issue".strip()
        data = run_gh_api(root, "search/issues", {"q": query}, stats)
        item = first_search_result(data)
        if item:
            return f"github:issue:{item.get('number') or item.get('id')}", str(item.get("html_url", ""))
    if operation == "github.pr.create":
        repo = require_param(params, "repo")
        query = f"{id_marker} {project_marker} repo:{repo} type:pr".strip()
        data = run_gh_api(root, "search/issues", {"q": query}, stats)
        item = first_search_result(data)
        if item:
            return f"github:pr:{item.get('number') or item.get('id')}", str(item.get("html_url", ""))
    if operation.startswith("notion.page."):
        token = env_token("NOTION_TOKEN", stats)
        base = os.environ.get("HARNESS_NOTION_API_URL", "https://api.notion.com").rstrip("/")
        path = "/notion/v1/search" if os.environ.get("HARNESS_NOTION_API_URL") else "/v1/search"
        data = http_json(f"{base}{path}", token, {"query": marker, "page_size": 1}, stats=stats, headers={"Notion-Version": "2022-06-28"})
        item = first_search_result(data)
        if item:
            return f"notion:page:{item.get('id')}", str(item.get("url", ""))
    if operation == "figma.comment.create":
        token = env_token("FIGMA_TOKEN", stats)
        base = os.environ.get("HARNESS_FIGMA_API_URL", "https://api.figma.com").rstrip("/")
        file_key = require_param(params, "file_key")
        path = f"/figma/v1/files/{file_key}/comments" if os.environ.get("HARNESS_FIGMA_API_URL") else f"/v1/files/{file_key}/comments"
        try:
            data = http_json(f"{base}{path}", token, None, stats=stats, method="GET", headers={"X-Figma-Token": token})
        except ConnectorFailure as exc:
            if exc.stats.last_status_code in {404, 405, 501}:
                return None
            raise
        comments = data.get("comments")
        if isinstance(comments, list):
            for comment in comments:
                message = str(comment.get("message", ""))
                if isinstance(comment, dict) and id_marker in message and (not project_marker or project_marker in message):
                    return f"figma:comment:{comment.get('id')}", f"https://www.figma.com/file/{file_key}?comment-id={comment.get('id')}"
    if operation in {"linear.issue.create", "linear.issue.comment", "linear.issue.update"}:
        token = env_token("LINEAR_API_KEY", stats)
        endpoint = f"{os.environ['HARNESS_LINEAR_API_URL'].rstrip('/')}/linear/graphql" if os.environ.get("HARNESS_LINEAR_API_URL") else "https://api.linear.app/graphql"
        data = http_json(
            endpoint,
            token,
            {"query": "query Search($query: String!) { search(query: $query) { nodes { ... on Issue { id identifier url } ... on Comment { id url } } } }", "variables": {"query": marker}},
            stats=stats,
        )
        item = first_search_result(data)
        if item:
            if item.get("identifier"):
                return f"linear:issue:{item.get('identifier')}", str(item.get("url", ""))
            return f"linear:comment:{item.get('id')}", str(item.get("url", ""))
    if operation == "slack.message.post":
        token = env_token("SLACK_BOT_TOKEN", stats)
        base = os.environ.get("HARNESS_SLACK_API_URL", "https://slack.com").rstrip("/")
        path = "/slack/api/search.messages" if os.environ.get("HARNESS_SLACK_API_URL") else "/api/search.messages"
        try:
            data = http_json(f"{base}{path}", token, {"query": marker, "count": 1}, stats=stats)
        except ConnectorFailure as exc:
            if exc.stats.last_status_code in {429, 529}:
                raise
            return None
        if data.get("ok") is False:
            return None
        item = first_search_result(data)
        if item:
            channel = item.get("channel", {})
            channel_id = channel.get("id") if isinstance(channel, dict) else ""
            return f"slack:message:{channel_id or params.get('channel', '')}:{item.get('ts', '')}", str(item.get("permalink", ""))
    return None


def notion_payload_for_page_create(params: dict[str, Any], idempotency_key: str, project_key: str = "") -> dict[str, Any]:
    children = params.get("children")
    if children is None:
        children = [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": str(params.get("content", ""))}}]}}]
    if not isinstance(children, list):
        raise HarnessError("Notion payload children must be an array")
    marker = connector_marker(idempotency_key, project_key)
    marker_block = {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": [{"type": "text", "text": {"content": marker}}]},
    }
    return {
        "parent": {"page_id": require_param(params, "parent_page_id")},
        "properties": {"title": {"title": [{"text": {"content": with_connector_marker(require_param(params, "title"), idempotency_key, project_key)}}]}},
        "children": [*children, marker_block],
    }


def validate_notion_payload(payload_value: dict[str, Any], stats: ConnectorStats) -> None:
    body = json.dumps(payload_value, ensure_ascii=False, sort_keys=True)
    children = payload_value.get("children", [])
    if len(body.encode("utf-8")) >= 500 * 1024:
        raise ConnectorFailure("Notion payload exceeds 500KB connector limit; use local fallback/link instead", stats)
    if isinstance(children, list) and len(children) >= 1000:
        raise ConnectorFailure("Notion payload children exceeds 1000 block connector limit; use local fallback/link instead", stats)
    attachments = payload_value.get("attachments", [])
    if isinstance(attachments, list):
        for attachment in attachments:
            if isinstance(attachment, dict) and int(attachment.get("size_bytes", 0) or 0) > 5 * 1024 * 1024:
                raise ConnectorFailure("Notion attachment exceeds 5MB; record a link or local fallback path instead", stats)


def execute_linear_connector(operation: str, params: dict[str, Any], idempotency_key: str, project_key: str = "") -> ConnectorOutcome:
    stats = connector_stats("linear", operation, params, project_key)
    token = env_token("LINEAR_API_KEY", stats)
    endpoint = f"{os.environ['HARNESS_LINEAR_API_URL'].rstrip('/')}/linear/graphql" if os.environ.get("HARNESS_LINEAR_API_URL") else "https://api.linear.app/graphql"
    if operation == "linear.probe":
        data = http_json(endpoint, token, {"query": "query Viewer { viewer { id name } }"}, stats=stats)
        viewer = data.get("data", {}).get("viewer", {})
        return ConnectorOutcome(f"linear:probe:{viewer.get('id', 'ok')}", "", stats)
    existing = find_existing_connector_object(Path.cwd(), operation, params, idempotency_key, stats, project_key)
    if existing:
        return ConnectorOutcome(existing[0], existing[1], stats)
    if operation == "linear.issue.create":
        data = http_json(
            endpoint,
            token,
            {
                "query": "mutation IssueCreate($input: IssueCreateInput!) { issueCreate(input: $input) { success issue { id identifier url } } }",
                "variables": {"input": {"teamId": require_param(params, "team_id"), "title": require_param(params, "title"), "description": with_connector_marker(str(params.get("description", "")), idempotency_key, project_key)}},
            },
            stats=stats,
        )
        issue = data.get("data", {}).get("issueCreate", {}).get("issue", {})
        return ConnectorOutcome(f"linear:issue:{issue.get('identifier') or issue.get('id')}", str(issue.get("url", "")), stats)
    if operation == "linear.issue.comment":
        data = http_json(
            endpoint,
            token,
            {
                "query": "mutation CommentCreate($input: CommentCreateInput!) { commentCreate(input: $input) { success comment { id url } } }",
                "variables": {"input": {"issueId": require_param(params, "issue_id"), "body": with_connector_marker(require_param(params, "body"), idempotency_key, project_key)}},
            },
            stats=stats,
        )
        comment = data.get("data", {}).get("commentCreate", {}).get("comment", {})
        return ConnectorOutcome(f"linear:comment:{comment.get('id')}", str(comment.get("url", "")), stats)
    if operation == "linear.issue.update":
        update_input = {k: v for k, v in params.items() if k != "issue_id"}
        data = http_json(
            endpoint,
            token,
            {
                "query": "mutation IssueUpdate($id: String!, $input: IssueUpdateInput!) { issueUpdate(id: $id, input: $input) { success issue { id identifier url } } }",
                "variables": {"id": require_param(params, "issue_id"), "input": update_input},
            },
            stats=stats,
        )
        issue = data.get("data", {}).get("issueUpdate", {}).get("issue", {})
        return ConnectorOutcome(f"linear:issue:{issue.get('identifier') or issue.get('id')}", str(issue.get("url", "")), stats)
    raise ConnectorFailure(f"unsupported linear connector operation: {operation}", stats)


def execute_notion_connector(operation: str, params: dict[str, Any], idempotency_key: str, project_key: str = "") -> ConnectorOutcome:
    stats = connector_stats("notion", operation, params, project_key)
    token = env_token("NOTION_TOKEN", stats)
    base = os.environ.get("HARNESS_NOTION_API_URL", "https://api.notion.com").rstrip("/")
    headers = {"Notion-Version": "2022-06-28"}
    if operation == "notion.probe":
        path = "/notion/v1/users/me" if os.environ.get("HARNESS_NOTION_API_URL") else "/v1/users/me"
        data = http_json(f"{base}{path}", token, None, stats=stats, method="GET", headers=headers)
        return ConnectorOutcome(f"notion:probe:{data.get('id', 'ok')}", "", stats)
    if operation == "notion.page.create":
        path = "/notion/v1/pages" if os.environ.get("HARNESS_NOTION_API_URL") else "/v1/pages"
        body = notion_payload_for_page_create(params, idempotency_key, project_key)
        validate_notion_payload(body, stats)
        existing = find_existing_connector_object(Path.cwd(), operation, params, idempotency_key, stats, project_key)
        if existing:
            return ConnectorOutcome(existing[0], existing[1], stats)
        data = http_json(
            f"{base}{path}",
            token,
            body,
            stats=stats,
            headers=headers,
        )
        return ConnectorOutcome(f"notion:page:{data.get('id')}", str(data.get("url", "")), stats)
    if operation == "notion.page.update":
        page_id = require_param(params, "page_id")
        path = f"/notion/v1/pages/{page_id}" if os.environ.get("HARNESS_NOTION_API_URL") else f"/v1/pages/{page_id}"
        body = {"properties": {"title": {"title": [{"text": {"content": with_connector_marker(str(params.get("title", "")), idempotency_key, project_key)}}]}}}
        validate_notion_payload(body, stats)
        existing = find_existing_connector_object(Path.cwd(), operation, params, idempotency_key, stats, project_key)
        if existing:
            return ConnectorOutcome(existing[0], existing[1], stats)
        data = http_json(
            f"{base}{path}",
            token,
            body,
            stats=stats,
            method="PATCH",
            headers=headers,
        )
        return ConnectorOutcome(f"notion:page:{data.get('id')}", str(data.get("url", "")), stats)
    raise ConnectorFailure(f"unsupported notion connector operation: {operation}", stats)


def execute_figma_connector(operation: str, params: dict[str, Any], idempotency_key: str, project_key: str = "") -> ConnectorOutcome:
    stats = connector_stats("figma", operation, params, project_key)
    token = env_token("FIGMA_TOKEN", stats)
    base = os.environ.get("HARNESS_FIGMA_API_URL", "https://api.figma.com").rstrip("/")
    if operation == "figma.probe":
        file_key = str(params.get("file_key", ""))
        if file_key:
            path = f"/figma/v1/files/{file_key}" if os.environ.get("HARNESS_FIGMA_API_URL") else f"/v1/files/{file_key}"
            data = http_json(f"{base}{path}", token, None, stats=stats, method="GET", headers={"X-Figma-Token": token})
            return ConnectorOutcome(f"figma:probe:{data.get('key') or file_key}", "", stats)
        path = "/figma/v1/me" if os.environ.get("HARNESS_FIGMA_API_URL") else "/v1/me"
        data = http_json(f"{base}{path}", token, None, stats=stats, method="GET", headers={"X-Figma-Token": token})
        return ConnectorOutcome(f"figma:probe:{data.get('id', 'ok')}", "", stats)
    if operation == "figma.comment.create":
        file_key = require_param(params, "file_key")
        existing = find_existing_connector_object(Path.cwd(), operation, params, idempotency_key, stats, project_key)
        if existing:
            return ConnectorOutcome(existing[0], existing[1], stats)
        path = f"/figma/v1/files/{file_key}/comments" if os.environ.get("HARNESS_FIGMA_API_URL") else f"/v1/files/{file_key}/comments"
        data = http_json(
            f"{base}{path}",
            token,
            {"message": with_connector_marker(require_param(params, "message"), idempotency_key, project_key)},
            stats=stats,
            headers={"X-Figma-Token": token},
        )
        return ConnectorOutcome(f"figma:comment:{data.get('id')}", f"https://www.figma.com/file/{data.get('file_key') or file_key}?comment-id={data.get('id')}", stats)
    raise ConnectorFailure(f"unsupported figma connector operation: {operation}", stats)


def execute_slack_connector(operation: str, params: dict[str, Any], idempotency_key: str, project_key: str = "") -> ConnectorOutcome:
    stats = connector_stats("slack", operation, params, project_key)
    token = env_token("SLACK_BOT_TOKEN", stats)
    base = os.environ.get("HARNESS_SLACK_API_URL", "https://slack.com").rstrip("/")
    if operation == "slack.probe":
        path = "/slack/api/auth.test" if os.environ.get("HARNESS_SLACK_API_URL") else "/api/auth.test"
        data = http_json(f"{base}{path}", token, {}, stats=stats)
        return ConnectorOutcome(f"slack:probe:{data.get('team_id', 'ok')}", "", stats)
    if operation == "slack.message.post":
        existing = find_existing_connector_object(Path.cwd(), operation, params, idempotency_key, stats, project_key)
        if existing:
            return ConnectorOutcome(existing[0], existing[1], stats)
        path = "/slack/api/chat.postMessage" if os.environ.get("HARNESS_SLACK_API_URL") else "/api/chat.postMessage"
        data = http_json(
            f"{base}{path}",
            token,
            {"channel": require_param(params, "channel"), "text": with_connector_marker(require_param(params, "text"), idempotency_key, project_key)},
            stats=stats,
        )
        if data.get("ok") is False:
            raise ConnectorFailure(f"slack connector failed: {data.get('error', 'unknown')}", stats)
        channel = data.get("channel") or require_param(params, "channel")
        timestamp = data.get("ts") or ""
        return ConnectorOutcome(f"slack:message:{channel}:{timestamp}", str(data.get("permalink", "")), stats)
    raise ConnectorFailure(f"unsupported slack connector operation: {operation}", stats)


def execute_connector_action(root: Path, row: sqlite3.Row) -> ConnectorOutcome:
    payload_value = load_connector_payload(row)
    operation, params = validate_connector_request(row, payload_value)
    project_key = validate_connector_namespace(root, row, payload_value, operation, params)
    idempotency_key = str(row["idempotency_key"])
    tool = str(row["tool"])
    if tool == "github":
        return execute_github_connector(root, operation, params, idempotency_key, project_key)
    if tool == "linear":
        return execute_linear_connector(operation, params, idempotency_key, project_key)
    if tool == "notion":
        return execute_notion_connector(operation, params, idempotency_key, project_key)
    if tool == "figma":
        return execute_figma_connector(operation, params, idempotency_key, project_key)
    if tool == "slack":
        return execute_slack_connector(operation, params, idempotency_key, project_key)
    raise HarnessError(f"unsupported connector tool: {tool}")


def connector_execution_deadline() -> str:
    try:
        ttl_seconds = int(os.environ.get("HARNESS_CONNECTOR_EXECUTION_TTL_SECONDS", "300"))
    except ValueError:
        ttl_seconds = 300
    return (datetime.now(timezone.utc) + timedelta(seconds=max(1, ttl_seconds))).replace(microsecond=0).isoformat()


def connector_action_is_claimable(row: sqlite3.Row) -> bool:
    status = str(row["status"])
    if status in {"planned", "draft", "confirmed", "retryable_failed", "unknown"}:
        return True
    return status == "executing" and is_expired(row["claim_expires_at"])


def connector_stats_for_row(row: sqlite3.Row) -> tuple[str, dict[str, Any], ConnectorStats]:
    payload_value = load_connector_payload(row)
    operation, params = validate_connector_request(row, payload_value)
    return operation, params, connector_stats(str(row["tool"]), operation, params)


def upsert_completed_adapter(conn: sqlite3.Connection, row: sqlite3.Row, external_id: str, external_link: str, evidence: str) -> None:
    conn.execute(
        """
        insert into adapters
        (id, tool, mode, artifact, external_id, external_link, idempotency_key, evidence, fallback, confirmation_needed, updated_at)
        values (?, ?, ?, ?, ?, ?, ?, ?, '', 'no', ?)
        on conflict(tool, idempotency_key) do update set mode=excluded.mode, artifact=excluded.artifact,
          external_id=excluded.external_id, external_link=excluded.external_link, evidence=excluded.evidence,
          updated_at=excluded.updated_at
        """,
        (str(uuid.uuid4()), row["tool"], row["mode"], row["artifact"], external_id, external_link, row["idempotency_key"], evidence, now_iso()),
    )


def mark_connector_unknown(root: Path, action_id: str, fence: int | None, stats: ConnectorStats, reason: str) -> None:
    stats.status = "degraded"
    stats.last_error = reason[:1000]
    with transaction(root) as conn:
        row = conn.execute("select * from adapter_actions where id = ?", (action_id,)).fetchone()
        if not row:
            raise HarnessError(f"missing adapter action: {action_id}")
        if row["status"] == "completed":
            return
        where = "id = ?"
        params: list[object] = [action_id]
        if fence is not None:
            where += " and execution_fence = ?"
            params.append(fence)
        conn.execute(
            f"""
            update adapter_actions set status = 'unknown', connector_status = 'degraded',
              blocked_reason = ?, attempt_count = ?, next_retry_at = ?, claimed_at = '',
              claim_expires_at = '', updated_at = ? where {where}
            """,
            (stats.last_error, stats.attempt_count, stats.retry_after_at, now_iso(), *params),
        )
        upsert_connector_budget(conn, stats)
        emit_event(conn, "connector_action_unknown", payload(id=action_id, tool=stats.tool, operation=stats.operation, reason=stats.last_error), idempotency_key=row["idempotency_key"])


def recover_connector_action(root: Path, action_id: str, reason: str = "") -> bool:
    with connection(root) as conn:
        row = conn.execute("select * from adapter_actions where id = ?", (action_id,)).fetchone()
    if not row:
        raise HarnessError(f"missing adapter action: {action_id}")
    if row["status"] == "completed":
        return True
    payload_value = load_connector_payload(row)
    operation, params = validate_connector_request(row, payload_value)
    project_key = validate_connector_namespace(root, row, payload_value, operation, params)
    stats = connector_stats(str(row["tool"]), operation, params, project_key)
    stats.last_error = reason[:1000]
    try:
        existing = find_existing_connector_object(root, operation, params, str(row["idempotency_key"]), stats, project_key)
    except ConnectorFailure as exc:
        mark_connector_blocked(root, action_id, exc.stats, str(exc))
        raise HarnessError(f"connector recovery failed: {exc}") from exc
    now = now_iso()
    if not existing:
        with transaction(root) as conn:
            current = conn.execute("select * from adapter_actions where id = ?", (action_id,)).fetchone()
            if current and current["status"] != "completed":
                conn.execute(
                    """
                    update adapter_actions set last_recovery_at = ?, attempt_count = ?,
                      next_retry_at = ?, connector_status = ?, updated_at = ? where id = ?
                    """,
                    (now, stats.attempt_count, stats.retry_after_at, stats.status, now, action_id),
                )
                upsert_connector_budget(conn, stats)
        return False
    external_id, external_link = existing
    with transaction(root) as conn:
        current = conn.execute("select * from adapter_actions where id = ?", (action_id,)).fetchone()
        if not current:
            raise HarnessError(f"missing adapter action: {action_id}")
        if current["status"] == "completed":
            return True
        cursor = conn.execute(
            """
            update adapter_actions set status = 'completed',
              external_id = ?, external_link = ?, attempt_count = ?, next_retry_at = ?,
              connector_status = ?, blocked_reason = '', claimed_at = '', claim_expires_at = '',
              last_recovery_at = ?, remote_recovery_count = remote_recovery_count + 1, updated_at = ?
            where id = ? and status != 'blocked'
            """,
            (external_id, external_link, stats.attempt_count, stats.retry_after_at, stats.status, now, now, action_id),
        )
        if cursor.rowcount != 1:
            return False
        upsert_connector_budget(conn, stats)
        upsert_completed_adapter(conn, current, external_id, external_link, f"connector recovery {action_id}")
        emit_event(conn, "adapter_action_recovered", payload(id=action_id, status="completed", connector=current["tool"]), idempotency_key=current["idempotency_key"])
    render_tooling_map(root)
    return True


def claim_connector_action(root: Path, action_id: str, confirmation: str) -> sqlite3.Row | None:
    with transaction(root) as conn:
        row = conn.execute("select * from adapter_actions where id = ?", (action_id,)).fetchone()
        if not row:
            raise HarnessError(f"missing adapter action: {action_id}")
        if row["status"] == "completed":
            return None
        if row["status"] == "blocked":
            reason = row["blocked_reason"] or "connector action is blocked"
            raise HarnessError(f"connector execution failed: {reason}")
        if row["status"] == "executing" and not is_expired(row["claim_expires_at"]):
            raise HarnessError(f"connector action already executing: {action_id}")
        if not connector_action_is_claimable(row):
            raise HarnessError(f"connector action cannot be confirmed from status {row['status']}: {action_id}")
        fence = int(row["execution_fence"] or 0) + 1
        now = now_iso()
        cursor = conn.execute(
            """
            update adapter_actions set status = 'executing', confirmation = coalesce(nullif(?, ''), confirmation),
              execution_fence = ?, claimed_at = ?, claim_expires_at = ?, connector_status = 'degraded',
              blocked_reason = '', updated_at = ?
            where id = ? and status = ? and execution_fence = ?
            """,
            (confirmation, fence, now, connector_execution_deadline(), now, action_id, row["status"], row["execution_fence"]),
        )
        if cursor.rowcount != 1:
            raise HarnessError(f"connector action claim lost race: {action_id}")
        claimed = conn.execute("select * from adapter_actions where id = ?", (action_id,)).fetchone()
        emit_event(conn, "adapter_action_claimed", payload(id=action_id, status="executing", fence=fence), idempotency_key=row["idempotency_key"])
        return claimed


def complete_connector_action(root: Path, action_id: str, fence: int, confirmation: str, outcome: ConnectorOutcome) -> None:
    stats = outcome.stats
    with transaction(root) as conn:
        row = conn.execute("select * from adapter_actions where id = ?", (action_id,)).fetchone()
        if not row:
            raise HarnessError(f"missing adapter action: {action_id}")
        if row["status"] == "completed":
            return
        if row["status"] != "executing" or int(row["execution_fence"] or 0) != fence:
            conn.execute(
                """
                update adapter_actions set status = 'unknown', connector_status = 'degraded',
                  blocked_reason = ?, attempt_count = ?, next_retry_at = ?, claimed_at = '',
                  claim_expires_at = '', updated_at = ? where id = ? and status != 'completed'
                """,
                ("connector completion fence mismatch; remote outcome must be recovered by marker", stats.attempt_count, stats.retry_after_at, now_iso(), action_id),
            )
            upsert_connector_budget(conn, stats)
            emit_event(conn, "connector_action_unknown", payload(id=action_id, tool=stats.tool, operation=stats.operation, reason="completion fence mismatch"), idempotency_key=row["idempotency_key"])
            raise HarnessError(f"connector completion fence mismatch: {action_id}")
        cursor = conn.execute(
            """
            update adapter_actions set status = 'completed', confirmation = coalesce(nullif(?, ''), confirmation),
              external_id = ?, external_link = ?, attempt_count = ?, next_retry_at = ?,
              connector_status = ?, blocked_reason = '', claimed_at = '', claim_expires_at = '',
              updated_at = ? where id = ? and status = 'executing' and execution_fence = ?
            """,
            (confirmation, outcome.external_id, outcome.external_link, stats.attempt_count, stats.retry_after_at, stats.status, now_iso(), action_id, fence),
        )
        if cursor.rowcount != 1:
            raise HarnessError(f"connector completion failed: {action_id}")
        upsert_connector_budget(conn, stats)
        upsert_completed_adapter(conn, row, outcome.external_id, outcome.external_link, f"connector action {action_id}")
        emit_event(conn, "adapter_action_updated", payload(id=action_id, status="completed", connector=row["tool"]), idempotency_key=row["idempotency_key"])
    render_tooling_map(root)


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
    payload_hash = adapter_action_payload_hash(tool, mode, artifact, action, payload_json)
    created = False
    with transaction(root) as conn:
        ensure_column(conn, "adapter_actions", "payload_hash", "text not null default ''")
        existing = conn.execute(
            "select * from adapter_actions where tool = ? and idempotency_key = ?",
            (tool, key),
        ).fetchone()
        if existing:
            existing_hash = str(existing["payload_hash"] or "")
            calculated_existing_hash = adapter_action_payload_hash(
                str(existing["tool"]),
                str(existing["mode"]),
                str(existing["artifact"]),
                str(existing["action"]),
                str(existing["payload_json"]),
            )
            if not existing_hash:
                if calculated_existing_hash != payload_hash:
                    raise HarnessError(
                        f"idempotency-conflict: adapter action key {key!r} is already bound to a different payload"
                    )
                conn.execute(
                    "update adapter_actions set payload_hash = ? where id = ? and payload_hash = ''",
                    (calculated_existing_hash, existing["id"]),
                )
                existing_hash = calculated_existing_hash
            if existing_hash != payload_hash:
                raise HarnessError(
                    f"idempotency-conflict: adapter action key {key!r} is already bound to a different payload"
                )
            action_id = str(existing["id"])
        else:
            now = now_iso()
            conn.execute(
                """
                insert into adapter_actions
                (id, tool, mode, artifact, action, payload_json, payload_hash, status, idempotency_key, created_at, updated_at)
                values (?, ?, ?, ?, ?, ?, ?, 'planned', ?, ?, ?)
                """,
                (action_id, tool, mode, artifact, action, payload_json, payload_hash, key, now, now),
            )
            emit_event(conn, "adapter_action_planned", payload(id=action_id, tool=tool, mode=mode), idempotency_key=key)
            created = True
    if created:
        render_tooling_map(root)
    return action_id


def adapter_transition(root: Path, action_id: str, status: str, *, confirmation: str = "", external_id: str = "", external_link: str = "") -> None:
    if status not in ADAPTER_ACTION_STATUSES:
        raise HarnessError(f"invalid adapter action status: {status}")
    ensure_adapter_action_payload_hash_state(root)
    with connection(root) as conn:
        existing = conn.execute("select * from adapter_actions where id = ?", (action_id,)).fetchone()
    if not existing:
        raise HarnessError(f"missing adapter action: {action_id}")
    load_connector_payload(existing)
    if existing["status"] == "completed":
        if status != "completed":
            raise HarnessError(f"adapter action is immutable after completion: {action_id}")
        supplied_external_id = external_id or str(existing["external_id"] or "")
        supplied_external_link = external_link or str(existing["external_link"] or "")
        if supplied_external_id != str(existing["external_id"] or "") or supplied_external_link != str(existing["external_link"] or ""):
            raise HarnessError(f"adapter action is immutable after completion: {action_id}")
        return
    if status == "confirmed":
        current = existing
        if current["status"] == "blocked":
            reason = current["blocked_reason"] or "connector action is blocked"
            raise HarnessError(f"connector execution failed: {reason}")
        if should_execute_connector(current):
            payload_value = load_connector_payload(current)
            operation, params = validate_connector_request(current, payload_value)
            validate_connector_namespace(root, current, payload_value, operation, params)
            if current["status"] == "unknown" or (current["status"] == "executing" and is_expired(current["claim_expires_at"])):
                if recover_connector_action(root, action_id, current["blocked_reason"] or "recovering connector action before retry"):
                    return
                with connection(root) as conn:
                    current = conn.execute("select * from adapter_actions where id = ?", (action_id,)).fetchone()
                    if not current:
                        raise HarnessError(f"missing adapter action: {action_id}")
                if operation == "notion.page.create" and current["status"] == "unknown":
                    raise HarnessError(
                        f"notion page create remains unknown after marker recovery miss; refusing duplicate create: {action_id}"
                    )
            if current["status"] == "executing" and not is_expired(current["claim_expires_at"]):
                raise HarnessError(f"connector action already executing: {action_id}")
            claimed = claim_connector_action(root, action_id, confirmation)
            if claimed is None:
                return
            fence = int(claimed["execution_fence"])
            try:
                outcome = execute_connector_action(root, claimed)
            except ConnectorFailure as exc:
                if exc.ambiguous:
                    mark_connector_unknown(root, action_id, fence, exc.stats, str(exc))
                else:
                    mark_connector_blocked(root, action_id, exc.stats, str(exc))
                raise HarnessError(f"connector execution failed: {exc}") from exc
            except HarnessError as exc:
                stats = connector_stats_for_row(claimed)[2]
                mark_connector_unknown(root, action_id, fence, stats, str(exc))
                raise HarnessError(f"connector execution failed: {exc}") from exc
            complete_connector_action(root, action_id, fence, confirmation, outcome)
            return
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
    ensure_adapter_action_payload_hash_state(root)
    with connection(root) as conn:
        completed = conn.execute("select * from adapter_actions where status = 'completed' order by tool, artifact").fetchall()
        for action in completed:
            try:
                load_connector_payload(action)
            except HarnessError as exc:
                issues.append(str(exc))
                continue
            adapter = conn.execute(
                "select * from adapters where tool = ? and idempotency_key = ?",
                (action["tool"], action["idempotency_key"]),
            ).fetchone()
            if not adapter:
                issues.append(f"completed adapter action has no adapter record: {action['id']}")
            elif adapter["external_id"] != action["external_id"] or adapter["external_link"] != action["external_link"]:
                issues.append(f"adapter action drift: {action['id']}")
        candidates = conn.execute("select * from adapter_actions where status in ('unknown', 'executing') order by tool, artifact").fetchall()
    for action in candidates:
        if action["status"] == "executing" and not is_expired(action["claim_expires_at"]):
            continue
        try:
            recovered = recover_connector_action(root, action["id"], action["blocked_reason"] or "adapter reconcile recovery")
        except HarnessError as exc:
            issues.append(str(exc))
            continue
        if not recovered:
            issues.append(f"adapter action remains unconfirmed after remote recovery search: {action['id']}")
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
        cycle_id = current_cycle_id(conn)
        conn.execute(
            "insert into dispatch_runs (id, cycle_id, scope, status, created_at, updated_at) values (?, ?, ?, 'planned', ?, ?)",
            (run_id, cycle_id, scope, now_iso(), now_iso()),
        )
        ready_ids = ready_queue(conn)
        for task_id in ready_ids:
            task = conn.execute("select id, owner from tasks where cycle_id = ? and id = ?", (cycle_id, task_id)).fetchone()
            capability = task["owner"] if task["owner"] and task["owner"] != "unassigned" else "developer"
            conn.execute(
                "insert into dispatch_assignments (run_id, cycle_id, task_id, capability, status, updated_at) values (?, ?, ?, ?, 'planned', ?)",
                (run_id, cycle_id, task["id"], capability, now_iso()),
            )
        emit_event(conn, "dispatch_planned", payload(id=run_id, scope=scope))
    return run_id


def default_codex_fanout_dir(root: Path, run_id: str) -> Path:
    return root / ".ai-team" / "runtime" / "codex-fanout" / safe_branch_part(run_id)


def default_native_dispatch_dir(root: Path, run_id: str) -> Path:
    return root / ".ai-team" / "runtime" / "native-dispatch" / safe_branch_part(run_id)


def stored_runtime_path(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix() if path.is_relative_to(root) else path.as_posix()


def native_package_without_hash(package: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in package.items() if key != "package_sha256"}


def native_package_hash(package: dict[str, Any]) -> str:
    return stable_digest(native_package_without_hash(package))


def dispatch_export_native(root: Path, run_id: str, *, out_dir: Path | None = None) -> Path:
    if not (root / ".git").exists():
        raise HarnessError("native dispatch requires a git repository")
    output_dir = out_dir if out_dir else default_native_dispatch_dir(root, run_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    candidate_sha = current_candidate_sha(root)
    base_sha = git_base_commit(root) or candidate_sha
    with connection(root) as conn:
        run = conn.execute("select * from dispatch_runs where id = ?", (run_id,)).fetchone()
        if not run:
            raise HarnessError(f"missing dispatch run: {run_id}")
        cycle_id = str(run["cycle_id"])
        assignments = provider_assignment_rows(conn, run_id)
        if not assignments:
            raise HarnessError(f"no ready dispatch assignments for run: {run_id}")
        packages: list[dict[str, Any]] = []
        for assignment in assignments:
            task_id = str(assignment["task_id"])
            agent_id = str(assignment["agent_id"] or assignment["capability"] or assignment["owner"] or "developer")
            agent = conn.execute("select role from agents where id = ?", (agent_id,)).fetchone()
            role = str(agent["role"]) if agent and str(agent["role"]) in SESSION_ROLES else str(assignment["capability"] or assignment["owner"] or "developer")
            if role not in SESSION_ROLES:
                role = "developer"
            target = task_target(conn, task_id, cycle_id)
            acceptance_ids = sorted(parse_ids(grouped(conn, "task_acceptance", "task_id", "acceptance_id", cycle_id).get(task_id, "")))
            failure_mode_ids = sorted(parse_ids(grouped(conn, "task_failure_modes", "task_id", "failure_mode_id", cycle_id).get(task_id, "")))
            target_rows = conn.execute(
                """
                select tt.* from task_test_targets ttt
                join test_targets tt on tt.id = ttt.target_id
                where ttt.cycle_id = ? and ttt.task_id = ? order by tt.id
                """,
                (cycle_id, task_id),
            ).fetchall()
            file_claims = [
                str(row["path"])
                for row in conn.execute(
                    "select path from task_file_claims where run_id = ? and task_id = ? and status = 'active' order by path",
                    (run_id, task_id),
                )
            ]
            risks = task_failure_mode_risks(conn, task_id, cycle_id)
            risk_order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
            risk = max(risks, key=lambda value: risk_order.get(value, -1), default="low")
            branch_name = f"agent/{safe_branch_part(run_id)}/{safe_branch_part(task_id)}/{safe_branch_part(agent_id)}"
            package: dict[str, Any] = {
                "package_version": "1",
                "run_id": run_id,
                "assignment_id": f"{run_id}:{task_id}",
                "task_id": task_id,
                "cycle_id": cycle_id,
                "candidate_sha": candidate_sha,
                "base_sha": base_sha,
                "base_ref": "HEAD",
                "target_branch": branch_name,
                "agent_id": agent_id,
                "role": role,
                "goal": str(assignment["task"]),
                "acceptance_ids": acceptance_ids,
                "failure_mode_ids": failure_mode_ids,
                "test_target_ids": [str(row["id"]) for row in target_rows],
                "target_id": str(target["id"]) if target else "",
                "command_template": str(target["command_template"]) if target else "",
                "file_claims": file_claims,
                "capability_hints": {
                    "risk": risk,
                    "task_shape": "small-verified-code-change" if agent_id == "developer" and target else "host-policy-required",
                    "requires_sandbox": bool(int(target["requires_sandbox"] or 0)) if target else False,
                    "requires_no_network": bool(int(target["requires_no_network"] or 0)) if target else False,
                    "gateable_target": bool(int(target["gateable"] or 0)) if target else False,
                },
                "state_transport": "root-controller-only",
            }
            package["package_sha256"] = native_package_hash(package)
            packages.append(package)

    manifest_entries: list[dict[str, str]] = []
    for package in packages:
        package_path = output_dir / f"{safe_branch_part(str(package['task_id']))}.task.json"
        package_path.write_text(json.dumps(package, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        manifest_entries.append(
            {
                "assignment_id": str(package["assignment_id"]),
                "task_id": str(package["task_id"]),
                "package_sha256": str(package["package_sha256"]),
                "path": stored_runtime_path(root, package_path),
            }
        )
    manifest = {
        "manifest_version": "1",
        "run_id": run_id,
        "state_transport": "root-controller-only",
        "packages": manifest_entries,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with transaction(root, touched=[("dispatch_run", run_id)]) as conn:
        for package, manifest_entry in zip(packages, manifest_entries, strict=True):
            existing = conn.execute(
                "select * from agent_provider_sessions where run_id = ? and task_id = ? and provider = 'native-codex'",
                (run_id, package["task_id"]),
            ).fetchone()
            input_data = {
                "native_package": package,
                "provider_metadata": {
                    "package_path": manifest_entry["path"],
                    "manifest_path": stored_runtime_path(root, manifest_path),
                    "lifecycle_owner": "native-host",
                    "state_transport": "root-controller-only",
                },
            }
            if existing:
                existing_data = json.loads(existing["input_json"] or "{}")
                existing_package = existing_data.get("native_package", {}) if isinstance(existing_data, dict) else {}
                if not isinstance(existing_package, dict) or native_package_hash(existing_package) != package["package_sha256"]:
                    raise HarnessError(f"native package conflict: {package['assignment_id']}")
                continue
            session_id = str(uuid.uuid4())
            conn.execute(
                """
                insert into agent_provider_sessions
                (id, run_id, task_id, provider, provider_session_id, provider_job_id, agent_id, status, fence,
                 branch_name, worktree_path, input_json, spawned_at)
                values (?, ?, ?, 'native-codex', '', '', ?, 'package_exported', ?, ?, '', ?, ?)
                """,
                (
                    session_id,
                    run_id,
                    package["task_id"],
                    package["agent_id"],
                    int(next(item["fence"] for item in assignments if str(item["task_id"]) == package["task_id"])),
                    package["target_branch"],
                    stable_json(input_data),
                    now_iso(),
                ),
            )
            session = conn.execute("select * from agent_provider_sessions where id = ?", (session_id,)).fetchone()
            provider_event(conn, session, "package_exported", {"package_sha256": package["package_sha256"], "package_path": manifest_entry["path"]})
            emit_event(conn, "native_task_package_exported", payload(run_id=run_id, task_id=package["task_id"], package_sha256=package["package_sha256"]))
    return manifest_path


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
        run = conn.execute("select cycle_id from dispatch_runs where id = ?", (run_id,)).fetchone()
        if not run:
            raise HarnessError(f"missing dispatch run: {run_id}")
        cycle_id = str(run["cycle_id"])
        ready_ids = set(ready_queue(conn, cycle_id))
        assignments = conn.execute(
            """
            select da.run_id, da.task_id, da.agent_id, da.capability, da.status as assignment_status,
                   t.task, t.owner, t.fence
            from dispatch_assignments da
            join tasks t on t.cycle_id = da.cycle_id and t.id = da.task_id
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
            acceptance = grouped(conn, "task_acceptance", "task_id", "acceptance_id", cycle_id).get(assignment["task_id"], "")
            failure_modes = grouped(conn, "task_failure_modes", "task_id", "failure_mode_id", cycle_id).get(assignment["task_id"], "")
            agent_id = assignment["agent_id"] or assignment["capability"] or assignment["owner"] or "developer"
            branch_name = f"agent/{safe_branch_part(run_id)}/{safe_branch_part(assignment['task_id'])}/{safe_branch_part(agent_id)}"
            target = task_target(conn, assignment["task_id"], cycle_id)
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
        cycle_id = current_cycle_id(conn)
        ready_ids = ready_queue(conn, cycle_id)
        if not ready_ids:
            raise HarnessError(f"no dispatch assignment for agent: {agent}")
        assignment = conn.execute(
            f"""
            select da.* from dispatch_assignments da
            join tasks t on t.cycle_id = da.cycle_id and t.id = da.task_id
            where da.cycle_id = ? and da.status = 'planned' and t.status = 'ready'
              and da.capability in ({','.join('?' for _ in capabilities)})
              and da.task_id in ({','.join('?' for _ in ready_ids)})
            order by da.updated_at, da.task_id
            limit 1
            """,
            (cycle_id, *tuple(capabilities), *tuple(ready_ids)),
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

    run = conn.execute("select cycle_id from dispatch_runs where id = ?", (run_id,)).fetchone()
    if not run:
        raise HarnessError(f"missing dispatch run: {run_id}")
    cycle_id = str(run["cycle_id"])
    ready_ids = set(ready_queue(conn, cycle_id))
    if not ready_ids:
        return []
    placeholders = ",".join("?" for _ in ready_ids)
    return conn.execute(
        f"""
        select da.*, t.task, t.owner, t.fence
        from dispatch_assignments da
        join tasks t on t.cycle_id = da.cycle_id and t.id = da.task_id
        where da.run_id = ? and da.status in ('planned', 'claimed') and t.status = 'ready'
          and da.task_id in ({placeholders})
        order by da.updated_at, da.task_id
        """,
        (run_id, *tuple(ready_ids)),
    ).fetchall()


def task_failure_mode_risks(conn: sqlite3.Connection, task_id: str, cycle_id: str = "") -> list[str]:
    cycle_id = cycle_id or current_cycle_id(conn)
    rows = conn.execute(
        """
        select fm.risk from task_failure_modes tfm
        join failure_modes fm on fm.cycle_id = tfm.cycle_id and fm.id = tfm.failure_mode_id
        where tfm.cycle_id = ? and tfm.task_id = ?
        order by fm.id
        """,
        (cycle_id, task_id),
    ).fetchall()
    return sorted({str(row["risk"]) for row in rows if row["risk"]})


def host_codex_model_policy_input(agent_id: str, target: sqlite3.Row | None, failure_mode_risks: list[str]) -> dict[str, Any]:
    blockers: list[str] = []
    if agent_id != "developer":
        blockers.append(f"agent is {agent_id}, not developer")
    if target is None:
        blockers.append("missing registered test target")
    else:
        if int(target["gateable"] or 0) != 1:
            blockers.append("target is not gateable")
        if not str(target["command_template"] or "").strip():
            blockers.append("target command template is empty")
        if int(target["requires_sandbox"] or 0) == 1:
            blockers.append("target requires sandbox")
        if int(target["requires_no_network"] or 0) == 1:
            blockers.append("target requires no-network")
    high_risks = [risk for risk in failure_mode_risks if risk in {"high", "critical"}]
    if high_risks:
        blockers.append(f"linked failure mode risk is {','.join(high_risks)}")
    eligible = not blockers
    reason = "spark eligible: developer task with gateable local target and no high/critical failure modes" if eligible else "spark ineligible: " + "; ".join(blockers)
    return {
        "spark_eligible": eligible,
        "model_selection_reason": reason,
        "failure_mode_risks": failure_mode_risks,
    }


def dispatch_route_advice(root: Path, run_id: str = "") -> dict[str, Any]:
    from core.scheduler import ready_queue

    with connection(root) as conn:
        rows: list[sqlite3.Row]
        run_scope = ""
        cycle_id = current_cycle_id(conn)
        if run_id:
            run = conn.execute("select * from dispatch_runs where id = ?", (run_id,)).fetchone()
            if not run:
                raise HarnessError(f"missing dispatch run: {run_id}")
            run_scope = run["scope"]
            cycle_id = str(run["cycle_id"])
            ready_ids = set(ready_queue(conn, cycle_id))
            rows = conn.execute(
                """
                select da.run_id, da.task_id, da.agent_id, da.capability, da.status as assignment_status,
                       t.task, t.owner, t.status as task_status, t.fence
                from dispatch_assignments da
                join tasks t on t.cycle_id = da.cycle_id and t.id = da.task_id
                where da.run_id = ?
                order by da.task_id
                """,
                (run_id,),
            ).fetchall()
        else:
            ready_ids = set(ready_queue(conn, cycle_id))
            placeholders = ",".join("?" for _ in ready_ids)
            rows = (
                conn.execute(
                    f"""
                    select '' as run_id, t.id as task_id, '' as agent_id,
                           case when t.owner = '' or t.owner = 'unassigned' then 'developer' else t.owner end as capability,
                           'ready' as assignment_status, t.task, t.owner, t.status as task_status, t.fence
                    from tasks t
                    where t.cycle_id = ? and t.id in ({placeholders})
                    order by t.id
                    """,
                    (cycle_id, *tuple(ready_ids)),
                ).fetchall()
                if ready_ids
                else []
            )
        tasks: list[dict[str, Any]] = []
        summary = {
            "task_count": 0,
            "ready_count": 0,
            "spark_eligible_count": 0,
            "host_codex_default_count": 0,
            "main_model_or_manual_count": 0,
        }
        for row in rows:
            task_id = row["task_id"]
            agent_id = row["agent_id"] or row["capability"] or row["owner"] or "developer"
            target = task_target(conn, task_id, cycle_id)
            failure_mode_risks = task_failure_mode_risks(conn, task_id, cycle_id)
            policy = host_codex_model_policy_input(agent_id, target, failure_mode_risks)
            ready = task_id in ready_ids and row["task_status"] == "ready" and row["assignment_status"] in {"ready", "planned", "claimed"}
            recommendation = "main-model-or-manual"
            if not ready:
                recommendation = "blocked-not-ready"
            elif policy["spark_eligible"]:
                recommendation = "host-codex-spark"
            elif agent_id == "developer" and target is not None and int(target["gateable"] or 0) == 1 and str(target["command_template"] or "").strip():
                recommendation = "host-codex-default"
            task_report = {
                "task_id": task_id,
                "task": row["task"],
                "agent_id": agent_id,
                "assignment_status": row["assignment_status"],
                "task_status": row["task_status"],
                "ready": ready,
                "target_id": target["id"] if target else "",
                "target_gateable": bool(int(target["gateable"] or 0)) if target else False,
                "target_requires_sandbox": bool(int(target["requires_sandbox"] or 0)) if target else False,
                "target_requires_no_network": bool(int(target["requires_no_network"] or 0)) if target else False,
                "failure_mode_risks": failure_mode_risks,
                "spark_eligible": bool(policy["spark_eligible"]) and ready,
                "recommendation": recommendation,
                "reason": policy["model_selection_reason"] if ready else "task is not ready for dispatch",
            }
            tasks.append(task_report)
            summary["task_count"] += 1
            if ready:
                summary["ready_count"] += 1
            if task_report["spark_eligible"]:
                summary["spark_eligible_count"] += 1
            elif recommendation == "host-codex-default":
                summary["host_codex_default_count"] += 1
            elif recommendation in {"main-model-or-manual", "blocked-not-ready"}:
                summary["main_model_or_manual_count"] += 1
        next_commands: list[str] = []
        if tasks and not run_id:
            next_commands.append("harness.py --root . dispatch plan --scope '<scope>'")
        if run_id and summary["spark_eligible_count"]:
            next_commands.append(f"HARNESS_CODEX_MODEL_POLICY=spark-deterministic harness.py --root . dispatch provider start --run-id {run_id} --provider host-codex")
        elif run_id and summary["host_codex_default_count"]:
            next_commands.append(f"harness.py --root . dispatch provider start --run-id {run_id} --provider host-codex")
        return {
            "run_id": run_id,
            "scope": run_scope,
            "policy": "spark-deterministic-advisory",
            "spark_model": os.environ.get("HARNESS_CODEX_SPARK_MODEL", "gpt-5.3-codex-spark"),
            "summary": summary,
            "tasks": tasks,
            "next_commands": next_commands,
            "boundaries": [
                "Spark is only an execution candidate for low-risk developer tasks with controller-verifiable targets.",
                "Spark output is not delivery evidence; dispatch verify-attempt and delivery gates remain mandatory.",
                "Architect, QA, high/critical risk, sandbox/no-network, missing-target, and ambiguous tasks require main-model or manual review.",
            ],
        }


def dispatch_route_advice_lines(root: Path, run_id: str = "") -> list[str]:
    report = dispatch_route_advice(root, run_id)
    lines = [
        f"policy: {report['policy']}",
        f"spark_model: {report['spark_model']}",
        f"run_id: {report['run_id'] or '(none)'}",
        markdown_row(["task", "agent", "target", "ready", "spark", "recommendation", "reason"]),
    ]
    for task in report["tasks"]:
        lines.append(
            markdown_row(
                [
                    task["task_id"],
                    task["agent_id"],
                    task["target_id"] or "-",
                    "yes" if task["ready"] else "no",
                    "yes" if task["spark_eligible"] else "no",
                    task["recommendation"],
                    task["reason"],
                ]
            )
        )
    if not report["tasks"]:
        lines.append("No ready dispatch tasks found.")
    for command in report["next_commands"]:
        lines.append(f"next: {command}")
    lines.extend(f"boundary: {boundary}" for boundary in report["boundaries"])
    return lines


def dispatch_provider_start(root: Path, run_id: str, provider_name: str, *, max_concurrency: int = 6) -> int:
    from core.agent_provider import AgentJobRequest, provider_for

    if max_concurrency < 1 or max_concurrency > 6:
        raise HarnessError("max concurrency must be between 1 and 6")
    provider = provider_for(provider_name)
    pending: list[AgentJobRequest] = []
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
            session_id = str(uuid.uuid4())
            provider_session_id = f"{provider.name}:{run_id}:{row['task_id']}:{session_id}"
            agent_session_id = f"AGENT-SESSION-{uuid.uuid4().hex[:12]}"
            role = agent_id if agent_id in SESSION_ROLES else "developer"
            context_id = f"{run_id}:{row['task_id']}"
            failure_mode_risks = task_failure_mode_risks(conn, row["task_id"])
            model_policy_input = host_codex_model_policy_input(agent_id, target, failure_mode_risks) if provider.name == "host-codex" else {}
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
                    "command_template": target["command_template"] if target else "",
                    **model_policy_input,
                },
                session_id=session_id,
                provider_session_id=provider_session_id,
            )
            now = now_iso()
            conn.execute(
                """
                insert into agent_sessions
                (session_id, agent_id, role, context_id, provider_session_id, origin, trust_level, status, started_at, ended_at)
                values (?, ?, ?, ?, ?, 'manual', 'local-only', 'running', ?, '')
                """,
                (agent_session_id, agent_id, role, context_id, provider_session_id, now),
            )
            conn.execute(
                """
                insert into agent_provider_sessions
                (id, run_id, task_id, provider, provider_session_id, provider_job_id, agent_id, status, fence,
                 agent_session_id, branch_name, worktree_path, input_json, report_id, attempt_id, last_error, spawned_at,
                 heartbeat_at, lease_expires_at, collected_at, cancelled_at, finished_at)
                values (?, ?, ?, ?, ?, '', ?, 'spawning', ?, ?, ?, '', ?, '', '', '', ?, ?, ?, '', '', '')
                """,
                (
                    session_id,
                    run_id,
                    row["task_id"],
                    provider.name,
                    provider_session_id,
                    agent_id,
                    int(row["fence"]),
                    agent_session_id,
                    branch_name,
                    stable_json(request.input_json),
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
                (agent_id, provider_session_id, now, now, lease_deadline(), now, run_id, row["task_id"]),
            )
            session = conn.execute("select * from agent_provider_sessions where id = ?", (session_id,)).fetchone()
            provider_event(conn, session, "spawning", {"provider_status": "spawning"})
            emit_event(conn, "agent_provider_session_spawning", payload(run_id=run_id, task_id=row["task_id"], provider=provider.name, provider_session_id=provider_session_id))
            pending.append(request)
        if pending:
            refresh_dispatch_run_status(conn, run_id)
    started = 0
    for request in pending:
        spawn_request = request
        prepared_worktree_path = ""
        try:
            if provider.name == "host-codex":
                branch_name, prepared_worktree_path = ensure_dispatch_worktree(root, request.run_id, request.task_id, request.agent_id)
                spawn_request = replace(
                    request,
                    branch_name=branch_name,
                    worktree_path=prepared_worktree_path,
                    input_json={**request.input_json, "branch_name": branch_name, "worktree_path": prepared_worktree_path},
                )
            handle = provider.spawn(spawn_request)
        except Exception as exc:  # noqa: BLE001 - provider errors are recorded as session failures.
            from core.agent_provider import AgentJobHandle

            handle = AgentJobHandle(provider.name, spawn_request.provider_session_id, spawn_request.task_id, "spawn_failed", str(exc))
        handle_meta: dict[str, Any] = {}
        if handle.message:
            try:
                handle_meta = json.loads(handle.message)
            except json.JSONDecodeError:
                handle_meta = {"message": handle.message}
        with transaction(root, touched=[("agent_provider_session", spawn_request.session_id)]) as conn:
            session = conn.execute(
                """
                select * from agent_provider_sessions
                where id = ? and provider_session_id = ? and fence = ?
                """,
                (spawn_request.session_id, spawn_request.provider_session_id, spawn_request.fence),
            ).fetchone()
            if not session or session["status"] != "spawning":
                if handle.status == "running":
                    provider.cancel(handle, "provider session no longer active")
                continue
            provider_input_json = json.loads(session["input_json"] or "{}")
            provider_input_json["branch_name"] = spawn_request.branch_name
            if spawn_request.worktree_path:
                provider_input_json["worktree_path"] = spawn_request.worktree_path
            if handle_meta:
                provider_input_json["provider_metadata"] = handle_meta
            now = now_iso()
            if handle.status == "spawn_failed":
                conn.execute(
                    """
                    update agent_provider_sessions
                    set status = 'spawn_failed', provider_job_id = ?, branch_name = ?, worktree_path = ?, input_json = ?, last_error = ?,
                        heartbeat_at = '', lease_expires_at = '', finished_at = ?
                    where id = ? and status = 'spawning'
                    """,
                    (handle.provider_job_id, spawn_request.branch_name, spawn_request.worktree_path, stable_json(provider_input_json), handle.message, now, spawn_request.session_id),
                )
                if session["agent_session_id"]:
                    conn.execute(
                        "update agent_sessions set status = 'verification_failed', ended_at = ? where session_id = ?",
                        (now, session["agent_session_id"]),
                    )
                conn.execute(
                    """
                    update dispatch_assignments
                    set status = 'planned', agent_id = '', provider_session_id = '', heartbeat_at = null,
                        lease_expires_at = null, updated_at = ?
                    where run_id = ? and task_id = ? and status != 'completed'
                    """,
                    (now, run_id, spawn_request.task_id),
                )
                updated = conn.execute("select * from agent_provider_sessions where id = ?", (spawn_request.session_id,)).fetchone()
                provider_event(conn, updated, "spawn_failed", handle_meta)
                emit_event(conn, "agent_provider_session_spawn_failed", payload(run_id=run_id, task_id=spawn_request.task_id, provider=provider.name, error=handle.message))
                continue
            conn.execute(
                """
                update agent_provider_sessions
                set status = ?, provider_session_id = ?, provider_job_id = ?, branch_name = ?, worktree_path = ?, input_json = ?, last_error = ?,
                    heartbeat_at = ?, lease_expires_at = ?
                where id = ? and status = 'spawning'
                """,
                (
                    handle.status,
                    handle.provider_session_id,
                    handle.provider_job_id,
                    spawn_request.branch_name,
                    spawn_request.worktree_path,
                    stable_json(provider_input_json),
                    handle.message,
                    now,
                    lease_deadline(),
                    spawn_request.session_id,
                ),
            )
            if session["agent_session_id"] and handle.provider_session_id != session["provider_session_id"]:
                conn.execute(
                    "update agent_sessions set provider_session_id = ? where session_id = ?",
                    (handle.provider_session_id, session["agent_session_id"]),
                )
            conn.execute(
                """
                update dispatch_assignments
                set provider_session_id = ?, heartbeat_at = ?, lease_expires_at = ?, updated_at = ?
                where run_id = ? and task_id = ?
                """,
                (handle.provider_session_id, now, lease_deadline(), now, run_id, spawn_request.task_id),
            )
            updated = conn.execute("select * from agent_provider_sessions where id = ?", (spawn_request.session_id,)).fetchone()
            provider_event(conn, updated, "started", {"provider_status": handle.status, **handle_meta})
            emit_event(conn, "agent_provider_session_started", payload(run_id=run_id, task_id=spawn_request.task_id, provider=provider.name, provider_session_id=handle.provider_session_id))
            started += 1
        if handle.status == "spawn_failed" and prepared_worktree_path:
            cleanup_dispatch_worktrees(root, run_id, spawn_request.task_id, spawn_request.agent_id)
    return started


def normalize_claim_path(path: str) -> str:
    value = path.strip()
    if not value:
        raise HarnessError("file claim path is required")
    if value.startswith(("/", "\\")):
        raise HarnessError(f"invalid file claim path: {path}")
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


def cleanup_dispatch_worktrees(root: Path, run_id: str, task_id: str, agent: str = "") -> None:
    clauses = ["run_id = ?", "task_id = ?", "status = 'active'"]
    params: list[str] = [run_id, task_id]
    if agent:
        clauses.append("agent_id = ?")
        params.append(agent)
    with connection(root) as conn:
        rows = conn.execute(f"select * from dispatch_worktrees where {' and '.join(clauses)}", tuple(params)).fetchall()
    for row in rows:
        if row["worktree_path"]:
            worktree = root / row["worktree_path"]
            if worktree.exists():
                subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=root, text=True, capture_output=True, check=False)
                shutil.rmtree(worktree, ignore_errors=True)
        with transaction(root, touched=[("dispatch_worktree", row["task_id"])]) as conn:
            conn.execute("update dispatch_worktrees set status = 'cleaned', cleaned_at = ? where id = ?", (now_iso(), row["id"]))
            emit_event(conn, "dispatch_worktree_cleaned", payload(run_id=run_id, task_id=row["task_id"], agent=row["agent_id"], worktree_path=row["worktree_path"]))


def remove_worktree_checkout(root: Path, worktree_path: str) -> None:
    if not worktree_path:
        return
    worktree = root / worktree_path
    if worktree.exists():
        subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=root, text=True, capture_output=True, check=False)
        shutil.rmtree(worktree, ignore_errors=True)


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
    cycle_id = str(assignment.get("cycle_id", ""))
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
        target = conn.execute("select * from test_targets where id = ?", (target_id,)).fetchone() if target_id else None
        if target_id and not target:
            raise HarnessError(f"missing test target: {target_id}")
        target_command = target["command_template"] if target else ""
        prefixes = executor_prefixes(conn)
    result_format = str(target["result_format"] or "regex") if target else "regex"
    result_path = str(target["result_path"] or "") if target else ""

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
        result_format=result_format,
        result_path=result_path,
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
                    refresh_dispatch_run_status(conn, run_id)
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
             target_id, executed_count, executed_count_source, result_format, result_path, semantic_status,
             allow_unlisted, no_network, policy_status, policy_reason,
             sandbox_profile, sandbox_status, allow_unlisted_reason, trust_anchor, trust_anchor_id,
             attempt_id, tree_sha, code_ref, verified_by,
             created_at)
            values (?, 'command', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'local-only', '', ?, ?, ?, 'controller-local', ?)
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
                result.result_format,
                result_path,
                result.semantic_status,
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
            refresh_dispatch_run_status(conn, run_id)
            if result.exit_code == 0:
                task = conn.execute("select uid, status from tasks where cycle_id = ? and id = ?", (cycle_id, task_id)).fetchone()
                if task and task["status"] in {"ready", "claimed", "in_progress"}:
                    conn.execute(
                        """
                        update tasks set status = 'submitted', evidence = ?, submitted_by = ?, lease_agent = null,
                          lease_token = null, lease_heartbeat_at = null, lease_expires_at = null,
                          revision = revision + 1, updated_at = ? where uid = ?
                        """,
                        (evidence_id, agent, now_iso(), task["uid"]),
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
            join tasks t on t.cycle_id = da.cycle_id and t.id = da.task_id
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
        conn.execute("update dispatch_assignments set status = 'verification_failed', updated_at = ? where run_id = ?", (now_iso(), run_id))
        refresh_dispatch_run_status(conn, run_id)
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
            "select * from dispatch_worktrees where run_id = ? and status in ('active', 'host-managed') order by created_at",
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
                    conn.execute("update dispatch_assignments set status = 'integration_conflict', updated_at = ? where run_id = ?", (now_iso(), run_id))
                    refresh_dispatch_run_status(conn, run_id)
                    emit_event(conn, "dispatch_integration_conflict", payload(run_id=run_id, branch=row["branch_name"]))
                raise HarnessError(f"integration conflict: {row['task_id']}")
        issues = validate_runtime(integration_worktree, delivery=True)
        if issues:
            issue_text = [str(issue) for issue in issues]
            summary = "; ".join(issue_text[:5])
            with transaction(root, touched=[("dispatch_run", run_id), ("finding", run_id)]) as conn:
                finding_id = record_integration_finding(conn, run_id, f"delivery validation failed after integration: {summary}")
                finish_integration_attempt(conn, integration_attempt_id, "verification_failed", merged_branches=[r["branch_name"] for r in rows], validation_result=summary, finding_id=finding_id)
                conn.execute("update dispatch_assignments set status = 'verification_failed', updated_at = ? where run_id = ?", (now_iso(), run_id))
                refresh_dispatch_run_status(conn, run_id)
                emit_event(conn, "dispatch_integration_verification_failed", payload(run_id=run_id, issues=issue_text[:10]))
            raise HarnessError(f"integration verification failed: {summary}")
        with transaction(root, touched=[("dispatch_run", run_id)]) as conn:
            finish_integration_attempt(conn, integration_attempt_id, "integrated", merged_branches=[r["branch_name"] for r in rows], validation_result="pass")
            conn.execute("update dispatch_assignments set status = 'integrated', updated_at = ? where run_id = ?", (now_iso(), run_id))
            refresh_dispatch_run_status(conn, run_id)
            for row in rows:
                if row["worktree_path"] and row["status"] == "active":
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


def resolve_container_image(root: Path, container_image: str = "", *, target_id: str = "", stack_profile: str = "python") -> str:
    if container_image.strip():
        return container_image.strip()
    if target_id:
        with connection(root) as conn:
            target = conn.execute("select container_image, stack_profile from test_targets where id = ?", (target_id,)).fetchone()
        if target:
            if str(target["container_image"] or "").strip():
                return str(target["container_image"]).strip()
            stack_profile = str(target["stack_profile"] or stack_profile or "python")
    config = root / ".ai-team" / "control" / "container-image.txt"
    if config.exists():
        configured = config.read_text(encoding="utf-8").strip()
        if configured:
            return configured
    return STACK_PROFILE_IMAGES.get(stack_profile, DEFAULT_CONTAINER_IMAGE)


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
    conn.execute("update dispatch_assignments set status = 'verification_failed', updated_at = ? where run_id = ? and task_id = ?", (now_iso(), run_id, task_id))
    refresh_dispatch_run_status(conn, run_id)


def codex_report_issues(root: Path, conn: sqlite3.Connection, expected: dict[str, str], result: dict[str, Any], *, strict_evidence_fields: bool = False) -> list[str]:
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
    if strict_evidence_fields:
        if target and int(target["gateable"]) != 1:
            issues.append(f"target is not gateable: {result['target_id']}")
        if expected.get("command_template") and result["command"] != expected["command_template"]:
            issues.append(f"command differs from target: expected={expected['command_template']} actual={result['command']}")
        try:
            if int(result["exit_code"]) != 0:
                issues.append(f"exit_code is not 0: {result['exit_code']}")
        except (TypeError, ValueError):
            issues.append(f"exit_code is not an integer: {result['exit_code']}")
        if result["executed_count_source"] != "parsed":
            issues.append(f"executed_count_source is not parsed: {result['executed_count_source']}")
        try:
            if int(result["executed_count"]) <= 0:
                issues.append(f"executed_count is not positive: {result['executed_count']}")
        except (TypeError, ValueError):
            issues.append(f"executed_count is not an integer: {result['executed_count']}")
    if result["branch_name"] != expected["branch_name"]:
        issues.append(f"branch differs from export: expected={expected['branch_name']} actual={result['branch_name']}")
    branch = subprocess.run(["git", "rev-parse", "--verify", str(result["branch_name"])], cwd=root, text=True, capture_output=True, check=False)
    if branch.returncode != 0:
        issues.append(f"branch is missing: {result['branch_name']}")
    task = conn.execute(
        "select fence from tasks where cycle_id = ? and id = ?",
        (expected.get("cycle_id", ""), expected["item_id"]),
    ).fetchone()
    if not task:
        issues.append(f"task is missing: {expected['item_id']}")
    elif int(task["fence"]) != int(expected["fence"]):
        issues.append(f"fence-stale: {expected['item_id']} expected={expected['fence']} actual={task['fence']}")
    return issues


def require_native_host_identity(host: dict[str, Any], field: str) -> str:
    value = str(host.get(field, "")).strip()
    lowered = value.lower()
    if not value or lowered in {"unknown", "none", "null", "sdk-turn", "sdk-thread", "sdk-worktree"}:
        raise HarnessError(f"native receipt requires real host {field}")
    if lowered.startswith("sdk-"):
        raise HarnessError(f"native receipt rejects placeholder host {field}: {value}")
    return value


def native_receipt_session(conn: sqlite3.Connection, run_id: str, assignment_id: str) -> tuple[sqlite3.Row, dict[str, Any], dict[str, Any]]:
    sessions = conn.execute(
        "select * from agent_provider_sessions where run_id = ? and provider = 'native-codex' order by task_id",
        (run_id,),
    ).fetchall()
    for session in sessions:
        try:
            input_data = json.loads(session["input_json"] or "{}")
        except json.JSONDecodeError:
            continue
        package = input_data.get("native_package", {}) if isinstance(input_data, dict) else {}
        if isinstance(package, dict) and str(package.get("assignment_id", "")) == assignment_id:
            return session, input_data, package
    raise HarnessError(f"native receipt has no exported assignment: {assignment_id}")


def dispatch_import_native(root: Path, run_id: str, receipt_path: Path) -> str:
    if not receipt_path.exists():
        raise HarnessError(f"native receipt file does not exist: {receipt_path}")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HarnessError(f"native receipt is invalid JSON: {exc.msg}") from exc
    if not isinstance(receipt, dict):
        raise HarnessError("native receipt must be a JSON object")
    if str(receipt.get("receipt_version", "")) != "1":
        raise HarnessError("native receipt_version must be 1")
    if str(receipt.get("run_id", "")) != run_id:
        raise HarnessError(f"native receipt run mismatch: expected {run_id}, got {receipt.get('run_id', '')}")
    assignment_id = str(receipt.get("assignment_id", ""))
    if not assignment_id:
        raise HarnessError("native receipt requires assignment_id")
    with connection(root) as conn:
        session, input_data, package = native_receipt_session(conn, run_id, assignment_id)
    package_hash = native_package_hash(package)
    if str(package.get("package_sha256", "")) != package_hash:
        raise HarnessError(f"stored native package hash mismatch: {assignment_id}")
    if str(receipt.get("package_sha256", "")) != package_hash:
        raise HarnessError(f"native receipt package hash mismatch: {assignment_id}")
    receipt_hash = stable_digest(receipt)
    metadata = input_data.get("provider_metadata", {}) if isinstance(input_data, dict) else {}
    metadata = metadata if isinstance(metadata, dict) else {}
    existing_receipt_hash = str(metadata.get("receipt_hash", ""))
    if session["status"] == "receipt_imported":
        if existing_receipt_hash == receipt_hash:
            return "imported 1 native receipt(s) (already applied)"
        raise HarnessError(f"native receipt conflict: {assignment_id}")
    if session["status"] != "package_exported":
        raise HarnessError(f"native receipt cannot import from session status {session['status']}: {assignment_id}")
    if str(package.get("candidate_sha", "")) != current_candidate_sha(root):
        raise HarnessError(f"native package candidate is stale: {assignment_id}")
    if str(receipt.get("status", "")) != "completed":
        raise HarnessError(f"native receipt status is not completed: {receipt.get('status', '')}")

    host = receipt.get("host")
    policy = receipt.get("policy")
    report = receipt.get("report")
    provenance = receipt.get("provenance")
    if not isinstance(host, dict):
        raise HarnessError("native receipt host must be an object")
    if not isinstance(policy, dict):
        raise HarnessError("native receipt policy must be an object")
    if not isinstance(report, dict):
        raise HarnessError("native receipt report must be an object")
    if not isinstance(provenance, dict):
        raise HarnessError("native receipt provenance must be an object")
    host_task_id = require_native_host_identity(host, "task_id")
    host_thread_id = require_native_host_identity(host, "thread_id")
    host_worktree_id = require_native_host_identity(host, "worktree_id")
    host_worktree_path = require_native_host_identity(host, "worktree_path")
    if not str(host.get("surface", "")).strip():
        raise HarnessError("native receipt host requires surface")
    kafa_identities = {run_id, assignment_id, str(package.get("task_id", ""))}
    for field, value in [("task_id", host_task_id), ("thread_id", host_thread_id), ("worktree_id", host_worktree_id)]:
        if value in kafa_identities:
            raise HarnessError(f"native receipt host {field} reuses a Kafa identity: {value}")
    for field in ["approval_mode", "sandbox", "network", "selected_model", "reasoning"]:
        if not str(policy.get(field, "")).strip():
            raise HarnessError(f"native receipt policy requires {field}")

    branch_name = str(receipt.get("branch", ""))
    if branch_name != str(package.get("target_branch", "")):
        raise HarnessError(f"native receipt branch mismatch: expected {package.get('target_branch', '')}, got {branch_name}")
    base_sha = str(receipt.get("base_sha", ""))
    if base_sha != str(package.get("base_sha", "")):
        raise HarnessError(f"native receipt base SHA mismatch: expected {package.get('base_sha', '')}, got {base_sha}")
    head_sha = str(receipt.get("head_sha", ""))
    actual_head = git_ref_commit(root, branch_name)
    if not actual_head or head_sha != actual_head:
        raise HarnessError(f"native receipt head SHA mismatch: expected branch {actual_head or '<missing>'}, got {head_sha or '<missing>'}")
    ancestor = subprocess.run(["git", "merge-base", "--is-ancestor", base_sha, head_sha], cwd=root, text=True, capture_output=True, check=False)
    if ancestor.returncode != 0:
        raise HarnessError(f"native receipt head is not descended from exported base: {assignment_id}")

    hints = package.get("capability_hints", {})
    hints = hints if isinstance(hints, dict) else {}
    if bool(hints.get("requires_sandbox")) and str(policy.get("sandbox", "")).lower() in {"", "unknown", "none", "unavailable"}:
        raise HarnessError(f"native target requires reported sandbox: {assignment_id}")
    if bool(hints.get("requires_no_network")) and str(policy.get("network", "")).lower() not in {"none", "no-network", "disabled"}:
        raise HarnessError(f"native target requires reported no-network policy: {assignment_id}")

    expected = {
        "item_id": str(package["task_id"]),
        "cycle_id": str(package["cycle_id"]),
        "target_id": str(package.get("target_id", "")),
        "command_template": str(package.get("command_template", "")),
        "branch_name": branch_name,
        "fence": str(session["fence"]),
        "agent_id": str(package["agent_id"]),
    }
    with connection(root) as conn:
        issues = codex_report_issues(root, conn, expected, report)
        task = conn.execute(
            "select * from tasks where cycle_id = ? and id = ?",
            (package["cycle_id"], package["task_id"]),
        ).fetchone()
        target = conn.execute("select * from test_targets where id = ?", (package.get("target_id", ""),)).fetchone()
        current_acceptance = sorted(parse_ids(grouped(conn, "task_acceptance", "task_id", "acceptance_id", str(package["cycle_id"])).get(str(package["task_id"]), "")))
        current_failure_modes = sorted(parse_ids(grouped(conn, "task_failure_modes", "task_id", "failure_mode_id", str(package["cycle_id"])).get(str(package["task_id"]), "")))
        current_targets = [
            str(row["target_id"])
            for row in conn.execute(
                "select target_id from task_test_targets where cycle_id = ? and task_id = ? order by target_id",
                (package["cycle_id"], package["task_id"]),
            )
        ]
    if not task:
        issues.append(f"native receipt task is missing: {package['task_id']}")
    elif int(task["fence"] or 0) != int(session["fence"] or 0):
        issues.append(f"native receipt fence is stale: {package['task_id']}")
    elif str(task["task"]) != str(package.get("goal", "")):
        issues.append(f"native package task goal changed: {package['task_id']}")
    if current_acceptance != sorted(str(value) for value in package.get("acceptance_ids", [])):
        issues.append(f"native package acceptance changed: {package['task_id']}")
    if current_failure_modes != sorted(str(value) for value in package.get("failure_mode_ids", [])):
        issues.append(f"native package failure modes changed: {package['task_id']}")
    if current_targets != sorted(str(value) for value in package.get("test_target_ids", [])):
        issues.append(f"native package test targets changed: {package['task_id']}")
    if bool(hints.get("requires_sandbox")) and target is None:
        issues.append(f"native receipt sandbox target is missing: {package.get('target_id', '')}")
    if issues:
        raise HarnessError("native receipt rejected: " + "; ".join(issues[:5]))

    report_id = f"REPORT-NATIVE-{receipt_hash[:12]}"
    attempt_id = f"ATTEMPT-NATIVE-{receipt_hash[:12]}"
    tree_sha = git_ref_tree(root, branch_name)
    finished_at = str(receipt.get("completed_at", "")) or now_iso()
    started_at = str(receipt.get("started_at", "")) or now_iso()
    metadata = {
        **metadata,
        "lifecycle_owner": "native-host",
        "receipt_hash": receipt_hash,
        "host": host,
        "policy": policy,
        "provenance": provenance,
    }
    updated_input = {**input_data, "provider_metadata": metadata, "native_receipt": receipt}
    with transaction(root, touched=[("dispatch_assignment", package["task_id"]), ("task_attempt", attempt_id)]) as conn:
        current = conn.execute("select * from agent_provider_sessions where id = ?", (session["id"],)).fetchone()
        if not current:
            raise HarnessError(f"native provider session disappeared: {assignment_id}")
        if current["status"] == "receipt_imported":
            current_data = json.loads(current["input_json"] or "{}")
            current_metadata = current_data.get("provider_metadata", {}) if isinstance(current_data, dict) else {}
            if isinstance(current_metadata, dict) and current_metadata.get("receipt_hash") == receipt_hash:
                return "imported 1 native receipt(s) (already applied)"
            raise HarnessError(f"native receipt conflict: {assignment_id}")
        if current["status"] != "package_exported":
            raise HarnessError(f"native receipt claim conflict: {assignment_id}")
        existing_thread = conn.execute("select * from agent_sessions where session_id = ?", (host_thread_id,)).fetchone()
        if existing_thread and (existing_thread["agent_id"] != package["agent_id"] or existing_thread["context_id"] != host_task_id):
            raise HarnessError(f"native thread identity is already bound to another task: {host_thread_id}")
        existing_worktree = conn.execute("select * from dispatch_worktrees where id = ?", (host_worktree_id,)).fetchone()
        if existing_worktree and (existing_worktree["run_id"] != run_id or existing_worktree["task_id"] != package["task_id"]):
            raise HarnessError(f"native worktree identity is already bound to another task: {host_worktree_id}")
        conn.execute(
            """
            insert into agent_sessions
            (session_id, agent_id, role, context_id, provider_session_id, origin, trust_level,
             effective_trust, receipt_provenance, status, started_at, ended_at)
            values (?, ?, ?, ?, ?, 'manual', 'local-only', 'local-only', ?, 'reported', ?, ?)
            on conflict(session_id) do update set status='reported', ended_at=excluded.ended_at
            """,
            (host_thread_id, package["agent_id"], package["role"], host_task_id, host_thread_id, str(provenance.get("kind", "audit-only")), started_at, finished_at),
        )
        conn.execute(
            """
            insert into agent_reports
            (id, run_id, task_id, provider_session_id, job_id, status, last_error, result_json, created_at)
            values (?, ?, ?, ?, ?, 'success', '', ?, ?)
            """,
            (report_id, run_id, package["task_id"], host_thread_id, host_task_id, stable_json(report), now_iso()),
        )
        conn.execute(
            """
            insert into task_attempts
            (id, run_id, cycle_id, task_id, agent_id, fence, base_commit_sha, head_commit_sha, tree_sha,
             branch_name, target_id, status, provider_session_id, agent_session_id, report_id, evidence_id, started_at, finished_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reported', ?, ?, ?, '', ?, ?)
            """,
            (
                attempt_id,
                run_id,
                package["cycle_id"],
                package["task_id"],
                package["agent_id"],
                int(session["fence"]),
                base_sha,
                head_sha,
                tree_sha,
                branch_name,
                package.get("target_id", ""),
                host_thread_id,
                host_thread_id,
                report_id,
                started_at,
                finished_at,
            ),
        )
        conn.execute(
            """
            update agent_provider_sessions
            set provider_session_id = ?, provider_job_id = ?, agent_session_id = ?, status = 'receipt_imported',
                worktree_path = ?, input_json = ?, report_id = ?, attempt_id = ?, collected_at = ?, finished_at = ?
            where id = ? and status = 'package_exported'
            """,
            (host_thread_id, host_task_id, host_thread_id, host_worktree_path, stable_json(updated_input), report_id, attempt_id, now_iso(), finished_at, session["id"]),
        )
        conn.execute(
            "update dispatch_assignments set agent_id = ?, status = 'reported', provider_session_id = ?, updated_at = ? where run_id = ? and task_id = ?",
            (package["agent_id"], host_thread_id, now_iso(), run_id, package["task_id"]),
        )
        conn.execute(
            """
            insert into dispatch_worktrees
            (id, run_id, task_id, agent_id, branch_name, worktree_path, status, created_at, cleaned_at)
            values (?, ?, ?, ?, ?, ?, 'host-managed', ?, '')
            """,
            (host_worktree_id, run_id, package["task_id"], package["agent_id"], branch_name, host_worktree_path, now_iso()),
        )
        updated = conn.execute("select * from agent_provider_sessions where id = ?", (session["id"],)).fetchone()
        provider_event(conn, updated, "native_receipt_imported", {"receipt_hash": receipt_hash, "host_task_id": host_task_id, "host_thread_id": host_thread_id, "host_worktree_id": host_worktree_id})
        emit_event(conn, "native_host_receipt_imported", payload(run_id=run_id, task_id=package["task_id"], receipt_hash=receipt_hash, host_task_id=host_task_id, host_thread_id=host_thread_id))
        refresh_dispatch_run_status(conn, run_id)
    return "imported 1 native receipt(s)"


def dispatch_import_csv(root: Path, run_id: str, result_csv: Path) -> str:
    with connection(root) as conn:
        export = latest_fanout_export(conn, run_id)
        input_csv = resolve_runtime_path(root, export["input_csv_path"])
        run = conn.execute("select cycle_id from dispatch_runs where id = ?", (run_id,)).fetchone()
        if not run:
            raise HarnessError(f"missing dispatch run: {run_id}")
        run_cycle_id = str(run["cycle_id"])
    expected_rows = {row["item_id"]: row for row in read_csv_dicts(input_csv)}
    for expected in expected_rows.values():
        expected["cycle_id"] = run_cycle_id
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
            assignment = conn.execute(
                "select cycle_id from dispatch_assignments where run_id = ? and task_id = ?",
                (run_id, item_id),
            ).fetchone()
            if not assignment:
                raise HarnessError(f"missing dispatch assignment: {run_id}:{item_id}")
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
                (id, run_id, cycle_id, task_id, agent_id, fence, base_commit_sha, head_commit_sha, tree_sha,
                 branch_name, target_id, status, provider_session_id, agent_session_id, report_id, evidence_id, started_at, finished_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reported', '', '', ?, '', ?, '')
                """,
                (
                    attempt_id,
                    run_id,
                    assignment["cycle_id"],
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
            refresh_dispatch_run_status(conn, run_id)
    if failed:
        raise HarnessError(f"codex fanout import failed: {run_id}")
    return f"imported {imported} report(s)"


def provider_handle_from_row(row: sqlite3.Row) -> Any:
    from core.agent_provider import AgentJobHandle

    message = row["last_error"]
    try:
        data = json.loads(row["input_json"] or "{}")
    except json.JSONDecodeError:
        data = {}
    if isinstance(data, dict) and isinstance(data.get("provider_metadata"), dict):
        message = stable_json(data["provider_metadata"])
    return AgentJobHandle(
        provider=row["provider"],
        provider_session_id=row["provider_session_id"],
        provider_job_id=row["provider_job_id"],
        status=row["status"],
        message=message,
    )


def expected_from_provider_session(session: sqlite3.Row) -> dict[str, str]:
    data = json.loads(session["input_json"] or "{}")
    return {
        "item_id": session["task_id"],
        "target_id": str(data.get("target_id", "")),
        "branch_name": session["branch_name"],
        "fence": str(session["fence"]),
        "agent_id": session["agent_id"],
        "command_template": str(data.get("command_template", "")),
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
                refresh_dispatch_run_status(conn, run_id)
                provider_event(conn, session, "collect_failed", {"status": report.status, "last_error": report.last_error})
            if session["provider"] == "host-codex":
                cleanup_dispatch_worktrees(root, run_id, session["task_id"], session["agent_id"])
            continue
        try:
            result = json.loads(report.result_json)
        except json.JSONDecodeError as exc:
            with transaction(root, touched=[("agent_provider_session", session["id"]), ("finding", session["task_id"])]) as conn:
                record_integration_finding(conn, run_id, f"provider report invalid for {session['task_id']}: {exc.msg}")
                conn.execute("update agent_provider_sessions set status = 'verification_failed', last_error = ?, finished_at = ? where id = ?", (exc.msg, now_iso(), session["id"]))
                conn.execute("update dispatch_assignments set status = 'verification_failed', updated_at = ? where run_id = ? and task_id = ?", (now_iso(), run_id, session["task_id"]))
                refresh_dispatch_run_status(conn, run_id)
            if session["provider"] == "host-codex":
                cleanup_dispatch_worktrees(root, run_id, session["task_id"], session["agent_id"])
            continue
        with connection(root) as conn:
            assignment = conn.execute(
                "select cycle_id from dispatch_assignments where run_id = ? and task_id = ?",
                (run_id, session["task_id"]),
            ).fetchone()
            expected = expected_from_provider_session(session)
            expected["cycle_id"] = str(assignment["cycle_id"] if assignment else "")
            issues = codex_report_issues(root, conn, expected, result, strict_evidence_fields=session["provider"] == "host-codex")
        if issues:
            with transaction(root, touched=[("agent_provider_session", session["id"]), ("finding", session["task_id"])]) as conn:
                record_integration_finding(conn, run_id, f"provider report rejected for {session['task_id']}: {'; '.join(issues[:5])}")
                conn.execute("update agent_provider_sessions set status = 'verification_failed', last_error = ?, finished_at = ? where id = ?", ("; ".join(issues[:5]), now_iso(), session["id"]))
                conn.execute("update dispatch_assignments set status = 'verification_failed', updated_at = ? where run_id = ? and task_id = ?", (now_iso(), run_id, session["task_id"]))
                refresh_dispatch_run_status(conn, run_id)
                provider_event(conn, session, "collect_rejected", {"issues": issues[:5]})
            if session["provider"] == "host-codex":
                cleanup_dispatch_worktrees(root, run_id, session["task_id"], session["agent_id"])
            continue
        report_id = f"REPORT-{uuid.uuid4().hex[:12]}"
        attempt_id = f"ATTEMPT-{uuid.uuid4().hex[:12]}"
        head_commit = git_ref_commit(root, result["branch_name"])
        tree_sha = git_ref_tree(root, result["branch_name"])
        with transaction(root, touched=[("agent_provider_session", session["id"]), ("task_attempt", attempt_id)]) as conn:
            assignment = conn.execute(
                "select cycle_id from dispatch_assignments where run_id = ? and task_id = ?",
                (run_id, session["task_id"]),
            ).fetchone()
            if not assignment:
                raise HarnessError(f"missing dispatch assignment: {run_id}:{session['task_id']}")
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
                (id, run_id, cycle_id, task_id, agent_id, fence, base_commit_sha, head_commit_sha, tree_sha,
                 branch_name, target_id, status, provider_session_id, agent_session_id, report_id, evidence_id, started_at, finished_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reported', ?, ?, ?, '', ?, '')
                """,
                (
                    attempt_id,
                    run_id,
                    assignment["cycle_id"],
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
            existing_worktree = conn.execute(
                """
                select id from dispatch_worktrees
                where run_id = ? and task_id = ? and agent_id = ? and branch_name = ? and status = 'active'
                order by created_at desc limit 1
                """,
                (run_id, session["task_id"], session["agent_id"], result["branch_name"]),
            ).fetchone()
            if not existing_worktree:
                conn.execute(
                    """
                    insert into dispatch_worktrees
                    (id, run_id, task_id, agent_id, branch_name, worktree_path, status, created_at, cleaned_at)
                    values (?, ?, ?, ?, ?, ?, 'active', ?, '')
                    """,
                    (str(uuid.uuid4()), run_id, session["task_id"], session["agent_id"], result["branch_name"], session["worktree_path"], now_iso()),
                )
            provider_event(conn, session, "collected", {"report_id": report_id, "attempt_id": attempt_id})
            emit_event(conn, "agent_provider_report_collected", payload(run_id=run_id, task_id=session["task_id"], provider=session["provider"], report_id=report_id, attempt_id=attempt_id))
        if session["provider"] == "host-codex":
            remove_worktree_checkout(root, session["worktree_path"])
        collected += 1
    if collected:
        with transaction(root, touched=[("dispatch_run", run_id)]) as conn:
            refresh_dispatch_run_status(conn, run_id)
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
        if session["provider"] == "host-codex":
            cleanup_dispatch_worktrees(root, run_id, session["task_id"], session["agent_id"])
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
    from core.agent_provider import provider_for

    with connection(root) as conn:
        rows = conn.execute(
            """
            select * from agent_provider_sessions
            where run_id = ? and status in ('spawning', 'running') and lease_expires_at is not null
              and lease_expires_at != '' and lease_expires_at <= ?
            """,
            (run_id, now_iso()),
        ).fetchall()
    for row in rows:
        try:
            provider_for(row["provider"]).cancel(provider_handle_from_row(row), "provider session timed out")
        except Exception:
            pass
        if row["provider"] == "host-codex":
            cleanup_dispatch_worktrees(root, run_id, row["task_id"], row["agent_id"])
        with transaction(root, touched=[("dispatch_run", run_id), ("agent_provider_session", row["id"])]) as conn:
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
        with transaction(root, touched=[("dispatch_run", run_id)]) as conn:
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
    refresh_dispatch_run_status(conn, run_id)


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
        acceptance = conn.execute(
            "select acceptance_id from task_acceptance where cycle_id = ? and task_id = ? order by acceptance_id limit 1",
            (attempt["cycle_id"], task_id),
        ).fetchone()
        task = conn.execute("select uid, fence from tasks where cycle_id = ? and id = ?", (attempt["cycle_id"], task_id)).fetchone()
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
    target_requires_sandbox = bool(int(target["requires_sandbox"] or 0))
    target_requires_no_network = bool(int(target["requires_no_network"] or 0))
    target_result_format = str(target["result_format"] or "regex")
    target_result_path = str(target["result_path"] or "")
    if runner != "container" and (target_requires_sandbox or target_requires_no_network):
        reason = "target requires sandbox"
        if target_requires_no_network:
            reason += "; target requires no-network sandbox"
        raise HarnessError(reason)

    worktree = ensure_verification_worktree(root, attempt["branch_name"], run_id, task_id)
    with connection(root) as conn:
        prefixes = executor_prefixes(conn)
    if runner == "container":
        image = resolve_container_image(root, container_image, target_id=target["id"], stack_profile=str(target["stack_profile"] or "python"))
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
                    result_format=target_result_format,
                    result_path=target_result_path,
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
            result_format=target_result_format,
            result_path=target_result_path,
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
        if not (target_result_format != "regex" and result.executed_count_source == "structured"):
            issues.append(f"executed_count_source={result.executed_count_source}")
    if target_result_format != "regex" and result.semantic_status != "pass":
        issues.append(f"semantic_status={result.semantic_status or 'empty'}")
    if result.executed_count <= 0:
        issues.append("executed_count must be > 0")
    if target_requires_sandbox and result.sandbox_status != "available":
        issues.append("target requires sandbox")
    if target_requires_no_network and (not result.no_network or result.sandbox_status != "available"):
        issues.append("target requires no-network sandbox")
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
             target_id, executed_count, executed_count_source, result_format, result_path, semantic_status,
             allow_unlisted, no_network, policy_status, policy_reason,
             sandbox_profile, sandbox_status, sandbox_execution_id, sandbox_engine, container_image, allow_unlisted_reason, trust_anchor, trust_anchor_id,
             attempt_id, tree_sha, code_ref, verified_by, created_at)
            values (?, 'command', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, '', 'local-only', '',
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
                result.result_format,
                target_result_path,
                result.semantic_status,
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
             target_id, executed_count, executed_count_source, result_format, result_path, semantic_status,
             allow_unlisted, no_network, policy_status,
             policy_reason, sandbox_profile, sandbox_status, sandbox_execution_id, sandbox_engine, container_image, allow_unlisted_reason, trust_anchor, trust_anchor_id,
             findings, result, residual_risk, head_commit, source_tree_hash, attempt_id, tree_sha, code_ref,
             verified_by, tracked_diff_hash, project_revision, created_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, '', 'local-only', '',
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
                result.result_format,
                target_result_path,
                result.semantic_status,
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
        refresh_dispatch_run_status(conn, run_id)
        task = conn.execute("select uid, status from tasks where cycle_id = ? and id = ?", (attempt["cycle_id"], task_id)).fetchone()
        if task and task["status"] in {"ready", "claimed", "in_progress"}:
            conn.execute(
                """
                update tasks set status = 'submitted', evidence = ?, submitted_by = ?, submitted_session_id = ?, lease_agent = null,
                  lease_token = null, lease_heartbeat_at = null, lease_expires_at = null,
                  revision = revision + 1, updated_at = ? where uid = ?
                """,
                (evidence_id, attempt["agent_id"], attempt["agent_session_id"], now_iso(), task["uid"]),
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


SCHEMA29_FACT_TABLES = ("requirements", "acceptance", "failure_modes", "tasks")
SCHEMA29_RELATION_TABLES = (
    "requirement_acceptance",
    "failure_mode_acceptance",
    "task_acceptance",
    "task_failure_modes",
    "task_dependencies",
    "task_test_targets",
    "validation_failure_modes",
    "delivery_acceptance",
    "task_attempts",
    "dispatch_assignments",
)


def schema29_fact_uid(table: str, cycle_id: str, local_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"codex-project-harness:{table}:{cycle_id}:{local_id}"))


def insert_snapshot_rows(conn: sqlite3.Connection, table: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    columns = table_columns(conn, table)
    for row in rows:
        writable = [column for column in columns if column in row]
        conn.execute(
            f"insert into {table} ({','.join(writable)}) values ({','.join('?' for _ in writable)})",
            [row[column] for column in writable],
        )


def repair_schema29_relation_cycles(conn: sqlite3.Connection) -> None:
    relation_specs = {
        "requirement_acceptance": (("requirements", "requirement_id"), ("acceptance", "acceptance_id")),
        "failure_mode_acceptance": (("failure_modes", "failure_mode_id"), ("acceptance", "acceptance_id")),
        "task_acceptance": (("tasks", "task_id"), ("acceptance", "acceptance_id")),
        "task_failure_modes": (("tasks", "task_id"), ("failure_modes", "failure_mode_id")),
        "task_dependencies": (("tasks", "task_id"), ("tasks", "depends_on")),
        "task_test_targets": (("tasks", "task_id"),),
        "task_attempts": (("tasks", "task_id"),),
        "dispatch_assignments": (("tasks", "task_id"),),
    }
    for relation, parents in relation_specs.items():
        rows = conn.execute(f"select rowid as migration_rowid, * from {relation}").fetchall()
        for row in rows:
            candidate_cycles: set[str] | None = None
            for parent_table, relation_column in parents:
                parent_cycles = {
                    str(parent["cycle_id"])
                    for parent in conn.execute(
                        f"select cycle_id from {parent_table} where id = ?",
                        (row[relation_column],),
                    )
                    if parent["cycle_id"]
                }
                candidate_cycles = parent_cycles if candidate_cycles is None else candidate_cycles & parent_cycles
            candidates = candidate_cycles or set()
            current_cycle = str(row["cycle_id"] or "")
            if current_cycle in candidates:
                continue
            if len(candidates) != 1:
                raise HarnessError(
                    f"schema29 migration cannot infer relation cycle: {relation}:rowid={row['migration_rowid']}"
                )
            conn.execute(
                f"update {relation} set cycle_id = ? where rowid = ?",
                (next(iter(candidates)), row["migration_rowid"]),
            )

    scoped_specs = {
        "validation_failure_modes": ("validations", "validation_id", "failure_modes", "failure_mode_id"),
        "delivery_acceptance": ("deliveries", "delivery_id", "acceptance", "acceptance_id"),
    }
    for relation, (owner_table, owner_column, parent_table, parent_column) in scoped_specs.items():
        rows = conn.execute(f"select rowid as migration_rowid, * from {relation}").fetchall()
        for row in rows:
            owner_cycles = {
                str(owner["cycle_id"])
                for owner in conn.execute(
                    f"select cycle_id from {owner_table} where id = ?",
                    (row[owner_column],),
                )
                if owner["cycle_id"]
            }
            valid_cycles = {
                cycle_id
                for cycle_id in owner_cycles
                if conn.execute(
                    f"select 1 from {parent_table} where cycle_id = ? and id = ?",
                    (cycle_id, row[parent_column]),
                ).fetchone()
            }
            current_cycle = str(row["cycle_id"] or "")
            if current_cycle in valid_cycles:
                continue
            if len(valid_cycles) != 1:
                raise HarnessError(
                    f"schema29 migration cannot infer relation cycle: {relation}:rowid={row['migration_rowid']}"
                )
            conn.execute(
                f"update {relation} set cycle_id = ? where rowid = ?",
                (next(iter(valid_cycles)), row["migration_rowid"]),
            )


def migrate_cycle_identity_schema29(conn: sqlite3.Connection, *, fallback_cycle_override: str = "") -> None:
    if "uid" in table_columns(conn, "requirements"):
        blank_fact_cycle = any(
            conn.execute(f"select 1 from {table} where cycle_id = '' limit 1").fetchone()
            for table in SCHEMA29_FACT_TABLES
        )
        if not blank_fact_cycle:
            repair_schema29_relation_cycles(conn)
        return
    project = conn.execute("select current_cycle_id from project where id = 1").fetchone()
    fallback_cycle = fallback_cycle_override or (
        str(project["current_cycle_id"] or DEFAULT_CYCLE_ID) if project else DEFAULT_CYCLE_ID
    )
    now = now_iso()
    fallback_status = "archived" if fallback_cycle == LEGACY_CYCLE_ID else "active"
    fallback_phase = "archived" if fallback_cycle == LEGACY_CYCLE_ID else "intake"
    conn.execute(
        """
        insert into delivery_cycles
        (id, name, goal, status, phase, base_ref, candidate_sha, started_at, closed_at, created_at, updated_at)
        values (?, 'Migrated Delivery Cycle', 'Schema 29 identity migration.', ?, ?, '', '', ?, ?, ?, ?)
        on conflict(id) do nothing
        """,
        (fallback_cycle, fallback_status, fallback_phase, now, now if fallback_status == "archived" else "", now, now),
    )
    if fallback_cycle == LEGACY_CYCLE_ID:
        conn.execute(
            """
            insert into delivery_cycles
            (id, name, goal, status, phase, base_ref, candidate_sha, started_at, closed_at, created_at, updated_at)
            values (?, 'Current Delivery Cycle', 'Current active delivery candidate.', 'active', 'intake', '', '', ?, '', ?, ?)
            on conflict(id) do nothing
            """,
            (DEFAULT_CYCLE_ID, now, now, now),
        )
    fact_rows = {table: table_rows(conn, table) for table in SCHEMA29_FACT_TABLES}
    for rows in fact_rows.values():
        for row in rows:
            if not row.get("cycle_id"):
                row["cycle_id"] = fallback_cycle
    relation_rows = {table: table_rows(conn, table) for table in SCHEMA29_RELATION_TABLES}
    cycle_maps = {
        table: {str(row["id"]): str(row["cycle_id"]) for row in rows}
        for table, rows in fact_rows.items()
    }

    def require_cycle(table: str, local_id: str) -> str:
        cycle_id = cycle_maps[table].get(str(local_id), "")
        if not cycle_id:
            raise HarnessError(f"schema29 migration missing cycle identity: {table}:{local_id}")
        return cycle_id

    for row in relation_rows["requirement_acceptance"]:
        req_cycle = require_cycle("requirements", row["requirement_id"])
        acceptance_cycle = require_cycle("acceptance", row["acceptance_id"])
        if req_cycle != acceptance_cycle:
            raise HarnessError("schema29 migration found cross-cycle requirement acceptance link")
        row["cycle_id"] = req_cycle
    for row in relation_rows["failure_mode_acceptance"]:
        fm_cycle = require_cycle("failure_modes", row["failure_mode_id"])
        acceptance_cycle = require_cycle("acceptance", row["acceptance_id"])
        if fm_cycle != acceptance_cycle:
            raise HarnessError("schema29 migration found cross-cycle failure-mode acceptance link")
        row["cycle_id"] = fm_cycle
    for table in ["task_acceptance", "task_failure_modes"]:
        other_table = "acceptance" if table == "task_acceptance" else "failure_modes"
        other_column = "acceptance_id" if table == "task_acceptance" else "failure_mode_id"
        for row in relation_rows[table]:
            task_cycle = require_cycle("tasks", row["task_id"])
            other_cycle = require_cycle(other_table, row[other_column])
            if task_cycle != other_cycle:
                raise HarnessError(f"schema29 migration found cross-cycle {table} link")
            row["cycle_id"] = task_cycle
    for row in relation_rows["task_dependencies"]:
        task_cycle = require_cycle("tasks", row["task_id"])
        dependency_cycle = require_cycle("tasks", row["depends_on"])
        if task_cycle != dependency_cycle:
            raise HarnessError("schema29 migration found cross-cycle task dependency")
        row["cycle_id"] = task_cycle
    for row in relation_rows["task_test_targets"]:
        row["cycle_id"] = require_cycle("tasks", row["task_id"])
    validation_cycles = {
        str(row["id"]): str(row["cycle_id"])
        for row in conn.execute("select id, cycle_id from validations")
    }
    for row in relation_rows["validation_failure_modes"]:
        fm_cycle = require_cycle("failure_modes", row["failure_mode_id"])
        validation_cycle = validation_cycles.get(str(row["validation_id"]), "")
        if not validation_cycle or fm_cycle != validation_cycle:
            raise HarnessError("schema29 migration found cross-cycle validation failure-mode link")
        row["cycle_id"] = fm_cycle
    delivery_cycles = {
        str(row["id"]): str(row["cycle_id"])
        for row in conn.execute("select id, cycle_id from deliveries")
    }
    for row in relation_rows["delivery_acceptance"]:
        acceptance_cycle = require_cycle("acceptance", row["acceptance_id"])
        delivery_cycle = delivery_cycles.get(str(row["delivery_id"]), "")
        if not delivery_cycle or acceptance_cycle != delivery_cycle:
            raise HarnessError("schema29 migration found cross-cycle delivery acceptance link")
        row["cycle_id"] = acceptance_cycle
    for table in ["task_attempts", "dispatch_assignments"]:
        for row in relation_rows[table]:
            row["cycle_id"] = require_cycle("tasks", row["task_id"])

    conn.execute("pragma defer_foreign_keys = on")
    for table in SCHEMA29_RELATION_TABLES:
        conn.execute(f"drop table {table}")
    for table in reversed(SCHEMA29_FACT_TABLES):
        conn.execute(f"drop table {table}")
    create_schema(conn)

    for table, rows in fact_rows.items():
        for row in rows:
            row["uid"] = schema29_fact_uid(table, str(row["cycle_id"]), str(row["id"]))
        insert_snapshot_rows(conn, table, rows)
    for table, rows in relation_rows.items():
        insert_snapshot_rows(conn, table, rows)

    for table, rows in fact_rows.items():
        restored = int(conn.execute(f"select count(*) from {table}").fetchone()[0])
        if restored != len(rows):
            raise HarnessError(f"schema29 migration row count mismatch: {table} expected={len(rows)} actual={restored}")
    for table, rows in relation_rows.items():
        restored = int(conn.execute(f"select count(*) from {table}").fetchone()[0])
        if restored != len(rows):
            raise HarnessError(f"schema29 migration link count mismatch: {table} expected={len(rows)} actual={restored}")


def migrate_quality_gate_schema29(conn: sqlite3.Connection) -> None:
    rows = [
        row_snapshot(row) or {}
        for row in conn.execute("select rowid as legacy_rowid, * from quality_gates order by created_at, rowid")
    ]
    timestamp_counts: dict[tuple[str, str, str], int] = {}
    by_candidate: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        timestamp_key = (str(row["cycle_id"]), str(row["candidate_sha"]), str(row["created_at"]))
        timestamp_counts[timestamp_key] = timestamp_counts.get(timestamp_key, 0) + 1
        by_candidate.setdefault((str(row["cycle_id"]), str(row["candidate_sha"])), []).append(row)
    for sequence, row in enumerate(rows, start=1):
        conn.execute("update quality_gates set sequence = ? where id = ?", (sequence, row["id"]))
    for candidate_rows in by_candidate.values():
        for index, row in enumerate(candidate_rows):
            timestamp_key = (str(row["cycle_id"]), str(row["candidate_sha"]), str(row["created_at"]))
            ambiguous = timestamp_counts[timestamp_key] > 1
            later = candidate_rows[index + 1 :]
            status = "legacy-ambiguous" if ambiguous else ("superseded" if later else "active")
            superseded_by = str(later[0]["id"]) if status == "superseded" else ""
            conn.execute(
                "update quality_gates set gate_status = ?, superseded_by = ? where id = ?",
                (status, superseded_by, row["id"]),
            )
    conn.execute("create unique index if not exists quality_gates_sequence_unique on quality_gates(sequence)")


def downgrade_legacy_trust_schema29(conn: sqlite3.Connection) -> None:
    for table in ["agent_sessions", "session_attestations"]:
        conn.execute(
            f"""
            update {table}
            set effective_trust = case when origin = 'connector' or trust_level = 'connector'
                  then 'legacy-untrusted' else trust_level end,
                receipt_provenance = case when origin = 'connector' or trust_level = 'connector'
                  then 'schema28-unprovable' else 'schema28-local' end
            """
        )
    for table in ["ci_verifications", "external_session_verifications"]:
        conn.execute(
            f"""
            update {table}
            set effective_trust = case when origin = 'connector' or token_status = 'hmac-valid'
                  then 'legacy-untrusted' else 'local-only' end,
                receipt_provenance = case when origin = 'connector' or token_status = 'hmac-valid'
                  then 'schema28-unprovable' else 'schema28-local' end
            """
        )
    conn.execute(
        "update quality_gates set review_trust_level = 'legacy-untrusted' where review_trust_level = 'connector'"
    )


def migrate_schema29(conn: sqlite3.Connection, source_version: int) -> None:
    if source_version >= 29:
        return
    migrate_cycle_identity_schema29(conn)
    migrate_quality_gate_schema29(conn)
    downgrade_legacy_trust_schema29(conn)
    foreign_key_errors = conn.execute("pragma foreign_key_check").fetchall()
    if foreign_key_errors:
        details = "; ".join(":".join(str(value) for value in row) for row in foreign_key_errors[:5])
        raise HarnessError(f"schema29 migration foreign key check failed: {len(foreign_key_errors)} issue(s): {details}")


def validated_migration_path(root: Path, from_version: str, to_version: int) -> tuple[int, list[tuple[int, int]]]:
    try:
        requested_from = int(from_version)
    except ValueError as exc:
        raise HarnessError(f"invalid migration source version: {from_version}") from exc
    if not db_file(root).exists():
        raise HarnessError("migration requires an initialized runtime")
    current_schema_issues: list[str] = []
    with connection(root) as conn:
        project_exists = conn.execute("select 1 from sqlite_master where type = 'table' and name = 'project'").fetchone()
        if not project_exists:
            raise HarnessError("migration requires an initialized runtime")
        row = conn.execute("select schema_version from project where id = 1").fetchone()
        if not row:
            raise HarnessError("migration requires project state")
        actual = int(row["schema_version"])
        if actual == SCHEMA_VERSION:
            try:
                current_schema_issues = runtime_schema_issues(conn)
            except sqlite3.Error as exc:
                current_schema_issues = [f"schema inspection failed: {exc}"]
    if requested_from != actual:
        raise HarnessError(f"migration source mismatch: expected database schema {actual}, received {requested_from}")
    if to_version < actual:
        raise HarnessError(f"schema downgrade is not supported: {actual}->{to_version}")
    if to_version != SCHEMA_VERSION:
        raise HarnessError(f"unregistered migration target: {actual}->{to_version}; supported target is {SCHEMA_VERSION}")
    if actual == SCHEMA_VERSION:
        if current_schema_issues:
            raise HarnessError("current schema is incomplete: " + "; ".join(current_schema_issues[:5]))
        return actual, []
    if actual not in REGISTERED_SCHEMA_SOURCES:
        raise HarnessError(f"unregistered migration source: {actual}")
    return actual, [(actual, SCHEMA_VERSION)]


def migrate(root: Path, from_version: str, to_version: int, *, dry_run: bool = False) -> dict[str, Any] | None:
    if from_version == "markdown-v1":
        if to_version != SCHEMA_VERSION:
            raise HarnessError(f"markdown-v1 target must be current schema {SCHEMA_VERSION}, received {to_version}")
        return migrate_markdown_v1(root, dry_run=dry_run)
    actual_version, path = validated_migration_path(root, from_version, to_version)
    if dry_run:
        return {
            "dry_run": True,
            "imported": {"schema_migration": len(path)},
            "skipped": {},
            "unrecognized": [],
        }
    if not path:
        render_all(root)
        return None
    backup_runtime(root, "migrate")
    try:
        with transaction(root, validate_invariants=False) as conn:
            current = conn.execute("select schema_version from project where id = 1").fetchone()
            if not current or int(current["schema_version"]) != actual_version:
                observed = "missing" if not current else str(current["schema_version"])
                raise HarnessError(f"migration source changed concurrently: expected {actual_version}, actual {observed}")
            source_version, target_version = path[0]
            create_schema(conn)
            if target_version == 29 and "uid" not in table_columns(conn, "requirements"):
                migrate_cycle_identity_schema29(
                    conn,
                    fallback_cycle_override=LEGACY_CYCLE_ID if source_version < 25 else "",
                )
            initialize_project(conn)
            if target_version == 29:
                migrate_schema29(conn, source_version)
            ensure_connector_project_key(conn, root)
            conn.execute(
                "insert into migrations (from_version, to_version, applied_at) values (?, ?, ?)",
                (source_version, target_version, now_iso()),
            )
            updated = conn.execute(
                """
                update project
                set schema_version = ?, runtime_version = ?, revision = revision + 1, updated_at = ?
                where id = 1 and schema_version = ?
                """,
                (target_version, RUNTIME_VERSION, now_iso(), source_version),
            )
            if updated.rowcount != 1:
                raise HarnessError(f"migration source changed concurrently: expected {source_version}")
            emit_event(conn, "migration_applied", payload(**{"from": source_version, "to": target_version}))
            schema_issues = runtime_schema_issues(conn)
            if schema_issues:
                raise HarnessError("migration schema validation failed: " + "; ".join(schema_issues[:5]))
            require_full_invariants(conn, root, "migration")
    except Exception:
        raise
    try:
        render_all(root)
    except Exception as exc:
        raise HarnessError(
            f"migration committed but projection rebuild failed; rerun migrate --from-version {SCHEMA_VERSION} --to-version {SCHEMA_VERSION}: {exc}"
        ) from exc
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
            if "connector_project_key" not in project.keys() or not project["connector_project_key"]:
                with transaction(root, validate_invariants=False) as write_conn:
                    create_schema(write_conn)
                    ensure_columns(write_conn)
                    ensure_connector_project_key(write_conn, root)
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
        ("delivery_cycles", "status", DELIVERY_CYCLE_STATUSES, "delivery cycle status"),
        ("delivery_cycles", "phase", set(PHASES), "delivery cycle phase"),
        ("tasks", "status", TASK_STATUSES, "task status"),
        ("failure_modes", "risk", {"low", "medium", "high", "critical"}, "failure mode risk"),
        ("failure_modes", "status", FAILURE_MODE_STATUSES, "failure mode status"),
        ("validations", "result", {"pass", "fail", "blocked", "partial"}, "validation result"),
        ("validations", "validation_status", VALIDATION_STATUSES, "validation status"),
        ("quality_gates", "reviewer_context", {"fresh", "same-context-degraded", "external"}, "quality gate reviewer context"),
        ("quality_gates", "result", {"pass", "fail", "conditional", "blocked"}, "quality gate result"),
        ("adapters", "mode", ADAPTER_MODES, "adapter mode"),
        ("adapter_actions", "mode", ADAPTER_MODES, "adapter action mode"),
        ("adapter_actions", "status", ADAPTER_ACTION_STATUSES, "adapter action status"),
        ("adapter_actions", "connector_status", CONNECTOR_STATUSES, "adapter action connector status"),
        ("connector_budgets", "status", CONNECTOR_STATUSES, "connector budget status"),
        ("connector_profiles", "status", CONNECTOR_PROFILE_STATUSES, "connector profile status"),
        ("advisory_fallbacks", "status", ADVISORY_FALLBACK_STATUSES, "advisory fallback status"),
        ("agents", "status", {"available", "leased", "disabled"}, "agent status"),
        ("dispatch_runs", "status", DISPATCH_STATUSES, "dispatch run status"),
        ("dispatch_assignments", "status", DISPATCH_STATUSES, "dispatch assignment status"),
        ("test_targets", "kind", TEST_TARGET_KINDS, "test target kind"),
        ("test_targets", "stack_profile", STACK_PROFILES, "test target stack profile"),
        ("test_targets", "result_format", RESULT_FORMATS, "test target result format"),
        ("validations", "trust_anchor", {"local-only", "human-confirmed", "external-session", "ci"}, "validation trust anchor"),
        ("evidence", "trust_anchor", {"local-only", "human-confirmed", "external-session", "ci"}, "evidence trust anchor"),
        ("validations", "sandbox_profile", SANDBOX_PROFILES, "validation sandbox profile"),
        ("evidence", "sandbox_profile", SANDBOX_PROFILES, "evidence sandbox profile"),
        ("validations", "sandbox_status", SANDBOX_STATUSES, "validation sandbox status"),
        ("evidence", "sandbox_status", SANDBOX_STATUSES, "evidence sandbox status"),
        ("validations", "executed_count_source", EXECUTED_COUNT_SOURCES, "validation executed count source"),
        ("evidence", "executed_count_source", EXECUTED_COUNT_SOURCES, "evidence executed count source"),
        ("validations", "result_format", RESULT_FORMATS, "validation result format"),
        ("evidence", "result_format", RESULT_FORMATS, "evidence result format"),
        ("validations", "semantic_status", SEMANTIC_STATUSES, "validation semantic status"),
        ("evidence", "semantic_status", SEMANTIC_STATUSES, "evidence semantic status"),
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
    for row in conn.execute("select tool, project_key, status, scope_json from connector_profiles"):
        issues.extend(connector_profile_issues_for_row(row["tool"], row["project_key"], row["status"], row["scope_json"]))
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
    for row in conn.execute("select * from tasks order by id"):
        data = row_snapshot(row) or {}
        data["acceptance_ids"] = [
            item["acceptance_id"]
            for item in conn.execute(
                "select acceptance_id from task_acceptance where cycle_id = ? and task_id = ? order by acceptance_id",
                (row["cycle_id"], row["id"]),
            )
        ]
        data["failure_mode_ids"] = [
            item["failure_mode_id"]
            for item in conn.execute(
                "select failure_mode_id from task_failure_modes where cycle_id = ? and task_id = ? order by failure_mode_id",
                (row["cycle_id"], row["id"]),
            )
        ]
        data["dependencies"] = [
            item["depends_on"]
            for item in conn.execute(
                "select depends_on from task_dependencies where cycle_id = ? and task_id = ? order by depends_on",
                (row["cycle_id"], row["id"]),
            )
        ]
        tasks.append(data)

    validations = []
    for row in conn.execute("select * from validations order by id"):
        data = row_snapshot(row) or {}
        data["failure_mode_ids"] = [
            item["failure_mode_id"]
            for item in conn.execute(
                "select failure_mode_id from validation_failure_modes where validation_id = ? and cycle_id = ? order by failure_mode_id",
                (row["id"], row["cycle_id"]),
            )
        ]
        validations.append(data)

    project = conn.execute(
        """
        select status, phase, current_cycle_id, connector_project_key, scope_status, current_owner, schema_version, runtime_version, project_id, revision, updated_at
        from project where id = 1
        """
    ).fetchall()

    return [
        ("project-state.schema.json", "project", [row_snapshot(row) or {} for row in project]),
        ("delivery-cycle.schema.json", "delivery_cycles", [row_snapshot(row) or {} for row in conn.execute("select * from delivery_cycles")]),
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
        ("connector-budget.schema.json", "connector_budgets", [row_snapshot(row) or {} for row in conn.execute("select * from connector_budgets")]),
        ("connector-profile.schema.json", "connector_profiles", [row_snapshot(row) or {} for row in conn.execute("select * from connector_profiles")]),
        ("advisory-fallback.schema.json", "advisory_fallbacks", [row_snapshot(row) or {} for row in conn.execute("select * from advisory_fallbacks")]),
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
    cycle_id = current_cycle_id(conn)
    clauses = ["r.status != 'cancelled'", "r.cycle_id = ?"]
    values: list[object] = [cycle_id]
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
          group_concat(distinct t.id) as task_ids,
          group_concat(distinct v.id) as validation_ids,
          group_concat(distinct vfm.failure_mode_id) as failure_mode_ids,
          group_concat(distinct d.id) as delivery_ids
        from requirements r
        left join requirement_acceptance ra on ra.cycle_id = r.cycle_id and ra.requirement_id = r.id
        left join acceptance a on a.cycle_id = ra.cycle_id and a.id = ra.acceptance_id
        left join task_acceptance ta on ta.cycle_id = ra.cycle_id and ta.acceptance_id = ra.acceptance_id
        left join tasks t on t.cycle_id = ta.cycle_id and t.id = ta.task_id
        left join validations v on v.acceptance_id = ra.acceptance_id and v.cycle_id = r.cycle_id and v.result = 'pass'
        left join validation_failure_modes vfm on vfm.validation_id = v.id and vfm.cycle_id = v.cycle_id
        left join delivery_acceptance da on da.cycle_id = ra.cycle_id and da.acceptance_id = ra.acceptance_id
        left join deliveries d on d.id = da.delivery_id and d.cycle_id = da.cycle_id
        where {' and '.join(clauses)}
        group by r.id, ra.acceptance_id
        order by r.id, ra.acceptance_id
        """,
        values,
    ).fetchall()
    return [{key: row[key] or "" for key in row.keys()} for row in rows]


def traceability_issues(conn: sqlite3.Connection, requirement_id: str | None = None) -> list[str]:
    issues: list[str] = []
    cycle_id = current_cycle_id(conn)
    req_clause = "where cycle_id = ? and status != 'cancelled'"
    values: list[object] = [cycle_id]
    if requirement_id:
        req_clause += " and id = ?"
        values.append(requirement_id)
    requirements = conn.execute(f"select id from requirements {req_clause} order by id", values).fetchall()
    for requirement in requirements:
        links = conn.execute(
            "select acceptance_id from requirement_acceptance where cycle_id = ? and requirement_id = ? order by acceptance_id",
            (cycle_id, requirement["id"]),
        ).fetchall()
        if not links:
            issues.append(f"requirement has no acceptance link: {requirement['id']}")
            continue
        for link in links:
            acceptance_id = link["acceptance_id"]
            accepted_task = conn.execute(
                """
                select 1 from task_acceptance ta
                join tasks t on t.cycle_id = ta.cycle_id and t.id = ta.task_id
                where ta.cycle_id = ? and ta.acceptance_id = ? and t.status in ('accepted', 'cancelled', 'skipped')
                limit 1
                """,
                (cycle_id, acceptance_id),
            ).fetchone()
            if not accepted_task:
                issues.append(f"acceptance has no completed task in trace: {requirement['id']} -> {acceptance_id}")
            passing_validation = conn.execute(
                "select 1 from validations where acceptance_id = ? and cycle_id = ? and validation_status = 'active' and result = 'pass' limit 1",
                (acceptance_id, cycle_id),
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


def quickstart_status(root: Path) -> dict[str, Any]:
    if not runtime_initialized(root):
        harness_py = Path(__file__).resolve().with_name("harness.py")
        return {
            "initialized": False,
            "ready_for_delivery": False,
            "missing": ["init"],
            "phase": "",
            "cycle_id": "",
            "cycle_status": "",
            "next_commands": [
                f"python3 {harness_py} --root {root} init",
                f"python3 {harness_py} --root {root} quickstart status",
            ],
        }
    missing: list[str] = []
    next_commands: list[str] = []
    with connection(root) as conn:
        project = project_row(conn)
        cycle = current_cycle_row(conn)
        cycle_id = cycle["id"]
        requirement_count = conn.execute("select count(*) from requirements where cycle_id = ? and status != 'cancelled'", (cycle_id,)).fetchone()[0]
        acceptance_count = conn.execute("select count(*) from acceptance where cycle_id = ?", (cycle_id,)).fetchone()[0]
        trace_count = conn.execute(
            """
            select count(*) from requirement_acceptance ra
            join requirements r on r.cycle_id = ra.cycle_id and r.id = ra.requirement_id
            join acceptance a on a.cycle_id = ra.cycle_id and a.id = ra.acceptance_id
            where ra.cycle_id = ?
            """,
            (cycle_id,),
        ).fetchone()[0]
        task_count = conn.execute("select count(*) from tasks where cycle_id = ?", (cycle_id,)).fetchone()[0]
        target_count = conn.execute("select count(*) from test_targets").fetchone()[0]
        linked_target_count = conn.execute("select count(*) from task_test_targets where cycle_id = ?", (cycle_id,)).fetchone()[0]
        evidence_count = conn.execute("select count(*) from evidence").fetchone()[0]
        accepted_count = conn.execute("select count(*) from tasks where cycle_id = ? and status = 'accepted'", (cycle_id,)).fetchone()[0]
        quality_gate_count = conn.execute(
            "select count(*) from quality_gates where cycle_id = ? and candidate_sha = ? and result = 'pass'",
            (cycle_id, current_candidate_sha(root)),
        ).fetchone()[0]
        delivery_count = conn.execute("select count(*) from deliveries where cycle_id = ?", (cycle_id,)).fetchone()[0]
        validation_count = conn.execute(
            """
            select count(*) from validations v
            where v.cycle_id = ? and v.candidate_sha = ? and v.validation_status = 'active' and v.result = 'pass'
            """,
            (cycle_id, current_candidate_sha(root)),
        ).fetchone()[0]
        baseline_missing = baseline_issues(conn)
        delivery_issues = validate_delivery(conn, root, require_phase=False)

    if requirement_count == 0:
        missing.append("requirement")
        next_commands.append("harness.py requirement add --id REQ1 --kind functional --body '...'")
    if acceptance_count == 0:
        missing.append("acceptance")
        next_commands.append("harness.py acceptance add --id AC1 --criterion '...'")
    if trace_count == 0:
        missing.append("requirement_acceptance_link")
        next_commands.append("harness.py requirement link --requirement REQ1 --acceptance AC1")
    if task_count == 0:
        missing.append("task")
        next_commands.append("harness.py task add --id T1 --task '...' --acceptance AC1")
    if target_count == 0 or linked_target_count == 0:
        missing.append("test_target")
        next_commands.append("harness.py test-target add --id UNIT --kind unit --command-template 'python3 -m unittest'")
    if baseline_missing:
        missing.append("baseline")
        next_commands.append("harness.py baseline freeze --id BL1 --summary 'current scope'")
    if evidence_count == 0:
        missing.append("controller_evidence")
        next_commands.append("harness.py dispatch plan --scope quickstart && harness.py dispatch run developer '...' --target UNIT")
    if validation_count == 0:
        missing.append("validation")
        next_commands.append("harness.py validation record --acceptance AC1 --evidence EXEC-... --result pass")
    if task_count and accepted_count < task_count:
        missing.append("accepted_task")
        next_commands.append("harness.py task accept-ready --id T1 --agent qa-reviewer --evidence 'reviewed'")
    if quality_gate_count == 0:
        missing.append("quality_gate")
        next_commands.append("harness.py gate record --reviewer-context fresh --result pass")
    if delivery_count == 0:
        missing.append("delivery")
        next_commands.append("harness.py phase delivery_readiness && harness.py delivery record --scope '...'")
    return {
        "initialized": True,
        "ready_for_delivery": not delivery_issues and project["phase"] in {"delivery_readiness", "retrospective"} and cycle["status"] in {"active", "delivered"},
        "missing": missing,
        "phase": project["phase"],
        "cycle_id": cycle_id,
        "cycle_status": cycle["status"],
        "delivery_issues": delivery_issues,
        "next_commands": next_commands,
    }


def quickstart_status_lines(root: Path) -> list[str]:
    report = quickstart_status(root)
    lines = [
        "# Kafa Quickstart Status",
        f"initialized: {str(report['initialized']).lower()}",
        f"ready_for_delivery: {str(report['ready_for_delivery']).lower()}",
    ]
    if report.get("phase"):
        lines.append(f"phase: {report['phase']}")
    if report.get("cycle_id"):
        lines.append(f"cycle: {report['cycle_id']} ({report['cycle_status']})")
    missing = report.get("missing", [])
    lines.append("missing: " + (", ".join(missing) if missing else "none"))
    if report.get("delivery_issues"):
        lines.append("delivery_issues:")
        lines.extend(f"- {issue}" for issue in report["delivery_issues"])
    if report.get("next_commands"):
        lines.append("next_commands:")
        lines.extend(f"- {command}" for command in report["next_commands"])
    return lines


def quickstart_minimal(root: Path, quickstart_id: str, goal: str, acceptance: str, task: str, test_command: str, *, execute: bool = False) -> list[str]:
    normalized_id = safe_branch_part(quickstart_id).upper()
    if not normalized_id:
        raise HarnessError("quickstart minimal requires --id")
    req_id = f"{normalized_id}-REQ1"
    ac_id = f"{normalized_id}-AC1"
    task_id = f"{normalized_id}-T1"
    target_id = f"{normalized_id}-UNIT"
    if not execute:
        return [
            f"DRY-RUN: would initialize runtime if needed for {normalized_id}",
            f"DRY-RUN: would record {req_id}, {ac_id}, {task_id}, {target_id}",
            "DRY-RUN: would run the controller-local test command and stop before independent review",
            f"NEXT: add --execute to run: {test_command}",
        ]
    lines: list[str] = []
    if not runtime_initialized(root):
        init_runtime(root)
        lines.append("OK: project harness initialized")
    add_requirement(root, req_id, "functional", goal, priority="must")
    add_acceptance(root, ac_id, acceptance, priority="must")
    link_requirement_acceptance(root, req_id, ac_id)
    add_test_target(root, target_id, "unit", test_command, "quickstart minimal executable target")
    add_task(root, task_id, task, owner="developer", acceptance=ac_id)
    link_task_test_target(root, task_id, target_id)
    lines.extend([f"OK: requirement added {req_id}", f"OK: acceptance added {ac_id}", f"OK: task added {task_id}", f"OK: test target recorded {target_id}"])

    for phase in ["project_bootstrap", "requirement_baseline"]:
        transition_if_needed(root, phase)
    freeze_baseline(root, f"{normalized_id}-BL1", "quickstart minimal baseline", by="quickstart")
    transition_if_needed(root, "confirmation")
    confirm_scope(root, "quickstart", f"{normalized_id}: {goal}")
    transition_if_needed(root, "planning")
    transition_if_needed(root, "implementation")

    run_id = dispatch_plan(root, f"quickstart {normalized_id}")
    evidence_id = dispatch_run(root, "developer", test_command, target_id=target_id, runner="null")
    record_validation(
        root,
        "quickstart minimal",
        "quickstart command passed",
        "pass",
        acceptance=ac_id,
        commands=test_command,
        evidence=evidence_id,
        target_id=target_id,
    )
    lines.append(f"OK: dispatch run {run_id} evidence {evidence_id}")
    lines.append(f"OK: quickstart minimal verified setup {normalized_id}")
    lines.append(f"NEXT: independent reviewer must review and accept {task_id}, then record the quality gate and delivery")
    return lines


def transition_if_needed(root: Path, phase: str) -> None:
    with connection(root) as conn:
        current = project_row(conn)["phase"]
    if current != phase:
        transition_phase(root, phase)


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
                    select uid, id, lease_agent from tasks
                    where lease_expires_at is not null and lease_expires_at <= ? and lease_agent is not null
                    order by id
                    """,
                    (now_iso(),),
                ).fetchall()
                for row in rows:
                    conn.execute(
                        """
                        update tasks set lease_agent = null, lease_token = null, lease_heartbeat_at = null,
                          lease_expires_at = null, revision = revision + 1, updated_at = ? where uid = ?
                        """,
                        (now_iso(), row["uid"]),
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


def grouped(conn: sqlite3.Connection, table: str, key: str, value: str, cycle_id: str = "") -> dict[str, str]:
    where = " where cycle_id = ?" if cycle_id else ""
    params: tuple[str, ...] = (cycle_id,) if cycle_id else ()
    return {
        row[key]: row["ids"]
        for row in conn.execute(
            f"select {key}, group_concat({value}, ', ') as ids from {table}{where} group by {key}",
            params,
        )
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

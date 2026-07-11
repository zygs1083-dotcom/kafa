"""Transactional SQLite schema creation and compatibility columns."""

from __future__ import annotations

import hashlib
import sqlite3

from harness_lib import now_iso
from .errors import HarnessError
from .schema_guard import adapter_action_payload_hash


class SchemaLifecycleError(HarnessError):
    """Raised when schema SQL cannot be applied transactionally."""


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


def execute_transactional_script(conn: sqlite3.Connection, script: str) -> None:
    if not conn.in_transaction:
        raise SchemaLifecycleError("schema SQL requires an active transaction")
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
        raise SchemaLifecycleError("incomplete schema SQL statement")


def ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    columns = {row["name"] for row in conn.execute(f"pragma table_info({table})")}
    if column not in columns:
        conn.execute(f"alter table {table} add column {column} {ddl}")


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

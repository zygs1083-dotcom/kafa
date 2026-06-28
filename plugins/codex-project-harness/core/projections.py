"""Markdown projection builder for SQLite runtime state."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from harness_lib import ensure_parent, markdown_row, write_state


def _runtime():
    import harness_db

    return harness_db


def render_all(root: Path) -> None:
    render_project_state(root)
    render_requirements(root)
    render_traceability(root)
    render_acceptance(root)
    render_failure_modes(root)
    render_tasks(root)
    render_test_targets(root)
    render_validation(root)
    render_evidence(root)
    render_findings(root)
    render_gates(root)
    render_deliveries(root)
    render_decisions(root)
    render_tooling_map(root)
    render_advisory_fallbacks(root)


def render_project_state(root: Path) -> None:
    runtime = _runtime()
    with runtime.connection(root) as conn:
        row = runtime.project_row(conn)
    write_state(
        root,
        {
            "status": row["status"],
            "phase": row["phase"],
            "connector_project_key": row["connector_project_key"],
            "scope_status": row["scope_status"],
            "current_owner": row["current_owner"],
            "schema_version": row["schema_version"],
            "runtime_version": row["runtime_version"],
            "project_id": row["project_id"],
            "revision": row["revision"],
        },
    )


def write_view(root: Path, relpath: str, content: str) -> None:
    path = root / relpath
    ensure_parent(path)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


def render_requirements(root: Path) -> None:
    runtime = _runtime()
    with runtime.connection(root) as conn:
        rows = conn.execute("select * from requirements order by id").fetchall()
    lines = ["# Requirements", "", "| ID | Kind | Body | Priority | Status | Tool Link | Revision |", "| --- | --- | --- | --- | --- | --- | --- |"]
    lines.extend(markdown_row([row["id"], row["kind"], row["body"], row["priority"], row["status"], row["tool_link"], row["revision"]]) for row in rows)
    write_view(root, ".ai-team/requirements/requirements.md", "\n".join(lines))


def render_traceability(root: Path) -> None:
    runtime = _runtime()
    write_view(root, ".ai-team/requirements/traceability.md", "\n".join(runtime.trace_show(root)))


def render_acceptance(root: Path) -> None:
    runtime = _runtime()
    with runtime.connection(root) as conn:
        rows = conn.execute("select * from acceptance order by id").fetchall()
    lines = ["# Acceptance Criteria", "", "| ID | Criterion | Priority | Tool Link | Status |", "| --- | --- | --- | --- | --- |"]
    lines.extend(markdown_row([row["id"], row["criterion"], row["priority"], row["tool_link"], row["status"]]) for row in rows)
    write_view(root, ".ai-team/requirements/acceptance.md", "\n".join(lines))


def render_failure_modes(root: Path) -> None:
    runtime = _runtime()
    with runtime.connection(root) as conn:
        rows = conn.execute("select * from failure_modes order by id").fetchall()
        mappings = {
            row["failure_mode_id"]: row["ids"]
            for row in conn.execute(
                "select failure_mode_id, group_concat(acceptance_id, ', ') as ids from failure_mode_acceptance group by failure_mode_id"
            )
        }
        covered = {
            row["failure_mode_id"]
            for row in conn.execute(
                """
                select distinct vfm.failure_mode_id
                from validation_failure_modes vfm
                join validations v on v.id = vfm.validation_id
                where v.result = 'pass'
                """
            )
        }
    lines = ["# Failure Modes", "", "| ID | Feature | Scenario | Trigger | Expected Behavior | Recovery | Data Safety | Risk | Test Mapping | Status | Derived Coverage | Accepted By | Acceptance Reason | Acceptance Scope | Accepted Revision | Expires At |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for row in rows:
        lines.append(
            markdown_row(
                [
                    row["id"],
                    row["feature"],
                    row["scenario"],
                    row["trigger"],
                    row["expected_behavior"],
                    row["recovery"],
                    row["data_safety"],
                    row["risk"],
                    mappings.get(row["id"], ""),
                    row["status"],
                    "covered" if row["id"] in covered else "",
                    row["accepted_by"] or "",
                    row["acceptance_reason"] or "",
                    row["acceptance_scope"] or "",
                    row["accepted_revision"] or "",
                    row["expires_at"] or "",
                ]
            )
        )
    write_view(root, ".ai-team/requirements/failure-modes.md", "\n".join(lines))


def render_tasks(root: Path) -> None:
    runtime = _runtime()
    with runtime.connection(root) as conn:
        rows = conn.execute("select * from tasks order by id").fetchall()
        acceptance = runtime.grouped(conn, "task_acceptance", "task_id", "acceptance_id")
        failure_modes = runtime.grouped(conn, "task_failure_modes", "task_id", "failure_mode_id")
        dependencies = runtime.grouped(conn, "task_dependencies", "task_id", "depends_on")
    lines = ["# Task Board", "", "| ID | Task | Owner | Status | Acceptance | Failure Modes | Depends On | Tool Link | Evidence | Revision | Lease |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for row in rows:
        lines.append(
            markdown_row(
                [
                    row["id"],
                    row["task"],
                    row["owner"],
                    row["status"],
                    acceptance.get(row["id"], ""),
                    failure_modes.get(row["id"], ""),
                    dependencies.get(row["id"], ""),
                    row["tool_link"],
                    row["evidence"],
                    row["revision"],
                    row["lease_agent"] or "",
                ]
            )
        )
    write_view(root, ".ai-team/planning/task-board.md", "\n".join(lines))


def grouped(conn: sqlite3.Connection, table: str, key: str, value: str) -> dict[str, str]:
    return {
        row[key]: row["ids"]
        for row in conn.execute(f"select {key}, group_concat({value}, ', ') as ids from {table} group by {key}")
    }


def render_test_targets(root: Path) -> None:
    runtime = _runtime()
    with runtime.connection(root) as conn:
        targets = conn.execute("select * from test_targets order by id").fetchall()
        prefixes = conn.execute("select prefix, reason from executor_allowlist order by prefix").fetchall()
    lines = [
        "# Test Targets",
        "",
        "## Registered Targets",
        "",
        "| ID | Kind | Command Template | Stack | Image | Requires Sandbox | Requires No Network | Result Format | Result Path | Gateable | Block Reason | Description |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    lines.extend(
        markdown_row(
            [
                row["id"],
                row["kind"],
                row["command_template"],
                row["stack_profile"] if "stack_profile" in row.keys() else "python",
                row["container_image"] if "container_image" in row.keys() else "",
                row["requires_sandbox"] if "requires_sandbox" in row.keys() else "",
                row["requires_no_network"] if "requires_no_network" in row.keys() else "",
                row["result_format"] if "result_format" in row.keys() else "regex",
                row["result_path"] if "result_path" in row.keys() else "",
                row["gateable"] if "gateable" in row.keys() else "",
                row["gate_block_reason"] if "gate_block_reason" in row.keys() else "",
                row["description"],
            ]
        )
        for row in targets
    )
    lines.extend(["", "## Executor Allow Prefixes", "", "| Prefix | Reason |", "| --- | --- |"])
    lines.extend(markdown_row([row["prefix"], row["reason"]]) for row in prefixes)
    write_view(root, ".ai-team/control/test-targets.md", "\n".join(lines))


def render_validation(root: Path) -> None:
    runtime = _runtime()
    with runtime.connection(root) as conn:
        rows = conn.execute("select * from validations order by created_at, id").fetchall()
        failure_modes = runtime.grouped(conn, "validation_failure_modes", "validation_id", "failure_mode_id")
    lines = ["# Validation", "", "| Surface | Acceptance | Failure Modes | Head | Source Hash | Diff Hash | Project Revision | Tool Context | Commands | Command | Target | Executed Count | Count Source | Exit Code | Stdout SHA256 | Artifact | Policy | Trust Anchor | Sandbox | Findings | Pass/Fail | Residual Risk |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    lines.extend(
        markdown_row(
            [
                row["surface"],
                row["acceptance_id"],
                failure_modes.get(row["id"], ""),
                row["head_commit"],
                row["source_tree_hash"],
                row["tracked_diff_hash"],
                row["project_revision"],
                "",
                row["commands"],
                row["command"] if "command" in row.keys() else "",
                row["target_id"] if "target_id" in row.keys() else "",
                row["executed_count"] if "executed_count" in row.keys() else "",
                row["executed_count_source"] if "executed_count_source" in row.keys() else "",
                row["exit_code"] if "exit_code" in row.keys() else "",
                row["stdout_sha256"] if "stdout_sha256" in row.keys() else "",
                row["artifact_path"] if "artifact_path" in row.keys() else "",
                row["policy_status"] if "policy_status" in row.keys() else "",
                row["trust_anchor"] if "trust_anchor" in row.keys() else "",
                row["sandbox_profile"] if "sandbox_profile" in row.keys() else "",
                row["findings"],
                row["result"],
                row["residual_risk"],
            ]
        )
        for row in rows
    )
    write_view(root, "docs/harness/validation.md", "\n".join(lines))


def render_evidence(root: Path) -> None:
    runtime = _runtime()
    with runtime.connection(root) as conn:
        evidence_rows = conn.execute("select * from evidence order by created_at, id").fetchall()
        test_rows = conn.execute("select * from tests order by created_at, id").fetchall()
    lines = ["# Evidence", "", "## Evidence Records", "", "| ID | Kind | Summary | URI | Hash | Command | Target | Executed Count | Count Source | Exit Code | Stdout SHA256 | Artifact | Source Hash | Policy | Trust Anchor | Sandbox | Created At |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    lines.extend(
        markdown_row(
            [
                row["id"],
                row["kind"],
                row["summary"],
                row["uri"],
                row["hash"],
                row["command"] if "command" in row.keys() else "",
                row["target_id"] if "target_id" in row.keys() else "",
                row["executed_count"] if "executed_count" in row.keys() else "",
                row["executed_count_source"] if "executed_count_source" in row.keys() else "",
                row["exit_code"] if "exit_code" in row.keys() else "",
                row["stdout_sha256"] if "stdout_sha256" in row.keys() else "",
                row["artifact_path"] if "artifact_path" in row.keys() else "",
                row["source_tree_hash"] if "source_tree_hash" in row.keys() else "",
                row["policy_status"] if "policy_status" in row.keys() else "",
                row["trust_anchor"] if "trust_anchor" in row.keys() else "",
                row["sandbox_profile"] if "sandbox_profile" in row.keys() else "",
                row["created_at"],
            ]
        )
        for row in evidence_rows
    )
    lines.extend(["", "## Test Records", "", "| ID | Surface | Command | Result | Evidence | Created At |", "| --- | --- | --- | --- | --- | --- |"])
    lines.extend(markdown_row([row["id"], row["surface"], row["command"], row["result"], row["evidence_id"], row["created_at"]]) for row in test_rows)
    write_view(root, "docs/harness/evidence.md", "\n".join(lines))


def render_findings(root: Path) -> None:
    runtime = _runtime()
    with runtime.connection(root) as conn:
        rows = conn.execute("select * from findings order by created_at, id").fetchall()
    lines = ["# Findings", "", "| ID | Surface | Severity | Status | Summary | Evidence | Created At |", "| --- | --- | --- | --- | --- | --- | --- |"]
    lines.extend(markdown_row([row["id"], row["surface"], row["severity"], row["status"], row["summary"], row["evidence_id"], row["created_at"]]) for row in rows)
    write_view(root, "docs/harness/findings.md", "\n".join(lines))


def render_gates(root: Path) -> None:
    runtime = _runtime()
    with runtime.connection(root) as conn:
        rows = conn.execute("select * from quality_gates order by created_at, id").fetchall()
    lines = ["# Quality Gates", "", "| Gate | Commit | Base | Head | Source Hash | Diff Hash | Project Revision | Reviewer Context | Result | Blocking Findings | Commands | Evidence | Residual Risk |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    lines.extend(markdown_row([row["gate"], row["reviewed_commit"], row["base_commit"], row["head_commit"], row["diff_hash"], row["tracked_diff_hash"], row["project_revision"], row["reviewer_context"], row["result"], row["blocking_findings"], row["commands"], row["evidence"], row["residual_risk"]]) for row in rows)
    write_view(root, "docs/harness/quality-gates.md", "\n".join(lines))


def render_deliveries(root: Path) -> None:
    runtime = _runtime()
    with runtime.connection(root) as conn:
        rows = conn.execute("select * from deliveries order by created_at, id").fetchall()
    lines = ["# Delivery", ""]
    for row in rows:
        lines.extend(
            [
                f"## Delivery Record {row['created_at']}",
                "",
                "### Scope",
                row["scope"],
                "",
                "### Acceptance Mapping",
                row["acceptance"],
                "",
                "### Changed Files",
                row["changed_files"],
                "",
                "### Validation",
                row["validation"],
                "",
                "### Independent QA",
                row["qa"],
                "",
                "### Collaboration Links",
                row["collaboration_links"],
                "",
                "### Failure Mode Coverage",
                row["failure_mode_coverage"],
                "",
                "### Quality Gate",
                row["quality_gate"],
                "",
                "### Data / Config Notes",
                row["data_config_notes"],
                "",
                "### Known Gaps",
                row["known_gaps"],
                "",
                "### Handoff Notes",
                row["handoff"],
                "",
                "### Out Of Scope",
                "Deployment, production release, infrastructure provisioning, production migrations, secret changes, and paid-resource creation.",
                "",
            ]
        )
    write_view(root, "docs/harness/delivery.md", "\n".join(lines))


def render_decisions(root: Path) -> None:
    runtime = _runtime()
    with runtime.connection(root) as conn:
        rows = conn.execute("select * from decisions order by created_at, id").fetchall()
    lines = ["# Decision Log", "", "| Date | Decision | Reason |", "| --- | --- | --- |"]
    lines.extend(markdown_row([row["created_at"], row["decision"], row["reason"]]) for row in rows)
    write_view(root, ".ai-team/control/decision-log.md", "\n".join(lines))


def render_tooling_map(root: Path) -> None:
    runtime = _runtime()
    with runtime.connection(root) as conn:
        rows = conn.execute("select * from adapters order by tool, artifact").fetchall()
    lines = ["# Tooling Map", "", "| Artifact | Source Of Truth | External Tool | External ID / Link | Fallback | Mode | Idempotency Key |", "| --- | --- | --- | --- | --- | --- | --- |"]
    if not rows:
        defaults = [
            ("Requirements", "local", "", "", ".ai-team/requirements/requirements.md", "", ""),
            ("Tasks", "local", "", "", ".ai-team/planning/task-board.md", "", ""),
            ("Validation", "local", "", "", "docs/harness/validation.md", "", ""),
            ("Delivery", "local", "", "", "docs/harness/delivery.md", "", ""),
        ]
        lines.extend(markdown_row(list(row)) for row in defaults)
    else:
        lines.extend(
            markdown_row(
                [
                    row["artifact"],
                    "local",
                    row["tool"],
                    row["external_link"] or row["external_id"],
                    row["fallback"],
                    row["mode"],
                    row["idempotency_key"],
                ]
            )
            for row in rows
        )
    write_view(root, ".ai-team/control/tooling-map.md", "\n".join(lines))


def render_advisory_fallbacks(root: Path) -> None:
    runtime = _runtime()
    with runtime.connection(root) as conn:
        exists = conn.execute("select 1 from sqlite_master where type='table' and name='advisory_fallbacks'").fetchone()
        rows = []
        if exists:
            rows = conn.execute("select * from advisory_fallbacks order by generated_at, action_id").fetchall()
    lines = [
        "# Advisory Fallbacks",
        "",
        "These records are local advisory drafts only. They are not delivery evidence.",
        "",
        "| Action | Connector | Operation | Kind | Official Capability | Status | Delivery Eligible | Artifact | Summary |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    lines.extend(
        markdown_row(
            [
                row["action_id"],
                row["tool"],
                row["operation"],
                row["fallback_kind"],
                row["official_capability"],
                row["status"],
                row["delivery_eligible"],
                row["artifact_path"],
                row["summary"],
            ]
        )
        for row in rows
    )
    write_view(root, ".ai-team/control/advisory-fallbacks.md", "\n".join(lines))

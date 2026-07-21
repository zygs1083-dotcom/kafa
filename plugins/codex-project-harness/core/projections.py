"""Markdown projection builder for SQLite runtime state."""
from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import Callable, Iterable

from harness_lib import ensure_parent, markdown_row, write_state
from .errors import HarnessError
from .project_fs import ProjectFS
from .schema_guard import (
    ACCEPTANCE_STATUSES,
    FAILURE_MODE_STATUSES,
    REQUIREMENT_STATUSES,
)


def _runtime():
    import harness_db

    return harness_db


def render_all(
    root: Path,
    *,
    failure_mode_evidence_root: Path | None = None,
    failure_mode_candidate: str | None = None,
    trace_evidence_root: Path | None = None,
    trace_candidate: str | None = None,
) -> None:
    render_affected(
        root,
        PROJECTION_NAMES,
        failure_mode_evidence_root=failure_mode_evidence_root,
        failure_mode_candidate=failure_mode_candidate,
        trace_evidence_root=trace_evidence_root,
        trace_candidate=trace_candidate,
    )


def render_project_state(root: Path) -> None:
    runtime = _runtime()
    with runtime.connection(root) as conn:
        row = runtime.project_row(conn)
    write_state(
        root,
        {
            "id": row["id"],
            "status": row["status"],
            "phase": row["phase"],
            "current_cycle_id": row["current_cycle_id"],
            "scope_status": row["scope_status"],
            "current_owner": row["current_owner"],
            "schema_version": row["schema_version"],
            "runtime_version": row["runtime_version"],
            "project_id": row["project_id"],
            "revision": row["revision"],
            "updated_at": row["updated_at"],
        },
        merge_existing=False,
        include_blocked_reason=False,
    )


def write_view(root: Path, relpath: str, content: str) -> None:
    with ProjectFS.open(root) as project_fs:
        project_fs.atomic_write(
            Path(relpath),
            (content.rstrip() + "\n").encode("utf-8"),
            mode=0o644,
        )


def _remove_retired_projection(root: Path, relative: Path) -> None:
    with ProjectFS.open(root) as project_fs:
        snapshot = project_fs._snapshot(relative, allow_missing=True)
        if snapshot.exists:
            project_fs.unlink_regular(relative, expected=snapshot)


def render_requirements(root: Path) -> None:
    runtime = _runtime()
    with runtime.connection(root) as conn:
        cycle_id = runtime.current_cycle_id(conn)
        rows = conn.execute("select * from requirements where cycle_id = ? order by id", (cycle_id,)).fetchall()
    lines = ["# Requirements", "", "| ID | Kind | Body | Priority | Status | Revision |", "| --- | --- | --- | --- | --- | --- |"]
    lines.extend(markdown_row([row["id"], row["kind"], row["body"], row["priority"], row["status"], row["revision"]]) for row in rows)
    write_view(root, ".ai-team/requirements/requirements.md", "\n".join(lines))


def render_traceability(
    root: Path,
    *,
    evidence_root: Path | None = None,
    candidate_override: str | None = None,
) -> None:
    runtime = _runtime()
    write_view(
        root,
        ".ai-team/requirements/traceability.md",
        "\n".join(
            runtime.trace_show(
                root,
                evidence_root=evidence_root,
                candidate_override=candidate_override,
            )
        ),
    )


def render_acceptance(root: Path) -> None:
    runtime = _runtime()
    with runtime.connection(root) as conn:
        cycle_id = runtime.current_cycle_id(conn)
        rows = conn.execute("select * from acceptance where cycle_id = ? order by id", (cycle_id,)).fetchall()
    lines = ["# Acceptance Criteria", "", "| ID | Criterion | Priority | Status |", "| --- | --- | --- | --- |"]
    lines.extend(markdown_row([row["id"], row["criterion"], row["priority"], row["status"]]) for row in rows)
    write_view(root, ".ai-team/requirements/acceptance.md", "\n".join(lines))


def render_failure_modes(
    root: Path,
    *,
    evidence_root: Path | None = None,
    candidate_override: str | None = None,
) -> None:
    """Render failure-mode coverage from the DB and its live evidence root.

    Schema 30 projects retain the historical audit-only coverage projection.
    Schema 31 coverage is stricter and depends on current-candidate immutable
    execution artifacts.  Projection verification renders from a DB backup in
    a temporary root, so it must explicitly use the original project as the
    evidence authority instead of treating the empty verifier root as a new
    candidate.
    """

    runtime = _runtime()
    from .cycle_ledger import current_candidate_sha
    from .delivery import qualified_validation_execution_issues

    with runtime.connection(root) as conn:
        cycle_id = runtime.current_cycle_id(conn)
        rows = conn.execute("select * from failure_modes where cycle_id = ? order by id", (cycle_id,)).fetchall()
        mappings = {
            row["failure_mode_id"]: row["ids"]
            for row in conn.execute(
                "select failure_mode_id, group_concat(acceptance_id, ', ') as ids from failure_mode_acceptance where cycle_id = ? group by failure_mode_id",
                (cycle_id,),
            )
        }
        validation_columns = {
            str(column["name"])
            for column in conn.execute("pragma table_info(validations)")
        }
        if "qualification_id" not in validation_columns:
            covered = {
                str(row["failure_mode_id"])
                for row in conn.execute(
                    """
                    select distinct vfm.failure_mode_id
                    from validation_failure_modes vfm
                    join validations v on v.id = vfm.validation_id
                    where vfm.cycle_id = ? and v.cycle_id = vfm.cycle_id
                      and v.result = 'pass'
                    """,
                    (cycle_id,),
                )
            }
        else:
            evidence_authority = evidence_root or root
            candidate = candidate_override or current_candidate_sha(
                evidence_authority
            )
            covered = set()
            coverage_candidates = conn.execute(
                """
                select vfm.failure_mode_id, fm.risk, v.*
                from validation_failure_modes vfm
                join failure_modes fm
                  on fm.cycle_id = vfm.cycle_id and fm.id = vfm.failure_mode_id
                join validations v on v.id = vfm.validation_id
                join failure_mode_acceptance fma
                  on fma.cycle_id = vfm.cycle_id
                 and fma.failure_mode_id = vfm.failure_mode_id
                 and fma.acceptance_id = v.acceptance_id
                where vfm.cycle_id = ? and v.cycle_id = vfm.cycle_id
                  and v.candidate_sha = ? and v.result = 'pass'
                  and v.validation_status = 'active'
                  and v.qualification_id is not null
                order by vfm.failure_mode_id, v.created_at desc, v.id desc
                """,
                (cycle_id, candidate),
            ).fetchall()
            for validation in coverage_candidates:
                qualification = conn.execute(
                    "select * from acceptance_target_qualifications where id = ?",
                    (validation["qualification_id"],),
                ).fetchone()
                if qualification is None:
                    continue
                if not qualified_validation_execution_issues(
                    conn,
                    evidence_authority,
                    validation,
                    qualification,
                    candidate,
                    require_structured=str(validation["risk"])
                    in {"medium", "high", "critical"},
                ):
                    covered.add(str(validation["failure_mode_id"]))
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
        cycle_id = runtime.current_cycle_id(conn)
        rows = conn.execute("select * from tasks where cycle_id = ? order by id", (cycle_id,)).fetchall()
        acceptance = runtime.grouped(conn, "task_acceptance", "task_id", "acceptance_id", cycle_id)
        failure_modes = runtime.grouped(conn, "task_failure_modes", "task_id", "failure_mode_id", cycle_id)
        dependencies = runtime.grouped(conn, "task_dependencies", "task_id", "depends_on", cycle_id)
    lines = ["# Task Board", "", "| ID | Task | Owner | Status | Acceptance | Failure Modes | Depends On | Evidence | Producer Context | Revision |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
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
                    row["evidence"],
                    row["submitted_context_id"],
                    row["revision"],
                ]
            )
        )
    write_view(root, ".ai-team/planning/task-board.md", "\n".join(lines))
def render_test_targets(root: Path) -> None:
    runtime = _runtime()
    with runtime.connection(root) as conn:
        targets = conn.execute("select * from test_targets order by id").fetchall()
        has_qualifications = conn.execute(
            "select 1 from sqlite_master where type='table' "
            "and name='acceptance_target_qualifications'"
        ).fetchone()
        qualifications = (
            conn.execute(
                """
                select q.*,
                       coalesce(
                         (
                           select g.id
                           from quality_gate_qualifications qg
                           join quality_gates g on g.id = qg.gate_id
                           where qg.qualification_id = q.id
                           order by g.sequence desc
                           limit 1
                         ),
                         'unreviewed'
                       ) as gate_id,
                       coalesce(
                         (
                           select g.candidate_sha
                           from quality_gate_qualifications qg
                           join quality_gates g on g.id = qg.gate_id
                           where qg.qualification_id = q.id
                           order by g.sequence desc
                           limit 1
                         ),
                         ''
                       ) as gate_candidate_sha,
                       coalesce(
                         (
                           select g.gate_status
                           from quality_gate_qualifications qg
                           join quality_gates g on g.id = qg.gate_id
                           where qg.qualification_id = q.id
                           order by g.sequence desc
                           limit 1
                         ),
                         'unreviewed'
                       ) as gate_status,
                       coalesce(
                         (
                           select g.review_status
                           from quality_gate_qualifications qg
                           join quality_gates g on g.id = qg.gate_id
                           where qg.qualification_id = q.id
                           order by g.sequence desc
                           limit 1
                         ),
                         'unreviewed'
                       ) as gate_review_status
                from acceptance_target_qualifications q
                order by q.created_at, q.id
                """
            ).fetchall()
            if has_qualifications
            else []
        )
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
    lines.extend(
        [
            "",
            "## Acceptance-Target Qualifications",
            "",
            "These insert-only rows record procedural accountability; they do not prove semantic correctness or cryptographic provenance.",
            "",
            "| ID | Cycle | Acceptance | Acceptance Revision | Target | Target Definition SHA-256 | Rationale | Qualified By | Gate ID | Gate Candidate | Gate Status | Gate Review Status | Created At |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    lines.extend(
        markdown_row(
            [
                row["id"],
                row["cycle_id"],
                row["acceptance_id"],
                row["acceptance_revision"],
                row["target_id"],
                row["target_definition_sha256"],
                row["rationale"],
                row["qualified_by"],
                row["gate_id"],
                row["gate_candidate_sha"],
                row["gate_status"],
                row["gate_review_status"],
                row["created_at"],
            ]
        )
        for row in qualifications
    )
    write_view(root, ".ai-team/control/test-targets.md", "\n".join(lines))


def render_validation(root: Path) -> None:
    runtime = _runtime()
    with runtime.connection(root) as conn:
        cycle_id = runtime.current_cycle_id(conn)
        rows = conn.execute("select * from validations where cycle_id = ? order by created_at, id", (cycle_id,)).fetchall()
        failure_modes = runtime.grouped(conn, "validation_failure_modes", "validation_id", "failure_mode_id", cycle_id)
        execution_rows = {
            row["id"]: conn.execute(
                """
                select e.* from validation_executions ve
                join executions e on e.id = ve.execution_id
                where ve.validation_id = ? order by e.created_at, e.id
                """,
                (row["id"],),
            ).fetchall()
            for row in rows
        }
    lines = [
        "# Validation",
        "",
        "| ID | Candidate | Surface | Acceptance | Failure Modes | Result | Status | Execution | Target | Command | Count | Format | Semantic | Exit | Stdout SHA256 | Artifact | Runner | Sandbox | No Network | Policy | Findings | Residual Risk |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        linked = execution_rows[row["id"]] or [None]
        for execution in linked:
            lines.append(
                markdown_row(
                    [
                        row["id"],
                        row["candidate_sha"],
                        row["surface"],
                        row["acceptance_id"] or "",
                        failure_modes.get(row["id"], ""),
                        row["result"],
                        row["validation_status"],
                        execution["id"] if execution else "",
                        execution["target_id"] if execution else "",
                        execution["command"] if execution else "",
                        execution["executed_count"] if execution else "",
                        execution["result_format"] if execution else "",
                        execution["semantic_status"] if execution else "",
                        execution["exit_code"] if execution else "",
                        execution["stdout_sha256"] if execution else "",
                        execution["artifact_path"] if execution else "",
                        execution["runner"] if execution else "",
                        execution["sandbox_status"] if execution else "",
                        execution["no_network"] if execution else "",
                        execution["policy_status"] if execution else "",
                        row["findings"],
                        row["residual_risk"],
                    ]
                )
            )
    write_view(root, "docs/harness/validation.md", "\n".join(lines))


def render_executions(root: Path) -> None:
    runtime = _runtime()
    with runtime.connection(root) as conn:
        rows = conn.execute("select * from executions order by created_at, id").fetchall()
        project = conn.execute(
            "select schema_version from project where id = 1"
        ).fetchone()
    schema_version = int(project[0]) if project is not None else 0
    if schema_version >= 31:
        lines = [
            "# Immutable Executions",
            "",
            "| ID | Cycle | Candidate | Target | Target Definition SHA-256 | Command | Exit | Stdout SHA256 | Artifact | Count | Format | Semantic | Runner | Sandbox | No Network | Policy | Platform | Runtime Executable | Runtime Version | Runtime Executable SHA-256 | Policy Version | Container Engine | Container Engine Version | Container Engine Endpoint | Container Image Requested | Container Image Digest | Provenance Status | Created At |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        lines.extend(
            markdown_row(
                [
                    row["id"], row["cycle_id"], row["candidate_sha"],
                    row["target_id"] or "", row["target_definition_sha256"],
                    row["command"], row["exit_code"], row["stdout_sha256"],
                    row["artifact_path"], row["executed_count"],
                    row["result_format"], row["semantic_status"], row["runner"],
                    row["sandbox_status"], row["no_network"], row["policy_status"],
                    row["platform"], row["runtime_executable"],
                    row["runtime_version"], row["runtime_executable_sha256"],
                    row["policy_version"], row["container_engine"],
                    row["container_engine_version"],
                    row["container_engine_endpoint"],
                    row["container_image_requested"],
                    row["container_image_digest"], row["provenance_status"],
                    row["created_at"],
                ]
            )
            for row in rows
        )
    else:
        lines = [
            "# Immutable Executions",
            "",
            "| ID | Cycle | Candidate | Target | Command | Exit | Stdout SHA256 | Artifact | Count | Format | Semantic | Runner | Sandbox | No Network | Policy | Created At |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        lines.extend(
            markdown_row(
                [
                    row["id"], row["cycle_id"], row["candidate_sha"],
                    row["target_id"] or "", row["command"], row["exit_code"],
                    row["stdout_sha256"], row["artifact_path"],
                    row["executed_count"], row["result_format"],
                    row["semantic_status"], row["runner"],
                    row["sandbox_status"], row["no_network"],
                    row["policy_status"], row["created_at"],
                ]
            )
            for row in rows
        )
    write_view(root, "docs/harness/executions.md", "\n".join(lines))
    _remove_retired_projection(root, Path("docs/harness/evidence.md"))


def render_findings(root: Path) -> None:
    runtime = _runtime()
    with runtime.connection(root) as conn:
        rows = conn.execute("select * from findings order by created_at, id").fetchall()
    lines = ["# Findings", "", "| ID | Cycle | Candidate | Surface | Severity | Status | Summary | Accepted By | Reason | Scope | Revision | Expires | Created At |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    lines.extend(markdown_row([row["id"], row["cycle_id"], row["candidate_sha"], row["surface"], row["severity"], row["status"], row["summary"], row["waived_by"], row["waiver_reason"], row["waiver_scope"], row["waived_revision"] or "", row["waiver_expires_at"], row["created_at"]]) for row in rows)
    write_view(root, "docs/harness/findings.md", "\n".join(lines))


def render_gates(root: Path) -> None:
    runtime = _runtime()
    with runtime.connection(root) as conn:
        cycle_id = runtime.current_cycle_id(conn)
        rows = conn.execute("select * from quality_gates where cycle_id = ? order by sequence", (cycle_id,)).fetchall()
    lines = ["# Quality Gates", "", "| Gate | Candidate | Producer Context | Reviewer Context | Review Status | Result | Blocking Findings | Residual Risk | Revision | Created At |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    lines.extend(markdown_row([row["gate"], row["candidate_sha"], row["producer_context_id"], row["reviewer_context_id"], row["review_status"], row["result"], row["blocking_findings"], row["residual_risk"], row["reviewed_revision"], row["created_at"]]) for row in rows)
    write_view(root, "docs/harness/quality-gates.md", "\n".join(lines))


def render_deliveries(root: Path) -> None:
    runtime = _runtime()
    with runtime.connection(root) as conn:
        cycle_id = runtime.current_cycle_id(conn)
        rows = conn.execute("select * from deliveries where cycle_id = ? order by created_at, id", (cycle_id,)).fetchall()
    lines = ["# Delivery", ""]
    for row in rows:
        lines.extend(
            [
                f"## Delivery Record {row['created_at']}",
                "",
                "### Decision Status",
                row["decision_status"],
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


PROJECTION_PATHS: tuple[Path, ...] = (
    Path(".ai-team/control/project-state.yaml"),
    Path(".ai-team/requirements/requirements.md"),
    Path(".ai-team/requirements/traceability.md"),
    Path(".ai-team/requirements/acceptance.md"),
    Path(".ai-team/requirements/failure-modes.md"),
    Path(".ai-team/planning/task-board.md"),
    Path(".ai-team/control/test-targets.md"),
    Path("docs/harness/validation.md"),
    Path("docs/harness/executions.md"),
    Path("docs/harness/findings.md"),
    Path("docs/harness/quality-gates.md"),
    Path("docs/harness/delivery.md"),
    Path(".ai-team/control/decision-log.md"),
)
PROJECTION_ROLLBACK_PATHS: tuple[Path, ...] = (
    *PROJECTION_PATHS,
    Path("docs/harness/evidence.md"),
)


PROJECTION_RENDERERS: tuple[tuple[str, Callable[[Path], None]], ...] = (
    ("project-state", render_project_state),
    ("requirements", render_requirements),
    ("traceability", render_traceability),
    ("acceptance", render_acceptance),
    ("failure-modes", render_failure_modes),
    ("tasks", render_tasks),
    ("test-targets", render_test_targets),
    ("validation", render_validation),
    ("executions", render_executions),
    ("findings", render_findings),
    ("gates", render_gates),
    ("deliveries", render_deliveries),
    ("decisions", render_decisions),
)
PROJECTION_NAMES = tuple(name for name, _ in PROJECTION_RENDERERS)


def preflight_projection_paths(root: Path) -> None:
    """Reject every unsafe live or retired projection before publication."""

    with ProjectFS.open(root) as project_fs:
        project_fs.audit(PROJECTION_ROLLBACK_PATHS, allow_missing=True)


def _preflight_projection_states(root: Path) -> None:
    """Reject non-canonical entity states before publishing any view bytes."""

    runtime = _runtime()
    with runtime.connection(root) as conn:
        project = conn.execute(
            "select schema_version from project where id = 1"
        ).fetchone()
        schema_version = int(project[0]) if project is not None else 0
        contracts = (
            ("requirements", "requirement", REQUIREMENT_STATUSES),
            ("acceptance", "acceptance", ACCEPTANCE_STATUSES),
            (
                "failure_modes",
                "failure mode",
                FAILURE_MODE_STATUSES
                | ({"active"} if schema_version <= 30 else set()),
            ),
        )
        for table, label, allowed in contracts:
            for row in conn.execute(
                f"select id, status from {table} order by id"
            ):
                value = str(row["status"])
                if value not in allowed:
                    raise HarnessError(
                        "projection state preflight failed: "
                        f"invalid {label} status: {table}:{row['id']}.status={value!r}"
                    )


def render_affected(
    root: Path,
    projections: Iterable[str],
    *,
    failure_mode_evidence_root: Path | None = None,
    failure_mode_candidate: str | None = None,
    trace_evidence_root: Path | None = None,
    trace_candidate: str | None = None,
) -> None:
    """Rebuild only explicitly affected generated views in stable order."""

    selected = frozenset(projections)
    unknown = sorted(selected - set(PROJECTION_NAMES))
    if unknown:
        raise ValueError(f"unknown projection(s): {', '.join(unknown)}")
    preflight_projection_paths(root)
    _preflight_projection_states(root)
    for name, renderer in PROJECTION_RENDERERS:
        if name in selected:
            if name == "failure-modes":
                render_failure_modes(
                    root,
                    evidence_root=failure_mode_evidence_root,
                    candidate_override=failure_mode_candidate,
                )
            elif name == "traceability":
                render_traceability(
                    root,
                    evidence_root=trace_evidence_root,
                    candidate_override=trace_candidate,
                )
            else:
                renderer(root)


def _snapshot_projection_execution_artifacts(
    actual_fs: ProjectFS,
    evidence_root: Path,
    artifact_paths: Iterable[str],
) -> tuple[dict[Path, tuple[object, str | None]], list[str]]:
    """Copy safe referenced artifacts into an isolated immutable verifier root."""

    receipts: dict[Path, tuple[object, str | None]] = {}
    issues: list[str] = []
    with ProjectFS.open(evidence_root) as evidence_fs:
        for raw_path in sorted(set(artifact_paths)):
            try:
                relative = actual_fs.relative_to_root(Path(raw_path))
                snapshot = actual_fs._snapshot(relative, allow_missing=True)
                if relative in receipts:
                    continue
                if not snapshot.exists:
                    receipts[relative] = (snapshot, None)
                    continue
                payload = actual_fs.read_bytes(relative, expected=snapshot)
                actual_fs._assert_unchanged(relative, snapshot)
                digest = hashlib.sha256(payload).hexdigest()
                evidence_fs.atomic_write(relative, payload, mode=0o600)
                receipts[relative] = (snapshot, digest)
            except Exception as exc:
                issues.append(
                    "execution artifact could not be snapshotted for projection "
                    f"verification: {raw_path}: {exc}"
                )
    return receipts, issues


def _projection_artifact_receipt_issues(
    actual_fs: ProjectFS,
    receipts: dict[Path, tuple[object, str | None]],
) -> list[str]:
    issues: list[str] = []
    for relative, (snapshot, expected_digest) in receipts.items():
        try:
            current = actual_fs._snapshot(relative, allow_missing=True)
            if current != snapshot:
                raise RuntimeError("path identity changed")
            if expected_digest is not None:
                payload = actual_fs.read_bytes(relative, expected=snapshot)
                actual_fs._assert_unchanged(relative, snapshot)
                if hashlib.sha256(payload).hexdigest() != expected_digest:
                    raise RuntimeError("content digest changed")
        except Exception as exc:
            issues.append(
                "execution artifact changed during projection verification: "
                f"{relative.as_posix()}: {exc}"
            )
    return issues


def projection_content_issues(root: Path) -> list[str]:
    """Compare every live projection with an independently rendered DB snapshot."""

    runtime = _runtime()
    try:
        from .cycle_ledger import current_candidate_sha
        from .store import project_db_operation

        with tempfile.TemporaryDirectory(prefix="kafa-projection-verify-") as temp:
            temp_root = Path(temp)
            expected_root = temp_root / "expected"
            evidence_root = temp_root / "evidence"
            expected_db = expected_root / runtime.DB_PATH
            ensure_parent(expected_db)
            with project_db_operation(root) as actual_fs:
                runtime.get_store(root).backup_to(expected_db)
                with runtime.connection(expected_root) as conn:
                    validation_columns = {
                        str(column["name"])
                        for column in conn.execute("pragma table_info(validations)")
                    }
                    strict_coverage = "qualification_id" in validation_columns
                    artifact_paths = (
                        [
                            str(row[0])
                            for row in conn.execute(
                                "select distinct artifact_path from executions "
                                "where artifact_path <> '' order by artifact_path"
                            )
                        ]
                        if strict_coverage
                        else []
                    )

                candidate = (
                    current_candidate_sha(root) if strict_coverage else None
                )
                receipts, issues = _snapshot_projection_execution_artifacts(
                    actual_fs,
                    evidence_root,
                    artifact_paths,
                )
                render_all(
                    expected_root,
                    failure_mode_evidence_root=(
                        evidence_root if strict_coverage else None
                    ),
                    failure_mode_candidate=candidate,
                    trace_evidence_root=(
                        evidence_root if strict_coverage else None
                    ),
                    trace_candidate=candidate,
                )

                with ProjectFS.open(expected_root) as expected_fs:
                    for relative_path in PROJECTION_PATHS:
                        try:
                            actual = actual_fs.read_bytes(relative_path)
                        except Exception:
                            issues.append(
                                f"missing or unsafe view: {relative_path.as_posix()}"
                            )
                            continue
                        try:
                            expected = expected_fs.read_bytes(relative_path)
                        except Exception:
                            issues.append(
                                "projection verifier did not generate expected view: "
                                f"{relative_path.as_posix()}"
                            )
                            continue
                        if actual != expected:
                            issues.append(
                                f"stale or invalid view content: {relative_path.as_posix()}"
                            )

                    retired_path = PROJECTION_ROLLBACK_PATHS[-1]
                    if actual_fs._snapshot(
                        retired_path,
                        allow_missing=True,
                    ).exists:
                        issues.append(
                            "retired projection is still present: "
                            f"{retired_path.as_posix()}"
                        )

                issues.extend(
                    _projection_artifact_receipt_issues(actual_fs, receipts)
                )
                if candidate is not None:
                    try:
                        current_candidate = current_candidate_sha(root)
                    except Exception as exc:
                        issues.append(
                            "candidate changed during projection verification: "
                            f"candidate identity became invalid: {exc}"
                        )
                    else:
                        if current_candidate != candidate:
                            issues.append(
                                "candidate changed during projection verification: "
                                f"before={candidate} after={current_candidate}"
                            )
                return issues
    except Exception as exc:
        return [f"projection content verification failed: {exc}"]

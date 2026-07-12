"""Markdown projection builder for SQLite runtime state."""
from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from typing import Callable, Iterable

from harness_lib import ensure_parent, markdown_row, write_state


def _runtime():
    import harness_db

    return harness_db


def render_all(root: Path) -> None:
    render_affected(root, PROJECTION_NAMES)


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
    path = root / relpath
    ensure_parent(path)
    path.write_bytes((content.rstrip() + "\n").encode("utf-8"))


def _remove_retired_projection(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except PermissionError:
        if path.is_symlink() or not path.is_file():
            raise
        os.chmod(path, stat.S_IMODE(path.stat().st_mode) | stat.S_IWUSR)
        path.unlink()


def render_requirements(root: Path) -> None:
    runtime = _runtime()
    with runtime.connection(root) as conn:
        cycle_id = runtime.current_cycle_id(conn)
        rows = conn.execute("select * from requirements where cycle_id = ? order by id", (cycle_id,)).fetchall()
    lines = ["# Requirements", "", "| ID | Kind | Body | Priority | Status | Revision |", "| --- | --- | --- | --- | --- | --- |"]
    lines.extend(markdown_row([row["id"], row["kind"], row["body"], row["priority"], row["status"], row["revision"]]) for row in rows)
    write_view(root, ".ai-team/requirements/requirements.md", "\n".join(lines))


def render_traceability(root: Path) -> None:
    runtime = _runtime()
    write_view(root, ".ai-team/requirements/traceability.md", "\n".join(runtime.trace_show(root)))


def render_acceptance(root: Path) -> None:
    runtime = _runtime()
    with runtime.connection(root) as conn:
        cycle_id = runtime.current_cycle_id(conn)
        rows = conn.execute("select * from acceptance where cycle_id = ? order by id", (cycle_id,)).fetchall()
    lines = ["# Acceptance Criteria", "", "| ID | Criterion | Priority | Status |", "| --- | --- | --- | --- |"]
    lines.extend(markdown_row([row["id"], row["criterion"], row["priority"], row["status"]]) for row in rows)
    write_view(root, ".ai-team/requirements/acceptance.md", "\n".join(lines))


def render_failure_modes(root: Path) -> None:
    runtime = _runtime()
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
        covered = {
            row["failure_mode_id"]
            for row in conn.execute(
                """
                select distinct vfm.failure_mode_id
                from validation_failure_modes vfm
                join validations v on v.id = vfm.validation_id
                where vfm.cycle_id = ? and v.cycle_id = vfm.cycle_id and v.result = 'pass'
                """,
                (cycle_id,),
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
    lines = [
        "# Immutable Executions",
        "",
        "| ID | Cycle | Candidate | Target | Command | Exit | Stdout SHA256 | Artifact | Count | Format | Semantic | Runner | Sandbox | No Network | Policy | Created At |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    lines.extend(
        markdown_row(
            [
                row["id"],
                row["cycle_id"],
                row["candidate_sha"],
                row["target_id"] or "",
                row["command"],
                row["exit_code"],
                row["stdout_sha256"],
                row["artifact_path"],
                row["executed_count"],
                row["result_format"],
                row["semantic_status"],
                row["runner"],
                row["sandbox_status"],
                row["no_network"],
                row["policy_status"],
                row["created_at"],
            ]
        )
        for row in rows
    )
    write_view(root, "docs/harness/executions.md", "\n".join(lines))
    _remove_retired_projection(root / "docs/harness/evidence.md")


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


def render_affected(root: Path, projections: Iterable[str]) -> None:
    """Rebuild only explicitly affected generated views in stable order."""

    selected = frozenset(projections)
    unknown = sorted(selected - set(PROJECTION_NAMES))
    if unknown:
        raise ValueError(f"unknown projection(s): {', '.join(unknown)}")
    for name, renderer in PROJECTION_RENDERERS:
        if name in selected:
            renderer(root)


def projection_content_issues(root: Path) -> list[str]:
    """Compare every live projection with an independently rendered DB snapshot."""

    runtime = _runtime()
    try:
        with tempfile.TemporaryDirectory(prefix="kafa-projection-verify-") as temp:
            expected_root = Path(temp)
            expected_db = expected_root / runtime.DB_PATH
            ensure_parent(expected_db)
            runtime.get_store(root).backup_to(expected_db)
            render_all(expected_root)

            issues: list[str] = []
            for relative_path in PROJECTION_PATHS:
                actual = root / relative_path
                expected = expected_root / relative_path
                if actual.is_symlink() or not actual.is_file():
                    issues.append(
                        f"missing or unsafe view: {relative_path.as_posix()}"
                    )
                    continue
                if expected.is_symlink() or not expected.is_file():
                    issues.append(
                        "projection verifier did not generate expected view: "
                        f"{relative_path.as_posix()}"
                    )
                    continue
                if actual.read_bytes() != expected.read_bytes():
                    issues.append(
                        f"stale or invalid view content: {relative_path.as_posix()}"
                    )

            retired = root / PROJECTION_ROLLBACK_PATHS[-1]
            if retired.exists() or retired.is_symlink():
                issues.append(
                    "retired projection is still present: "
                    f"{PROJECTION_ROLLBACK_PATHS[-1].as_posix()}"
                )
            return issues
    except Exception as exc:
        return [f"projection content verification failed: {exc}"]

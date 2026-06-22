"""Runtime API facade for Codex Harness Kernel v3.0.

The CLI imports this module as the public runtime API. Existing legacy scripts
continue to execute the CLI, while older direct imports from ``harness_db`` remain
available as compatibility facades.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import harness_db as _db


for _name in dir(_db):
    if not _name.startswith("_"):
        globals()[_name] = getattr(_db, _name)


HarnessError = _db.HarnessError


def _state_db_exists(root: Path) -> bool:
    return (Path(root) / _db.DB_PATH).exists()


def _assert_invariants(root: Path) -> None:
    if not _state_db_exists(root):
        return
    from core.invariant_checker import check_runtime_invariants

    with _db.connection(root) as conn:
        issues = check_runtime_invariants(conn, root)
    if issues:
        raise HarnessError("; ".join(issues))


def _checked_write(function_name: str) -> Callable[..., Any]:
    function = getattr(_db, function_name)

    def wrapped(root: Path, *args: Any, **kwargs: Any) -> Any:
        result = function(root, *args, **kwargs)
        _assert_invariants(root)
        return result

    wrapped.__name__ = function_name
    wrapped.__doc__ = function.__doc__
    return wrapped


for _write_name in [
    "init_runtime",
    "transition_phase",
    "confirm_scope",
    "freeze_baseline",
    "add_requirement",
    "add_acceptance",
    "add_failure_mode",
    "link_requirement_acceptance",
    "add_task",
    "update_task",
    "claim_task",
    "heartbeat_task",
    "recover_stale_leases",
    "release_task",
    "start_task",
    "submit_task",
    "complete_task",
    "review_task",
    "accept_task",
    "block_task",
    "record_decision",
    "record_validation",
    "record_evidence",
    "record_test",
    "record_finding",
    "sweep_expired_risks",
    "record_gate",
    "record_delivery",
    "record_adapter",
    "adapter_plan",
    "adapter_transition",
    "create_checkpoint",
    "add_agent_capability",
    "dispatch_plan",
    "dispatch_claim_next",
    "dispatch_recover_stale",
]:
    globals()[_write_name] = _checked_write(_write_name)


def __getattr__(name: str) -> Any:
    return getattr(_db, name)


def import_checkpoint(root: Path, file_path: Path, *, apply: bool = False) -> list[str]:
    issues = _db.import_checkpoint(root, file_path, apply=apply)
    if apply:
        _assert_invariants(root)
    return issues


def invariant_validate(root):
    from core.invariant_checker import check_runtime_invariants

    with _db.connection(root) as conn:
        return check_runtime_invariants(conn, root)


def projection_rebuild(root):
    from core.projections import render_all

    render_all(root)


def kernel_doctor(root):
    return _db.doctor(root)

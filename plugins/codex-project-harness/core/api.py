"""Runtime API facade for Codex Harness Kernel v3.0.

The CLI imports this module as the public runtime API. Existing legacy scripts
continue to execute the CLI, while older direct imports from ``harness_db`` remain
available as compatibility facades.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import harness_db as _db


for _name in dir(_db):
    if not _name.startswith("_"):
        globals()[_name] = getattr(_db, _name)


HarnessError = _db.HarnessError


def __getattr__(name: str) -> Any:
    return getattr(_db, name)


def import_checkpoint(root: Path, file_path: Path, *, apply: bool = False) -> list[str]:
    return _db.import_checkpoint(root, file_path, apply=apply)


def invariant_validate(root):
    from core.invariant_checker import check_runtime_invariants

    with _db.connection(root) as conn:
        return check_runtime_invariants(conn, root)


def projection_rebuild(root):
    from core.projections import render_all

    render_all(root)


def kernel_doctor(root):
    return _db.doctor(root)

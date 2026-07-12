#!/usr/bin/env python3
"""Run the deterministic local-only Kafa evaluation matrix."""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import math
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[3]
PLUGIN_ROOT = ROOT / "plugins" / "codex-project-harness"
SCRIPTS_ROOT = PLUGIN_ROOT / "scripts"
for path in [ROOT, PLUGIN_ROOT, SCRIPTS_ROOT]:
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

HARNESS = SCRIPTS_ROOT / "harness.py"
_CONTROLLER_HARNESS: ContextVar[Path] = ContextVar(
    "kafa_controller_harness",
    default=HARNESS,
)

import harness_db  # noqa: E402
from harness_lib import (  # noqa: E402
    framed_source_digest,
    git_blob_objects_available,
    isolated_git_environment,
    now_iso,
)
from core.local_core_migration import (  # noqa: E402
    InjectedLocalCoreMigrationFailure,
    migrate_project_to_schema30,
)
from core.schema_lifecycle import (  # noqa: E402
    SCHEMA30_CATALOG_TABLES,
    SCHEMA30_TABLES,
    SCHEMA30_VERSION,
)
from kafa.codex_app_server import (  # noqa: E402
    APPROVED_AGENT_TEMPLATES,
    APPROVED_RUNTIME_SCRIPTS,
    APPROVED_SCHEMA_FILES,
    APPROVED_SKILLS,
    RETIRED_RUNTIME_PATHS,
)


def run_harness(
    root: Path,
    *args: str,
    check: bool = True,
    env: dict[str, str] | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    command_env = os.environ.copy()
    if env:
        command_env.update(env)
    result = subprocess.run(
        [
            sys.executable,
            str(_CONTROLLER_HARNESS.get()),
            "--root",
            str(root),
            *args,
        ],
        text=True,
        capture_output=True,
        check=False,
        env=command_env,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return result


def db_rows(root: Path, query: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
    with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(query, params).fetchall()


def active_table_contract(root: Path) -> tuple[list[str], bool]:
    """Return unexpected tables and exact schema-30 inventory status."""

    tables = {
        str(row[0])
        for row in db_rows(
            root,
            "select name from sqlite_master where type='table'",
        )
    }
    expected = set(SCHEMA30_CATALOG_TABLES)
    return sorted(tables - expected), tables == expected


def scenario_result(
    name: str,
    started: float,
    ok: bool,
    details: dict[str, Any] | None = None,
    *,
    category: str = "fixture",
    mode: str = "fixture",
    skip_reason: str = "",
) -> dict[str, Any]:
    return {
        "name": name,
        "category": category,
        "mode": mode,
        "pass": bool(ok),
        "duration_seconds": round(time.perf_counter() - started, 6),
        "skip_reason": skip_reason,
        "details": details or {},
    }


NATIVE_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)
LIVE_WORKLOAD_FAMILY = "isolated-one-file-value-flip-v1"
LIVE_WORKLOAD_UNIT_SHA256 = hashlib.sha256(
    b"one exclusive Python module: VALUE before-to-after; immutable one-file unittest"
).hexdigest()
NATIVE_TOKEN_SCOPE = "native-producers-only"
REPORT_VERSION = 1
REPORT_MODES = frozenset(
    {"fixture", "stability", "live-codex", "live-codex-parallel"}
)
LOCAL_SCENARIO_CONTRACT: dict[str, tuple[str, str]] = {
    "fresh_local_install_and_init": ("cold-start", "local"),
    "quickstart_stops_before_independent_review": ("quickstart", "local"),
    "current_candidate_supersedes_stale_validation": ("candidate", "local"),
    "manual_evidence_cannot_satisfy_delivery": ("trust", "local"),
    "open_high_finding_blocks_delivery": ("findings", "local"),
    "high_risk_requires_human_review": ("trust", "local"),
    "structured_and_no_network_policy_fail_closed": ("execution-policy", "local"),
    "cycle_isolation": ("cycle", "local"),
    "sqlite_contention_stress": ("sqlite", "local"),
    "schema27_29_migration_and_rollback": ("migration", "local"),
    "installed_plugin_surface": ("installation", "local"),
}
EXPECTED_CODEX_CLI_VERSION = (
    "codex-cli "
    + str(json.loads((ROOT / "release.json").read_text(encoding="utf-8"))["codex_cli_smoke_version"])
)
PARALLEL_PRODUCER_CONTRACT: dict[str, dict[str, object]] = {
    "LIVE-P1": {
        "exclusive_files": ["alpha.py"],
        "changed_files": ["alpha.py"],
        "scope_valid": True,
        "test_file_unchanged": True,
        "capability_hint": "fast",
        "context_id": "native-alpha-producer",
        "target": "LIVE-ALPHA",
        "acceptance": "LIVE-AC-A",
        "returncode": 0,
        "token_source": "codex-json-turn.completed",
        "last_message_recorded": True,
        "error": "",
    },
    "LIVE-P2": {
        "exclusive_files": ["beta.py"],
        "changed_files": ["beta.py"],
        "scope_valid": True,
        "test_file_unchanged": True,
        "capability_hint": "fast",
        "context_id": "native-beta-producer",
        "target": "LIVE-BETA",
        "acceptance": "LIVE-AC-B",
        "returncode": 0,
        "token_source": "codex-json-turn.completed",
        "last_message_recorded": True,
        "error": "",
    },
}


def parse_native_usage_jsonl(output: str) -> dict[str, int] | None:
    """Read the Codex JSONL turn-completion usage event, never assistant text."""

    completed: list[dict[str, Any]] = []
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "turn.completed":
            completed.append(event)
    if len(completed) != 1:
        return None
    usage = completed[0].get("usage")
    if not isinstance(usage, dict):
        return None
    normalized: dict[str, int] = {}
    for field in NATIVE_USAGE_FIELDS:
        value = usage.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
        normalized[field] = value
    if normalized["cached_input_tokens"] > normalized["input_tokens"]:
        return None
    if normalized["reasoning_output_tokens"] > normalized["output_tokens"]:
        return None
    normalized["token_count"] = normalized["input_tokens"] + normalized["output_tokens"]
    return normalized


def normalize_live_eval_path(value: object) -> str:
    raw = str(value).replace("\\", "/")
    path = PurePosixPath(raw)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"unsafe live eval path: {raw}")
    normalized = PurePosixPath(*(part for part in path.parts if part not in {"", "."})).as_posix()
    if not normalized or normalized == ".":
        raise ValueError(f"unsafe live eval path: {raw}")
    return normalized


def live_eval_scope_conflicts(producers: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Return overlapping write scopes before an opt-in parallel Host eval starts."""

    owners: dict[str, list[str]] = {}
    invalid: dict[str, list[str]] = {}
    for producer in producers:
        task = str(producer["task"])
        for value in producer.get("exclusive_files", []):
            try:
                relative = normalize_live_eval_path(value)
            except ValueError:
                invalid.setdefault(f"<invalid:{value}>", []).append(task)
                continue
            owners.setdefault(relative, []).append(task)
    conflicts = {
        relative: tasks
        for relative, tasks in sorted(owners.items())
        if len(set(tasks)) > 1
    }
    conflicts.update(invalid)
    return conflicts


VERBOSE_NATIVE_OUTPUT_KEYS = {
    "native_stdout_tail",
    "native_stderr_tail",
    "controller_verify_output",
    "stdout_tail",
    "stderr_tail",
}
REPORT_KEYS = frozenset(
    {
        "report_version",
        "mode",
        "evaluation_source",
        "live_skipped",
        "live_status",
        "matrix",
        "native_host",
        "evidence_scope",
        "token_count",
        "token_usage",
        "estimated_cost",
        "agent_runtime_seconds",
        "summary",
        "scenarios",
    }
)
SUMMARY_KEYS = frozenset(
    {
        "scenario_count",
        "passed_count",
        "failed_count",
        "skipped_count",
        "scenario_pass_rate",
        "false_pass_count",
        "forged_evidence_block_count",
        "expected_human_review_required_count",
        "sqlite_lock_error_count",
        "human_intervention_count",
        "duration_seconds",
    }
)
SUMMARY_INTEGER_FIELDS = frozenset(
    {
        "scenario_count",
        "passed_count",
        "failed_count",
        "skipped_count",
        "false_pass_count",
        "forged_evidence_block_count",
        "expected_human_review_required_count",
        "sqlite_lock_error_count",
        "human_intervention_count",
    }
)
EVALUATION_SOURCE_KEYS = frozenset(
    {
        "generated_at",
        "git_head",
        "git_dirty",
        "workspace_sha256",
        "status_sha256",
        "status_entry_count",
        "source_scope",
    }
)
MATRIX_KEYS = frozenset(
    {
        "profile",
        "platform",
        "python_version",
        "git_version",
        "codex_available",
        "container_available",
        "sqlite_stress",
        "live_skipped_reasons",
    }
)
NATIVE_HOST_KEYS = frozenset({"resolved_path", "sha256", "source", "trust"})
SCENARIO_KEYS = frozenset(
    {"name", "category", "mode", "pass", "duration_seconds", "skip_reason", "details"}
)
USAGE_KEYS = frozenset({*NATIVE_USAGE_FIELDS, "token_count"})
COMMON_PASSING_LIVE_DETAIL_KEYS = frozenset(
    {
        "capability_status",
        "codex_version",
        "pre_edit_returncode",
        "native_runtime_seconds",
        "native_runtime_source",
        "native_usage",
        "native_token_count",
        "native_token_source",
        "native_token_scope",
        "workload_family",
        "workload_unit_sha256",
        "workload_units",
        "changed_files",
        "integrated_files",
        "controller_state_unchanged_during_native",
        "execution_count",
        "validation_count",
        "retired_host_tables",
        "provider_surface_absent",
        "human_intervention_count",
        "false_pass_count",
    }
)
SINGLE_PASSING_DETAIL_KEYS = COMMON_PASSING_LIVE_DETAIL_KEYS | frozenset(
    {
        "native_returncode",
        "exclusive_files",
        "producer_changed_files",
        "producer_scope_valid",
        "producer_workspace_isolated",
        "test_file_unchanged",
        "controller_test_unchanged",
        "controller_verify_returncode",
        "controller_verify_status",
        "controller_verify_output",
        "task_submit_returncode",
        "task_status",
        "last_message_recorded",
        "native_stdout_tail",
        "native_stderr_tail",
    }
)
PARALLEL_PASSING_DETAIL_KEYS = COMMON_PASSING_LIVE_DETAIL_KEYS | frozenset(
    {
        "producer_count",
        "producers",
        "producer_overlap_seconds",
        "producer_attribution_valid",
        "scope_enforcement",
        "test_files_unchanged",
        "targeted_verify_returncodes",
        "producer_state_returncodes",
        "integration_start_returncode",
        "combined_verify_returncode",
        "combined_verify_status",
        "integration_submit_returncode",
        "integration_dependency_blocked_before_producers",
        "task_statuses",
        "scope_conflicts",
        "overlap_policy",
    }
)
PARALLEL_PRODUCER_KEYS = frozenset(
    {
        "task",
        "exclusive_files",
        "changed_files",
        "scope_valid",
        "test_file_unchanged",
        "capability_hint",
        "context_id",
        "target",
        "acceptance",
        "returncode",
        "runtime_seconds",
        "native_usage",
        "token_count",
        "token_source",
        "stdout_tail",
        "stderr_tail",
        "last_message_recorded",
        "error",
        "started_offset_seconds",
        "finished_offset_seconds",
    }
)


def _unexpected_key_errors(
    prefix: str,
    value: dict[str, Any],
    allowed: frozenset[str],
) -> list[str]:
    unexpected = sorted(set(value) - allowed)
    return [f"{prefix} contains unsupported fields: {','.join(unexpected)}"] if unexpected else []


def compact_evidence_report(value: Any) -> Any:
    """Remove verbose Native Host output while preserving result and telemetry facts."""

    if isinstance(value, dict):
        return {
            key: compact_evidence_report(item)
            for key, item in value.items()
            if key not in VERBOSE_NATIVE_OUTPUT_KEYS
        }
    if isinstance(value, list):
        return [compact_evidence_report(item) for item in value]
    return value


def skipped_scenario(name: str, reason: str, *, category: str, mode: str) -> dict[str, Any]:
    return {
        "name": name,
        "category": category,
        "mode": mode,
        "pass": False,
        "duration_seconds": 0,
        "skip_reason": reason,
        "details": {},
    }


def command_version(command: list[str], *, env: dict[str, str] | None = None) -> str:
    try:
        result = subprocess.run(
            command,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (result.stdout or result.stderr).strip().splitlines()[0] if (result.stdout or result.stderr).strip() else ""


EVALUATION_SOURCE_PREFIXES = ("kafa/", "plugins/", "tests/", "benchmarks/")
EVALUATION_SOURCE_FILES = {
    ".gitattributes",
    "VERSION",
    "release.json",
    "pyproject.toml",
    "docs/runtime/fresh-skill-eval-prompts.md",
    "docs/runtime/skill-eval-transcript-fixture.txt",
}


def _in_evaluation_source_scope(relative: str) -> bool:
    return relative in EVALUATION_SOURCE_FILES or relative.startswith(EVALUATION_SOURCE_PREFIXES)


def _is_evaluation_cache_path(relative: str) -> bool:
    parts = Path(relative).parts
    return "__pycache__" in parts or ".pytest_cache" in parts


def _git_blob_oid(content: bytes, object_format: str) -> str:
    digest = hashlib.new(object_format)
    digest.update(f"blob {len(content)}\0".encode("ascii"))
    digest.update(content)
    return digest.hexdigest()


def _absolute_path_flavor(value: str) -> str | None:
    if not value or "\0" in value:
        return None
    windows_path = PureWindowsPath(value)
    if windows_path.is_absolute() and windows_path.name:
        return "windows"
    posix_path = PurePosixPath(value)
    if posix_path.is_absolute() and posix_path.name:
        return "posix"
    return None


def _is_cross_platform_absolute_path(value: str) -> bool:
    return _absolute_path_flavor(value) is not None


def _source_identity_git_environment(root: Path = ROOT) -> dict[str, str]:
    """Return a Git environment isolated from ambient repository overrides."""

    return isolated_git_environment(work_tree=root)


def _invalid_evaluation_source_identity() -> dict[str, Any]:
    return {
        "generated_at": now_iso(),
        "git_head": "",
        "git_dirty": None,
        "workspace_sha256": "",
        "status_sha256": "",
        "status_entry_count": 0,
        "source_scope": [],
    }


def evaluation_source_identity(root: Path = ROOT) -> dict[str, Any]:
    """Bind a report to actual executable bytes without running Git content filters."""

    root = root.resolve()
    git_environment = _source_identity_git_environment(root)
    try:
        head = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "rev-parse", "--verify", "HEAD"],
            cwd=root,
            env=git_environment,
            text=True,
            capture_output=True,
            check=False,
        ).stdout.strip()
        listed = subprocess.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "ls-files",
                "--cached",
                "--others",
                "-z",
            ],
            cwd=root,
            env=git_environment,
            capture_output=True,
            check=True,
        ).stdout.split(b"\0")
        staged = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "ls-files", "--stage", "-z"],
            cwd=root,
            env=git_environment,
            capture_output=True,
            check=True,
        ).stdout.split(b"\0")
        object_format = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "rev-parse", "--show-object-format"],
            cwd=root,
            env=git_environment,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        head_tree = (
            subprocess.run(
                ["git", "-c", "core.fsmonitor=false", "ls-tree", "-r", "-z", "HEAD"],
                cwd=root,
                env=git_environment,
                capture_output=True,
                check=True,
            ).stdout.split(b"\0")
            if head
            else []
        )
    except (OSError, subprocess.CalledProcessError):
        return _invalid_evaluation_source_identity()
    if object_format not in {"sha1", "sha256"}:
        return _invalid_evaluation_source_identity()
    tracked_entries: dict[bytes, tuple[bytes, str]] = {}
    unmerged_paths: set[bytes] = set()
    for raw_entry in (entry for entry in staged if entry):
        try:
            metadata, raw_relative = raw_entry.split(b"\t", 1)
            mode, object_id, stage = metadata.split(b" ", 2)
        except ValueError:
            return _invalid_evaluation_source_identity()
        if stage == b"0":
            tracked_entries[raw_relative] = (mode, object_id.decode("ascii"))
        else:
            unmerged_paths.add(raw_relative)

    head_entries: dict[bytes, tuple[bytes, str]] = {}
    for raw_entry in (entry for entry in head_tree if entry):
        try:
            metadata, raw_relative = raw_entry.split(b"\t", 1)
            mode, _object_type, object_id = metadata.split(b" ", 2)
        except ValueError:
            return _invalid_evaluation_source_identity()
        head_entries[raw_relative] = (mode, object_id.decode("ascii"))

    scoped_unmerged = {
        raw_relative
        for raw_relative in unmerged_paths
        if _in_evaluation_source_scope(
            raw_relative.decode("utf-8", errors="surrogateescape")
        )
        and not _is_evaluation_cache_path(
            raw_relative.decode("utf-8", errors="surrogateescape")
        )
    }
    if scoped_unmerged:
        return _invalid_evaluation_source_identity()

    for entries in (tracked_entries, head_entries):
        for raw_relative, (mode, _object_id) in entries.items():
            relative = raw_relative.decode("utf-8", errors="surrogateescape")
            if (
                _in_evaluation_source_scope(relative)
                and not _is_evaluation_cache_path(relative)
                and mode not in {b"100644", b"100755"}
            ):
                return _invalid_evaluation_source_identity()

    required_objects = {
        object_id
        for entries in (tracked_entries, head_entries)
        for raw_relative, (mode, object_id) in entries.items()
        if mode in {b"100644", b"100755"}
        and _in_evaluation_source_scope(
            raw_relative.decode("utf-8", errors="surrogateescape")
        )
        and not _is_evaluation_cache_path(
            raw_relative.decode("utf-8", errors="surrogateescape")
        )
    }
    if not git_blob_objects_available(
        root,
        required_objects,
        environment=git_environment,
    ):
        return _invalid_evaluation_source_identity()

    source_entries: list[tuple[bytes, bytes, bytes, str, str]] = []
    for raw_relative in sorted(relative for relative in listed if relative):
        relative = raw_relative.decode("utf-8", errors="surrogateescape")
        if not _in_evaluation_source_scope(relative):
            continue
        if _is_evaluation_cache_path(relative):
            continue
        path = root / relative
        tracked = tracked_entries.get(raw_relative)
        if (
            path.is_symlink()
            or (tracked is not None and tracked[0] not in {b"100644", b"100755"})
            or (path.exists() and not path.is_file())
        ):
            return _invalid_evaluation_source_identity()
        if not path.is_file():
            continue
        content = path.read_bytes()
        actual_mode = b"100755" if path.stat().st_mode & 0o111 else b"100644"
        workspace_mode = (
            tracked[0] if os.name == "nt" and tracked is not None else actual_mode
        )
        source_entries.append(
            (
                raw_relative,
                workspace_mode,
                actual_mode,
                hashlib.sha256(content).hexdigest(),
                _git_blob_oid(content, object_format),
            )
        )
    actual_entries: dict[bytes, tuple[bytes, str]] = {}
    for (
        raw_relative,
        workspace_mode,
        actual_mode,
        runtime_sha256,
        actual_object_id,
    ) in source_entries:
        actual_entries[raw_relative] = (actual_mode, actual_object_id)
    workspace_sha256 = framed_source_digest(
        (raw_relative, workspace_mode, runtime_sha256)
        for raw_relative, workspace_mode, _actual_mode, runtime_sha256, _actual_object_id
        in source_entries
    )

    status_lines: list[str] = []
    all_paths = set(head_entries) | set(tracked_entries) | set(actual_entries) | unmerged_paths
    for raw_relative in sorted(all_paths):
        relative = raw_relative.decode("utf-8", errors="surrogateescape")
        if not _in_evaluation_source_scope(relative) or _is_evaluation_cache_path(relative):
            continue
        if raw_relative in unmerged_paths:
            status_lines.append(f"UU {relative}")
            continue
        head_entry = head_entries.get(raw_relative)
        index_entry = tracked_entries.get(raw_relative)
        actual_entry = actual_entries.get(raw_relative)
        if head_entry is None and index_entry is None and actual_entry is not None:
            status_lines.append(f"?? {relative}")
            continue
        if head_entry is None and index_entry is not None:
            index_status = "A"
        elif head_entry is not None and index_entry is None:
            index_status = "D"
        elif head_entry != index_entry:
            index_status = "M"
        else:
            index_status = " "
        if index_entry is None:
            worktree_status = "?" if actual_entry is not None else " "
        elif actual_entry is None:
            worktree_status = "D"
        else:
            actual_mode, actual_object_id = actual_entry
            mode_changed = (
                os.name != "nt"
                and index_entry[0] in {b"100644", b"100755"}
                and actual_mode != index_entry[0]
            )
            worktree_status = (
                "M" if actual_object_id != index_entry[1] or mode_changed else " "
            )
        if index_status != " " or worktree_status != " ":
            status_lines.append(f"{index_status}{worktree_status} {relative}")
    status = "\n".join(status_lines) + ("\n" if status_lines else "")
    return {
        "generated_at": now_iso(),
        "git_head": head,
        "git_dirty": bool(status_lines),
        "workspace_sha256": workspace_sha256,
        "status_sha256": hashlib.sha256(status.encode("utf-8")).hexdigest(),
        "status_entry_count": len(status_lines),
        "source_scope": [*EVALUATION_SOURCE_PREFIXES, *sorted(EVALUATION_SOURCE_FILES)],
    }


def _copy_evaluation_source_scope(source_root: Path, snapshot_root: Path) -> None:
    def ignore_generated(_directory: str, names: list[str]) -> set[str]:
        return {
            name
            for name in names
            if name in {"__pycache__", ".pytest_cache"}
        }

    for prefix in EVALUATION_SOURCE_PREFIXES:
        source = source_root / prefix.rstrip("/")
        if not source.exists():
            continue
        shutil.copytree(
            source,
            snapshot_root / prefix.rstrip("/"),
            copy_function=shutil.copy2,
            symlinks=True,
            ignore=ignore_generated,
        )
    for relative in EVALUATION_SOURCE_FILES:
        source = source_root / relative
        if not source.is_file() or source.is_symlink():
            continue
        destination = snapshot_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _tracked_evaluation_modes(root: Path) -> dict[str, bytes]:
    environment = _source_identity_git_environment(root)
    try:
        entries = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "ls-files", "--stage", "-z"],
            cwd=root,
            env=environment,
            capture_output=True,
            check=True,
            timeout=10,
        ).stdout.split(b"\0")
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("cannot capture evaluation source index modes") from exc
    modes: dict[str, bytes] = {}
    for entry in (value for value in entries if value):
        try:
            metadata, raw_relative = entry.split(b"\t", 1)
            mode, _object_id, stage = metadata.split(b" ", 2)
        except ValueError as exc:
            raise RuntimeError("malformed evaluation source index entry") from exc
        relative = raw_relative.decode("utf-8", errors="surrogateescape")
        if stage == b"0" and _in_evaluation_source_scope(relative):
            modes[relative] = mode
    return modes


@contextmanager
def pinned_evaluation_source(
    root: Path,
    expected_identity: dict[str, Any],
) -> Any:
    """Run controller subprocesses from a start-identity-verified private snapshot."""

    root = root.resolve()
    expected_workspace = expected_identity.get("workspace_sha256")
    if not isinstance(expected_workspace, str) or len(expected_workspace) != 64:
        raise RuntimeError("live evaluation source identity is unavailable at profile start")
    tracked_modes = _tracked_evaluation_modes(root)
    with tempfile.TemporaryDirectory(prefix="kafa-controller-source-") as temp:
        snapshot_root = Path(temp) / "source"
        snapshot_root.mkdir()
        _copy_evaluation_source_scope(root, snapshot_root)
        init_environment = isolated_git_environment()
        init_environment.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
            }
        )
        empty_template = Path(temp) / "empty-git-template"
        empty_template.mkdir()
        subprocess.run(
            ["git", "init", f"--template={empty_template}"],
            cwd=snapshot_root,
            env=init_environment,
            check=True,
            capture_output=True,
            text=True,
        )
        snapshot_environment = isolated_git_environment(work_tree=snapshot_root)
        snapshot_environment.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
            }
        )
        for snapshot_path in sorted(snapshot_root.rglob("*")):
            relative = snapshot_path.relative_to(snapshot_root).as_posix()
            if relative == ".git" or relative.startswith(".git/"):
                continue
            if not _in_evaluation_source_scope(relative) or _is_evaluation_cache_path(
                relative
            ):
                continue
            if snapshot_path.is_symlink() or (
                snapshot_path.exists() and not snapshot_path.is_file()
            ):
                if snapshot_path.is_dir() and not snapshot_path.is_symlink():
                    continue
                raise RuntimeError(
                    f"private controller snapshot contains unsafe source: {relative}"
                )
            if not snapshot_path.is_file():
                continue
            mode = tracked_modes.get(relative)
            if mode is None:
                mode = (
                    b"100755"
                    if snapshot_path.stat().st_mode & 0o111
                    else b"100644"
                )
            if mode not in {b"100644", b"100755"}:
                raise RuntimeError(
                    f"unsupported tracked evaluation source mode: {relative} {mode!r}"
                )
            object_id = subprocess.run(
                ["git", "hash-object", "-w", "--stdin"],
                cwd=snapshot_root,
                env=snapshot_environment,
                input=snapshot_path.read_bytes(),
                capture_output=True,
                check=True,
            ).stdout.decode("ascii").strip()
            subprocess.run(
                [
                    "git",
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    f"{mode.decode('ascii')},{object_id},{relative}",
                ],
                cwd=snapshot_root,
                env=snapshot_environment,
                check=True,
                capture_output=True,
            )
        snapshot_identity = evaluation_source_identity(snapshot_root)
        if snapshot_identity.get("workspace_sha256") != expected_workspace:
            raise RuntimeError(
                "live evaluation source changed while the controller snapshot was captured"
            )
        pinned_harness = (
            snapshot_root
            / "plugins/codex-project-harness/scripts/harness.py"
        )
        if not pinned_harness.is_file() or pinned_harness.is_symlink():
            raise RuntimeError("private controller snapshot is missing a safe harness.py")
        token = _CONTROLLER_HARNESS.set(pinned_harness)
        try:
            yield pinned_harness
        finally:
            _CONTROLLER_HARNESS.reset(token)


def _evaluation_identity_matches(
    expected: dict[str, Any],
    actual: dict[str, Any],
) -> bool:
    return all(
        expected.get(field) == actual.get(field)
        for field in (
            "git_head",
            "git_dirty",
            "workspace_sha256",
            "status_sha256",
            "status_entry_count",
            "source_scope",
        )
    )


def _source_failure_report(
    mode: str,
    scenario_name: str,
    started: float,
    identity: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    report = summarize(
        mode,
        [
            scenario_result(
                scenario_name,
                started,
                False,
                {"capability_status": "failed", "reason": reason},
                category=mode,
                mode=mode,
            )
        ],
        started,
        live_status="failed",
    )
    report["evaluation_source"] = identity
    return report


def _run_pinned_live_profile(
    *,
    mode: str,
    scenario_name: str,
    enable_env: str,
    runner: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    if os.environ.get(enable_env) != "1":
        return runner()
    started = time.perf_counter()
    start_identity = evaluation_source_identity()
    try:
        with pinned_evaluation_source(ROOT, start_identity):
            report = runner()
    except Exception as exc:
        return _source_failure_report(
            mode,
            scenario_name,
            started,
            start_identity,
            str(exc),
        )
    end_identity = evaluation_source_identity()
    report["evaluation_source"] = start_identity
    if not _evaluation_identity_matches(start_identity, end_identity):
        return _source_failure_report(
            mode,
            scenario_name,
            started,
            start_identity,
            "evaluation source changed before profile completion",
        )
    return report


def matrix_info(profile: str, *, live_skipped_reasons: list[str] | None = None) -> dict[str, Any]:
    return {
        "profile": profile,
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "git_version": command_version(["git", "--version"]),
        "codex_available": bool(live_codex_binary()) if profile.startswith("live-codex") else shutil.which("codex") is not None,
        "container_available": shutil.which("docker") is not None or shutil.which("podman") is not None,
        "sqlite_stress": profile == "stability",
        "live_skipped_reasons": live_skipped_reasons or [],
    }


def _scalar(root: Path, query: str, params: tuple[object, ...] = ()) -> object:
    rows = db_rows(root, query, params)
    if not rows:
        raise AssertionError(f"query returned no rows: {query}")
    return rows[0][0]


def _require_ok(result: subprocess.CompletedProcess[str]) -> None:
    if result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)


def _write_passing_unittest(root: Path, name: str = "test_candidate.py") -> None:
    (root / name).write_text(
        "import unittest\n\n"
        "class CandidateTest(unittest.TestCase):\n"
        "    def test_candidate(self):\n"
        "        self.assertTrue(True)\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scenario_fresh_local_install_and_init() -> dict[str, Any]:
    started = time.perf_counter()
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        initialized = run_harness(root, "init", check=False)
        status = run_harness(root, "status", check=False)
        tables = {
            str(row[0])
            for row in db_rows(
                root,
                "select name from sqlite_master where type='table' and name not like 'sqlite_%'",
            )
        }
        templates = {path.name for path in (root / ".codex/agents").glob("*.toml")}
        retired_views = [
            relative
            for relative in (
                ".ai-team/control/tooling-map.md",
                ".ai-team/control/advisory-fallbacks.md",
            )
            if (root / relative).exists()
        ]
        ok = (
            initialized.returncode == 0
            and status.returncode == 0
            and f"schema_version: {SCHEMA30_VERSION}" in status.stdout
            and tables == set(SCHEMA30_TABLES)
            and templates == APPROVED_AGENT_TEMPLATES
            and not retired_views
        )
        return scenario_result(
            "fresh_local_install_and_init",
            started,
            ok,
            {
                "schema_version": SCHEMA30_VERSION,
                "table_count": len(tables),
                "template_names": sorted(templates),
                "retired_views": retired_views,
                "external_credentials_required": False,
            },
            category="cold-start",
            mode="local",
        )


def scenario_quickstart_stops_before_independent_review() -> dict[str, Any]:
    started = time.perf_counter()
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        _write_passing_unittest(root)
        quickstart = run_harness(
            root,
            "quickstart",
            "minimal",
            "--id",
            "EVAL",
            "--goal",
            "verify a local candidate",
            "--acceptance",
            "candidate test passes",
            "--task",
            "implement candidate",
            "--test-command",
            "python3 -B -m unittest test_candidate.py",
            "--execute",
            check=False,
        )
        delivery_check = run_harness(root, "validate", "--delivery", check=False)
        task_status = str(_scalar(root, "select status from tasks where id='EVAL-T1'"))
        counts = {
            table: int(_scalar(root, f"select count(*) from {table}"))
            for table in ("executions", "validations", "quality_gates", "deliveries")
        }
        ok = (
            quickstart.returncode == 0
            and task_status == "submitted"
            and counts == {"executions": 1, "validations": 1, "quality_gates": 0, "deliveries": 0}
            and delivery_check.returncode != 0
            and "independent review" in quickstart.stdout.lower()
        )
        return scenario_result(
            "quickstart_stops_before_independent_review",
            started,
            ok,
            {
                "task_status": task_status,
                **counts,
                "delivery_validation_returncode": delivery_check.returncode,
                "false_pass_count": int(delivery_check.returncode == 0),
            },
            category="quickstart",
            mode="local",
        )


def scenario_current_candidate_supersedes_stale_validation() -> dict[str, Any]:
    started = time.perf_counter()
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        _write_passing_unittest(root)
        for args in (
            ("init",),
            ("acceptance", "add", "--id", "AC1", "--criterion", "candidate passes"),
            (
                "test-target",
                "add",
                "--id",
                "UNIT",
                "--kind",
                "unit",
                "--command-template",
                "python3 -B -m unittest test_candidate.py",
            ),
        ):
            _require_ok(run_harness(root, *args, check=False))
        _require_ok(run_harness(root, "verify", "run", "--target", "UNIT", "--acceptance", "AC1", check=False))
        candidate = root / "test_candidate.py"
        candidate.write_text(candidate.read_text(encoding="utf-8") + "\n# second candidate\n", encoding="utf-8")
        _require_ok(run_harness(root, "verify", "run", "--target", "UNIT", "--acceptance", "AC1", check=False))
        rows = db_rows(
            root,
            "select candidate_sha, validation_status, superseded_by from validations order by created_at, id",
        )
        statuses = [str(row["validation_status"]) for row in rows]
        candidates = {str(row["candidate_sha"]) for row in rows}
        ok = (
            len(rows) == 2
            and statuses.count("superseded") == 1
            and statuses.count("active") == 1
            and len(candidates) == 2
            and any(str(row["superseded_by"] or "") for row in rows if row["validation_status"] == "superseded")
        )
        return scenario_result(
            "current_candidate_supersedes_stale_validation",
            started,
            ok,
            {"validation_statuses": statuses, "candidate_count": len(candidates)},
            category="candidate",
            mode="local",
        )


def scenario_manual_evidence_cannot_satisfy_delivery() -> dict[str, Any]:
    started = time.perf_counter()
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        commands = (
            ("init",),
            ("requirement", "add", "--id", "R1", "--kind", "functional", "--body", "local requirement"),
            ("acceptance", "add", "--id", "AC1", "--criterion", "local acceptance"),
            ("requirement", "link", "--requirement", "R1", "--acceptance", "AC1"),
            ("baseline", "freeze", "--id", "B1", "--summary", "locked baseline"),
            ("task", "add", "--id", "T1", "--task", "implement", "--acceptance", "AC1"),
            ("task", "start", "T1"),
            ("task", "submit", "T1", "--context-id", "producer", "--evidence", "claimed complete"),
            ("task", "accept", "T1", "--evidence", "manual review"),
            (
                "validation",
                "record",
                "--surface",
                "manual claim",
                "--acceptance",
                "AC1",
                "--findings",
                "claimed pass",
                "--result",
                "pass",
            ),
            ("gate", "record", "--reviewer-context", "same-context-degraded", "--result", "pass"),
        )
        for args in commands:
            _require_ok(run_harness(root, *args, check=False))
        delivery = run_harness(root, "delivery", "record", "--scope", "forged", "--acceptance", "AC1", check=False)
        delivery_count = int(_scalar(root, "select count(*) from deliveries"))
        execution_count = int(_scalar(root, "select count(*) from executions"))
        output = (delivery.stdout + delivery.stderr).lower()
        blocked = delivery.returncode != 0 and "no linked immutable execution" in output
        return scenario_result(
            "manual_evidence_cannot_satisfy_delivery",
            started,
            blocked and delivery_count == 0 and execution_count == 0,
            {
                "delivery_returncode": delivery.returncode,
                "delivery_count": delivery_count,
                "execution_count": execution_count,
                "forged_evidence_block_count": int(blocked),
                "false_pass_count": int(delivery.returncode == 0),
            },
            category="trust",
            mode="local",
        )


def scenario_open_high_finding_blocks_delivery() -> dict[str, Any]:
    started = time.perf_counter()
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        _write_passing_unittest(root)
        _require_ok(
            run_harness(
                root,
                "quickstart",
                "minimal",
                "--id",
                "FINDING",
                "--goal",
                "verify finding gate",
                "--acceptance",
                "candidate passes",
                "--task",
                "implement",
                "--test-command",
                "python3 -B -m unittest test_candidate.py",
                "--execute",
                check=False,
            )
        )
        _require_ok(run_harness(root, "task", "accept", "FINDING-T1", "--evidence", "reviewed", check=False))
        _require_ok(
            run_harness(
                root,
                "finding",
                "record",
                "--id",
                "F1",
                "--surface",
                "delivery",
                "--severity",
                "high",
                "--status",
                "open",
                "--summary",
                "blocking finding",
                check=False,
            )
        )
        _require_ok(
            run_harness(
                root,
                "gate",
                "record",
                "--reviewer-context",
                "fresh",
                "--reviewer-context-id",
                "reviewer",
                "--result",
                "pass",
                check=False,
            )
        )
        delivery = run_harness(root, "delivery", "record", "--scope", "finding", check=False)
        output = (delivery.stdout + delivery.stderr).lower()
        blocked = delivery.returncode != 0 and "high finding blocks delivery" in output
        return scenario_result(
            "open_high_finding_blocks_delivery",
            started,
            blocked and int(_scalar(root, "select count(*) from deliveries")) == 0,
            {
                "delivery_returncode": delivery.returncode,
                "finding_block_count": int(blocked),
                "false_pass_count": int(delivery.returncode == 0),
            },
            category="findings",
            mode="local",
        )


def scenario_high_risk_requires_human_review() -> dict[str, Any]:
    started = time.perf_counter()
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        (root / "emit_result.py").write_text(
            "from pathlib import Path\n"
            "Path('.ai-team/runtime/eval-result.json').parent.mkdir(parents=True, exist_ok=True)\n"
            "Path('.ai-team/runtime/eval-result.json').write_text("
            "'{\"summary\":{\"total\":1,\"passed\":1,\"failed\":0,\"errors\":0}}', encoding='utf-8')\n",
            encoding="utf-8",
        )
        commands = (
            ("init",),
            ("requirement", "add", "--id", "R1", "--kind", "functional", "--body", "high-risk flow"),
            ("acceptance", "add", "--id", "AC1", "--criterion", "structured pass"),
            ("requirement", "link", "--requirement", "R1", "--acceptance", "AC1"),
            (
                "failure-mode",
                "add",
                "--id",
                "FM1",
                "--feature",
                "delivery",
                "--scenario",
                "critical failure",
                "--trigger",
                "bad candidate",
                "--expected",
                "fail closed",
                "--risk",
                "high",
                "--acceptance",
                "AC1",
            ),
            ("baseline", "freeze", "--id", "B1", "--summary", "high risk baseline"),
            ("task", "add", "--id", "T1", "--task", "implement", "--acceptance", "AC1", "--failure-mode", "FM1"),
            (
                "test-target",
                "add",
                "--id",
                "STRUCTURED",
                "--kind",
                "build",
                "--command-template",
                "python3 emit_result.py",
                "--result-format",
                "pytest-json",
                "--result-path",
                ".ai-team/runtime/eval-result.json",
            ),
            ("test-target", "link", "--task", "T1", "--target", "STRUCTURED"),
            ("task", "start", "T1"),
            ("verify", "run", "--target", "STRUCTURED", "--acceptance", "AC1", "--failure-mode", "FM1"),
            ("task", "submit", "T1", "--context-id", "producer", "--evidence", "verified"),
            ("task", "accept", "T1", "--evidence", "reviewed"),
            (
                "gate",
                "record",
                "--reviewer-context",
                "fresh",
                "--reviewer-context-id",
                "reviewer",
                "--result",
                "pass",
            ),
        )
        for args in commands:
            _require_ok(run_harness(root, *args, check=False))
        delivery = run_harness(root, "delivery", "record", "--scope", "high-risk", check=False)
        output = (delivery.stdout + delivery.stderr).lower()
        human_review = delivery.returncode != 0 and "human-review-required" in output
        return scenario_result(
            "high_risk_requires_human_review",
            started,
            human_review and int(_scalar(root, "select count(*) from deliveries")) == 0,
            {
                "delivery_returncode": delivery.returncode,
                "expected_human_review_required_count": int(human_review),
                "human_intervention_count": 0,
                "false_pass_count": int(delivery.returncode == 0),
            },
            category="trust",
            mode="local",
        )


def scenario_structured_and_no_network_policy_fail_closed() -> dict[str, Any]:
    started = time.perf_counter()
    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp)
        structured_root = base / "structured"
        structured_root.mkdir()
        (structured_root / "emit_zero.py").write_text(
            "from pathlib import Path\n"
            "Path('.ai-team/runtime/zero.json').parent.mkdir(parents=True, exist_ok=True)\n"
            "Path('.ai-team/runtime/zero.json').write_text("
            "'{\"summary\":{\"total\":0,\"passed\":0,\"failed\":0,\"errors\":0}}', encoding='utf-8')\n",
            encoding="utf-8",
        )
        for args in (
            ("init",),
            ("acceptance", "add", "--id", "AC1", "--criterion", "positive count"),
            (
                "test-target",
                "add",
                "--id",
                "ZERO",
                "--kind",
                "build",
                "--command-template",
                "python3 emit_zero.py",
                "--result-format",
                "pytest-json",
                "--result-path",
                ".ai-team/runtime/zero.json",
            ),
        ):
            _require_ok(run_harness(structured_root, *args, check=False))
        zero = run_harness(structured_root, "verify", "run", "--target", "ZERO", "--acceptance", "AC1", check=False)

        network_root = base / "network"
        network_root.mkdir()
        _write_passing_unittest(network_root)
        for args in (
            ("init",),
            ("acceptance", "add", "--id", "AC1", "--criterion", "no network"),
            (
                "test-target",
                "add",
                "--id",
                "NO-NET",
                "--kind",
                "unit",
                "--command-template",
                "python3 -B -m unittest test_candidate.py",
                "--requires-no-network",
            ),
        ):
            _require_ok(run_harness(network_root, *args, check=False))
        no_network = run_harness(network_root, "verify", "run", "--target", "NO-NET", "--acceptance", "AC1", check=False)
        zero_facts = int(_scalar(structured_root, "select count(*) from executions"))
        network_facts = int(_scalar(network_root, "select count(*) from executions"))
        zero_blocked = zero.returncode != 0 and "executed_count" in (zero.stdout + zero.stderr)
        network_blocked = no_network.returncode != 0 and "no-network" in (no_network.stdout + no_network.stderr)
        return scenario_result(
            "structured_and_no_network_policy_fail_closed",
            started,
            zero_blocked and network_blocked and zero_facts == 0 and network_facts == 0,
            {
                "structured_zero_blocked": zero_blocked,
                "local_no_network_blocked": network_blocked,
                "policy_block_count": int(zero_blocked) + int(network_blocked),
                "false_pass_count": int(zero.returncode == 0) + int(no_network.returncode == 0),
            },
            category="execution-policy",
            mode="local",
        )


def scenario_cycle_isolation() -> dict[str, Any]:
    started = time.perf_counter()
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        commands = (
            ("init",),
            ("requirement", "add", "--id", "R1", "--kind", "functional", "--body", "first cycle"),
            ("cycle", "close", "--status", "archived"),
            ("cycle", "start", "--id", "CYCLE-next", "--name", "Next", "--goal", "iterate"),
            ("requirement", "add", "--id", "R1", "--kind", "functional", "--body", "second cycle"),
        )
        for args in commands:
            _require_ok(run_harness(root, *args, check=False))
        rows = db_rows(root, "select cycle_id, id, body from requirements where id='R1' order by cycle_id")
        current = str(_scalar(root, "select current_cycle_id from project where id=1"))
        ok = len(rows) == 2 and current == "CYCLE-next" and {str(row["body"]) for row in rows} == {"first cycle", "second cycle"}
        return scenario_result(
            "cycle_isolation",
            started,
            ok,
            {"current_cycle": current, "cycle_ids": [str(row["cycle_id"]) for row in rows]},
            category="cycle",
            mode="local",
        )


def scenario_sqlite_contention_stress() -> dict[str, Any]:
    started = time.perf_counter()
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        _require_ok(run_harness(root, "init", check=False))
        results: list[subprocess.CompletedProcess[str]] = []
        result_lock = threading.Lock()

        def worker(index: int) -> None:
            acceptance_id = f"AC{index // 2}"
            result = run_harness(
                root,
                "acceptance",
                "add",
                "--id",
                acceptance_id,
                "--criterion",
                f"contention criterion {acceptance_id}",
                check=False,
                timeout=30,
            )
            with result_lock:
                results.append(result)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        alive = sum(thread.is_alive() for thread in threads)
        lock_errors = sum("database is locked" in (result.stdout + result.stderr).lower() for result in results)
        failed = [result.returncode for result in results if result.returncode != 0]
        doctor = run_harness(root, "doctor", check=False)
        acceptance_count = int(_scalar(root, "select count(*) from acceptance"))
        ok = len(results) == 12 and alive == 0 and not failed and lock_errors == 0 and doctor.returncode == 0 and acceptance_count == 6
        return scenario_result(
            "sqlite_contention_stress",
            started,
            ok,
            {
                "operation_count": len(results),
                "thread_leak_count": alive,
                "failed_returncodes": failed,
                "sqlite_lock_error_count": lock_errors,
                "acceptance_count": acceptance_count,
                "doctor_returncode": doctor.returncode,
            },
            category="sqlite",
            mode="local",
        )


def _create_schema27_fixture(root: Path) -> Path:
    db = root / ".ai-team/state/harness.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    encoded = (SCRIPTS_ROOT / "fixtures/schema27-v1.21.3.sql.gz.b64").read_bytes()
    ddl = gzip.decompress(base64.b64decode(encoded))
    if hashlib.sha256(ddl).hexdigest() != "62c1046ed093ab3acdd1ceb22994b8c8c81242b26a997f5c2e77840e08b205f8":
        raise AssertionError("published schema 27 fixture digest mismatch")
    artifact = root / ".ai-team/runtime/executions/legacy/stdout.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("schema27 execution\n", encoding="utf-8")
    seed = (SCRIPTS_ROOT / "fixtures/schema27-v1.21.3-seed.sql").read_text(encoding="utf-8")
    seed = seed.replace("__CANDIDATE__", "a" * 64).replace("__ARTIFACT_PATH__", str(artifact.relative_to(root))).replace("__ARTIFACT_SHA__", _sha256(artifact)).replace("__RETIRED_SECRET__", "SCHEMA27-RETIRED-SENTINEL-9e4f")
    with closing(sqlite3.connect(db)) as conn:
        conn.executescript(ddl.decode("utf-8"))
        conn.executescript(seed)
        inventory = (
            conn.execute("select count(*) from sqlite_master where type='table' and name not like 'sqlite_%'").fetchone()[0],
            conn.execute("select count(*) from sqlite_master where type='index'").fetchone()[0],
        )
        if inventory != (53, 60):
            raise AssertionError(f"published schema 27 inventory mismatch: {inventory}")
        conn.commit()
    return db


def _migration_projection_validator(root: Path) -> Callable[[Path], None]:
    def validate(_active_path: Path) -> None:
        harness_db.render_all(root)
        issues = harness_db.doctor(root, require_project_files=False)
        if issues:
            raise AssertionError("migration projection validation failed: " + "; ".join(issues))

    return validate


def scenario_schema27_29_migration_and_rollback() -> dict[str, Any]:
    started = time.perf_counter()
    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp)
        success_root = base / "schema27-success"
        _create_schema27_fixture(success_root)
        success = migrate_project_to_schema30(
            success_root,
            active_validator=_migration_projection_validator(success_root),
        )
        success_version = int(_scalar(success_root, "select schema_version from project where id=1"))
        preserved = tuple(_scalar(success_root, f"select count(*) from {table}") for table in ("requirements", "tasks", "executions", "validations", "decisions"))
        retired_tables = int(_scalar(success_root, "select count(*) from sqlite_master where type='table' and name in ('adapter_actions','agent_provider_sessions','runtime_snapshots','command_log')"))

        rollback_root = base / "schema27-rollback"
        rollback_source = _create_schema27_fixture(rollback_root)
        rolled_back = False
        try:
            migrate_project_to_schema30(
                rollback_root,
                fail_at="after_atomic_replace",
                active_validator=_migration_projection_validator(rollback_root),
            )
        except InjectedLocalCoreMigrationFailure:
            rolled_back = True
        rollback_version = int(_scalar(rollback_root, "select schema_version from project where id=1"))
        sentinel = str(_scalar(rollback_root, "select decision from decisions where id='D-sentinel'"))
        backup_dirs = list((rollback_root / ".ai-team/backups").glob("schema-27-before-local-core-*"))
        ok = (
            success.source_version == 27
            and success.target_version == SCHEMA30_VERSION
            and success_version == SCHEMA30_VERSION
            and preserved == (1, 2, 1, 1, 1)
            and retired_tables == 0
            and Path(success.migration_manifest_path).is_file()
            and rolled_back
            and rollback_source.is_file()
            and rollback_version == 27
            and sentinel == "keep"
            and len(backup_dirs) == 1
        )
        return scenario_result(
            "schema27_29_migration_and_rollback",
            started,
            ok,
            {
                "schema27_source_table_count": 53,
                "schema27_target_version": success.target_version,
                "schema27_success_version": success_version,
                "schema27_rollback_version": rollback_version,
                "preserved_local_fact_counts": list(preserved),
                "retired_active_table_count": retired_tables,
                "rollback_observed": rolled_back,
                "rollback_backup_count": len(backup_dirs),
                "migration_rollback_count": int(rolled_back and rollback_version == 27),
            },
            category="migration",
            mode="local",
        )


def scenario_installed_plugin_surface() -> dict[str, Any]:
    started = time.perf_counter()
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        home = root / "home"
        env = os.environ.copy()
        env["HOME"] = str(home)
        env["PYTHONPATH"] = str(ROOT)
        installed = subprocess.run(
            [
                sys.executable,
                "-m",
                "kafa.cli",
                "plugin",
                "install",
                "--scope",
                "user",
                "--repo",
                str(ROOT),
            ],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        plugin = home / ".agents/plugins/codex-project-harness"
        skills = {path.name for path in (plugin / "skills").iterdir() if path.is_dir()} if plugin.is_dir() else set()
        hooks_payload = json.loads((plugin / "hooks/hooks.json").read_text(encoding="utf-8")) if plugin.is_dir() else {}
        hooks = set(hooks_payload.get("hooks", {}))
        templates = {path.name for path in (plugin / "templates/agents").glob("*.toml")}
        scripts = {path.name for path in (plugin / "scripts").glob("*.py")}
        schemas = {path.name for path in (plugin / "schemas").glob("*.json")}
        retired = [relative for relative in RETIRED_RUNTIME_PATHS if (plugin / relative).exists()]
        ok = (
            installed.returncode == 0
            and skills == APPROVED_SKILLS
            and hooks == {"SessionStart", "SubagentStart", "Stop"}
            and templates == APPROVED_AGENT_TEMPLATES
            and scripts == APPROVED_RUNTIME_SCRIPTS
            and schemas == APPROVED_SCHEMA_FILES
            and not retired
        )
        return scenario_result(
            "installed_plugin_surface",
            started,
            ok,
            {
                "discovery_scope": "isolated installed filesystem contract",
                "skill_count": len(skills),
                "hook_count": len(hooks),
                "template_count": len(templates),
                "runtime_script_count": len(scripts),
                "schema_count": len(schemas),
                "retired_paths": sorted(retired),
            },
            category="installation",
            mode="local",
        )


FIXTURE_SCENARIOS: list[Callable[[], dict[str, Any]]] = [
    scenario_fresh_local_install_and_init,
    scenario_quickstart_stops_before_independent_review,
    scenario_current_candidate_supersedes_stale_validation,
    scenario_manual_evidence_cannot_satisfy_delivery,
    scenario_open_high_finding_blocks_delivery,
    scenario_high_risk_requires_human_review,
]

STABILITY_SCENARIOS: list[Callable[[], dict[str, Any]]] = [
    scenario_structured_and_no_network_policy_fail_closed,
    scenario_cycle_isolation,
    scenario_sqlite_contention_stress,
    scenario_schema27_29_migration_and_rollback,
    scenario_installed_plugin_surface,
]


def summarize(
    mode: str,
    scenarios: list[dict[str, Any]],
    started: float,
    *,
    live_skipped: bool = False,
    live_skipped_reasons: list[str] | None = None,
    live_status: str = "",
    native_host: dict[str, Any] | None = None,
) -> dict[str, Any]:
    skipped = sum(1 for scenario in scenarios if scenario.get("skip_reason"))
    passed = sum(1 for scenario in scenarios if scenario["pass"] and not scenario.get("skip_reason"))
    failed = sum(1 for scenario in scenarios if not scenario["pass"] and not scenario.get("skip_reason"))
    sqlite_lock_errors = sum(int(scenario.get("details", {}).get("sqlite_lock_error_count", 0) or 0) for scenario in scenarios)
    false_passes = sum(int(scenario.get("details", {}).get("false_pass_count", 0) or 0) for scenario in scenarios)
    forged_blocks = sum(int(scenario.get("details", {}).get("forged_evidence_block_count", 0) or 0) for scenario in scenarios)
    expected_human_reviews = sum(
        int(scenario.get("details", {}).get("expected_human_review_required_count", 0) or 0)
        for scenario in scenarios
    )
    human_interventions = sum(
        int(scenario.get("details", {}).get("human_intervention_count", 0) or 0)
        for scenario in scenarios
    )
    pass_rate = round(passed / max(len(scenarios), 1), 4)
    summary = {
        "scenario_count": len(scenarios),
        "passed_count": passed,
        "failed_count": failed,
        "skipped_count": skipped,
        "scenario_pass_rate": pass_rate,
        "false_pass_count": false_passes,
        "forged_evidence_block_count": forged_blocks,
        "expected_human_review_required_count": expected_human_reviews,
        "sqlite_lock_error_count": sqlite_lock_errors,
        "human_intervention_count": human_interventions,
        "duration_seconds": round(time.perf_counter() - started, 6),
    }
    resolved_live_status = live_status or "not-applicable"
    if not live_status and mode.startswith("live-codex"):
        if live_skipped:
            resolved_live_status = "not-run"
        elif failed:
            resolved_live_status = "failed"
        elif scenarios:
            resolved_live_status = "passed"
    native_token_counts = [
        scenario.get("details", {}).get("native_token_count")
        for scenario in scenarios
        if isinstance(scenario.get("details", {}).get("native_token_count"), int)
    ]
    native_runtime_seconds = [
        scenario.get("details", {}).get("native_runtime_seconds")
        for scenario in scenarios
        if isinstance(scenario.get("details", {}).get("native_runtime_seconds"), (int, float))
    ]
    native_usages = [
        scenario.get("details", {}).get("native_usage")
        for scenario in scenarios
        if isinstance(scenario.get("details", {}).get("native_usage"), dict)
    ]
    aggregate_usage = (
        {
            field: sum(int(usage[field]) for usage in native_usages)
            for field in (*NATIVE_USAGE_FIELDS, "token_count")
        }
        if native_usages
        else None
    )
    return {
        "report_version": REPORT_VERSION,
        "mode": mode,
        "evaluation_source": evaluation_source_identity(),
        "live_skipped": live_skipped,
        "live_status": resolved_live_status,
        "matrix": matrix_info(mode, live_skipped_reasons=live_skipped_reasons),
        "native_host": native_host,
        "evidence_scope": "deterministic-local-runtime" if mode in {"fixture", "stability"} else mode,
        "token_count": sum(native_token_counts) if native_token_counts else None,
        "token_usage": aggregate_usage,
        "estimated_cost": None,
        "agent_runtime_seconds": (
            round(sum(float(value) for value in native_runtime_seconds), 6)
            if native_runtime_seconds
            else None
        ),
        "summary": summary,
        "scenarios": scenarios,
    }


def run_fixture() -> dict[str, Any]:
    started = time.perf_counter()
    scenarios: list[dict[str, Any]] = []
    for scenario in FIXTURE_SCENARIOS:
        try:
            scenarios.append(scenario())
        except Exception as exc:  # noqa: BLE001 - eval output should show scenario failure.
            name = scenario.__name__.replace("scenario_", "")
            category, scenario_mode = LOCAL_SCENARIO_CONTRACT[name]
            scenarios.append(
                scenario_result(
                    name,
                    started,
                    False,
                    {"error": str(exc)},
                    category=category,
                    mode=scenario_mode,
                )
            )
    return summarize("fixture", scenarios, started)


def run_stability() -> dict[str, Any]:
    started = time.perf_counter()
    scenarios: list[dict[str, Any]] = []
    for scenario in [*FIXTURE_SCENARIOS, *STABILITY_SCENARIOS]:
        try:
            scenarios.append(scenario())
        except Exception as exc:  # noqa: BLE001 - eval output should show scenario failure.
            name = scenario.__name__.replace("scenario_", "")
            category, scenario_mode = LOCAL_SCENARIO_CONTRACT[name]
            scenarios.append(
                scenario_result(
                    name,
                    started,
                    False,
                    {"error": str(exc)},
                    category=category,
                    mode=scenario_mode,
                )
            )
    return summarize("stability", scenarios, started)


class LiveCapabilityBlocked(RuntimeError):
    pass


def codex_cli_command(codex: str, *args: str) -> list[str]:
    if os.name == "nt" and Path(codex).suffix.lower() in {".cmd", ".bat"}:
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", codex, *args]
    return [codex, *args]


def live_codex_binary() -> str:
    configured = os.environ.get("HARNESS_E2E_CODEX_BIN", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if path.is_file():
            return str(path.resolve())
        return shutil.which(configured) or ""
    return shutil.which("codex") or ""


def live_codex_binary_metadata(codex: str) -> dict[str, Any]:
    path = Path(codex).expanduser().resolve()
    return {
        "resolved_path": str(path),
        "sha256": _sha256(path) if path.is_file() else "",
        "source": (
            "explicit-test-override"
            if os.environ.get("HARNESS_E2E_CODEX_BIN", "").strip()
            else "path-discovery"
        ),
        "trust": "local-capability-only-not-delivery-provenance",
    }


LIVE_ENV_ALLOWLIST = {
    "PATH",
    "SHELL",
    "TMPDIR",
    "TEMP",
    "TMP",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "NO_COLOR",
    "CODEX_CI",
    "CODEX_INTERNAL_ORIGINATOR_OVERRIDE",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
}


def isolated_live_codex_environment(target: Path) -> dict[str, str]:
    """Copy portable auth and an explicit non-secret process environment."""

    source_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
    source_auth = source_home / "auth.json"
    if not source_auth.is_file():
        raise LiveCapabilityBlocked("authenticated Codex home has no portable auth.json")
    target.mkdir(parents=True, mode=0o700, exist_ok=True)
    target_auth = target / "auth.json"
    shutil.copy2(source_auth, target_auth)
    target_auth.chmod(0o600)
    env = {key: value for key, value in os.environ.items() if key in LIVE_ENV_ALLOWLIST}
    env["HOME"] = str(target)
    env["CODEX_HOME"] = str(target)
    if os.name == "nt":
        env["USERPROFILE"] = str(target)
        env["APPDATA"] = str(target / "AppData/Roaming")
        env["LOCALAPPDATA"] = str(target / "AppData/Local")
    return env


def run_live_preflight(codex: str, env: dict[str, str]) -> str:
    try:
        login = subprocess.run(
            codex_cli_command(codex, "login", "status"),
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LiveCapabilityBlocked(f"Codex login capability is unavailable: {exc}") from exc
    if login.returncode != 0:
        raise LiveCapabilityBlocked(f"Codex login status failed (exit {login.returncode})")
    version = command_version(codex_cli_command(codex, "--version"), env=env)
    if version != EXPECTED_CODEX_CLI_VERSION:
        raise LiveCapabilityBlocked(
            f"Codex CLI version mismatch; expected {EXPECTED_CODEX_CLI_VERSION}"
        )
    return version


def prepare_live_profile(
    *,
    mode: str,
    scenario_name: str,
    enable_env: str,
    started: float,
) -> tuple[str, str, dict[str, Any] | None, dict[str, Any] | None]:
    """Resolve an opt-in Native Host capability without selecting a model."""

    if os.environ.get(enable_env) != "1":
        reasons = [f"{enable_env} is not set to 1"]
        scenarios = [
            skipped_scenario(
                scenario_name,
                "; ".join(reasons),
                category=mode,
                mode=mode,
            )
        ]
        return "", "", None, summarize(
            mode,
            scenarios,
            started,
            live_skipped=True,
            live_skipped_reasons=reasons,
        )
    if (
        os.environ.get("HARNESS_E2E_CODEX_BIN", "").strip()
        and os.environ.get("HARNESS_E2E_ALLOW_CODEX_BIN_OVERRIDE") != "1"
    ):
        reason = (
            "HARNESS_E2E_CODEX_BIN is a test override and requires "
            "HARNESS_E2E_ALLOW_CODEX_BIN_OVERRIDE=1"
        )
        scenarios = [
            scenario_result(
                scenario_name,
                started,
                False,
                {"capability_status": "blocked", "reason": reason},
                category=mode,
                mode=mode,
            )
        ]
        return "", "", None, summarize(mode, scenarios, started, live_status="blocked")
    codex = live_codex_binary()
    if not codex:
        reason = "codex CLI is not available on PATH or HARNESS_E2E_CODEX_BIN"
        scenarios = [
            scenario_result(
                scenario_name,
                started,
                False,
                {"capability_status": "blocked", "reason": reason},
                category=mode,
                mode=mode,
            )
        ]
        return "", "", None, summarize(mode, scenarios, started, live_status="blocked")
    native_host = live_codex_binary_metadata(codex)
    try:
        with tempfile.TemporaryDirectory(prefix="kafa-live-preflight-") as live_home:
            preflight_env = isolated_live_codex_environment(Path(live_home))
            version = run_live_preflight(codex, preflight_env)
    except LiveCapabilityBlocked as exc:
        scenarios = [
            scenario_result(
                scenario_name,
                started,
                False,
                {"capability_status": "blocked", "reason": str(exc)},
                category=mode,
                mode=mode,
            )
        ]
        return "", "", native_host, summarize(
            mode,
            scenarios,
            started,
            live_status="blocked",
            native_host=native_host,
        )
    return codex, version, native_host, None


def _run_live_codex_unpinned() -> dict[str, Any]:
    started = time.perf_counter()
    mode = "live-codex"
    scenario_name = "native_codex_edit_and_controller_verify"
    codex, codex_version, native_host, unavailable = prepare_live_profile(
        mode=mode,
        scenario_name=scenario_name,
        enable_env="HARNESS_E2E_ENABLE_LIVE_CODEX",
        started=started,
    )
    if unavailable is not None:
        return unavailable

    try:
        timeout = int(os.environ.get("HARNESS_E2E_LIVE_TIMEOUT", "600"))
    except ValueError:
        timeout = 600
    with (
        tempfile.TemporaryDirectory(prefix="kafa-live-controller-") as temp,
        tempfile.TemporaryDirectory(prefix="kafa-live-producer-") as producer_temp,
        tempfile.TemporaryDirectory(prefix="kafa-live-codex-home-") as live_home,
    ):
        root = Path(temp)
        producer_root = Path(producer_temp)
        try:
            native_env = isolated_live_codex_environment(Path(live_home))
        except LiveCapabilityBlocked as exc:
            scenario = scenario_result(
                scenario_name,
                started,
                False,
                {"capability_status": "blocked", "reason": str(exc)},
                category="live-codex",
                mode="live-codex",
            )
            return summarize(
                "live-codex",
                [scenario],
                started,
                live_status="blocked",
                native_host=native_host,
            )
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.name", "Kafa Live Eval"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "kafa-live@example.invalid"], cwd=root, check=True)
        (root / "candidate.py").write_text('VALUE = "before"\n', encoding="utf-8")
        (root / "test_candidate.py").write_text(
            "import unittest\n"
            "import candidate\n\n"
            "class CandidateTest(unittest.TestCase):\n"
            "    def test_native_edit(self):\n"
            "        self.assertEqual(candidate.VALUE, 'after')\n",
            encoding="utf-8",
        )
        _require_ok(run_harness(root, "init", check=False))
        subprocess.run(
            ["git", "add", ".gitignore", "candidate.py", "test_candidate.py"],
            cwd=root,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "red live candidate"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        _initialize_producer_workspace(
            producer_root,
            {
                "candidate.py": 'VALUE = "before"\n',
                "test_candidate.py": (
                    "import unittest\n"
                    "import candidate\n\n"
                    "class CandidateTest(unittest.TestCase):\n"
                    "    def test_native_edit(self):\n"
                    "        self.assertEqual(candidate.VALUE, 'after')\n"
                ),
            },
            name="Kafa Single Producer Eval",
        )
        for args in (
            ("acceptance", "add", "--id", "LIVE-AC1", "--criterion", "candidate value is after"),
            ("task", "add", "--id", "LIVE-T1", "--task", "edit candidate", "--acceptance", "LIVE-AC1"),
            (
                "test-target",
                "add",
                "--id",
                "LIVE-UNIT",
                "--kind",
                "unit",
                "--command-template",
                "python3 -B -m unittest test_candidate.py",
            ),
            ("test-target", "link", "--task", "LIVE-T1", "--target", "LIVE-UNIT"),
            ("task", "start", "LIVE-T1"),
        ):
            _require_ok(run_harness(root, *args, check=False))

        pre_edit = subprocess.run(
            [sys.executable, "-B", "-m", "unittest", "test_candidate.py"],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
        controller_test_digest = _sha256(root / "test_candidate.py")
        controller_state_digest = _sha256(root / ".ai-team/state/harness.db")
        output_dir = Path(live_home) / "messages"
        output_dir.mkdir(parents=True)
        producer_result = _run_native_eval_producer(
            codex=codex,
            root=producer_root,
            native_env=native_env,
            timeout=timeout,
            output_dir=output_dir,
            producer={
                "task": "LIVE-T1",
                "marker": "SINGLE-PRODUCER",
                "exclusive_files": ["candidate.py"],
                "test_file": "test_candidate.py",
                "test_command": "python3 -B -m unittest test_candidate.py",
                "context_id": "native-codex-task",
                "capability_hint": "fast",
            },
        )
        controller_state_unchanged = (
            _sha256(root / ".ai-team/state/harness.db") == controller_state_digest
        )
        producer_scope_valid = bool(producer_result["scope_valid"])
        integrated_files: list[str] = []
        if (
            producer_result["returncode"] == 0
            and producer_scope_valid
            and controller_state_unchanged
        ):
            shutil.copy2(producer_root / "candidate.py", root / "candidate.py")
            integrated_files.append("candidate.py")
        controller_test_unchanged = _sha256(root / "test_candidate.py") == controller_test_digest
        if integrated_files == ["candidate.py"] and controller_test_unchanged:
            controller = run_harness(
                root,
                "verify",
                "run",
                "--target",
                "LIVE-UNIT",
                "--acceptance",
                "LIVE-AC1",
                check=False,
                timeout=120,
            )
            controller_verify_status = "passed" if controller.returncode == 0 else "failed"
        else:
            controller = subprocess.CompletedProcess([], 1, "", "producer scope rejected before verification")
            controller_verify_status = "not-run"
        if controller.returncode == 0:
            submit = run_harness(
                root,
                "task",
                "submit",
                "LIVE-T1",
                "--context-id",
                "native-codex-task",
                "--evidence",
                "controller verification passed after Native Codex returned",
                check=False,
            )
        else:
            submit = subprocess.CompletedProcess([], 1, "", "controller verification failed")
        execution_count = int(_scalar(root, "select count(*) from executions"))
        validation_count = int(_scalar(root, "select count(*) from validations"))
        task_status = str(_scalar(root, "select status from tasks where id='LIVE-T1'"))
        retired_host_tables, provider_surface_absent = active_table_contract(root)
        passed = (
            pre_edit.returncode != 0
            and producer_result["returncode"] == 0
            and producer_scope_valid
            and producer_result["changed_files"] == ["candidate.py"]
            and producer_result["test_file_unchanged"]
            and controller_state_unchanged
            and controller_test_unchanged
            and integrated_files == ["candidate.py"]
            and producer_result["native_usage"] is not None
            and controller.returncode == 0
            and submit.returncode == 0
            and execution_count == 1
            and validation_count == 1
            and task_status == "submitted"
            and provider_surface_absent
        )
        scenario = scenario_result(
            scenario_name,
            started,
            passed,
            {
                "capability_status": "passed" if passed else "failed",
                "codex_version": codex_version,
                "pre_edit_returncode": pre_edit.returncode,
                "native_returncode": producer_result["returncode"],
                "native_runtime_seconds": producer_result["runtime_seconds"],
                "native_runtime_source": "controller-wall-clock",
                "native_usage": producer_result["native_usage"],
                "native_token_count": producer_result["token_count"],
                "native_token_source": producer_result["token_source"],
                "native_token_scope": NATIVE_TOKEN_SCOPE,
                "workload_family": LIVE_WORKLOAD_FAMILY,
                "workload_unit_sha256": LIVE_WORKLOAD_UNIT_SHA256,
                "workload_units": 1,
                "native_stdout_tail": producer_result["stdout_tail"],
                "native_stderr_tail": producer_result["stderr_tail"],
                "exclusive_files": producer_result["exclusive_files"],
                "changed_files": producer_result["changed_files"],
                "producer_changed_files": producer_result["changed_files"],
                "integrated_files": integrated_files,
                "producer_scope_valid": producer_scope_valid,
                "producer_workspace_isolated": True,
                "test_file_unchanged": bool(producer_result["test_file_unchanged"]),
                "controller_test_unchanged": controller_test_unchanged,
                "controller_state_unchanged_during_native": controller_state_unchanged,
                "controller_verify_returncode": controller.returncode,
                "controller_verify_status": controller_verify_status,
                "controller_verify_output": (controller.stdout + controller.stderr)[-2000:],
                "task_submit_returncode": submit.returncode,
                "execution_count": execution_count,
                "validation_count": validation_count,
                "task_status": task_status,
                "provider_surface_absent": provider_surface_absent,
                "retired_host_tables": retired_host_tables,
                "last_message_recorded": producer_result["last_message_recorded"],
                "human_intervention_count": 0,
                "false_pass_count": int(
                    producer_result["returncode"] == 0
                    and controller_verify_status == "failed"
                ),
            },
            category="live-codex",
            mode="live-codex",
        )
        return summarize(
            "live-codex",
            [scenario],
            started,
            live_status="passed" if passed else "failed",
            native_host=native_host,
        )


def _initialize_producer_workspace(root: Path, files: dict[str, str], *, name: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", name], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "kafa-native-eval@example.invalid"], cwd=root, check=True)
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        ["git", "commit", "-m", "red Native Host producer candidate"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


def _git_changed_files(root: Path) -> list[str]:
    lines = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.splitlines()
    return sorted(line[3:] for line in lines if len(line) > 3)


def _run_native_eval_producer(
    *,
    codex: str,
    root: Path,
    native_env: dict[str, str],
    timeout: int,
    output_dir: Path,
    producer: dict[str, Any],
) -> dict[str, Any]:
    task = str(producer["task"])
    relative = normalize_live_eval_path(producer["exclusive_files"][0])
    test_command = str(producer["test_command"])
    marker = str(producer["marker"])
    test_file = normalize_live_eval_path(producer["test_file"])
    test_digest_before = _sha256(root / test_file)
    last_message = output_dir / f"{task}.txt"
    prompt = (
        f"{marker}. Capability hint: fast; actual model selection remains Host-owned. "
        f"In this isolated local repository, edit only {relative} so `{test_command}` passes. "
        "Do not edit tests, .gitignore, .ai-team, .codex, or docs/harness. "
        f"Run {test_command}. Do not commit, create branches, or use network services. "
        "Stop after the bounded local edit and test."
    )
    command = codex_cli_command(
        codex,
        "exec",
        "--ignore-user-config",
        "--cd",
        str(root),
        "--sandbox",
        "workspace-write",
        "--ephemeral",
        "--json",
        "--color",
        "never",
        "--output-last-message",
        str(last_message),
        prompt,
    )
    launched = time.perf_counter()
    try:
        result = subprocess.run(
            command,
            cwd=root,
            env=native_env,
            text=True,
            capture_output=True,
            check=False,
            timeout=max(timeout, 1),
        )
        error = ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        result = subprocess.CompletedProcess(command, 124, "", str(exc))
        error = str(exc)
    finished = time.perf_counter()
    usage = parse_native_usage_jsonl(result.stdout)
    changed_files = _git_changed_files(root)
    test_file_unchanged = _sha256(root / test_file) == test_digest_before
    exclusive_files = sorted(normalize_live_eval_path(path) for path in producer["exclusive_files"])
    scope_valid = changed_files == exclusive_files and test_file_unchanged
    return {
        "task": task,
        "exclusive_files": exclusive_files,
        "changed_files": changed_files,
        "scope_valid": scope_valid,
        "test_file_unchanged": test_file_unchanged,
        "capability_hint": str(producer["capability_hint"]),
        "context_id": str(producer["context_id"]),
        "target": str(producer.get("target") or ""),
        "acceptance": str(producer.get("acceptance") or ""),
        "returncode": result.returncode,
        "started_at": launched,
        "finished_at": finished,
        "runtime_seconds": round(finished - launched, 6),
        "native_usage": usage,
        "token_count": usage["token_count"] if usage is not None else None,
        "token_source": "codex-json-turn.completed" if usage is not None else "unavailable",
        "stdout_tail": result.stdout[-2000:],
        "stderr_tail": result.stderr[-2000:],
        "last_message_recorded": last_message.is_file(),
        "error": error,
    }


def _run_live_codex_parallel_unpinned() -> dict[str, Any]:
    """Run two disjoint Native Host producers and root-owned integration verification."""

    started = time.perf_counter()
    mode = "live-codex-parallel"
    scenario_name = "native_codex_two_producer_integration"
    codex, codex_version, native_host, unavailable = prepare_live_profile(
        mode=mode,
        scenario_name=scenario_name,
        enable_env="HARNESS_E2E_ENABLE_LIVE_CODEX_PARALLEL",
        started=started,
    )
    if unavailable is not None:
        return unavailable
    try:
        timeout = int(os.environ.get("HARNESS_E2E_LIVE_TIMEOUT", "600"))
    except ValueError:
        timeout = 600

    with (
        tempfile.TemporaryDirectory(prefix="kafa-live-parallel-controller-") as temp,
        tempfile.TemporaryDirectory(prefix="kafa-live-alpha-producer-") as alpha_temp,
        tempfile.TemporaryDirectory(prefix="kafa-live-beta-producer-") as beta_temp,
        tempfile.TemporaryDirectory(prefix="kafa-live-parallel-home-") as live_home,
    ):
        root = Path(temp)
        producer_roots = [Path(alpha_temp), Path(beta_temp)]
        output_dir = Path(live_home) / "messages"
        output_dir.mkdir(parents=True)
        producers = [
            {
                "task": "LIVE-P1",
                "marker": "ALPHA-PRODUCER",
                "exclusive_files": ["alpha.py"],
                "test_file": "test_alpha.py",
                "test_command": "python3 -B -m unittest test_alpha.py",
                "target": "LIVE-ALPHA",
                "acceptance": "LIVE-AC-A",
                "context_id": "native-alpha-producer",
                "capability_hint": "fast",
            },
            {
                "task": "LIVE-P2",
                "marker": "BETA-PRODUCER",
                "exclusive_files": ["beta.py"],
                "test_file": "test_beta.py",
                "test_command": "python3 -B -m unittest test_beta.py",
                "target": "LIVE-BETA",
                "acceptance": "LIVE-AC-B",
                "context_id": "native-beta-producer",
                "capability_hint": "fast",
            },
        ]
        scope_conflicts = live_eval_scope_conflicts(producers)
        if scope_conflicts:
            scenario = scenario_result(
                scenario_name,
                started,
                False,
                {
                    "capability_status": "blocked",
                    "reason": "parallel live eval write scopes overlap",
                    "scope_conflicts": scope_conflicts,
                    "overlap_policy": "block-parallel-on-declared-overlap",
                },
                category=mode,
                mode=mode,
            )
            return summarize(
                mode,
                [scenario],
                started,
                live_status="blocked",
                native_host=native_host,
            )
        try:
            native_envs = [
                isolated_live_codex_environment(Path(live_home) / f"producer-{index}")
                for index in range(len(producers))
            ]
        except LiveCapabilityBlocked as exc:
            scenario = scenario_result(
                scenario_name,
                started,
                False,
                {"capability_status": "blocked", "reason": str(exc)},
                category=mode,
                mode=mode,
            )
            return summarize(
                mode,
                [scenario],
                started,
                live_status="blocked",
                native_host=native_host,
            )

        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.name", "Kafa Parallel Eval"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "kafa-parallel@example.invalid"], cwd=root, check=True)
        (root / "alpha.py").write_text('VALUE = "before"\n', encoding="utf-8")
        (root / "beta.py").write_text('VALUE = "before"\n', encoding="utf-8")
        (root / "test_alpha.py").write_text(
            "import unittest\nimport alpha\n\n"
            "class AlphaTest(unittest.TestCase):\n"
            "    def test_alpha(self):\n"
            "        self.assertEqual(alpha.VALUE, 'after')\n",
            encoding="utf-8",
        )
        (root / "test_beta.py").write_text(
            "import unittest\nimport beta\n\n"
            "class BetaTest(unittest.TestCase):\n"
            "    def test_beta(self):\n"
            "        self.assertEqual(beta.VALUE, 'after')\n",
            encoding="utf-8",
        )
        (root / "test_integration.py").write_text(
            "import unittest\nimport alpha\nimport beta\n\n"
            "class IntegrationTest(unittest.TestCase):\n"
            "    def test_both(self):\n"
            "        self.assertEqual((alpha.VALUE, beta.VALUE), ('after', 'after'))\n",
            encoding="utf-8",
        )
        _require_ok(run_harness(root, "init", check=False))
        subprocess.run(
            [
                "git",
                "add",
                ".gitignore",
                "alpha.py",
                "beta.py",
                "test_alpha.py",
                "test_beta.py",
                "test_integration.py",
            ],
            cwd=root,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "red parallel candidate"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        _initialize_producer_workspace(
            producer_roots[0],
            {
                "alpha.py": (root / "alpha.py").read_text(encoding="utf-8"),
                "test_alpha.py": (root / "test_alpha.py").read_text(encoding="utf-8"),
            },
            name="Kafa Alpha Producer Eval",
        )
        _initialize_producer_workspace(
            producer_roots[1],
            {
                "beta.py": (root / "beta.py").read_text(encoding="utf-8"),
                "test_beta.py": (root / "test_beta.py").read_text(encoding="utf-8"),
            },
            name="Kafa Beta Producer Eval",
        )
        test_digests_before = {
            name: _sha256(root / name)
            for name in ("test_alpha.py", "test_beta.py", "test_integration.py")
        }
        pre_edit = subprocess.run(
            [sys.executable, "-B", "-m", "unittest", "test_integration.py"],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
        setup_commands = [
            ("acceptance", "add", "--id", "LIVE-AC-A", "--criterion", "alpha value is after"),
            ("acceptance", "add", "--id", "LIVE-AC-B", "--criterion", "beta value is after"),
            ("acceptance", "add", "--id", "LIVE-AC-I", "--criterion", "alpha and beta are after together"),
            ("task", "add", "--id", "LIVE-P1", "--task", "edit alpha", "--acceptance", "LIVE-AC-A"),
            ("task", "add", "--id", "LIVE-P2", "--task", "edit beta", "--acceptance", "LIVE-AC-B"),
            (
                "task",
                "add",
                "--id",
                "LIVE-INTEGRATE",
                "--task",
                "verify combined candidate",
                "--acceptance",
                "LIVE-AC-I",
                "--depends-on",
                "LIVE-P1,LIVE-P2",
            ),
            (
                "test-target",
                "add",
                "--id",
                "LIVE-ALPHA",
                "--kind",
                "unit",
                "--command-template",
                "python3 -B -m unittest test_alpha.py",
            ),
            (
                "test-target",
                "add",
                "--id",
                "LIVE-BETA",
                "--kind",
                "unit",
                "--command-template",
                "python3 -B -m unittest test_beta.py",
            ),
            (
                "test-target",
                "add",
                "--id",
                "LIVE-COMBINED",
                "--kind",
                "integration",
                "--command-template",
                "python3 -B -m unittest test_integration.py",
            ),
            ("test-target", "link", "--task", "LIVE-P1", "--target", "LIVE-ALPHA"),
            ("test-target", "link", "--task", "LIVE-P2", "--target", "LIVE-BETA"),
            ("test-target", "link", "--task", "LIVE-INTEGRATE", "--target", "LIVE-COMBINED"),
            ("task", "start", "LIVE-P1"),
            ("task", "start", "LIVE-P2"),
        ]
        for args in setup_commands:
            _require_ok(run_harness(root, *args, check=False))
        integration_blocked = run_harness(root, "task", "start", "LIVE-INTEGRATE", check=False)
        controller_state_digest = _sha256(root / ".ai-team/state/harness.db")

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    _run_native_eval_producer,
                    codex=codex,
                    root=producer_roots[index],
                    native_env=native_envs[index],
                    timeout=timeout,
                    output_dir=output_dir,
                    producer=producer,
                )
                for index, producer in enumerate(producers)
            ]
            producer_results = [future.result() for future in futures]

        producer_overlap_seconds = round(
            max(
                0.0,
                min(float(result["finished_at"]) for result in producer_results)
                - max(float(result["started_at"]) for result in producer_results),
            ),
            6,
        )
        native_runtime_seconds = round(
            max(float(result["finished_at"]) for result in producer_results)
            - min(float(result["started_at"]) for result in producer_results),
            6,
        )
        usage_values = [result["native_usage"] for result in producer_results]
        native_usage = (
            {
                field: sum(int(usage[field]) for usage in usage_values)
                for field in (*NATIVE_USAGE_FIELDS, "token_count")
            }
            if all(isinstance(usage, dict) for usage in usage_values)
            else None
        )
        native_token_count = native_usage["token_count"] if native_usage is not None else None
        producer_attribution_valid = all(
            result["returncode"] == 0 and result["scope_valid"]
            for result in producer_results
        )
        controller_state_unchanged = (
            _sha256(root / ".ai-team/state/harness.db") == controller_state_digest
        )
        integrated_files: list[str] = []
        if producer_attribution_valid and controller_state_unchanged:
            for producer_root, producer in zip(producer_roots, producers, strict=True):
                for relative in sorted(
                    normalize_live_eval_path(path) for path in producer["exclusive_files"]
                ):
                    shutil.copy2(producer_root / relative, root / relative)
                    integrated_files.append(relative)
        integrated_files.sort()
        changed_files = [
            relative
            for relative in _git_changed_files(root)
            if relative != ".gitignore"
            and not relative.startswith((".ai-team/", ".codex/agents/", "docs/harness/"))
        ]
        test_files_unchanged = all(
            _sha256(root / name) == digest for name, digest in test_digests_before.items()
        )
        expected_integrated_files = sorted(
            normalize_live_eval_path(path)
            for producer in producers
            for path in producer["exclusive_files"]
        )
        if (
            producer_attribution_valid
            and controller_state_unchanged
            and test_files_unchanged
            and integrated_files == expected_integrated_files
        ):
            targeted_results = {
                str(producer["target"]): run_harness(
                    root,
                    "verify",
                    "run",
                    "--target",
                    str(producer["target"]),
                    "--acceptance",
                    str(producer["acceptance"]),
                    check=False,
                )
                for producer in producers
            }
        else:
            targeted_results = {
                str(producer["target"]): subprocess.CompletedProcess(
                    [], 1, "", "producer scope rejected before verification"
                )
                for producer in producers
            }
        producer_state_results: list[subprocess.CompletedProcess[str]] = []
        if all(result.returncode == 0 for result in targeted_results.values()):
            for producer in producers:
                submitted = run_harness(
                    root,
                    "task",
                    "submit",
                    str(producer["task"]),
                    "--context-id",
                    str(producer["context_id"]),
                    "--evidence",
                    "root verified bounded Native Host producer output",
                    check=False,
                )
                accepted = run_harness(
                    root,
                    "task",
                    "accept",
                    str(producer["task"]),
                    "--evidence",
                    "root inspected exclusive file diff and targeted verification",
                    check=False,
                )
                producer_state_results.extend([submitted, accepted])
        integration_started = run_harness(root, "task", "start", "LIVE-INTEGRATE", check=False)
        if integration_started.returncode == 0:
            combined = run_harness(
                root,
                "verify",
                "run",
                "--target",
                "LIVE-COMBINED",
                "--acceptance",
                "LIVE-AC-I",
                check=False,
            )
            combined_verify_status = "passed" if combined.returncode == 0 else "failed"
        else:
            combined = subprocess.CompletedProcess([], 1, "", "integration prerequisites not accepted")
            combined_verify_status = "not-run"
        if combined.returncode == 0:
            integration_submitted = run_harness(
                root,
                "task",
                "submit",
                "LIVE-INTEGRATE",
                "--context-id",
                "native-root-integrator",
                "--evidence",
                "combined candidate passed root controller integration verification",
                check=False,
            )
        else:
            integration_submitted = subprocess.CompletedProcess([], 1, "", "combined verification failed")
        task_statuses = {
            str(row[0]): str(row[1])
            for row in db_rows(
                root,
                "select id, status from tasks where id in ('LIVE-P1', 'LIVE-P2', 'LIVE-INTEGRATE') order by id",
            )
        }
        execution_count = int(_scalar(root, "select count(*) from executions"))
        validation_count = int(_scalar(root, "select count(*) from validations"))
        retired_host_tables, provider_surface_absent = active_table_contract(root)
        targeted_returncodes = {
            target: result.returncode for target, result in targeted_results.items()
        }
        producer_window_start = min(float(result["started_at"]) for result in producer_results)
        producer_summaries = []
        for result in producer_results:
            summary = {
                key: value
                for key, value in result.items()
                if key not in {"started_at", "finished_at"}
            }
            summary["started_offset_seconds"] = round(
                float(result["started_at"]) - producer_window_start,
                6,
            )
            summary["finished_offset_seconds"] = round(
                float(result["finished_at"]) - producer_window_start,
                6,
            )
            summary["runtime_seconds"] = round(
                float(summary["finished_offset_seconds"])
                - float(summary["started_offset_seconds"]),
                6,
            )
            producer_summaries.append(summary)
        producer_overlap_seconds = round(
            max(
                0.0,
                min(float(item["finished_offset_seconds"]) for item in producer_summaries)
                - max(float(item["started_offset_seconds"]) for item in producer_summaries),
            ),
            6,
        )
        native_runtime_seconds = round(
            max(float(item["finished_offset_seconds"]) for item in producer_summaries)
            - min(float(item["started_offset_seconds"]) for item in producer_summaries),
            6,
        )
        passed = (
            pre_edit.returncode != 0
            and integration_blocked.returncode != 0
            and not scope_conflicts
            and all(result["returncode"] == 0 for result in producer_results)
            and producer_attribution_valid
            and controller_state_unchanged
            and producer_overlap_seconds > 0
            and integrated_files == ["alpha.py", "beta.py"]
            and changed_files == ["alpha.py", "beta.py"]
            and test_files_unchanged
            and all(code == 0 for code in targeted_returncodes.values())
            and len(producer_state_results) == 4
            and all(result.returncode == 0 for result in producer_state_results)
            and integration_started.returncode == 0
            and combined.returncode == 0
            and integration_submitted.returncode == 0
            and task_statuses
            == {"LIVE-INTEGRATE": "submitted", "LIVE-P1": "accepted", "LIVE-P2": "accepted"}
            and execution_count == 3
            and validation_count == 3
            and provider_surface_absent
            and native_usage is not None
            and all(result["last_message_recorded"] for result in producer_results)
        )
        scenario = scenario_result(
            scenario_name,
            started,
            passed,
            {
                "capability_status": "passed" if passed else "failed",
                "codex_version": codex_version,
                "pre_edit_returncode": pre_edit.returncode,
                "producer_count": len(producer_results),
                "producers": producer_summaries,
                "producer_overlap_seconds": producer_overlap_seconds,
                "native_runtime_seconds": native_runtime_seconds,
                "native_runtime_source": "controller-parallel-wall-clock",
                "native_usage": native_usage,
                "native_token_count": native_token_count,
                "native_token_source": "codex-json-turn.completed" if native_usage is not None else "unavailable",
                "native_token_scope": NATIVE_TOKEN_SCOPE,
                "workload_family": LIVE_WORKLOAD_FAMILY,
                "workload_unit_sha256": LIVE_WORKLOAD_UNIT_SHA256,
                "workload_units": len(producer_results),
                "changed_files": changed_files,
                "integrated_files": integrated_files,
                "producer_attribution_valid": producer_attribution_valid,
                "controller_state_unchanged_during_native": controller_state_unchanged,
                "scope_enforcement": "isolated-producer-workspaces-plus-exact-diff-integration",
                "test_files_unchanged": test_files_unchanged,
                "targeted_verify_returncodes": targeted_returncodes,
                "producer_state_returncodes": [
                    result.returncode for result in producer_state_results
                ],
                "integration_start_returncode": integration_started.returncode,
                "combined_verify_returncode": combined.returncode,
                "combined_verify_status": combined_verify_status,
                "integration_submit_returncode": integration_submitted.returncode,
                "integration_dependency_blocked_before_producers": integration_blocked.returncode != 0,
                "task_statuses": task_statuses,
                "execution_count": execution_count,
                "validation_count": validation_count,
                "scope_conflicts": scope_conflicts,
                "overlap_policy": "block-parallel-on-declared-overlap",
                "retired_host_tables": retired_host_tables,
                "provider_surface_absent": provider_surface_absent,
                "human_intervention_count": 0,
                "false_pass_count": int(
                    all(result["returncode"] == 0 for result in producer_results)
                    and combined_verify_status == "failed"
                ),
            },
            category=mode,
            mode=mode,
        )
        return summarize(
            mode,
            [scenario],
            started,
            live_status="passed" if passed else "failed",
            native_host=native_host,
        )


def run_live_codex() -> dict[str, Any]:
    return _run_pinned_live_profile(
        mode="live-codex",
        scenario_name="native_codex_edit_and_controller_verify",
        enable_env="HARNESS_E2E_ENABLE_LIVE_CODEX",
        runner=_run_live_codex_unpinned,
    )


def run_live_codex_parallel() -> dict[str, Any]:
    return _run_pinned_live_profile(
        mode="live-codex-parallel",
        scenario_name="native_codex_parallel_overlap_and_controller_verify",
        enable_env="HARNESS_E2E_ENABLE_LIVE_CODEX_PARALLEL",
        runner=_run_live_codex_parallel_unpinned,
    )


def _valid_nonzero_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value != "0" * 64
        and all(character in "0123456789abcdef" for character in value.lower())
    )


def _valid_aware_iso8601(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _matches_exact_json_contract(actual: object, expected: object) -> bool:
    """Compare closed-report values without Python bool/int or int/float coercion."""

    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(
            _matches_exact_json_contract(actual[key], value)
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(
            _matches_exact_json_contract(item, contract)
            for item, contract in zip(actual, expected, strict=True)
        )
    return actual == expected


def _usage_errors(prefix: str, usage: object) -> list[str]:
    if not isinstance(usage, dict):
        return [f"{prefix} usage is not an object"]
    errors = _unexpected_key_errors(f"{prefix} usage", usage, USAGE_KEYS)
    for field in (*NATIVE_USAGE_FIELDS, "token_count"):
        value = usage.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            errors.append(f"{prefix} {field} is not a non-negative integer")
    if errors:
        return errors
    if usage["cached_input_tokens"] > usage["input_tokens"]:
        errors.append(f"{prefix} cached_input_tokens exceeds input_tokens")
    if usage["reasoning_output_tokens"] > usage["output_tokens"]:
        errors.append(f"{prefix} reasoning_output_tokens exceeds output_tokens")
    if usage["token_count"] != usage["input_tokens"] + usage["output_tokens"]:
        errors.append(f"{prefix} token_count does not equal input_tokens + output_tokens")
    if usage["token_count"] <= 0:
        errors.append(f"{prefix} token_count is not positive")
    return errors


def _aggregate_usages(usages: list[dict[str, Any]]) -> dict[str, int] | None:
    if not usages:
        return None
    return {
        field: sum(int(usage[field]) for usage in usages)
        for field in (*NATIVE_USAGE_FIELDS, "token_count")
    }


def _passing_live_scenario_errors(
    mode: str,
    scenario: dict[str, Any],
) -> list[str]:
    """Validate every persisted fact required by a passing Native profile."""

    name = str(scenario.get("name", "<unknown>"))
    details = scenario.get("details")
    if not isinstance(details, dict):
        return [f"scenario {name} details is not an object"]
    expected_detail_keys = (
        SINGLE_PASSING_DETAIL_KEYS
        if mode == "live-codex"
        else PARALLEL_PASSING_DETAIL_KEYS
        if mode == "live-codex-parallel"
        else frozenset()
    )
    errors = _unexpected_key_errors(
        f"scenario {name} details",
        details,
        expected_detail_keys,
    )

    def require(field: str, expected: object) -> None:
        if not _matches_exact_json_contract(details.get(field), expected):
            errors.append(f"scenario {name} {field} is inconsistent with passing contract")

    if scenario.get("category") != mode:
        errors.append(f"scenario {name} category is inconsistent with live profile")
    if scenario.get("mode") != mode:
        errors.append(f"scenario {name} mode is inconsistent with live profile")
    require("capability_status", "passed")
    pre_edit_returncode = details.get("pre_edit_returncode")
    if (
        not isinstance(pre_edit_returncode, int)
        or isinstance(pre_edit_returncode, bool)
        or pre_edit_returncode == 0
    ):
        errors.append(f"scenario {name} pre_edit_returncode did not prove the red state")
    if not isinstance(details.get("native_usage"), dict):
        errors.append(f"scenario {name} native_usage is required for a passing live profile")
    token_count = details.get("native_token_count")
    if not isinstance(token_count, int) or isinstance(token_count, bool) or token_count <= 0:
        errors.append(f"scenario {name} native_token_count is required")
    runtime = details.get("native_runtime_seconds")
    if (
        not isinstance(runtime, (int, float))
        or isinstance(runtime, bool)
        or not math.isfinite(float(runtime))
        or runtime <= 0
    ):
        errors.append(f"scenario {name} native_runtime_seconds is required")
    duration = scenario.get("duration_seconds")
    if (
        not isinstance(duration, (int, float))
        or isinstance(duration, bool)
        or not math.isfinite(float(duration))
        or duration <= 0
    ):
        errors.append(f"scenario {name} duration_seconds is not positive and finite")
    elif isinstance(runtime, (int, float)) and math.isfinite(float(runtime)) and duration < runtime:
        errors.append(f"scenario {name} duration_seconds is shorter than Native runtime")
    require("native_token_scope", NATIVE_TOKEN_SCOPE)
    require("codex_version", EXPECTED_CODEX_CLI_VERSION)
    require("human_intervention_count", 0)
    require("false_pass_count", 0)

    if mode == "live-codex":
        if name != "native_codex_edit_and_controller_verify":
            errors.append("passing single Native profile has an unexpected scenario")
            return errors
        exact = {
            "native_returncode": 0,
            "native_runtime_source": "controller-wall-clock",
            "exclusive_files": ["candidate.py"],
            "changed_files": ["candidate.py"],
            "producer_changed_files": ["candidate.py"],
            "integrated_files": ["candidate.py"],
            "producer_scope_valid": True,
            "producer_workspace_isolated": True,
            "test_file_unchanged": True,
            "controller_test_unchanged": True,
            "controller_state_unchanged_during_native": True,
            "controller_verify_returncode": 0,
            "controller_verify_status": "passed",
            "task_submit_returncode": 0,
            "execution_count": 1,
            "validation_count": 1,
            "workload_units": 1,
            "task_status": "submitted",
            "provider_surface_absent": True,
            "retired_host_tables": [],
            "last_message_recorded": True,
        }
        for field, expected in exact.items():
            require(field, expected)
        return errors

    if mode == "live-codex-parallel":
        if name != "native_codex_two_producer_integration":
            errors.append("passing parallel Native profile has an unexpected scenario")
            return errors
        exact = {
            "producer_count": 2,
            "native_runtime_source": "controller-parallel-wall-clock",
            "changed_files": ["alpha.py", "beta.py"],
            "integrated_files": ["alpha.py", "beta.py"],
            "producer_attribution_valid": True,
            "controller_state_unchanged_during_native": True,
            "scope_enforcement": "isolated-producer-workspaces-plus-exact-diff-integration",
            "test_files_unchanged": True,
            "targeted_verify_returncodes": {"LIVE-ALPHA": 0, "LIVE-BETA": 0},
            "producer_state_returncodes": [0, 0, 0, 0],
            "integration_start_returncode": 0,
            "combined_verify_returncode": 0,
            "combined_verify_status": "passed",
            "integration_submit_returncode": 0,
            "integration_dependency_blocked_before_producers": True,
            "task_statuses": {
                "LIVE-INTEGRATE": "submitted",
                "LIVE-P1": "accepted",
                "LIVE-P2": "accepted",
            },
            "execution_count": 3,
            "validation_count": 3,
            "workload_units": 2,
            "scope_conflicts": {},
            "overlap_policy": "block-parallel-on-declared-overlap",
            "retired_host_tables": [],
            "provider_surface_absent": True,
        }
        for field, expected in exact.items():
            require(field, expected)
        overlap = details.get("producer_overlap_seconds")
        if not isinstance(overlap, (int, float)) or isinstance(overlap, bool) or overlap <= 0:
            errors.append(f"scenario {name} producer_overlap_seconds is not positive")
        producers = details.get("producers")
        if not isinstance(producers, list) or len(producers) != len(
            PARALLEL_PRODUCER_CONTRACT
        ):
            errors.append(f"scenario {name} producer task set is inconsistent")
        else:
            by_task = {
                str(producer.get("task")): producer
                for producer in producers
                if isinstance(producer, dict)
            }
            if set(by_task) != set(PARALLEL_PRODUCER_CONTRACT):
                errors.append(f"scenario {name} producer task set is inconsistent")
            else:
                for task, contract in PARALLEL_PRODUCER_CONTRACT.items():
                    producer = by_task[task]
                    errors.extend(
                        _unexpected_key_errors(
                            f"scenario {name} producer {task}",
                            producer,
                            PARALLEL_PRODUCER_KEYS,
                        )
                    )
                    for field, expected in contract.items():
                        if not _matches_exact_json_contract(
                            producer.get(field),
                            expected,
                        ):
                            errors.append(
                                f"scenario {name} producer {task} {field} is inconsistent"
                            )
        return errors

    return [f"scenario {name} uses unsupported passing live mode: {mode}"]


def report_consistency_errors(
    report: dict[str, Any],
    *,
    require_current_binary: bool = True,
    require_current_git_state: bool = True,
    require_current_matrix: bool = True,
) -> list[str]:
    """Recompute evidence facts instead of trusting report summary fields.

    Persisted reports retain the Git metadata from their execution.  A later
    commit necessarily changes HEAD and status without changing executable
    bytes, so callers reading committed evidence may disable only the current
    Git-state comparison.  The executable digest and source scope always have
    to match the current checkout.
    """

    errors = _unexpected_key_errors("report", report, REPORT_KEYS)
    scenarios = report.get("scenarios")
    if not isinstance(scenarios, list):
        return ["scenarios is not a list"]
    if any(not isinstance(scenario, dict) for scenario in scenarios):
        return ["scenario entry is not an object"]
    for scenario in scenarios:
        errors.extend(_unexpected_key_errors("scenario", scenario, SCENARIO_KEYS))

    summary = report.get("summary")
    if not isinstance(summary, dict):
        return ["summary is not an object"]
    errors.extend(_unexpected_key_errors("summary", summary, SUMMARY_KEYS))
    mode = report.get("mode")
    if (
        type(report.get("report_version")) is not int
        or report.get("report_version") != REPORT_VERSION
    ):
        errors.append("report_version is unsupported")
    if mode not in REPORT_MODES:
        errors.append(f"report mode is unsupported: {mode!r}")
    expected_evidence_scope = (
        "deterministic-local-runtime"
        if mode in {"fixture", "stability"}
        else mode
    )
    if report.get("evidence_scope") != expected_evidence_scope:
        errors.append("evidence_scope is inconsistent with report mode")
    if not isinstance(report.get("live_skipped"), bool):
        errors.append("live_skipped is not a boolean")
    matrix = report.get("matrix")
    if not isinstance(matrix, dict):
        errors.append("matrix is not an object")
    else:
        errors.extend(_unexpected_key_errors("matrix", matrix, MATRIX_KEYS))
        if matrix.get("profile") != mode:
            errors.append("matrix profile is inconsistent with report mode")
        for field in ("platform", "python_version", "git_version"):
            value = matrix.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"matrix {field} is not a non-empty string")
        for field in ("codex_available", "container_available", "sqlite_stress"):
            if not isinstance(matrix.get(field), bool):
                errors.append(f"matrix {field} is not a boolean")
        if matrix.get("sqlite_stress") is not (mode == "stability"):
            errors.append("matrix sqlite_stress is inconsistent with report mode")
        skipped_reasons = matrix.get("live_skipped_reasons")
        if not isinstance(skipped_reasons, list):
            errors.append("matrix live_skipped_reasons is not a list")
        elif any(not isinstance(reason, str) or not reason.strip() for reason in skipped_reasons):
            errors.append("matrix live_skipped_reasons contains an invalid reason")
        if require_current_matrix:
            current_matrix = {
                "platform": platform.platform(),
                "python_version": platform.python_version(),
                "git_version": command_version(["git", "--version"]),
                "container_available": shutil.which("docker") is not None
                or shutil.which("podman") is not None,
            }
            for field, expected in current_matrix.items():
                if matrix.get(field) != expected:
                    errors.append(f"matrix {field} does not match current generation environment")

    summary_duration = summary.get("duration_seconds")
    if (
        not isinstance(summary_duration, (int, float))
        or isinstance(summary_duration, bool)
        or not math.isfinite(float(summary_duration))
        or summary_duration < 0
    ):
        errors.append("summary duration_seconds is not non-negative and finite")
    scenario_durations: list[float] = []
    for scenario in scenarios:
        duration = scenario.get("duration_seconds")
        if (
            not isinstance(duration, (int, float))
            or isinstance(duration, bool)
            or not math.isfinite(float(duration))
            or duration < 0
        ):
            errors.append(
                f"scenario {scenario.get('name', '<unknown>')} duration_seconds is not non-negative and finite"
            )
        else:
            scenario_durations.append(float(duration))
    if (
        isinstance(summary_duration, (int, float))
        and not isinstance(summary_duration, bool)
        and math.isfinite(float(summary_duration))
        and scenario_durations
        and float(summary_duration) < max(scenario_durations)
    ):
        errors.append("summary duration_seconds is shorter than a scenario duration")
    skipped = sum(1 for scenario in scenarios if scenario.get("skip_reason"))
    passed = sum(
        1
        for scenario in scenarios
        if scenario.get("pass") is True and not scenario.get("skip_reason")
    )
    failed = sum(
        1
        for scenario in scenarios
        if scenario.get("pass") is not True and not scenario.get("skip_reason")
    )
    detail_counters = {
        "false_pass_count": "false_pass_count",
        "forged_evidence_block_count": "forged_evidence_block_count",
        "expected_human_review_required_count": "expected_human_review_required_count",
        "sqlite_lock_error_count": "sqlite_lock_error_count",
        "human_intervention_count": "human_intervention_count",
    }
    expected_summary: dict[str, int | float] = {
        "scenario_count": len(scenarios),
        "passed_count": passed,
        "failed_count": failed,
        "skipped_count": skipped,
        "scenario_pass_rate": round(passed / max(len(scenarios), 1), 4),
    }
    for summary_field, detail_field in detail_counters.items():
        values: list[int] = []
        for scenario in scenarios:
            details = scenario.get("details")
            value = details.get(detail_field, 0) if isinstance(details, dict) else 0
            if type(value) is not int or value < 0:
                errors.append(
                    f"scenario {scenario.get('name', '<unknown>')} {detail_field} "
                    "is not a non-negative integer"
                )
                value = 0
            values.append(value)
        expected_summary[summary_field] = sum(values)
    for field, expected in expected_summary.items():
        actual = summary.get(field)
        if field in SUMMARY_INTEGER_FIELDS and type(actual) is not int:
            errors.append(f"summary {field} is not an exact integer")
        elif field == "scenario_pass_rate" and (
            not isinstance(actual, (int, float))
            or isinstance(actual, bool)
            or not math.isfinite(float(actual))
        ):
            errors.append("summary scenario_pass_rate is not numeric and finite")
        if actual != expected:
            errors.append(f"summary {field} is inconsistent")

    if mode in {"live-codex", "live-codex-parallel"}:
        if report.get("live_skipped") is True:
            expected_live_status = "not-run"
        elif failed and all(
            isinstance(scenario.get("details"), dict)
            and scenario["details"].get("capability_status") == "blocked"
            for scenario in scenarios
            if not scenario.get("skip_reason")
        ):
            expected_live_status = "blocked"
        elif failed:
            expected_live_status = "failed"
        elif scenarios and passed == len(scenarios):
            expected_live_status = "passed"
        else:
            expected_live_status = "failed"
    else:
        expected_live_status = "not-applicable"
    if report.get("live_status") != expected_live_status:
        errors.append("live_status is inconsistent with scenarios")
    expected_scenario_inventory = {
        "fixture": [
            scenario.__name__.removeprefix("scenario_")
            for scenario in FIXTURE_SCENARIOS
        ],
        "stability": [
            scenario.__name__.removeprefix("scenario_")
            for scenario in (*FIXTURE_SCENARIOS, *STABILITY_SCENARIOS)
        ],
        "live-codex": ["native_codex_edit_and_controller_verify"],
        "live-codex-parallel": ["native_codex_two_producer_integration"],
    }
    if mode in expected_scenario_inventory and [
        scenario.get("name") for scenario in scenarios
    ] != expected_scenario_inventory[mode]:
        errors.append("profile scenario inventory is inconsistent")
    if mode in {"fixture", "stability"}:
        for scenario in scenarios:
            name = str(scenario.get("name", ""))
            expected_local_contract = LOCAL_SCENARIO_CONTRACT.get(name)
            if expected_local_contract is None:
                continue
            expected_category, expected_mode = expected_local_contract
            if scenario.get("category") != expected_category:
                errors.append(
                    f"scenario {name} category is inconsistent with local profile"
                )
            if scenario.get("mode") != expected_mode:
                errors.append(
                    f"scenario {name} mode is inconsistent with local profile"
                )
    if expected_live_status == "passed":
        if not isinstance(summary_duration, (int, float)) or isinstance(
            summary_duration, bool
        ) or not math.isfinite(float(summary_duration)) or summary_duration <= 0:
            errors.append("passing live summary duration_seconds is not positive and finite")
        if isinstance(matrix, dict):
            if matrix.get("codex_available") is not True:
                errors.append("passing live matrix does not report Codex available")
            if matrix.get("live_skipped_reasons") != []:
                errors.append("passing live matrix contains skip reasons")

    identity = report.get("evaluation_source")
    if not isinstance(identity, dict):
        errors.append("evaluation_source is not an object")
    else:
        errors.extend(
            _unexpected_key_errors(
                "evaluation_source",
                identity,
                EVALUATION_SOURCE_KEYS,
            )
        )
        if not _valid_aware_iso8601(identity.get("generated_at")):
            errors.append("evaluation_source generated_at is not timezone-aware ISO-8601")
        for field in ("workspace_sha256", "status_sha256"):
            if not _valid_nonzero_sha256(identity.get(field)):
                errors.append(f"evaluation_source {field} is not a nonzero SHA-256")
        git_head = identity.get("git_head")
        if (
            not isinstance(git_head, str)
            or len(git_head) not in {40, 64}
            or git_head == "0" * len(git_head)
            or any(character not in "0123456789abcdef" for character in git_head)
        ):
            errors.append("evaluation_source git_head is not a nonzero Git object ID")
        if not isinstance(identity.get("git_dirty"), bool):
            errors.append("evaluation_source git_dirty is not a boolean")
        status_entry_count = identity.get("status_entry_count")
        if (
            not isinstance(status_entry_count, int)
            or isinstance(status_entry_count, bool)
            or status_entry_count < 0
        ):
            errors.append("evaluation_source status_entry_count is not non-negative")
        git_dirty = identity.get("git_dirty")
        empty_status_sha256 = hashlib.sha256(b"").hexdigest()
        if isinstance(git_dirty, bool) and isinstance(status_entry_count, int) and not isinstance(
            status_entry_count, bool
        ) and status_entry_count >= 0:
            if git_dirty != (status_entry_count > 0):
                errors.append(
                    "evaluation_source git_dirty is inconsistent with status_entry_count"
                )
            if status_entry_count == 0 and identity.get("status_sha256") != empty_status_sha256:
                errors.append(
                    "evaluation_source clean status_sha256 is inconsistent"
                )
            if status_entry_count > 0 and identity.get("status_sha256") == empty_status_sha256:
                errors.append(
                    "evaluation_source dirty status_sha256 is inconsistent"
                )
        source_scope = identity.get("source_scope")
        if (
            not isinstance(source_scope, list)
            or not source_scope
            or any(not isinstance(entry, str) or not entry for entry in source_scope)
        ):
            errors.append("evaluation_source source_scope is invalid")
        current_identity = evaluation_source_identity()
        for field in ("workspace_sha256", "source_scope"):
            if identity.get(field) != current_identity.get(field):
                errors.append(
                    f"evaluation_source {field} does not match current executable source"
                )
        if require_current_git_state:
            for field in (
                "git_head",
                "git_dirty",
                "status_sha256",
                "status_entry_count",
            ):
                if identity.get(field) != current_identity.get(field):
                    errors.append(
                        f"evaluation_source {field} does not match current checkout"
                    )

    native_host = report.get("native_host")
    if expected_live_status == "passed" and not isinstance(native_host, dict):
        errors.append("passing live report has no Native Host binary metadata")
    if isinstance(native_host, dict):
        errors.extend(
            _unexpected_key_errors("Native Host", native_host, NATIVE_HOST_KEYS)
        )
        resolved_path = native_host.get("resolved_path")
        path_flavor = (
            _absolute_path_flavor(resolved_path)
            if isinstance(resolved_path, str)
            else None
        )
        if path_flavor is None:
            errors.append("Native Host resolved_path is not absolute")
        if not _valid_nonzero_sha256(native_host.get("sha256")):
            errors.append("Native Host sha256 is not a nonzero SHA-256")
        if native_host.get("source") not in {"explicit-test-override", "path-discovery"}:
            errors.append("Native Host source is invalid")
        if native_host.get("trust") != "local-capability-only-not-delivery-provenance":
            errors.append("Native Host trust label is invalid")
        if (
            require_current_binary
            and expected_live_status == "passed"
            and isinstance(resolved_path, str)
            and path_flavor is not None
        ):
            current_flavor = "windows" if os.name == "nt" else "posix"
            if path_flavor != current_flavor:
                errors.append("Native Host resolved_path is for a different operating system")
                binary_path = None
            else:
                binary_path = Path(resolved_path)
            if binary_path is None:
                pass
            elif not binary_path.is_file():
                errors.append("Native Host resolved binary is unavailable")
            elif native_host.get("sha256") != _sha256(binary_path):
                errors.append("Native Host sha256 does not match resolved binary")
            expected_binary = live_codex_binary()
            if not expected_binary:
                errors.append("current Native Codex binary is unavailable")
            elif binary_path is not None and binary_path.is_file():
                expected_path = Path(expected_binary).expanduser().resolve()
                if binary_path.expanduser().resolve() != expected_path:
                    errors.append(
                        "Native Host resolved binary does not match current Native Codex binary"
                    )
                expected_source = (
                    "explicit-test-override"
                    if os.environ.get("HARNESS_E2E_CODEX_BIN", "").strip()
                    else "path-discovery"
                )
                if native_host.get("source") != expected_source:
                    errors.append(
                        "Native Host source does not match current binary discovery"
                    )

    scenario_usages: list[dict[str, Any]] = []
    scenario_token_counts: list[int] = []
    scenario_runtimes: list[float] = []
    for scenario in scenarios:
        name = str(scenario.get("name", "<unknown>"))
        details = scenario.get("details")
        if not isinstance(details, dict):
            continue
        usage = details.get("native_usage")
        token_count = details.get("native_token_count")
        if usage is not None:
            usage_errors = _usage_errors(f"scenario {name}", usage)
            errors.extend(usage_errors)
            if not usage_errors:
                scenario_usages.append(usage)
                if token_count != usage["token_count"]:
                    errors.append(f"scenario {name} native_token_count is inconsistent")
                if details.get("native_token_source") != "codex-json-turn.completed":
                    errors.append(f"scenario {name} native_token_source is invalid")
        if isinstance(token_count, int) and not isinstance(token_count, bool):
            scenario_token_counts.append(token_count)
        runtime = details.get("native_runtime_seconds")
        if isinstance(runtime, (int, float)) and not isinstance(runtime, bool):
            if not math.isfinite(float(runtime)) or runtime < 0:
                errors.append(
                    f"scenario {name} native_runtime_seconds is not non-negative and finite"
                )
            else:
                scenario_runtimes.append(float(runtime))
        elif runtime is not None:
            errors.append(f"scenario {name} native_runtime_seconds is not numeric")

        if mode in {"live-codex", "live-codex-parallel"} and scenario.get("pass") is True:
            if details.get("native_token_scope") != NATIVE_TOKEN_SCOPE:
                errors.append(f"scenario {name} native_token_scope is invalid")
            if details.get("workload_family") != LIVE_WORKLOAD_FAMILY:
                errors.append(f"scenario {name} workload_family is invalid")
            if details.get("workload_unit_sha256") != LIVE_WORKLOAD_UNIT_SHA256:
                errors.append(f"scenario {name} workload_unit_sha256 is invalid")
            errors.extend(_passing_live_scenario_errors(mode, scenario))

        producers = details.get("producers")
        if not isinstance(producers, list):
            if "producer_scope_valid" in details:
                exclusive: list[str] = []
                try:
                    exclusive = sorted(
                        normalize_live_eval_path(path)
                        for path in details.get("exclusive_files", [])
                    )
                    changed = sorted(
                        normalize_live_eval_path(path)
                        for path in details.get("producer_changed_files", [])
                    )
                    expected_scope_valid = changed == exclusive and bool(
                        details.get("test_file_unchanged")
                    )
                except (TypeError, ValueError):
                    expected_scope_valid = False
                if details.get("producer_scope_valid") is not expected_scope_valid:
                    errors.append(f"scenario {name} producer_scope_valid is inconsistent")
                if scenario.get("pass") is True:
                    if not exclusive:
                        errors.append(f"scenario {name} exclusive_files is empty")
                    if type(details.get("native_returncode")) is not int or details.get(
                        "native_returncode"
                    ) != 0:
                        errors.append(f"scenario {name} native_returncode is inconsistent")
                    if details.get("integrated_files") != exclusive:
                        errors.append(f"scenario {name} integrated_files is inconsistent with scope")
            if (
                isinstance(mode, str)
                and mode.startswith("live-codex")
                and scenario.get("pass") is True
                and (
                    type(details.get("workload_units")) is not int
                    or details.get("workload_units") != 1
                )
            ):
                errors.append(f"scenario {name} workload_units is inconsistent")
            continue
        if type(details.get("producer_count")) is not int or details.get(
            "producer_count"
        ) != len(producers):
            errors.append(f"scenario {name} producer_count is inconsistent")
        try:
            expected_conflicts = live_eval_scope_conflicts(producers)
        except (KeyError, TypeError, ValueError):
            expected_conflicts = {"<invalid-producer>": [name]}
        if details.get("scope_conflicts") != expected_conflicts:
            errors.append(f"scenario {name} scope_conflicts is inconsistent")
        expected_attribution = True
        producer_usages: list[dict[str, Any]] = []
        producer_changed_files: set[str] = set()
        for producer in producers:
            if not isinstance(producer, dict):
                expected_attribution = False
                continue
            try:
                exclusive = sorted(
                    normalize_live_eval_path(path)
                    for path in producer.get("exclusive_files", [])
                )
                changed = sorted(
                    normalize_live_eval_path(path)
                    for path in producer.get("changed_files", [])
                )
            except (TypeError, ValueError):
                expected_attribution = False
                continue
            producer_changed_files.update(changed)
            if (
                type(producer.get("returncode")) is not int
                or producer.get("returncode") != 0
                or producer.get("scope_valid") is not True
                or producer.get("test_file_unchanged") is not True
                or changed != exclusive
            ):
                expected_attribution = False
            producer_usage = producer.get("native_usage")
            producer_usage_errors = _usage_errors(
                f"scenario {name} producer {producer.get('task', '<unknown>')}",
                producer_usage,
            )
            errors.extend(producer_usage_errors)
            if not producer_usage_errors:
                producer_usages.append(producer_usage)
                if (
                    type(producer.get("token_count")) is not int
                    or producer.get("token_count") != producer_usage["token_count"]
                ):
                    errors.append(
                        f"scenario {name} producer {producer.get('task', '<unknown>')} token_count is inconsistent"
                    )
                if producer.get("token_source") != "codex-json-turn.completed":
                    errors.append(
                        f"scenario {name} producer {producer.get('task', '<unknown>')} token_source is invalid"
                    )
            producer_start = producer.get("started_offset_seconds")
            producer_finish = producer.get("finished_offset_seconds")
            if (
                isinstance(producer_start, (int, float))
                and not isinstance(producer_start, bool)
                and math.isfinite(float(producer_start))
                and isinstance(producer_finish, (int, float))
                and not isinstance(producer_finish, bool)
                and math.isfinite(float(producer_finish))
                and producer_finish > producer_start
            ):
                expected_producer_runtime = round(
                    float(producer_finish) - float(producer_start),
                    6,
                )
                producer_runtime = producer.get("runtime_seconds")
                if (
                    not isinstance(producer_runtime, (int, float))
                    or isinstance(producer_runtime, bool)
                    or not math.isfinite(float(producer_runtime))
                    or producer_runtime <= 0
                    or producer_runtime != expected_producer_runtime
                ):
                    errors.append(
                        f"scenario {name} producer {producer.get('task', '<unknown>')} runtime_seconds is inconsistent"
                    )
            else:
                errors.append(
                    f"scenario {name} producer {producer.get('task', '<unknown>')} timing is not positive and finite"
                )
        if details.get("producer_attribution_valid") is not expected_attribution:
            errors.append(f"scenario {name} producer_attribution_valid is inconsistent")
        if producer_usages and details.get("native_usage") != _aggregate_usages(producer_usages):
            errors.append(f"scenario {name} producer usage aggregate is inconsistent")
        if scenario.get("pass") is True:
            if type(details.get("workload_units")) is not int or details.get(
                "workload_units"
            ) != len(producers):
                errors.append(f"scenario {name} workload_units is inconsistent")
            expected_files = sorted(producer_changed_files)
            if details.get("changed_files") != expected_files:
                errors.append(f"scenario {name} changed_files is inconsistent with producers")
            if details.get("integrated_files") != expected_files:
                errors.append(f"scenario {name} integrated_files is inconsistent with producers")
        starts = [producer.get("started_offset_seconds") for producer in producers if isinstance(producer, dict)]
        finishes = [producer.get("finished_offset_seconds") for producer in producers if isinstance(producer, dict)]
        if (
            len(starts) == len(producers)
            and len(finishes) == len(producers)
            and all(
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
                for value in starts + finishes
            )
        ):
            expected_overlap = round(
                max(0.0, min(float(value) for value in finishes) - max(float(value) for value in starts)),
                6,
            )
            expected_runtime = round(
                max(float(value) for value in finishes) - min(float(value) for value in starts),
                6,
            )
            if details.get("producer_overlap_seconds") != expected_overlap:
                errors.append(f"scenario {name} producer_overlap_seconds is inconsistent")
            if details.get("native_runtime_seconds") != expected_runtime:
                errors.append(f"scenario {name} native_runtime_seconds is inconsistent with producers")
        else:
            errors.append(f"scenario {name} producer timing window is incomplete")

    expected_usage = _aggregate_usages(scenario_usages)
    if expected_usage is not None:
        errors.extend(_usage_errors("top-level", report.get("token_usage")))
    if not _matches_exact_json_contract(report.get("token_usage"), expected_usage):
        errors.append("top-level token_usage is inconsistent")
    expected_token_count = sum(scenario_token_counts) if scenario_token_counts else None
    if not _matches_exact_json_contract(report.get("token_count"), expected_token_count):
        errors.append("top-level token_count is inconsistent")
    if expected_usage is not None and expected_token_count != expected_usage["token_count"]:
        errors.append("scenario token totals disagree with structured usage")
    expected_runtime = round(sum(scenario_runtimes), 6) if scenario_runtimes else None
    actual_runtime = report.get("agent_runtime_seconds")
    if expected_runtime is None:
        runtime_matches = actual_runtime is None
    else:
        runtime_matches = (
            isinstance(actual_runtime, (int, float))
            and not isinstance(actual_runtime, bool)
            and math.isfinite(float(actual_runtime))
            and actual_runtime == expected_runtime
        )
    if not runtime_matches:
        errors.append("top-level agent_runtime_seconds is inconsistent")
    if expected_live_status == "passed" and (
        expected_runtime is None or not math.isfinite(expected_runtime) or expected_runtime <= 0
    ):
        errors.append("passing live agent_runtime_seconds is not positive and finite")
    if report.get("estimated_cost") is not None:
        errors.append("estimated_cost must remain null because cost is not exposed")
    return errors


def persistent_report_consistency_errors(
    report: dict[str, Any],
    *,
    require_current_binary: bool = False,
    require_current_git_state: bool = False,
    require_current_matrix: bool = False,
) -> list[str]:
    """Validate persisted evidence while rejecting explicit test binaries."""

    errors = report_consistency_errors(
        report,
        require_current_binary=require_current_binary,
        require_current_git_state=require_current_git_state,
        require_current_matrix=require_current_matrix,
    )
    native_host = report.get("native_host")
    if (
        report.get("mode") in {"live-codex", "live-codex-parallel"}
        and report.get("live_status") == "passed"
        and (
            not isinstance(native_host, dict)
            or native_host.get("source") != "path-discovery"
        )
    ):
        errors.append(
            "persisted passing Native evidence requires a path-discovered binary"
        )
    return errors


def should_fail(report: dict[str, Any]) -> bool:
    if report_consistency_errors(report):
        return True
    summary = report["summary"]
    if report["mode"].startswith("live-codex"):
        if report["live_skipped"] or report.get("live_status") != "passed":
            return True
        if (
            summary.get("scenario_count") != 1
            or summary.get("passed_count") != 1
            or summary.get("failed_count") != 0
            or summary.get("skipped_count") != 0
            or summary.get("false_pass_count") != 0
            or summary.get("human_intervention_count") != 0
        ):
            return True
    if summary["failed_count"] != 0:
        return True
    if report["mode"] == "fixture" and summary["scenario_count"] != len(FIXTURE_SCENARIOS):
        return True
    if report["mode"] == "stability" and summary["scenario_count"] != len(FIXTURE_SCENARIOS) + len(STABILITY_SCENARIOS):
        return True
    if report["mode"] in {"fixture", "stability"}:
        if summary["false_pass_count"] != 0:
            return True
        if summary["human_intervention_count"] != 0:
            return True
        if summary.get("sqlite_lock_error_count", 0) != 0:
            return True
        if summary.get("skipped_count", 0) != 0:
            return True
        if summary.get("forged_evidence_block_count", 0) != 1:
            return True
        if summary.get("expected_human_review_required_count", 0) != 1:
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Run agent E2E evaluation scenarios")
    parser.add_argument(
        "--mode",
        choices=["fixture", "stability", "live-codex", "live-codex-parallel"],
        default="fixture",
    )
    parser.add_argument("--out", default="", help="Write the compact evidence report")
    parser.add_argument(
        "--evidence-out",
        default="",
        help="Write a compact report with verbose Native Host output removed",
    )
    parser.add_argument(
        "--debug-out",
        default="",
        help="Explicitly write the full local debug report including Native output tails",
    )
    args = parser.parse_args()

    runners = {
        "fixture": run_fixture,
        "stability": run_stability,
        "live-codex": run_live_codex,
        "live-codex-parallel": run_live_codex_parallel,
    }
    report = runners[args.mode]()
    evidence_report = compact_evidence_report(report)
    persistent_errors = (
        persistent_report_consistency_errors(
            evidence_report,
            require_current_binary=True,
            require_current_git_state=True,
            require_current_matrix=True,
        )
        if args.evidence_out
        else []
    )
    text = json.dumps(evidence_report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    if args.evidence_out:
        if persistent_errors:
            print(
                "ERROR: refusing persistent evidence: " + "; ".join(persistent_errors),
                file=sys.stderr,
            )
        else:
            evidence_out = Path(args.evidence_out)
            evidence_out.parent.mkdir(parents=True, exist_ok=True)
            evidence_out.write_text(text, encoding="utf-8")
    if args.debug_out:
        debug_out = Path(args.debug_out)
        debug_out.parent.mkdir(parents=True, exist_ok=True)
        debug_text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        debug_out.write_text(debug_text, encoding="utf-8")
    print(text, end="")
    return 1 if should_fail(report) or persistent_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

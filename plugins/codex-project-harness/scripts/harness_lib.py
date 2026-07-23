#!/usr/bin/env python3
"""Shared helpers for Codex Project Harness runtime scripts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


STATE_PATH = Path(".ai-team/control/project-state.yaml")
EVENT_PATH = Path(".ai-team/runtime/events.jsonl")
HARNESS_GIT_PREFIXES = (
    ".ai-team/",
)
HARNESS_PROJECTION_SOURCE_PATHS = frozenset(
    {
        "docs/harness/validation.md",
        "docs/harness/executions.md",
        "docs/harness/findings.md",
        "docs/harness/quality-gates.md",
        "docs/harness/delivery.md",
        "docs/harness/evidence.md",
    }
)
CONTENT_HASH_EXCLUDE_PREFIXES = HARNESS_GIT_PREFIXES + (
    ".git/",
    "__pycache__/",
    ".pytest_cache/",
)
SOURCE_CACHE_PARTS = frozenset(
    {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
)
SOURCE_ENVIRONMENT_ROOTS = frozenset(
    {".venv", "venv", ".tox", ".nox", "node_modules"}
)
DISTRIBUTION_MANIFEST_RELATIVE = Path("references/distribution-manifest.json")
_EXACT_SOURCE_PATH_CACHE: tuple[tuple[int, int], frozenset[str]] | None = None
_DISTRIBUTION_MANIFEST_KEYS = {
    "manifest_version",
    "plugin_name",
    "skills",
    "hooks",
    "templates",
    "schemas",
    "core",
    "scripts",
    "references",
    "additional_files",
    "public_runtime_domains",
}


class DistributionManifestError(ValueError):
    """The inspected plugin distribution manifest is missing or malformed."""


def _distribution_object(
    value: object,
    expected: set[str],
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) for key in value
    ):
        raise DistributionManifestError(f"{label} must be an object")
    actual = set(value)
    if actual != expected:
        raise DistributionManifestError(
            f"{label} keys mismatch: missing={sorted(expected - actual)} "
            f"extra={sorted(actual - expected)}"
        )
    return value


def _distribution_names(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise DistributionManifestError(f"{label} must be a non-empty list")
    names: list[str] = []
    for item in value:
        if (
            not isinstance(item, str)
            or not item
            or item != item.strip()
            or item in {".", ".."}
            or any(character in item for character in ("/", "\\", "\x00", "\r", "\n"))
        ):
            raise DistributionManifestError(
                f"{label} contains unsafe basename: {item!r}"
            )
        names.append(item)
    if len(names) != len(set(names)):
        raise DistributionManifestError(f"{label} contains duplicate entries")
    return tuple(names)


def _distribution_paths(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise DistributionManifestError(f"{label} must be a non-empty list")
    paths: list[str] = []
    for item in value:
        path = PurePosixPath(item) if isinstance(item, str) else PurePosixPath(".")
        if (
            not isinstance(item, str)
            or not item
            or item != item.strip()
            or "\\" in item
            or any(character in item for character in ("\x00", "\r", "\n", ":"))
            or path.is_absolute()
            or path.as_posix() != item
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise DistributionManifestError(
                f"{label} contains unsafe relative path: {item!r}"
            )
        paths.append(item)
    if len(paths) != len(set(paths)):
        raise DistributionManifestError(f"{label} contains duplicate entries")
    return tuple(paths)


def load_distribution_manifest(plugin_root: Path) -> dict[str, Any]:
    """Load the closed inventory contract from exactly ``plugin_root``."""

    path = Path(plugin_root) / DISTRIBUTION_MANIFEST_RELATIVE

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise DistributionManifestError(
                    f"duplicate distribution manifest key: {key}"
                )
            result[key] = item
        return result

    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicates,
        )
    except DistributionManifestError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DistributionManifestError(
            f"invalid distribution manifest {path}: {exc}"
        ) from exc
    manifest = _distribution_object(
        value,
        _DISTRIBUTION_MANIFEST_KEYS,
        "distribution manifest",
    )
    version = manifest["manifest_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise DistributionManifestError(
            "distribution manifest_version must be integer 1"
        )
    if manifest["plugin_name"] != "codex-project-harness":
        raise DistributionManifestError(
            "distribution manifest plugin_name must be codex-project-harness"
        )
    hooks = _distribution_object(
        manifest["hooks"], {"files", "events"}, "distribution hooks"
    )
    templates = _distribution_object(
        manifest["templates"],
        {"native_agents", "project_support"},
        "distribution templates",
    )
    normalized: dict[str, Any] = {
        "manifest_version": 1,
        "plugin_name": "codex-project-harness",
        "skills": _distribution_names(manifest["skills"], "distribution skills"),
        "hooks": {
            "files": _distribution_names(hooks["files"], "distribution hook files"),
            "events": _distribution_names(hooks["events"], "distribution hook events"),
        },
        "templates": {
            "native_agents": _distribution_names(
                templates["native_agents"], "distribution native templates"
            ),
            "project_support": _distribution_names(
                templates["project_support"], "distribution project templates"
            ),
        },
        "schemas": _distribution_names(manifest["schemas"], "distribution schemas"),
        "core": _distribution_names(manifest["core"], "distribution core"),
        "scripts": _distribution_names(manifest["scripts"], "distribution scripts"),
        "references": _distribution_names(
            manifest["references"], "distribution references"
        ),
        "additional_files": _distribution_paths(
            manifest["additional_files"], "distribution additional files"
        ),
        "public_runtime_domains": _distribution_names(
            manifest["public_runtime_domains"], "distribution runtime domains"
        ),
    }
    if DISTRIBUTION_MANIFEST_RELATIVE.name not in normalized["references"]:
        raise DistributionManifestError(
            "distribution references must include distribution-manifest.json"
        )
    if "doctor" not in normalized["public_runtime_domains"]:
        raise DistributionManifestError(
            "distribution runtime domains must include doctor"
        )
    invalid_domains = [
        name
        for name in normalized["public_runtime_domains"]
        if re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", name) is None
    ]
    if invalid_domains:
        raise DistributionManifestError(
            "distribution runtime domains contain invalid command names: "
            f"{invalid_domains}"
        )
    derived = set(distribution_file_inventory(normalized, include_additional=False))
    overlap = derived & set(normalized["additional_files"])
    if overlap:
        raise DistributionManifestError(
            f"distribution additional files overlap derived inventory: {sorted(overlap)}"
        )
    return normalized


def distribution_file_inventory(
    distribution: dict[str, Any],
    *,
    include_additional: bool = True,
) -> tuple[str, ...]:
    paths = {
        *(f"core/{name}" for name in distribution["core"]),
        *(f"scripts/{name}" for name in distribution["scripts"]),
        *(f"hooks/{name}" for name in distribution["hooks"]["files"]),
        *(f"schemas/{name}" for name in distribution["schemas"]),
        *(f"references/{name}" for name in distribution["references"]),
        *(
            f"templates/agents/{name}"
            for name in distribution["templates"]["native_agents"]
        ),
        *(
            f"templates/project/{name}"
            for name in distribution["templates"]["project_support"]
        ),
        *(
            path
            for skill in distribution["skills"]
            for path in (
                f"skills/{skill}/SKILL.md",
                f"skills/{skill}/agents/openai.yaml",
            )
        ),
    }
    if include_additional:
        paths.update(distribution["additional_files"])
    return tuple(sorted(paths))


def plugin_file_inventory(root: Path) -> tuple[str, ...]:
    files: list[str] = []
    try:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(root)
            if (
                path.parent.name == "__pycache__"
                and path.suffix in {".pyc", ".pyo"}
            ):
                continue
            files.append(relative.as_posix())
    except OSError:
        return ()
    return tuple(sorted(files))


def _distribution_path_is_link(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        if attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction and is_junction())
    except OSError:
        return True


def distribution_inventory_issues(
    root: Path,
    distribution: dict[str, Any],
) -> list[str]:
    expected = set(distribution_file_inventory(distribution))
    actual = set(plugin_file_inventory(root))
    issues: list[str] = []
    if not root.is_dir() or _distribution_path_is_link(root):
        issues.append(f"distribution root is missing or linked: {root}")
        return issues
    try:
        linked = sorted(
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if _distribution_path_is_link(path)
        )
    except OSError as exc:
        issues.append(f"distribution inventory unreadable: {exc}")
        return issues
    if linked:
        issues.append(f"distribution file inventory linked paths: {linked}")
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing:
        issues.append(f"distribution file inventory missing: {missing}")
    if extra:
        issues.append(f"distribution file inventory extra: {extra}")
    return issues


def hook_definition_issues(
    plugin_root: Path,
    distribution: dict[str, Any],
) -> list[str]:
    """Validate one manifest-selected Hook definition and exact commands."""

    hook_files = tuple(distribution["hooks"]["files"])
    definitions = [name for name in hook_files if Path(name).suffix == ".json"]
    runners = [name for name in hook_files if Path(name).suffix == ".py"]
    if len(definitions) != 1 or len(runners) != 1:
        return [
            "distribution hooks require one JSON definition and one Python runner"
        ]
    definition_path = plugin_root / "hooks" / definitions[0]

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate Hook definition key: {key}")
            value[key] = item
        return value

    try:
        payload = json.loads(
            definition_path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicates,
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        return [f"invalid Hook definition {definition_path}: {exc}"]
    if not isinstance(payload, dict) or set(payload) != {"hooks"}:
        return ["Hook definition must contain only the hooks object"]
    hooks = payload["hooks"]
    expected_events = set(distribution["hooks"]["events"])
    if not isinstance(hooks, dict) or set(hooks) != expected_events:
        actual = set(hooks) if isinstance(hooks, dict) else set()
        return [
            f"Hook event inventory mismatch: actual={sorted(actual)} "
            f"expected={sorted(expected_events)}"
        ]

    issues: list[str] = []
    runner_relative = f"hooks/{runners[0]}"
    for event, groups in hooks.items():
        if not isinstance(groups, list) or not groups:
            issues.append(f"{event}: no Hook groups")
            continue
        for group in groups:
            entries = group.get("hooks", []) if isinstance(group, dict) else []
            if not isinstance(entries, list) or not entries:
                issues.append(f"{event}: no command Hooks")
                continue
            for hook in entries:
                if not isinstance(hook, dict) or hook.get("type") != "command":
                    issues.append(f"{event}: Hook entry must be a command")
                    continue
                for field, root_token, windows in (
                    ("command", "${PLUGIN_ROOT}", False),
                    ("commandWindows", "%PLUGIN_ROOT%", True),
                ):
                    command = hook.get(field)
                    if not isinstance(command, str) or not command.strip():
                        issues.append(f"{event}: {field} must be a command string")
                        continue
                    try:
                        tokens = shlex.split(command, posix=not windows)
                    except ValueError as exc:
                        issues.append(f"{event}: invalid {field}: {exc}")
                        continue
                    normalized = [
                        token.strip('"').replace("\\", "/") for token in tokens
                    ]
                    interpreter = (
                        normalized[0].rsplit("/", 1)[-1].lower()
                        if normalized
                        else ""
                    )
                    if (
                        len(normalized) != 3
                        or re.fullmatch(
                            r"python(?:3(?:\.\d+)*)?(?:\.exe)?",
                            interpreter,
                        )
                        is None
                        or normalized[1] != f"{root_token}/{runner_relative}"
                        or normalized[2] != event
                    ):
                        issues.append(
                            f"{event}: {field} must contain exactly Python, "
                            "the manifest Hook runner, and the matching event"
                        )
    return issues


def harness_exact_source_paths() -> frozenset[str]:
    """Derive generated source exclusions from this exact plugin manifest."""

    global _EXACT_SOURCE_PATH_CACHE
    manifest_path = Path(__file__).resolve().parents[1] / DISTRIBUTION_MANIFEST_RELATIVE
    try:
        stat_result = manifest_path.stat()
    except OSError as exc:
        raise DistributionManifestError(
            f"invalid distribution manifest {manifest_path}: {exc}"
        ) from exc
    key = (stat_result.st_mtime_ns, stat_result.st_size)
    if _EXACT_SOURCE_PATH_CACHE is not None and _EXACT_SOURCE_PATH_CACHE[0] == key:
        return _EXACT_SOURCE_PATH_CACHE[1]
    distribution = load_distribution_manifest(Path(__file__).resolve().parents[1])
    paths = frozenset(
        {
            *HARNESS_PROJECTION_SOURCE_PATHS,
            *(
                f".codex/agents/{name}"
                for name in distribution["templates"]["native_agents"]
            ),
        }
    )
    _EXACT_SOURCE_PATH_CACHE = (key, paths)
    return paths


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def read_state(root: Path) -> dict[str, str]:
    state: dict[str, str] = {}
    from core.project_fs import ProjectFS

    with ProjectFS.open(root) as project_fs:
        snapshot = project_fs._snapshot(STATE_PATH, allow_missing=True)
        if not snapshot.exists:
            return state
        content = project_fs.read_bytes(STATE_PATH).decode(
            "utf-8",
            errors="strict",
        )

    for raw_line in content.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if ":" not in raw_line:
            continue
        key, value = raw_line.split(":", 1)
        state[key.strip()] = value.strip() or ""
    return state


def write_state(
    root: Path,
    updates: dict[str, object],
    *,
    merge_existing: bool = True,
    include_blocked_reason: bool = True,
) -> dict[str, str]:
    normalized_updates = {key: str(value) for key, value in updates.items()}
    state = read_state(root) if merge_existing else {}
    state.update(normalized_updates)
    state.setdefault("status", "draft")
    state.setdefault("phase", "intake")
    state.setdefault("scope_status", "unconfirmed")
    state.setdefault("current_owner", "project-manager")
    if include_blocked_reason:
        state.setdefault("blocked_reason", "null")
    if "updated_at" not in normalized_updates:
        state["updated_at"] = now_iso()

    ordered_keys = [
        "status",
        "phase",
        "scope_status",
        "current_owner",
        "blocked_reason",
        "updated_at",
    ]
    lines = [f"{key}: {state[key]}" for key in ordered_keys if key in state]
    for key in sorted(k for k in state if k not in ordered_keys):
        lines.append(f"{key}: {state[key]}")
    from core.project_fs import ProjectFS

    with ProjectFS.open(root) as project_fs:
        project_fs.atomic_write(
            STATE_PATH,
            ("\n".join(lines) + "\n").encode("utf-8"),
            mode=0o644,
        )
    return state


def markdown_row(values: list[str]) -> str:
    safe_values = [str(value).replace("\n", " ").replace("|", "\\|") for value in values]
    return "| " + " | ".join(safe_values) + " |"


def isolated_git_environment(*, work_tree: Path | None = None) -> dict[str, str]:
    """Remove ambient overrides, replacements, and network-capable lazy reads."""

    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    environment.update(
        {
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    if work_tree is not None:
        environment["GIT_WORK_TREE"] = os.fspath(Path(work_tree).resolve())
    return environment


def framed_source_digest(entries: Iterable[tuple[bytes, bytes, str]]) -> str:
    """Hash unambiguous path/mode/fixed-content records in deterministic order."""

    digest = hashlib.sha256()
    for raw_relative, mode, content_sha256 in sorted(entries, key=lambda item: item[0]):
        digest.update(raw_relative)
        digest.update(b"\0")
        digest.update(mode)
        digest.update(b"\0")
        digest.update(content_sha256.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def git_blob_objects_available(
    root: Path,
    object_ids: Iterable[str],
    *,
    environment: dict[str, str] | None = None,
) -> bool:
    """Verify every required blob is local without permitting a promisor fetch."""

    expected = sorted(set(object_ids))
    if not expected:
        return True
    command_environment = environment or isolated_git_environment(work_tree=root)
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "cat-file",
                "--batch-check=%(objectname) %(objecttype)",
            ],
            cwd=root,
            env=command_environment,
            input="".join(f"{object_id}\n" for object_id in expected),
            text=True,
            capture_output=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    lines = result.stdout.splitlines()
    if len(lines) != len(expected):
        return False
    return all(
        line.split() == [object_id, "blob"]
        for object_id, line in zip(expected, lines, strict=True)
    )


def _source_path_excluded(
    relative: str,
    *,
    versioned_environment_roots: frozenset[str] = frozenset(),
) -> bool:
    parts = Path(relative).parts
    if any(part in SOURCE_CACHE_PARTS for part in parts):
        return True
    if (
        parts
        and parts[0] in SOURCE_ENVIRONMENT_ROOTS
        and parts[0] not in versioned_environment_roots
    ):
        return True
    if relative in harness_exact_source_paths():
        return True
    return any(
        relative == prefix.rstrip("/") or relative.startswith(prefix)
        for prefix in CONTENT_HASH_EXCLUDE_PREFIXES
    )


def source_path_excluded(
    relative: str,
    *,
    versioned_environment_roots: frozenset[str] = frozenset(),
) -> bool:
    """Return whether a path is outside canonical candidate-source identity."""

    return _source_path_excluded(
        relative,
        versioned_environment_roots=versioned_environment_roots,
    )


def _git_blob_oid(content: bytes, object_format: str) -> str:
    digest = hashlib.new(object_format)
    digest.update(f"blob {len(content)}\0".encode("ascii"))
    digest.update(content)
    return digest.hexdigest()


def _invalid_git_identity(reason: str) -> RuntimeError:
    return RuntimeError(f"candidate Git source identity is invalid: {reason}")


def _parse_stage_entries(
    entries: list[bytes],
) -> tuple[dict[bytes, tuple[bytes, str]], set[bytes]]:
    tracked: dict[bytes, tuple[bytes, str]] = {}
    unmerged: set[bytes] = set()
    for raw_entry in (entry for entry in entries if entry):
        try:
            metadata, raw_relative = raw_entry.split(b"\t", 1)
            mode, object_id, stage = metadata.split(b" ", 2)
            decoded_object_id = object_id.decode("ascii")
        except (UnicodeDecodeError, ValueError) as exc:
            raise _invalid_git_identity("malformed index entry") from exc
        if stage == b"0":
            tracked[raw_relative] = (mode, decoded_object_id)
        else:
            unmerged.add(raw_relative)
    return tracked, unmerged


def _parse_tree_entries(entries: list[bytes]) -> dict[bytes, tuple[bytes, str]]:
    tree: dict[bytes, tuple[bytes, str]] = {}
    for raw_entry in (entry for entry in entries if entry):
        try:
            metadata, raw_relative = raw_entry.split(b"\t", 1)
            mode, object_type, object_id = metadata.split(b" ", 2)
            if object_type not in {b"blob", b"commit"}:
                raise ValueError("unexpected object type")
            tree[raw_relative] = (mode, object_id.decode("ascii"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise _invalid_git_identity("malformed HEAD tree entry") from exc
    return tree


def _git_source_snapshot(root: Path) -> tuple[str, bool, bool] | None:
    root = root.resolve()
    environment = isolated_git_environment(work_tree=root)
    try:
        repository = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "rev-parse", "--is-inside-work-tree"],
            cwd=root,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if repository.returncode != 0 or repository.stdout.strip() != "true":
        return None
    try:
        head = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "rev-parse", "--verify", "HEAD"],
            cwd=root,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
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
            env=environment,
            capture_output=True,
            check=True,
            timeout=10,
        ).stdout.split(b"\0")
        visible_untracked = set(
            subprocess.run(
                [
                    "git",
                    "-c",
                    "core.fsmonitor=false",
                    "ls-files",
                    "--others",
                    "--exclude-standard",
                    "-z",
                ],
                cwd=root,
                env=environment,
                capture_output=True,
                check=True,
                timeout=10,
            ).stdout.split(b"\0")
        )
        staged_raw = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "ls-files", "--stage", "-z"],
            cwd=root,
            env=environment,
            capture_output=True,
            check=True,
            timeout=10,
        ).stdout.split(b"\0")
        object_format = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "rev-parse", "--show-object-format"],
            cwd=root,
            env=environment,
            text=True,
            capture_output=True,
            check=True,
            timeout=10,
        ).stdout.strip()
        head_raw = (
            subprocess.run(
                ["git", "-c", "core.fsmonitor=false", "ls-tree", "-r", "-z", "HEAD"],
                cwd=root,
                env=environment,
                capture_output=True,
                check=True,
                timeout=10,
            ).stdout.split(b"\0")
            if head
            else []
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise _invalid_git_identity("controlled Git inventory failed") from exc
    if object_format not in {"sha1", "sha256"}:
        raise _invalid_git_identity(f"unsupported object format {object_format!r}")

    tracked, unmerged = _parse_stage_entries(staged_raw)
    head_entries = _parse_tree_entries(head_raw)
    versioned_environment_roots = frozenset(
        parts[0]
        for raw_relative in set(tracked) | set(head_entries) | unmerged
        if (
            (parts := Path(
                raw_relative.decode("utf-8", errors="surrogateescape")
            ).parts)
            and parts[0] in SOURCE_ENVIRONMENT_ROOTS
        )
    )
    scoped_unmerged = {
        relative
        for relative in unmerged
        if not _source_path_excluded(
            relative.decode("utf-8", errors="surrogateescape"),
            versioned_environment_roots=versioned_environment_roots,
        )
    }
    if scoped_unmerged:
        raise _invalid_git_identity("unmerged source path")

    required_objects: set[str] = set()
    for raw_relative, (mode, object_id) in tracked.items():
        relative = raw_relative.decode("utf-8", errors="surrogateescape")
        if _source_path_excluded(
            relative,
            versioned_environment_roots=versioned_environment_roots,
        ):
            continue
        if mode not in {b"100644", b"100755"}:
            raise _invalid_git_identity(f"non-regular tracked source path: {relative}")
        required_objects.add(object_id)
    for raw_relative, (mode, object_id) in head_entries.items():
        relative = raw_relative.decode("utf-8", errors="surrogateescape")
        if _source_path_excluded(
            relative,
            versioned_environment_roots=versioned_environment_roots,
        ):
            continue
        if mode not in {b"100644", b"100755"}:
            raise _invalid_git_identity(f"non-regular HEAD source path: {relative}")
        required_objects.add(object_id)
    if not git_blob_objects_available(root, required_objects, environment=environment):
        raise _invalid_git_identity("required local Git object is unavailable")

    records: list[tuple[bytes, bytes, str]] = []
    actual_entries: dict[bytes, tuple[bytes, str]] = {}
    for raw_relative in sorted(relative for relative in listed if relative):
        relative = raw_relative.decode("utf-8", errors="surrogateescape")
        if _source_path_excluded(
            relative,
            versioned_environment_roots=versioned_environment_roots,
        ):
            continue
        path = root / relative
        tracked_entry = tracked.get(raw_relative)
        if path.is_symlink():
            raise _invalid_git_identity(f"symlink source path: {relative}")
        if path.exists() and not path.is_file():
            raise _invalid_git_identity(f"non-regular source path: {relative}")
        if not path.is_file():
            continue
        try:
            content = path.read_bytes()
            actual_mode = b"100755" if path.stat().st_mode & 0o111 else b"100644"
        except OSError as exc:
            raise _invalid_git_identity(f"unreadable source path: {relative}") from exc
        workspace_mode = (
            tracked_entry[0]
            if os.name == "nt" and tracked_entry is not None
            else actual_mode
        )
        content_sha256 = hashlib.sha256(content).hexdigest()
        records.append((raw_relative, workspace_mode, content_sha256))
        actual_entries[raw_relative] = (
            actual_mode,
            _git_blob_oid(content, object_format),
        )

    head_comparable = all(
        raw_relative in head_entries for raw_relative in actual_entries
    )
    all_paths = set(head_entries) | set(tracked) | set(actual_entries) | scoped_unmerged
    dirty = False
    for raw_relative in all_paths:
        relative = raw_relative.decode("utf-8", errors="surrogateescape")
        if _source_path_excluded(
            relative,
            versioned_environment_roots=versioned_environment_roots,
        ):
            continue
        head_entry = head_entries.get(raw_relative)
        index_entry = tracked.get(raw_relative)
        actual_entry = actual_entries.get(raw_relative)
        if head_entry != index_entry:
            dirty = True
            break
        if index_entry is None:
            if actual_entry is not None and raw_relative in visible_untracked:
                dirty = True
                break
            continue
        if actual_entry is None:
            dirty = True
            break
        mode_changed = (
            os.name != "nt"
            and index_entry[0] in {b"100644", b"100755"}
            and actual_entry[0] != index_entry[0]
        )
        if actual_entry[1] != index_entry[1] or mode_changed:
            dirty = True
            break
    return framed_source_digest(records), dirty, head_comparable


def git_head_sha(root: Path) -> str | None:
    environment = isolated_git_environment(work_tree=root)
    try:
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "rev-parse", "--verify", "HEAD"],
            cwd=root,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None


def git_dirty(root: Path) -> bool | None:
    snapshot = _git_source_snapshot(root)
    return snapshot[1] if snapshot is not None else None


def git_source_snapshot(root: Path) -> tuple[str, bool, bool] | None:
    """Return candidate, dirty, and exact-HEAD-comparability in one observation."""

    return _git_source_snapshot(root)


def git_source_tree_hash(root: Path) -> str | None:
    snapshot = _git_source_snapshot(root)
    return snapshot[0] if snapshot is not None else None


def content_source_tree_hash(root: Path) -> str:
    records: list[tuple[bytes, bytes, str]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if _source_path_excluded(relative):
            continue
        if path.is_symlink():
            raise RuntimeError(
                f"candidate content source identity is invalid: symlink source path: {relative}"
            )
        if path.is_dir():
            continue
        if not path.is_file():
            raise RuntimeError(
                "candidate content source identity is invalid: "
                f"non-regular source path: {relative}"
            )
        content = path.read_bytes()
        mode = (
            b"100755"
            if os.name != "nt" and path.stat().st_mode & 0o111
            else b"100644"
        )
        records.append(
            (
                relative.encode("utf-8", errors="surrogateescape"),
                mode,
                hashlib.sha256(content).hexdigest(),
            )
        )
    return f"content:{framed_source_digest(records)}"

"""Command line installer for Codex Project Harness."""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from . import __version__

PLUGIN_NAME = "codex-project-harness"
DEFAULT_MARKETPLACE_NAME = "kafa-local"
DISPLAY_NAME = "Kafa Local Plugins"
PLUGIN_CATEGORY = "Developer Tools"
REPO_VERSION_FILE = Path(__file__).resolve().parents[1] / "VERSION"

RETIRED_CORE_FILES = ("agent_provider.py", "agent_runner.py", "connector_trust.py")
FORBIDDEN_RUNTIME_LITERALS = (
    "gh api",
    "api.github.com",
    "api.linear.app",
    "api.notion.com",
    "api.figma.com",
    "slack.com/api",
    "github_token",
    "gh_token",
    "linear_api_key",
    "notion_token",
    "figma_token",
    "slack_bot_token",
    "harness_connector_key",
)
FORBIDDEN_PROVIDER_IMPORTS = {"github", "linear", "notion_client", "figma", "slack_sdk", "openai_codex"}

DISTRIBUTION_MANIFEST_RELATIVE = Path("references/distribution-manifest.json")
DISTRIBUTION_MANIFEST_KEYS = {
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


class KafaError(RuntimeError):
    """User-facing CLI error."""


@dataclass(frozen=True)
class ProjectRuntimeAuthority:
    """One validated installed runtime selected independently of a business repo."""

    root: Path
    distribution: dict[str, Any]
    version: str
    digest: str


def _distribution_object(
    value: object,
    expected: set[str],
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) for key in value
    ):
        raise KafaError(f"{label} must be an object")
    actual = set(value)
    if actual != expected:
        raise KafaError(
            f"{label} keys mismatch: missing={sorted(expected - actual)} "
            f"extra={sorted(actual - expected)}"
        )
    return value


def _distribution_names(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise KafaError(f"{label} must be a non-empty list")
    names: list[str] = []
    for item in value:
        if (
            not isinstance(item, str)
            or not item
            or item != item.strip()
            or item in {".", ".."}
            or any(character in item for character in ("/", "\\", "\x00", "\r", "\n"))
        ):
            raise KafaError(f"{label} contains unsafe basename: {item!r}")
        names.append(item)
    if len(names) != len(set(names)):
        raise KafaError(f"{label} contains duplicate entries")
    return tuple(names)


def _distribution_paths(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise KafaError(f"{label} must be a non-empty list")
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
            raise KafaError(f"{label} contains unsafe relative path: {item!r}")
        paths.append(item)
    if len(paths) != len(set(paths)):
        raise KafaError(f"{label} contains duplicate entries")
    return tuple(paths)


def load_distribution_manifest(plugin_root: Path) -> dict[str, Any]:
    """Load the closed inventory contract from the plugin being inspected."""

    path = plugin_root / DISTRIBUTION_MANIFEST_RELATIVE

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise KafaError(f"duplicate distribution manifest key: {key}")
            result[key] = item
        return result

    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicates,
        )
    except KafaError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise KafaError(f"invalid distribution manifest {path}: {exc}") from exc
    manifest = _distribution_object(
        value,
        DISTRIBUTION_MANIFEST_KEYS,
        "distribution manifest",
    )
    version = manifest["manifest_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise KafaError("distribution manifest_version must be integer 1")
    if manifest["plugin_name"] != PLUGIN_NAME:
        raise KafaError(
            f"distribution manifest plugin_name must be {PLUGIN_NAME}"
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
        "plugin_name": PLUGIN_NAME,
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
        raise KafaError(
            "distribution references must include distribution-manifest.json"
        )
    if "doctor" not in normalized["public_runtime_domains"]:
        raise KafaError("distribution runtime domains must include doctor")
    invalid_domains = [
        name
        for name in normalized["public_runtime_domains"]
        if re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", name) is None
    ]
    if invalid_domains:
        raise KafaError(
            f"distribution runtime domains contain invalid command names: {invalid_domains}"
        )
    derived = set(distribution_file_inventory(normalized, include_additional=False))
    overlap = derived & set(normalized["additional_files"])
    if overlap:
        raise KafaError(
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
        candidates = root.rglob("*")
        for path in candidates:
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


def distribution_inventory_issues(
    root: Path,
    distribution: dict[str, Any],
) -> list[str]:
    expected = set(distribution_file_inventory(distribution))
    actual = set(plugin_file_inventory(root))
    issues: list[str] = []
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing:
        issues.append(f"distribution file inventory missing: {missing}")
    if extra:
        issues.append(f"distribution file inventory extra: {extra}")
    return issues


def release_version() -> str:
    if REPO_VERSION_FILE.exists():
        return REPO_VERSION_FILE.read_text(encoding="utf-8").strip()
    return __version__


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw[:1] == ["project"]:
        try:
            return command_project_argv(raw[1:])
        except KafaError as exc:
            if _project_json_error_requested(raw[1:]):
                print(
                    json.dumps(
                        {
                            "state": "error",
                            "blockers": [
                                {
                                    "code": "runtime-unavailable",
                                    "message": str(exc),
                                }
                            ],
                            "actions": [],
                            "details": {
                                "kind": "project-wrapper",
                                "error": str(exc),
                            },
                        },
                        ensure_ascii=False,
                    )
                )
                return 1
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
    parser = build_parser()
    args = parser.parse_args(raw)
    if args.version:
        print(release_version())
        return 0
    if not args.command:
        parser.print_help()
        return 2
    try:
        if args.command == "doctor":
            return command_doctor(args)
        if args.command == "plugin":
            return command_plugin(args)
    except KafaError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    parser.error("unknown command")
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install and inspect Codex Project Harness local plugin distribution.")
    parser.add_argument("--version", action="store_true", help="Print the repository release version and exit.")
    sub = parser.add_subparsers(dest="command")

    doctor = sub.add_parser("doctor", help="Check local packaging, plugin, and marketplace readiness.")
    add_common_scope_args(doctor)
    doctor.add_argument("--json", action="store_true", help="Print machine-readable check results.")

    sub.add_parser(
        "project",
        help="Run an installed local Harness domain in an ordinary project.",
    )

    plugin = sub.add_parser("plugin", help="Manage Codex marketplace entries for the harness plugin.")
    plugin_sub = plugin.add_subparsers(dest="plugin_command", required=True)
    for name in ["install", "upgrade"]:
        command = plugin_sub.add_parser(name)
        add_common_scope_args(command)
        command.add_argument("--plugin-path", default="", help="Source plugin path. Defaults to <repo>/plugins/codex-project-harness.")
        command.add_argument("--marketplace-name", default=DEFAULT_MARKETPLACE_NAME)
        command.add_argument("--force", action="store_true", help="Replace an existing copied plugin directory.")
        command.add_argument("--dry-run", action="store_true")
    uninstall = plugin_sub.add_parser("uninstall")
    add_common_scope_args(uninstall)
    uninstall.add_argument("--remove-files", action="store_true", help="Also remove the managed copied plugin directory.")
    uninstall.add_argument("--dry-run", action="store_true")
    return parser


def add_common_scope_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", default=".", help="Repository root. Defaults to the current directory.")
    parser.add_argument("--scope", choices=["repo", "user"], default="repo", help="Marketplace scope. Defaults to repo.")


def command_plugin(args: argparse.Namespace) -> int:
    if args.plugin_command == "install":
        result = install_plugin(args, upgrade=False)
    elif args.plugin_command == "upgrade":
        result = install_plugin(args, upgrade=True)
    elif args.plugin_command == "uninstall":
        result = uninstall_plugin(args)
    else:
        raise KafaError(f"unknown plugin command: {args.plugin_command}")
    for line in result:
        print(line)
    return 0


def command_doctor(args: argparse.Namespace) -> int:
    report = doctor_report(Path(args.repo).expanduser().resolve(), args.scope)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for check in report["checks"]:
            prefix = "OK" if check["ok"] else "ERROR"
            print(f"{prefix}: {check['name']}: {check['details']}")
    return 0 if report["ok"] else 1


def _project_help() -> str:
    return (
        "usage: kafa project <domain> [--repo PATH] [runtime arguments...]\n"
        "The domain inventory comes from the installed local Kafa plugin.\n"
        "Use 'kafa project doctor [--repo PATH] [--verbose|--json]' for the "
        "specialized project check."
    )


def _project_json_error_requested(tokens: list[str]) -> bool:
    if not tokens or "--json" not in tokens:
        return False
    if tokens[0] in {"status", "doctor"}:
        return True
    return tokens[0] == "quickstart" and "status" in tokens[1:]


def _parse_project_invocation(tokens: list[str]) -> tuple[str, Path, list[str]]:
    if not tokens or tokens[0] in {"-h", "--help"}:
        print(_project_help())
        return "", Path.cwd().resolve(), []
    domain = tokens[0]
    repo_value = "."
    index = 1
    if index < len(tokens) and tokens[index] == "--repo":
        if index + 1 >= len(tokens) or not tokens[index + 1]:
            raise KafaError("project --repo requires a non-empty path")
        repo_value = tokens[index + 1]
        index += 2
    elif index < len(tokens) and tokens[index].startswith("--repo="):
        repo_value = tokens[index].split("=", 1)[1]
        if not repo_value:
            raise KafaError("project --repo requires a non-empty path")
        index += 1
    if index < len(tokens) and (
        tokens[index] == "--repo" or tokens[index].startswith("--repo=")
    ):
        raise KafaError("project --repo may be specified only once")
    return domain, Path(repo_value).expanduser().resolve(), tokens[index:]


def command_project_argv(tokens: list[str]) -> int:
    domain, repo, runtime_args = _parse_project_invocation(tokens)
    if not domain:
        return 0
    if domain == "doctor":
        allowed = {"--verbose", "--json"}
        unknown = [item for item in runtime_args if item not in allowed]
        if unknown or len(runtime_args) != len(set(runtime_args)) or len(runtime_args) > 1:
            raise KafaError(
                "project doctor accepts only one of --verbose or --json after --repo"
            )
        authority = resolve_project_runtime_authority()
        if domain not in authority.distribution["public_runtime_domains"]:
            raise KafaError(
                "project runtime domain 'doctor' is not declared by the installed plugin"
            )
        report = project_doctor_report(repo, authority=authority)
        envelope = project_doctor_operator_report(report)
        if "--json" in runtime_args:
            print(json.dumps(envelope, ensure_ascii=False))
        elif "--verbose" in runtime_args:
            for line in project_doctor_verbose_lines(envelope):
                print(line)
        else:
            blocker = envelope["blockers"][0] if envelope["blockers"] else None
            action = envelope["actions"][0] if envelope["actions"] else "none"
            print(f"state: {envelope['state']}")
            print(
                "blocker: none"
                if blocker is None
                else f"blocker: [{blocker['code']}] {blocker['message']}"
            )
            print(f"next: {action}")
        return 0 if report["ok"] else 1

    authority = resolve_project_runtime_authority()
    if domain not in authority.distribution["public_runtime_domains"]:
        raise KafaError(
            f"project runtime domain {domain!r} is not declared by the installed plugin"
        )
    return run_project_harness(
        repo,
        [domain, *runtime_args],
        authority=authority,
    )


def _load_plugin_metadata(root: Path) -> dict[str, Any]:
    path = root / ".codex-plugin/plugin.json"

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise KafaError(f"duplicate plugin manifest key: {key}")
            value[key] = item
        return value

    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicates,
        )
    except KafaError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise KafaError(f"invalid installed plugin manifest {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise KafaError(f"installed plugin manifest must be an object: {path}")
    return value


def static_runtime_domains(path: Path) -> set[str]:
    """Read top-level ``sub.add_parser`` literals without importing runtime code."""

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return set()
    domains: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        function = node.func
        if not (
            isinstance(function, ast.Attribute)
            and function.attr == "add_parser"
            and isinstance(function.value, ast.Name)
            and function.value.id == "sub"
        ):
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            domains.add(first.value)
    return domains


def _runtime_distribution_issues(
    root: Path,
    distribution: dict[str, Any],
) -> list[str]:
    errors: list[str] = distribution_inventory_issues(root, distribution)
    actual_skills = directory_names(root / "skills")
    if actual_skills != set(distribution["skills"]):
        errors.append(
            f"skills inventory mismatch: {sorted(actual_skills ^ set(distribution['skills']))}"
        )
    for directory, names, label in (
        (root / "core", distribution["core"], "core"),
        (root / "scripts", distribution["scripts"], "scripts"),
        (root / "hooks", distribution["hooks"]["files"], "hooks"),
        (root / "schemas", distribution["schemas"], "schemas"),
        (
            root / "templates/agents",
            distribution["templates"]["native_agents"],
            "agent templates",
        ),
        (
            root / "templates/project",
            distribution["templates"]["project_support"],
            "project templates",
        ),
        (root / "references", distribution["references"], "references"),
    ):
        check_exact_file_inventory(errors, directory, names, "", label)
    retired = [name for name in RETIRED_CORE_FILES if (root / "core" / name).exists()]
    if retired:
        errors.append(f"retired core files exist: {retired}")
    hook_ok, hook_details = static_hook_definition(root, distribution=distribution)
    if not hook_ok:
        errors.append(hook_details)
    actual_domains = static_runtime_domains(root / "scripts/harness.py")
    expected_domains = set(distribution["public_runtime_domains"])
    if actual_domains != expected_domains:
        errors.append(
            "public runtime domain inventory mismatch: "
            f"actual={sorted(actual_domains)} expected={sorted(expected_domains)}"
        )
    import_errors, boundary_failures = _runtime_python_source_issues(root)
    errors.extend(import_errors)
    if boundary_failures:
        errors.append("; ".join(boundary_failures[:6]))
    return errors


def validate_project_runtime_root(
    candidate: Path,
    *,
    label: str,
) -> ProjectRuntimeAuthority:
    candidate = candidate.expanduser()
    if not candidate.is_absolute():
        raise KafaError(f"{label} must be an absolute path: {candidate}")
    if path_is_link(candidate):
        raise KafaError(f"{label} contains a symlink/junction: {candidate}")
    try:
        root = candidate.resolve(strict=True)
    except OSError as exc:
        raise KafaError(f"{label} is unavailable: {candidate}: {exc}") from exc
    if not managed_tree_is_safe(root):
        raise KafaError(f"{label} is missing or contains a symlink/junction: {root}")
    semantic_digest = plugin_tree_digest(root)
    if not semantic_digest:
        raise KafaError(f"{label} digest is unavailable before validation: {root}")
    metadata = _load_plugin_metadata(root)
    if metadata.get("name") != PLUGIN_NAME:
        raise KafaError(
            f"{label} plugin name mismatch: {metadata.get('name')!r}"
        )
    version = str(metadata.get("version", ""))
    expected_version = release_version()
    if version != expected_version:
        raise KafaError(
            f"{label} version mismatch: actual={version!r} expected={expected_version!r}"
        )
    distribution = load_distribution_manifest(root)
    issues = _runtime_distribution_issues(root, distribution)
    digest = plugin_tree_digest(root)
    if not digest or digest != semantic_digest:
        raise KafaError(
            f"{label} changed during validation: "
            f"before={semantic_digest} after={digest or 'unavailable'}"
        )
    if issues:
        raise KafaError(f"{label} is invalid: {'; '.join(issues[:6])}")
    return ProjectRuntimeAuthority(root, distribution, version, digest)


def _registered_project_runtime() -> tuple[Path, str]:
    codex = shutil.which("codex")
    if not codex:
        raise KafaError(
            "installed codex-project-harness runtime registration is unavailable; "
            "install and enable the user plugin first"
        )
    completed = subprocess.run(
        [codex, "plugin", "list", "--json"],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        details = (completed.stderr or completed.stdout).strip()[:500]
        raise KafaError(
            "installed codex-project-harness runtime registration could not be read: "
            f"{details or f'exit {completed.returncode}'}"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise KafaError(f"invalid codex plugin list JSON: {exc}") from exc
    installed = payload.get("installed", []) if isinstance(payload, dict) else []
    entries = installed if isinstance(installed, list) else []
    matches = [
        entry
        for entry in entries
        if isinstance(entry, dict)
        and (
            entry.get("name") == PLUGIN_NAME
            or str(entry.get("pluginId", "")).startswith(f"{PLUGIN_NAME}@")
        )
        and entry.get("installed") is True
        and entry.get("enabled") is True
    ]
    if len(matches) != 1:
        raise KafaError(
            "installed codex-project-harness runtime authority must be exactly one "
            f"enabled registration; found {len(matches)}"
        )
    entry = matches[0]
    source = entry.get("source")
    source_path = source.get("path") if isinstance(source, dict) else ""
    source_kind = source.get("source") if isinstance(source, dict) else ""
    if source_kind != "local" or not isinstance(source_path, str) or not source_path:
        raise KafaError(
            "installed codex-project-harness registration must have one local source path"
        )
    candidate = Path(source_path).expanduser()
    if not candidate.is_absolute():
        raise KafaError(
            f"installed codex-project-harness source path must be absolute: {source_path}"
        )
    return candidate, str(entry.get("version", ""))


def resolve_project_runtime_authority() -> ProjectRuntimeAuthority:
    env_root = os.environ.get("CODEX_PROJECT_HARNESS_PLUGIN_ROOT", "").strip()
    if env_root:
        if os.environ.get("KAFA_MAINTAINER_RUNTIME", "") != "1":
            raise KafaError(
                "explicit project runtime requires KAFA_MAINTAINER_RUNTIME=1; "
                "no fallback was attempted"
            )
        return validate_project_runtime_root(
            Path(env_root),
            label="explicit project runtime",
        )
    candidate, registered_version = _registered_project_runtime()
    authority = validate_project_runtime_root(
        candidate,
        label="installed codex-project-harness runtime",
    )
    if registered_version != authority.version:
        raise KafaError(
            "installed codex-project-harness registration version mismatch: "
            f"registered={registered_version!r} plugin={authority.version!r}"
        )
    return authority


def installed_plugin_root(_repo: Path | None = None) -> Path:
    """Compatibility accessor for the repo-independent installed authority."""

    return resolve_project_runtime_authority().root


def _before_project_runtime_exec(_root: Path, _harness: Path) -> None:
    """Deterministic seam immediately before the authority is revalidated."""


def _after_project_runtime_snapshot(
    _source_root: Path,
    _snapshot_root: Path,
    _harness: Path,
) -> None:
    """Deterministic seam after private capture and before final verification."""


def _verified_snapshot_authority(
    snapshot_root: Path,
    expected: ProjectRuntimeAuthority,
    *,
    stage: str,
) -> ProjectRuntimeAuthority:
    if not managed_tree_is_safe(snapshot_root):
        raise KafaError(f"private project runtime snapshot {stage} is unsafe")
    digest = plugin_tree_digest(snapshot_root)
    if not digest or digest != expected.digest:
        raise KafaError(
            f"private project runtime snapshot {stage} digest mismatch: "
            f"actual={digest or 'unavailable'} expected={expected.digest}"
        )
    return ProjectRuntimeAuthority(
        snapshot_root.resolve(),
        expected.distribution,
        expected.version,
        digest,
    )


def _completed_stream_bytes(value: object, label: str) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    raise KafaError(f"project runtime returned invalid {label} stream")


def run_project_harness_capture(
    repo: Path,
    harness_args: list[str],
    *,
    authority: ProjectRuntimeAuthority | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Execute one command from a complete verified private plugin snapshot."""

    selected = authority or resolve_project_runtime_authority()
    source_harness = selected.root / "scripts/harness.py"
    _before_project_runtime_exec(selected.root, source_harness)
    try:
        with tempfile.TemporaryDirectory(prefix="kafa-project-runtime-") as temp:
            snapshot_root = Path(temp) / PLUGIN_NAME
            shutil.copytree(
                selected.root,
                snapshot_root,
                symlinks=True,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
            )
            try:
                captured = _verified_snapshot_authority(
                    snapshot_root,
                    selected,
                    stage="capture",
                )
            except KafaError as exc:
                raise KafaError(
                    "installed codex-project-harness runtime changed after validation: "
                    f"{exc}"
                ) from exc
            harness = captured.root / "scripts/harness.py"
            _after_project_runtime_snapshot(selected.root, captured.root, harness)
            try:
                final = _verified_snapshot_authority(
                    captured.root,
                    captured,
                    stage="final verification",
                )
            except KafaError as exc:
                raise KafaError(
                    "private project runtime snapshot changed after verification: "
                    f"{exc}"
                ) from exc
            command = [
                sys.executable,
                "-I",
                "-S",
                "-B",
                str(final.root / "scripts/harness.py"),
                "--root",
                str(repo),
                *harness_args,
            ]
            child_env = os.environ.copy()
            child_env.pop("CODEX_PROJECT_HARNESS_PLUGIN_ROOT", None)
            child_env.pop("KAFA_MAINTAINER_RUNTIME", None)
            child_env["KAFA_PROJECT_ENTRYPOINT"] = "1"
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    check=False,
                    env=child_env,
                )
            except OSError as exc:
                raise KafaError(f"project runtime could not start: {exc}") from exc
            return subprocess.CompletedProcess(
                completed.args,
                completed.returncode,
                stdout=_completed_stream_bytes(completed.stdout, "stdout"),
                stderr=_completed_stream_bytes(completed.stderr, "stderr"),
            )
    except KafaError:
        raise
    except OSError as exc:
        raise KafaError(
            "installed codex-project-harness runtime changed after validation: "
            f"{exc}"
        ) from exc


def _write_runtime_stream(data: bytes, stream: Any, label: str) -> None:
    if not data:
        return
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        buffer.write(data)
        buffer.flush()
        return
    try:
        stream.write(data.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise KafaError(f"project runtime emitted invalid UTF-8 on {label}") from exc


def run_project_harness(
    repo: Path,
    harness_args: list[str],
    *,
    authority: ProjectRuntimeAuthority | None = None,
) -> int:
    completed = run_project_harness_capture(
        repo,
        harness_args,
        authority=authority,
    )
    _write_runtime_stream(completed.stdout, sys.stdout, "stdout")
    _write_runtime_stream(completed.stderr, sys.stderr, "stderr")
    return completed.returncode


def install_plugin(args: argparse.Namespace, *, upgrade: bool) -> list[str]:
    repo = Path(args.repo).expanduser().resolve()
    source = plugin_source(repo, args.plugin_path)
    validate_plugin_source(repo, source)
    marketplace_path, plugin_target, source_path = marketplace_locations(repo, args.scope)
    actions: list[str] = []
    if args.scope == "repo":
        if source.resolve() != plugin_target.resolve():
            copy_action(source, plugin_target, force=args.force or upgrade, dry_run=args.dry_run, actions=actions)
        else:
            actions.append(f"using repo plugin {plugin_target}")
    else:
        copy_action(source, plugin_target, force=args.force or upgrade, dry_run=args.dry_run, actions=actions)
    marketplace = read_marketplace(marketplace_path)
    marketplace = upsert_marketplace_entry(marketplace, args.marketplace_name, source_path)
    write_marketplace(marketplace_path, marketplace, dry_run=args.dry_run, actions=actions)
    actions.append(f"{'would install' if args.dry_run else 'installed'} {PLUGIN_NAME} in {args.scope} marketplace")
    return actions


def uninstall_plugin(args: argparse.Namespace) -> list[str]:
    repo = Path(args.repo).expanduser().resolve()
    marketplace_path, plugin_target, _source_path = marketplace_locations(repo, args.scope)
    marketplace = read_marketplace(marketplace_path)
    plugins = [plugin for plugin in marketplace.get("plugins", []) if plugin.get("name") != PLUGIN_NAME]
    removed = len(marketplace.get("plugins", [])) - len(plugins)
    marketplace["plugins"] = plugins
    actions: list[str] = []
    write_marketplace(marketplace_path, marketplace, dry_run=args.dry_run, actions=actions)
    actions.append(f"{'would remove' if args.dry_run else 'removed'} {removed} marketplace entr{'y' if removed == 1 else 'ies'}")
    if args.remove_files:
        if args.scope == "repo":
            raise KafaError("--remove-files is only supported for user scope")
        if plugin_target.exists():
            if args.dry_run:
                actions.append(f"would remove copied plugin {plugin_target}")
            else:
                shutil.rmtree(plugin_target)
                actions.append(f"removed copied plugin {plugin_target}")
    return actions


def plugin_source(repo: Path, value: str) -> Path:
    if value:
        path = Path(value).expanduser()
        return (repo / path).resolve() if not path.is_absolute() else path.resolve()
    return (repo / "plugins" / PLUGIN_NAME).resolve()


def marketplace_locations(repo: Path, scope: str) -> tuple[Path, Path, str]:
    if scope == "repo":
        return (
            repo / ".agents" / "plugins" / "marketplace.json",
            repo / "plugins" / PLUGIN_NAME,
            "./plugins/codex-project-harness",
        )
    home = Path(os.environ.get("HOME", str(Path.home()))).expanduser()
    user_root = home / ".agents" / "plugins"
    return (
        user_root / "marketplace.json",
        user_root / PLUGIN_NAME,
        "./.agents/plugins/codex-project-harness",
    )


def validate_plugin_source(repo: Path, source: Path) -> None:
    if not managed_tree_is_safe(source):
        raise KafaError(
            f"plugin source is missing or contains a symlink/junction: {source}"
        )
    manifest = source / ".codex-plugin" / "plugin.json"
    if not manifest.exists():
        raise KafaError(f"plugin manifest not found: {manifest}")
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise KafaError(f"invalid plugin manifest: {exc}") from exc
    if data.get("name") != PLUGIN_NAME:
        raise KafaError(f"plugin manifest name must be {PLUGIN_NAME}")
    load_distribution_manifest(source)
    version_file = repo / "VERSION"
    if version_file.exists() and data.get("version") != version_file.read_text(encoding="utf-8").strip():
        raise KafaError("plugin manifest version must match repo VERSION")
    structure_ok, structure_details = static_plugin_structure(source)
    if not structure_ok:
        raise KafaError(f"plugin source structure is invalid: {structure_details}")
    contract_ok, contract_details = control_plane_contract(source)
    if not contract_ok:
        raise KafaError(f"plugin source control-plane contract is invalid: {contract_details}")
    local_only_ok, local_only_details = local_only_runtime_boundary(source)
    if not local_only_ok:
        raise KafaError(f"plugin source is not local-only: {local_only_details}")


def copy_action(source: Path, target: Path, *, force: bool, dry_run: bool, actions: list[str]) -> None:
    if target.exists() and not force:
        raise KafaError(f"target plugin already exists: {target}; pass --force or use plugin upgrade")
    if dry_run:
        actions.append(f"would copy {source} -> {target}")
        return
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
    actions.append(f"copied {source} -> {target}")


def read_marketplace(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"name": DEFAULT_MARKETPLACE_NAME, "interface": {"displayName": DISPLAY_NAME}, "plugins": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise KafaError(f"invalid marketplace JSON: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise KafaError(f"marketplace JSON must be an object: {path}")
    plugins = data.get("plugins", [])
    if not isinstance(plugins, list):
        raise KafaError(f"marketplace plugins must be a list: {path}")
    return data


def upsert_marketplace_entry(marketplace: dict[str, Any], marketplace_name: str, source_path: str) -> dict[str, Any]:
    if not marketplace.get("name"):
        marketplace["name"] = marketplace_name
    interface = marketplace.get("interface")
    if not isinstance(interface, dict):
        interface = {}
    interface.setdefault("displayName", DISPLAY_NAME)
    marketplace["interface"] = interface
    entry = {
        "name": PLUGIN_NAME,
        "source": {"source": "local", "path": source_path},
        "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        "category": PLUGIN_CATEGORY,
    }
    plugins = [plugin for plugin in marketplace.get("plugins", []) if plugin.get("name") != PLUGIN_NAME]
    plugins.append(entry)
    marketplace["plugins"] = plugins
    return marketplace


def write_marketplace(path: Path, data: dict[str, Any], *, dry_run: bool, actions: list[str]) -> None:
    if dry_run:
        actions.append(f"would write {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    actions.append(f"wrote {path}")


def doctor_report(repo: Path, scope: str) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    add_check(checks, "python", sys.version_info >= (3, 11), f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
    add_check(checks, "git", shutil.which("git") is not None, shutil.which("git") or "not found")
    add_check(checks, "repo", repo.exists(), str(repo))
    source = plugin_source(repo, "")
    try:
        distribution = load_distribution_manifest(source)
    except KafaError as exc:
        distribution = None
        add_check(checks, "distribution manifest", False, str(exc))
    else:
        add_check(
            checks,
            "distribution manifest",
            True,
            str(source / DISTRIBUTION_MANIFEST_RELATIVE),
        )
    manifest = source / ".codex-plugin" / "plugin.json"
    add_check(checks, "plugin manifest", manifest.exists(), str(manifest))
    source_metadata: dict[str, Any] = {}
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("plugin manifest root must be an object")
            source_metadata = data
            version = (repo / "VERSION").read_text(encoding="utf-8").strip() if (repo / "VERSION").exists() else ""
            add_check(checks, "plugin name", data.get("name") == PLUGIN_NAME, str(data.get("name", "")))
            add_check(checks, "plugin version", not version or data.get("version") == version, f"plugin={data.get('version', '')} repo={version}")
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            add_check(checks, "plugin metadata", False, str(exc))
    structure_ok, structure_details = static_plugin_structure(
        source, distribution=distribution
    )
    add_check(checks, "plugin structure", structure_ok, structure_details)
    contract_ok, contract_details = control_plane_contract(source)
    add_check(checks, "control plane contract", contract_ok, contract_details)
    local_only_ok, local_only_details = local_only_runtime_boundary(source)
    add_check(checks, "local-only runtime boundary", local_only_ok, local_only_details)
    add_install_health_checks(checks, repo, scope, source, source_metadata)
    return {"ok": all(check["ok"] for check in checks), "scope": scope, "repo": str(repo), "checks": checks}


def add_install_health_checks(
    checks: list[dict[str, Any]],
    repo: Path,
    scope: str,
    source: Path,
    source_metadata: dict[str, Any],
) -> None:
    marketplace_path, plugin_target, expected_source_path = marketplace_locations(repo, scope)
    marketplace: dict[str, Any] = {}
    marketplace_error = ""
    try:
        parsed = json.loads(marketplace_path.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("marketplace root must be an object")
        marketplace = parsed
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        marketplace_error = str(exc)
    add_check(
        checks,
        "marketplace manifest",
        bool(marketplace),
        str(marketplace_path) if marketplace else f"{marketplace_path}: {marketplace_error or 'unreadable'}",
    )

    entries = marketplace.get("plugins", []) if isinstance(marketplace.get("plugins", []), list) else []
    matching_entries = [entry for entry in entries if isinstance(entry, dict) and entry.get("name") == PLUGIN_NAME]
    entry_ok = len(matching_entries) == 1
    add_check(checks, "marketplace plugin entry", entry_ok, f"found {len(matching_entries)} {PLUGIN_NAME} entries")
    entry_source = matching_entries[0].get("source") if entry_ok else None
    expected_source = {"source": "local", "path": expected_source_path}
    source_ok = entry_source == expected_source
    add_check(checks, "marketplace source", source_ok, f"actual={entry_source!r} expected={expected_source!r}")

    installed_tree_safe = managed_tree_is_safe(plugin_target)
    installed_manifest = plugin_target / ".codex-plugin" / "plugin.json"
    installed_metadata: dict[str, Any] = {}
    installed_error = ""
    if installed_tree_safe:
        try:
            parsed = json.loads(installed_manifest.read_text(encoding="utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError("plugin manifest root must be an object")
            installed_metadata = parsed
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            installed_error = str(exc)
    else:
        installed_error = "plugin tree contains a symlink or junction"
    add_check(
        checks,
        "installed plugin manifest",
        bool(installed_metadata),
        str(installed_manifest) if installed_metadata else f"{installed_manifest}: {installed_error or 'unreadable'}",
    )
    identity_ok = bool(installed_metadata) and all(
        installed_metadata.get(field) == source_metadata.get(field) for field in ["name", "version"]
    )
    add_check(
        checks,
        "installed plugin identity",
        identity_ok,
        f"installed={installed_metadata.get('name', '')}@{installed_metadata.get('version', '')} "
        f"source={source_metadata.get('name', '')}@{source_metadata.get('version', '')}",
    )

    source_digest = plugin_tree_digest(source)
    installed_digest = plugin_tree_digest(plugin_target) if installed_tree_safe else ""
    content_ok = bool(source_digest) and source_digest == installed_digest
    add_check(
        checks,
        "installed plugin content",
        content_ok,
        f"installed={installed_digest or 'unavailable'} source={source_digest or 'unavailable'}",
    )
    hook_ok, hook_details = static_hook_definition(plugin_target) if content_ok else (False, "not checked: installed content does not match source")
    add_check(checks, "hook definition", hook_ok, hook_details)
    if scope == "user":
        registration_ok, registration_details, cache_ok, cache_details = codex_plugin_health(
            repo,
            plugin_target,
            str(marketplace.get("name", "")),
            str(installed_metadata.get("version", "")),
        )
        add_check(checks, "codex plugin registration", registration_ok, registration_details)
        add_check(checks, "codex plugin cache", cache_ok, cache_details)


def plugin_tree_digest(root: Path) -> str:
    if not managed_tree_is_safe(root):
        return ""
    digest = hashlib.sha256()
    try:
        files = sorted(
            path
            for path in root.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}
        )
        for path in files:
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    except OSError:
        return ""
    return digest.hexdigest()


def managed_tree_is_safe(root: Path) -> bool:
    if not root.is_dir() or path_is_link(root):
        return False
    try:
        return not any(path_is_link(path) for path in root.rglob("*"))
    except OSError:
        return False


def path_is_link(path: Path) -> bool:
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


def read_static_python_constants(path: Path) -> dict[str, object]:
    """Resolve literal and same-module alias constants without importing source."""

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return {}
    resolved: dict[str, object] = {}
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        try:
            value: object = ast.literal_eval(node.value)
        except (ValueError, TypeError):
            value = resolved.get(node.value.id) if isinstance(node.value, ast.Name) else None
        for target in targets:
            if isinstance(target, ast.Name):
                resolved[target.id] = value
    return resolved


def static_plugin_structure(
    source: Path,
    *,
    distribution: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    errors: list[str] = []
    if not managed_tree_is_safe(source):
        return False, f"plugin tree is missing or contains a symlink/junction: {source}"
    if distribution is None:
        try:
            distribution = load_distribution_manifest(source)
        except KafaError as exc:
            return False, str(exc)
    errors.extend(distribution_inventory_issues(source, distribution))
    repo_root = source.parent.parent
    manifest_path = source / ".codex-plugin" / "plugin.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("plugin manifest root must be an object")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return False, f"invalid plugin manifest: {exc}"

    version_path = repo_root / "VERSION"
    version_text = read_text(version_path).strip()
    release_path = repo_root / "release.json"
    release_manifest: dict[str, object] = {}
    if release_path.exists():
        try:
            release_value = json.loads(release_path.read_text(encoding="utf-8"))
            if not isinstance(release_value, dict):
                raise ValueError("root must be an object")
            release_manifest = release_value
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            errors.append(f"invalid release.json: {exc}")
    version = str(release_manifest.get("version", "") or version_text)
    if manifest.get("name") != PLUGIN_NAME:
        errors.append(f"plugin name must be {PLUGIN_NAME}")
    if release_manifest and version_text != version:
        errors.append("root VERSION must match release.json")
    if version and manifest.get("version") != version:
        errors.append("plugin version must match release.json")
    if "schema_version" in manifest or "display_name" in manifest:
        errors.append("plugin manifest contains legacy fields")
    if not isinstance(manifest.get("author"), dict):
        errors.append("plugin author must be an object")
    if manifest.get("skills") != "./skills/":
        errors.append("plugin skills must be ./skills/")
    interface = manifest.get("interface")
    interface_fields = {
        "displayName", "shortDescription", "longDescription", "developerName",
        "category", "capabilities", "defaultPrompt",
    }
    if not isinstance(interface, dict) or not interface_fields.issubset(interface):
        errors.append("plugin interface metadata is incomplete")
    elif not isinstance(interface.get("capabilities"), list) or not isinstance(interface.get("defaultPrompt"), list):
        errors.append("plugin interface list fields are invalid")

    pyproject_path = repo_root / "pyproject.toml"
    try:
        package = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        raw_project = package.get("project", {}) if isinstance(package, dict) else {}
        project = raw_project if isinstance(raw_project, dict) else {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        errors.append(f"invalid pyproject.toml: {exc}")
        project = {}
    expected_package_version = str(
        release_manifest.get("pep440_version", "")
        or (version.replace("-beta.", "b") if "-beta." in version else version)
    )
    if project.get("name") != "kafa":
        errors.append("pyproject project.name must be kafa")
    if version and project.get("version") != expected_package_version:
        errors.append("pyproject version must match release.json")
    if project.get("requires-python") != ">=3.11":
        errors.append("pyproject requires-python must be >=3.11")
    dependencies = project.get("dependencies", [])
    if not isinstance(dependencies, list) or dependencies:
        errors.append("pyproject base dependencies must remain empty")
    optional_dependencies = project.get("optional-dependencies", {})
    if isinstance(optional_dependencies, dict):
        flattened = [str(item).lower() for values in optional_dependencies.values() if isinstance(values, list) for item in values]
        if "host-codex" in optional_dependencies or any("openai-codex" in item for item in flattened):
            errors.append("pyproject must not declare the retired Host Codex SDK dependency")
    if not isinstance(project.get("scripts"), dict) or project["scripts"].get("kafa") != "kafa.cli:main":
        errors.append("pyproject must expose kafa = kafa.cli:main")

    if release_manifest:
        runtime_identity = read_static_python_constants(source / "core" / "__init__.py")
        for constant, field in [
            ("RUNTIME_VERSION", "runtime_version"),
            ("KERNEL_VERSION", "kernel_version"),
            ("SCHEMA_VERSION", "schema_version_runtime"),
        ]:
            if runtime_identity.get(constant) != release_manifest.get(field):
                errors.append(
                    f"{constant} must match release.json {field}: "
                    f"runtime={runtime_identity.get(constant)!r} "
                    f"manifest={release_manifest.get(field)!r}"
                )

    skills_root = source / "skills"
    for skill in distribution["skills"]:
        skill_root = skills_root / skill
        skill_md = skill_root / "SKILL.md"
        text = read_text(skill_md)
        if not text or path_is_link(skill_md):
            errors.append(f"missing skill file: {skill}")
            continue
        front_matter = text.split("---", 2)
        if not text.startswith("---") or len(front_matter) < 3:
            errors.append(f"missing skill front matter: {skill}")
        elif (f'name: "{skill}"' not in text and f"name: {skill}" not in text) or "description:" not in front_matter[1]:
            errors.append(f"invalid skill metadata: {skill}")
        ui_metadata = read_text(skill_root / "agents" / "openai.yaml")
        if not all(marker in ui_metadata for marker in ["interface:", "display_name:", "short_description:", "default_prompt:"]):
            errors.append(f"invalid skill UI metadata: {skill}")
    actual_skills = directory_names(skills_root)
    if actual_skills != set(distribution["skills"]):
        errors.append(
            "skill inventory mismatch: "
            f"{sorted(actual_skills ^ set(distribution['skills']))}"
        )

    check_exact_file_inventory(
        errors, source / "core", distribution["core"], ".py", "core"
    )
    for retired in RETIRED_CORE_FILES:
        if (source / "core" / retired).exists():
            errors.append(f"retired core file exists: {retired}")
    errors.extend(local_python_import_errors(source))
    check_exact_file_inventory(
        errors, source / "scripts", distribution["scripts"], ".py", "scripts"
    )
    actual_domains = static_runtime_domains(source / "scripts/harness.py")
    expected_domains = set(distribution["public_runtime_domains"])
    if actual_domains != expected_domains:
        errors.append(
            "public runtime domain inventory mismatch: "
            f"actual={sorted(actual_domains)} expected={sorted(expected_domains)}"
        )
    check_exact_file_inventory(
        errors,
        source / "hooks",
        distribution["hooks"]["files"],
        "",
        "hooks",
    )
    check_exact_file_inventory(
        errors, source / "schemas", distribution["schemas"], ".json", "schemas"
    )
    check_exact_file_inventory(
        errors,
        source / "templates" / "agents",
        distribution["templates"]["native_agents"],
        ".toml",
        "agent templates",
    )
    check_exact_file_inventory(
        errors,
        source / "templates" / "project",
        distribution["templates"]["project_support"],
        "",
        "project templates",
    )
    check_exact_file_inventory(
        errors,
        source / "references",
        distribution["references"],
        "",
        "references",
    )
    proxy = source / "skills" / "project-harness" / "scripts" / "harness.py"
    if not proxy.is_file() or path_is_link(proxy):
        errors.append("missing project-harness self-contained CLI")
    for template_name in distribution["templates"]["native_agents"]:
        template = source / "templates" / "agents" / template_name
        try:
            payload = tomllib.loads(template.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            errors.append(f"invalid agent template {template_name}: {exc}")
            continue
        expected_name = template_name.removesuffix(".toml")
        if set(payload) != {"name", "description", "developer_instructions"}:
            errors.append(f"invalid agent template fields: {template_name}")
        if payload.get("name") != expected_name:
            errors.append(f"agent template name mismatch: {template_name}")
    for schema in distribution["schemas"]:
        try:
            json.loads((source / "schemas" / schema).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"invalid schema {schema}: {exc}")
    hooks_ok, hooks_details = static_hook_definition(
        source, distribution=distribution
    )
    if not hooks_ok:
        errors.append(hooks_details)
    if (source / "skills" / "release-readiness" / "SKILL.md").exists() or (source / "templates" / "agents" / "release-engineer.toml").exists():
        errors.append("stale delivery-only replacement exists")

    return (not errors, "complete static plugin contract" if not errors else "; ".join(errors[:6]))


def directory_names(root: Path) -> set[str]:
    try:
        return {
            path.name for path in root.iterdir()
            if path.name != "__pycache__"
        }
    except OSError:
        return set()


def check_exact_file_inventory(
    errors: list[str],
    root: Path,
    required: tuple[str, ...],
    suffix: str,
    label: str,
) -> None:
    try:
        actual = {
            path.name for path in root.iterdir()
            if path.is_file()
        }
    except OSError:
        actual = set()
    expected = set(required)
    if actual != expected:
        errors.append(f"{label} inventory mismatch: {sorted(actual ^ expected)}")


def check_required_file_inventory(
    errors: list[str],
    root: Path,
    required: tuple[str, ...],
    suffix: str,
    label: str,
) -> None:
    try:
        actual = {
            path.name for path in root.iterdir()
            if path.is_file() and not path_is_link(path) and (not suffix or path.suffix == suffix)
        }
    except OSError:
        actual = set()
    missing = set(required) - actual
    if missing:
        errors.append(f"{label} required files missing: {sorted(missing)}")


def _runtime_import_nodes(tree: ast.AST) -> list[ast.Import | ast.ImportFrom]:
    """Return every import statement without walking expression-only subtrees."""

    imports: list[ast.Import | ast.ImportFrom] = []
    pending = [tree]
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.append(node)
            continue
        if isinstance(node, ast.expr):
            continue
        pending.extend(ast.iter_child_nodes(node))
    return imports


def _runtime_python_source_issues(source: Path) -> tuple[list[str], list[str]]:
    """Inspect runtime Python once for local-import and local-only violations."""

    core_root = source / "core"
    available_core = {
        path.stem for path in core_root.glob("*.py")
        if path.is_file() and not path_is_link(path)
    }
    source_paths = [
        *core_root.glob("*.py"),
        *(source / "scripts").glob("*.py"),
        *(source / "hooks").glob("*.py"),
    ]
    import_errors: set[str] = set()
    boundary_failures: list[str] = []
    for path in source_paths:
        if path_is_link(path):
            boundary_failures.append(
                f"runtime path is a link: {path.relative_to(source)}"
            )
            continue
        try:
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text, filename=str(path))
        except (OSError, SyntaxError) as exc:
            relative = path.relative_to(source)
            import_errors.add(f"invalid Python source: {relative}: {exc}")
            boundary_failures.append(f"runtime source unreadable: {relative}: {exc}")
            continue

        lowered = text.lower()
        if path.name != "validate_structure.py":
            for marker in FORBIDDEN_RUNTIME_LITERALS:
                if marker in lowered:
                    boundary_failures.append(
                        f"external runtime marker {marker!r} in {path.relative_to(source)}"
                    )

        for node in _runtime_import_nodes(tree):
            module = ""
            if isinstance(node, ast.ImportFrom):
                if node.level and path.parent == core_root and node.module:
                    module = node.module.split(".", 1)[0]
                elif node.module and node.module.startswith("core."):
                    module = node.module.split(".", 2)[1]
                provider_modules = [node.module] if node.module else []
            elif isinstance(node, ast.Import):
                provider_modules = [alias.name for alias in node.names]
                for alias in node.names:
                    if alias.name.startswith("core."):
                        module = alias.name.split(".", 2)[1]
                        if module not in available_core:
                            import_errors.add(
                                f"missing local Python import: core.{module} referenced by {path.relative_to(source)}"
                            )
            else:  # pragma: no cover - narrowed by _runtime_import_nodes
                provider_modules = []
            if module and module not in available_core:
                import_errors.add(
                    f"missing local Python import: core.{module} referenced by {path.relative_to(source)}"
                )
            for provider_module in provider_modules:
                if provider_module.split(".", 1)[0] in FORBIDDEN_PROVIDER_IMPORTS:
                    boundary_failures.append(
                        f"external provider import {provider_module!r} in {path.relative_to(source)}"
                    )
    return sorted(import_errors), boundary_failures


def local_python_import_errors(source: Path) -> list[str]:
    errors, _ = _runtime_python_source_issues(source)
    return errors


def static_hook_definition(
    plugin_root: Path,
    *,
    distribution: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    if distribution is None:
        try:
            distribution = load_distribution_manifest(plugin_root)
        except KafaError as exc:
            return False, str(exc)
    hook_files = tuple(distribution["hooks"]["files"])
    definitions = [name for name in hook_files if Path(name).suffix == ".json"]
    runners = [name for name in hook_files if Path(name).suffix == ".py"]
    if len(definitions) != 1 or len(runners) != 1:
        return False, "distribution hooks require one JSON definition and one Python runner"
    hooks_path = plugin_root / "hooks" / definitions[0]
    runner_relative = f"hooks/{runners[0]}"

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate Hook definition key: {key}")
            value[key] = item
        return value

    try:
        payload = json.loads(
            hooks_path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicates,
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        return False, f"invalid Hook definition: {exc}"
    if not isinstance(payload, dict) or set(payload) != {"hooks"}:
        return False, "Hook definition must contain only the hooks object"
    hooks = payload.get("hooks", {}) if isinstance(payload, dict) else {}
    expected = set(distribution["hooks"]["events"])
    if set(hooks) != expected:
        return False, f"hook events={sorted(hooks)} expected={sorted(expected)}"
    for event, groups in hooks.items():
        if not isinstance(groups, list) or not groups:
            return False, f"{event}: no hook groups"
        for group in groups:
            entries = group.get("hooks", []) if isinstance(group, dict) else []
            if not isinstance(entries, list) or not entries:
                return False, f"{event}: no command hooks"
            for hook in entries:
                if not isinstance(hook, dict):
                    return False, f"{event}: invalid hook entry"
                if hook.get("type") != "command":
                    return False, f"{event}: hook type must be command"
                for field, root_token, windows in (
                    ("command", "${PLUGIN_ROOT}", False),
                    ("commandWindows", "%PLUGIN_ROOT%", True),
                ):
                    command = hook.get(field)
                    if not isinstance(command, str) or not command.strip():
                        return False, f"{event}: {field} must be a command string"
                    try:
                        tokens = shlex.split(command, posix=not windows)
                    except ValueError as exc:
                        return False, f"{event}: invalid {field}: {exc}"
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
                        return False, (
                            f"{event}: {field} must contain exactly Python, "
                            "the manifest Hook runner, and the matching event"
                        )
    return (
        True,
        f"{len(expected)} warn-only lifecycle events use installed PLUGIN_ROOT commands",
    )


def codex_plugin_health(
    repo: Path,
    plugin_root: Path,
    marketplace_name: str,
    version: str,
) -> tuple[bool, str, bool, str]:
    codex = shutil.which("codex")
    if not codex:
        return False, "codex CLI not found", False, "not checked: codex CLI not found"
    completed = subprocess.run(
        [codex, "plugin", "list", "--json"],
        text=True,
        capture_output=True,
        cwd=repo,
        check=False,
    )
    if completed.returncode != 0:
        details = (completed.stderr or completed.stdout).strip()[:500] or f"codex plugin list exited {completed.returncode}"
        return False, details, False, "not checked: plugin registration unavailable"
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return False, f"invalid codex plugin list JSON: {exc}", False, "not checked: invalid plugin list JSON"
    installed = payload.get("installed", []) if isinstance(payload, dict) else []
    expected_id = f"{PLUGIN_NAME}@{marketplace_name}" if marketplace_name else ""
    for entry in installed if isinstance(installed, list) else []:
        if not isinstance(entry, dict) or entry.get("pluginId") != expected_id:
            continue
        source_path = entry.get("source", {}).get("path") if isinstance(entry.get("source"), dict) else ""
        source_matches = bool(source_path) and Path(str(source_path)).expanduser().resolve() == plugin_root.resolve()
        ok = (
            entry.get("installed") is True
            and entry.get("enabled") is True
            and entry.get("version") == version
            and source_matches
        )
        registration_details = (
            f"id={entry.get('pluginId')} installed={entry.get('installed')} enabled={entry.get('enabled')} "
            f"version={entry.get('version')} source={source_path}"
        )
        cache_root = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser() / "plugins" / "cache"
        cache_path = cache_root / marketplace_name / PLUGIN_NAME / version
        try:
            cache_safe = cache_path.resolve().is_relative_to(cache_root.resolve())
        except OSError:
            cache_safe = False
        plugin_digest = plugin_tree_digest(plugin_root)
        cache_digest = plugin_tree_digest(cache_path) if cache_safe else ""
        cache_ok = ok and bool(plugin_digest) and cache_digest == plugin_digest
        cache_details = (
            f"path={cache_path} cache={cache_digest or 'unavailable'} installed={plugin_digest or 'unavailable'}"
            if cache_safe
            else f"unsafe cache path derived from marketplace={marketplace_name!r} version={version!r}"
        )
        return ok, registration_details, cache_ok, cache_details
    missing = f"missing enabled installation {expected_id or PLUGIN_NAME}"
    return False, missing, False, "not checked: plugin registration missing"


def _after_project_doctor_runtime_report(
    _repo: Path,
    _runtime: dict[str, Any],
) -> None:
    """Deterministic test seam after one complete captured runtime report."""


def _strict_project_doctor_envelope(
    completed: subprocess.CompletedProcess[bytes],
) -> dict[str, Any]:
    stderr = _completed_stream_bytes(completed.stderr, "stderr")
    if stderr:
        details = stderr.decode("utf-8", errors="replace").strip()[:500]
        raise KafaError(
            "installed project runtime doctor emitted stderr: "
            f"{details or 'non-empty diagnostics'}"
        )
    try:
        text = _completed_stream_bytes(completed.stdout, "stdout").decode("utf-8")
    except UnicodeDecodeError as exc:
        raise KafaError("installed project runtime doctor emitted invalid UTF-8") from exc

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise KafaError(f"duplicate project doctor JSON key: {key}")
            value[key] = item
        return value

    def reject_nonfinite(value: str) -> object:
        raise KafaError(
            f"installed project runtime doctor emitted non-finite JSON value: {value}"
        )

    try:
        payload = json.loads(
            text,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_nonfinite,
        )
    except KafaError:
        raise
    except json.JSONDecodeError as exc:
        raise KafaError(f"installed project runtime doctor returned invalid JSON: {exc}") from exc
    expected = {"state", "blockers", "actions", "details"}
    if not isinstance(payload, dict) or set(payload) != expected:
        actual = set(payload) if isinstance(payload, dict) else set()
        raise KafaError(
            "installed project runtime doctor envelope keys mismatch: "
            f"missing={sorted(expected - actual)} extra={sorted(actual - expected)}"
        )
    state = payload["state"]
    if state not in {
        "healthy",
        "unhealthy",
        "not-initialized",
        "recovery-required",
        "error",
    }:
        raise KafaError(
            f"installed project runtime doctor returned invalid state: {state!r}"
        )
    blockers = payload["blockers"]
    if not isinstance(blockers, list):
        raise KafaError("installed project runtime doctor blockers must be a list")
    for index, blocker in enumerate(blockers):
        if not isinstance(blocker, dict) or set(blocker) != {"code", "message"}:
            raise KafaError(
                f"installed project runtime doctor blocker {index} has invalid shape"
            )
        for field in ("code", "message"):
            value = blocker[field]
            if (
                not isinstance(value, str)
                or not value.strip()
                or value != value.strip()
                or any(character in value for character in ("\r", "\n", "\x00"))
            ):
                raise KafaError(
                    f"installed project runtime doctor blocker {index} {field} is invalid"
                )
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", blocker["code"]) is None:
            raise KafaError(
                f"installed project runtime doctor blocker {index} code is invalid"
            )
    actions = payload["actions"]
    if not isinstance(actions, list) or not all(
        isinstance(action, str)
        and action.strip()
        and action == action.strip()
        and not any(character in action for character in ("\r", "\n", "\x00"))
        for action in actions
    ):
        raise KafaError("installed project runtime doctor actions are invalid")
    details = payload["details"]
    if (
        not isinstance(details, dict)
        or not isinstance(details.get("initialized"), bool)
    ):
        raise KafaError(
            "installed project runtime doctor details require boolean initialized"
        )
    initialized = details["initialized"]
    if state in {"healthy", "unhealthy"} and initialized is not True:
        raise KafaError(
            f"installed project runtime doctor {state} state requires initialized=true"
        )
    if state in {"not-initialized", "recovery-required", "error"} and initialized is not False:
        raise KafaError(
            f"installed project runtime doctor {state} state requires initialized=false"
        )
    if state in {"healthy", "unhealthy"}:
        issues = details.get("issues")
        if (
            not isinstance(issues, list)
            or not all(isinstance(issue, str) and issue for issue in issues)
            or issues != [blocker["message"] for blocker in blockers]
        ):
            raise KafaError(
                f"installed project runtime doctor {state} details/issues mismatch"
            )
    else:
        error = details.get("error")
        if not isinstance(error, str) or not error.strip():
            raise KafaError(
                f"installed project runtime doctor {state} details require error text"
            )
    if state == "healthy" and blockers:
        raise KafaError(
            "installed project runtime doctor healthy state is internally inconsistent"
        )
    if state != "healthy" and not blockers:
        raise KafaError(
            "installed project runtime doctor non-healthy state requires blockers"
        )
    expected_returncode = 0 if state == "healthy" else 1
    if completed.returncode != expected_returncode:
        raise KafaError(
            "installed project runtime doctor exit/state mismatch: "
            f"exit={completed.returncode} state={state}"
        )
    return payload


def _public_project_command(repo: Path, domain: str, *args: str) -> str:
    command = ["kafa", "project", domain, "--repo", str(repo), *args]
    return subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)


def _public_project_doctor_actions(repo: Path, state: str) -> list[str]:
    if state == "not-initialized":
        return [
            _public_project_command(repo, "init"),
            _public_project_command(repo, "quickstart", "status"),
        ]
    if state == "unhealthy":
        return [_public_project_command(repo, "repair", "--dry-run")]
    return []


def project_doctor_report(
    repo: Path,
    *,
    authority: ProjectRuntimeAuthority | None = None,
) -> dict[str, Any]:
    selected = authority or resolve_project_runtime_authority()
    checks: list[dict[str, Any]] = []
    add_check(checks, "python", sys.version_info >= (3, 11), f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
    add_check(checks, "git", shutil.which("git") is not None, shutil.which("git") or "not found")
    root_is_directory = repo.is_dir()
    add_check(checks, "project root", root_is_directory, str(repo))
    if root_is_directory and shutil.which("git"):
        try:
            completed = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=repo,
                text=True,
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            add_check(checks, "git project", False, f"git probe failed: {exc}")
        else:
            add_check(
                checks,
                "git project",
                completed.returncode == 0 and completed.stdout.strip() == "true",
                "git repo" if completed.returncode == 0 else "not a git repo",
            )
    else:
        add_check(checks, "git project", False, "project root or git missing")

    completed = run_project_harness_capture(
        repo,
        ["doctor", "--json"],
        authority=selected,
    )
    runtime = _strict_project_doctor_envelope(completed)
    runtime["actions"] = _public_project_doctor_actions(repo, str(runtime["state"]))
    _after_project_doctor_runtime_report(repo, runtime)
    db_path = repo / ".ai-team" / "state" / "harness.db"
    initialized = bool(runtime["details"]["initialized"])
    runtime_messages = [
        str(blocker["message"]) for blocker in runtime["blockers"]
    ]
    initialized_details = (
        str(db_path)
        if initialized
        else runtime_messages[0]
        if runtime_messages
        else f"missing initialized runtime at {db_path}"
    )
    add_check(checks, "harness initialized", initialized, initialized_details)

    gitignore_issues = [
        str(blocker["message"])
        for blocker in runtime["blockers"]
        if blocker["code"] in {"gitignore-missing", "runtime-gitignore"}
    ]
    if not initialized:
        ignored = False
        details = f"not checked: runtime path audit blocked: {initialized_details}"
    else:
        ignored = not gitignore_issues
        details = "ok" if ignored else "; ".join(gitignore_issues)
    add_check(checks, "runtime gitignore", ignored, details)
    add_check(checks, "local-only runtime boundary", True, "project doctor requires no remote profile or credential")
    ok = all(check["ok"] for check in checks) and runtime["state"] == "healthy"
    return {
        "ok": ok,
        "kind": "project",
        "repo": str(repo),
        "checks": checks,
        "next_commands": list(runtime["actions"]),
        "runtime": runtime,
        "runtime_authority": {
            "root": str(selected.root),
            "version": selected.version,
            "digest": selected.digest,
        },
    }


def _project_doctor_check_code(name: str, details: str) -> str:
    lowered = details.lower()
    for code in (
        "rollback-incomplete",
        "recovery-required",
        "migration-in-progress",
    ):
        if code in lowered:
            return code
    if "unsafe-project-path" in lowered:
        return "path-safety"
    if "existing harness database is unreadable" in lowered:
        return "runtime-error"
    codes = {
        "python": "python-unavailable",
        "git": "git-unavailable",
        "project root": "project-root-missing",
        "git project": "git-project-invalid",
        "harness initialized": "not-initialized",
        "runtime gitignore": "runtime-gitignore",
        "local-only runtime boundary": "local-only-boundary",
    }
    return codes.get(name, "project-doctor-issue")


def project_doctor_operator_report(report: dict[str, Any]) -> dict[str, Any]:
    """Project complete wrapper-specific checks into the shared public shape."""

    runtime = report["runtime"]
    blockers = [dict(blocker) for blocker in runtime["blockers"]]
    compatibility_checks = {"harness initialized", "runtime gitignore"}
    failed = [
        check
        for check in report["checks"]
        if not check["ok"] and check["name"] not in compatibility_checks
    ]
    blockers.extend(
        {
            "code": _project_doctor_check_code(
                str(check["name"]),
                str(check["details"]),
            ),
            "message": (
                f"{check['name']}: "
                + "; ".join(
                    line.strip()
                    for line in str(check["details"]).splitlines()
                    if line.strip()
                )
            ),
        }
        for check in failed
    )
    priority = {
        "rollback-incomplete": 0,
        "recovery-required": 0,
        "migration-in-progress": 0,
        "path-safety": 0,
        "project-root-missing": 0,
        "sqlite-integrity": 1,
        "foreign-key-integrity": 1,
        "runtime-error": 1,
        "state-missing": 2,
        "not-initialized": 2,
        "schema-version-mismatch": 2,
        "runtime-version-mismatch": 2,
        "doctor-issue": 2,
        "projection-invalid": 3,
        "gitignore-missing": 4,
        "runtime-gitignore": 4,
        "python-unavailable": 4,
        "git-unavailable": 4,
        "git-project-invalid": 4,
        "local-only-boundary": 4,
    }
    blockers = [
        blocker
        for _, blocker in sorted(
            enumerate(blockers),
            key=lambda item: (priority.get(item[1]["code"], 2), item[0]),
        )
    ]
    top_code = blockers[0]["code"] if blockers else ""
    recovery = top_code in {
        "rollback-incomplete",
        "recovery-required",
        "migration-in-progress",
        "path-safety",
    }
    state = (
        "healthy"
        if report["ok"] and not blockers
        else "recovery-required"
        if recovery
        else "error"
        if runtime["state"] == "error" or top_code == "runtime-error"
        else "not-initialized"
        if runtime["state"] == "not-initialized" or top_code == "not-initialized"
        else "unhealthy"
    )
    return {
        "state": state,
        "blockers": blockers,
        "actions": (
            []
            if recovery or state == "error"
            else list(report["next_commands"])
        ),
        "details": report,
    }


def project_doctor_verbose_lines(envelope: dict[str, Any]) -> list[str]:
    """Render complete public doctor detail without rerunning any probe."""

    report = envelope["details"]
    compatibility_checks = {"harness initialized", "runtime gitignore"}
    lines = [
        f"{'OK' if check['ok'] else 'ERROR'}: {check['name']}: {check['details']}"
        for check in report["checks"]
        if check["ok"] or check["name"] not in compatibility_checks
    ]
    lines.extend(
        f"ERROR: runtime: [{blocker['code']}] {blocker['message']}"
        for blocker in envelope["blockers"]
    )
    lines.extend(f"NEXT: {action}" for action in envelope["actions"])
    return lines


def _load_project_runtime_api(
    authority: ProjectRuntimeAuthority | None = None,
) -> Any:
    selected = authority or resolve_project_runtime_authority()
    current = validate_project_runtime_root(
        selected.root,
        label="installed codex-project-harness runtime",
    )
    if current.digest != selected.digest:
        raise KafaError(
            "installed codex-project-harness runtime changed after validation"
        )
    plugin_root = current.root
    expected = (plugin_root / "core" / "api.py").resolve()
    added: list[str] = []
    for path in (plugin_root / "scripts", plugin_root):
        value = str(path)
        if value not in sys.path:
            sys.path.insert(0, value)
            added.append(value)
    try:
        importlib.invalidate_caches()
        runtime_api = importlib.import_module("core.api")
    except (ImportError, OSError) as exc:
        raise KafaError(
            f"installed project runtime could not be loaded from {plugin_root}: {exc}"
        ) from exc
    finally:
        for value in added:
            try:
                sys.path.remove(value)
            except ValueError:
                pass
    actual_value = getattr(runtime_api, "__file__", "")
    try:
        actual = Path(str(actual_value)).resolve(strict=True)
    except OSError as exc:
        raise KafaError(
            f"installed project runtime identity is unavailable: {actual_value}"
        ) from exc
    if actual != expected:
        raise KafaError(
            "installed project runtime identity mismatch: "
            f"loaded={actual} expected={expected}; restart the Kafa command after upgrading"
        )
    return runtime_api


def harness_project_initialized(
    root: Path,
    *,
    authority: ProjectRuntimeAuthority | None = None,
) -> bool:
    runtime_api = _load_project_runtime_api(authority)
    try:
        return bool(runtime_api.runtime_initialized(root))
    except runtime_api.HarnessError as exc:
        raise KafaError(str(exc)) from exc


def harness_project_doctor_probe(
    root: Path,
    *,
    authority: ProjectRuntimeAuthority | None = None,
) -> dict[str, object]:
    runtime_api = _load_project_runtime_api(authority)
    try:
        probe = runtime_api.project_doctor_probe(root)
    except runtime_api.HarnessError as exc:
        raise KafaError(str(exc)) from exc
    if not isinstance(probe, dict):
        raise KafaError("installed project runtime returned an invalid doctor probe")
    return {str(key): value for key, value in probe.items()}


def local_only_runtime_boundary(
    source: Path,
    *,
    require_package_metadata: bool = True,
) -> tuple[bool, str]:
    failures: list[str] = []
    for retired in RETIRED_CORE_FILES:
        if (source / "core" / retired).exists():
            failures.append(f"retired core file exists: {retired}")

    _, source_failures = _runtime_python_source_issues(source)
    failures.extend(source_failures)

    if require_package_metadata:
        pyproject = source.parent.parent / "pyproject.toml"
        try:
            package = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            failures.append(f"pyproject unreadable: {exc}")
        else:
            project = package.get("project", {}) if isinstance(package, dict) else {}
            optional = project.get("optional-dependencies", {}) if isinstance(project, dict) else {}
            flattened = (
                [str(item).lower() for values in optional.values() if isinstance(values, list) for item in values]
                if isinstance(optional, dict)
                else []
            )
            if isinstance(optional, dict) and (
                "host-codex" in optional or any("openai-codex" in item for item in flattened)
            ):
                failures.append("retired Host Codex SDK dependency exists")

    if failures:
        return False, "; ".join(failures[:6])
    return True, "local files only; no Connector, provider SDK, token, or network-call runtime"


def control_plane_contract(source: Path) -> tuple[bool, str]:
    failures: list[str] = []
    layers = [
        "Skill Entry",
        "Workflow Presentation",
        "Plugin Distribution",
        "Hooks Advisory Layer",
        "Local Runtime Boundary",
        "Kernel Trust Layer",
        "Local Eval Boundary",
    ]

    distribution: dict[str, Any] | None = None
    try:
        distribution = load_distribution_manifest(source)
    except KafaError as exc:
        failures.append(f"Plugin Distribution: {exc}")

    workflow_contract: dict[str, Any] | None = None
    workflow_path = source / "references" / "workflow-contract.json"
    try:
        loaded_workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        failures.append(f"Workflow Presentation: contract unreadable: {exc}")
    else:
        if not isinstance(loaded_workflow, dict) or loaded_workflow.get("contract_version") != 1:
            failures.append("Workflow Presentation: contract must be a version-1 object")
        else:
            workflow_contract = loaded_workflow
            authority_ids = {
                item.get("id")
                for item in loaded_workflow.get("authorities", [])
                if isinstance(item, dict)
            }
            safeguard_ids = {
                item.get("id")
                for item in loaded_workflow.get("safeguards", [])
                if isinstance(item, dict)
            }
            route_ids = {
                item.get("id")
                for item in loaded_workflow.get("routes", [])
                if isinstance(item, dict)
            }
            missing_authorities = {
                "openspec",
                "sqlite",
                "delivery-evaluator",
                "workflow-contract",
                "native-host",
                "root-controller",
            } - authority_ids
            missing_safeguards = {
                "local-only",
                "root-controller-single-writer",
                "native-host-lifecycle",
                "immutable-execution",
                "current-candidate-verification",
                "fail-closed-delivery-gate",
            } - safeguard_ids
            expected_routes = {
                "project-harness",
                "minimal-safe-change",
                "bug-fix-loop",
                "test-first-delivery",
                "independent-quality-gate",
                "harness-audit",
                "project-retrospective",
            }
            if missing_authorities:
                failures.append(
                    "Workflow Presentation: missing authorities "
                    f"{sorted(missing_authorities)}"
                )
            if missing_safeguards:
                failures.append(
                    "Workflow Presentation: missing safeguards "
                    f"{sorted(missing_safeguards)}"
                )
            if route_ids != expected_routes:
                failures.append(
                    "Workflow Presentation: route inventory mismatch "
                    f"missing={sorted(expected_routes - route_ids)} "
                    f"extra={sorted(route_ids - expected_routes)}"
                )

    skill_path = source / "skills" / "project-harness" / "SKILL.md"
    skill_text = read_text(skill_path)
    if not skill_text:
        failures.append(f"Skill Entry: missing {skill_path}")
    elif workflow_contract is not None:
        dynamic_markers = [
            "BEGIN GENERATED: workflow-contract:entry-workflow",
            "END GENERATED: workflow-contract:entry-workflow",
        ]
        for collection, fields in (
            ("safeguards", ("id", "rule")),
            ("routes", ("id", "when", "obligation")),
        ):
            for item in workflow_contract.get(collection, []):
                if not isinstance(item, dict):
                    failures.append(
                        f"Workflow Presentation: {collection} entry must be an object"
                    )
                    continue
                for field in fields:
                    value = item.get(field)
                    if not isinstance(value, str) or not value:
                        failures.append(
                            f"Workflow Presentation: {collection}.{field} must be non-empty"
                        )
                    else:
                        dynamic_markers.append(value)
        review_label = workflow_contract.get("output_labels", {}).get(
            "human_review_required"
        )
        if isinstance(review_label, str) and review_label:
            dynamic_markers.append(review_label)
        else:
            failures.append(
                "Workflow Presentation: missing human_review_required output label"
            )
        for marker in dynamic_markers:
            if marker not in skill_text:
                failures.append(
                    f"Skill Entry: generated workflow marker missing {marker!r}"
                )

    manifest = source / ".codex-plugin" / "plugin.json"
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if data.get("skills") != "./skills/":
                failures.append("Plugin Distribution: plugin manifest must point skills at ./skills/")
            description = str(data.get("description", ""))
            long_description = str(data.get("interface", {}).get("longDescription", ""))
            if "local-only verified delivery kernel" not in description:
                failures.append("Plugin Distribution: manifest must declare the local-only verified delivery kernel")
            if "does not perform external tool writes" not in description:
                failures.append("Plugin Distribution: manifest must declare the no-external-write boundary")
            if "does not perform deployment" not in description and "不执行生产部署" not in long_description:
                failures.append("Plugin Distribution: manifest must declare verified-handoff/deployment boundary")
        except (OSError, json.JSONDecodeError) as exc:
            failures.append(f"Plugin Distribution: manifest unreadable: {exc}")
    else:
        failures.append(f"Plugin Distribution: missing {manifest}")

    hook_runner_path: Path | None = None
    if distribution is not None:
        hook_ok, hook_details = static_hook_definition(
            source,
            distribution=distribution,
        )
        if not hook_ok:
            failures.append(f"Hooks Advisory Layer: {hook_details}")
        hook_runners = [
            name
            for name in distribution["hooks"]["files"]
            if Path(name).suffix == ".py"
        ]
        if len(hook_runners) != 1:
            failures.append(
                "Hooks Advisory Layer: manifest requires one Python Hook runner"
            )
        else:
            hook_runner_path = source / "hooks" / hook_runners[0]

    required_markers = [
        (
            "Kernel Trust Layer",
            source / "core" / "delivery.py",
            [
                "def evaluate_schema30_delivery_readiness",
                "human-review-required",
            ],
        ),
        (
            "Local Eval Boundary",
            source / "scripts" / "run_agent_e2e_eval.py",
            [
                "scenario_sqlite_contention_stress",
                "\"stability\": run_stability",
                "false_pass_count",
            ],
        ),
    ]
    if hook_runner_path is not None:
        required_markers.insert(
            0,
            (
                "Hooks Advisory Layer",
                hook_runner_path,
                [
                    "Hooks are advisory",
                    "never create delivery facts or evidence",
                    "Stop is warn-only",
                ],
            ),
        )
    for layer, path, markers in required_markers:
        text = read_text(path)
        if not text:
            failures.append(f"{layer}: missing {path}")
            continue
        for marker in markers:
            if marker not in text:
                failures.append(f"{layer}: missing marker {marker!r} in {path.name}")

    local_only_ok, local_only_details = local_only_runtime_boundary(source)
    if not local_only_ok:
        failures.append(f"Local Runtime Boundary: {local_only_details}")

    if failures:
        return False, "; ".join(failures[:6])
    return True, ", ".join(layers)


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def add_check(checks: list[dict[str, Any]], name: str, ok: bool, details: str) -> None:
    checks.append({"name": name, "ok": bool(ok), "details": details})


if __name__ == "__main__":
    raise SystemExit(main())

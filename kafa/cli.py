"""Command line installer for Codex Project Harness."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tomllib
from contextlib import closing
from pathlib import Path
from typing import Any

from . import __version__


PLUGIN_NAME = "codex-project-harness"
DEFAULT_MARKETPLACE_NAME = "kafa-local"
DISPLAY_NAME = "Kafa Local Plugins"
PLUGIN_CATEGORY = "Developer Tools"
REPO_VERSION_FILE = Path(__file__).resolve().parents[1] / "VERSION"

REQUIRED_SKILLS = (
    "project-harness",
    "project-bootstrap",
    "project-runtime",
    "requirement-baseline",
    "team-architecture",
    "minimal-safe-change",
    "test-first-delivery",
    "bug-fix-loop",
    "independent-quality-gate",
    "delivery-readiness",
    "harness-audit",
    "project-retrospective",
)
REQUIRED_REFERENCES = ("collaboration-tools.md", "tool-adapters.md")
REQUIRED_CORE = (
    "__init__.py", "api.py",
)
REQUIRED_SCRIPTS = (
    "init_project_harness.py", "validate_structure.py", "harness_lib.py", "harness_wrapper.py",
    "harness_status.py", "update_phase.py", "add_acceptance.py", "add_failure_mode.py", "add_task.py",
    "update_task.py", "record_decision.py", "record_validation.py", "record_quality_gate.py",
    "record_delivery.py", "validate_harness_state.py", "harness_db.py", "harness.py",
    "run_runtime_smoke.py", "run_forward_eval.py", "run_skill_eval.py", "run_agent_e2e_eval.py",
)
REQUIRED_HOOKS = ("hooks.json", "harness_hook.py")
REQUIRED_SCHEMAS = (
    "project-state.schema.json", "delivery-cycle.schema.json", "requirement.schema.json",
    "acceptance.schema.json", "task.schema.json", "task-attempt.schema.json", "task-test-target.schema.json",
    "event.schema.json", "quality-gate.schema.json", "failure-mode.schema.json", "validation.schema.json",
    "evidence.schema.json", "test.schema.json", "test-target.schema.json", "finding.schema.json",
    "invalidation.schema.json", "delivery.schema.json", "adapter.schema.json", "adapter-action.schema.json",
    "connector-budget.schema.json", "connector-profile.schema.json", "advisory-fallback.schema.json",
    "ci-verification.schema.json", "command-log.schema.json", "codex-fanout-export.schema.json",
    "external-session-verification.schema.json", "agent.schema.json", "agent-session.schema.json",
    "session-attestation.schema.json", "baseline.schema.json", "dispatch-run.schema.json",
    "dispatch-assignment.schema.json", "dispatch-worktree.schema.json", "task-file-claim.schema.json",
    "agent-report.schema.json", "agent-provider-session.schema.json", "agent-provider-event.schema.json",
    "sandbox-execution.schema.json", "integration-attempt.schema.json", "runtime-snapshot.schema.json",
)


class KafaError(RuntimeError):
    """User-facing CLI error."""


def release_version() -> str:
    if REPO_VERSION_FILE.exists():
        return REPO_VERSION_FILE.read_text(encoding="utf-8").strip()
    return __version__


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        print(release_version())
        return 0
    if not args.command:
        parser.print_help()
        return 2
    try:
        if args.command == "doctor":
            return command_doctor(args)
        if args.command == "project":
            return command_project(args)
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

    project = sub.add_parser("project", help="Inspect an ordinary project using Kafa runtime state.")
    project_sub = project.add_subparsers(dest="project_command", required=True)
    project_doctor = project_sub.add_parser("doctor", help="Check a business project without requiring plugin source files.")
    project_doctor.add_argument("--repo", default=".", help="Project root. Defaults to the current directory.")
    project_doctor.add_argument("--json", action="store_true", help="Print machine-readable check results.")
    for name in ["init", "status"]:
        command = project_sub.add_parser(name, help=f"Run Harness {name} in an ordinary project.")
        command.add_argument("--repo", default=".", help="Project root. Defaults to the current directory.")
    project_quickstart = project_sub.add_parser("quickstart", help="Run Harness quickstart in an ordinary project.")
    project_quickstart.add_argument("--repo", default=".", help="Project root. Defaults to the current directory.")
    project_quickstart.add_argument("harness_args", nargs=argparse.REMAINDER)

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


def command_project(args: argparse.Namespace) -> int:
    repo = Path(args.repo).expanduser().resolve()
    if args.project_command == "doctor":
        report = project_doctor_report(repo)
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            for check in report["checks"]:
                prefix = "OK" if check["ok"] else "ERROR"
                print(f"{prefix}: {check['name']}: {check['details']}")
            for command in report["next_commands"]:
                print(f"NEXT: {command}")
        return 0 if report["ok"] else 1
    harness_args = [args.project_command]
    if args.project_command == "quickstart":
        if not args.harness_args:
            raise KafaError("project quickstart requires status or minimal arguments")
        harness_args = ["quickstart", *args.harness_args]
    return run_project_harness(repo, harness_args)


def installed_plugin_root(repo: Path) -> Path:
    candidates = []
    env_root = os.environ.get("CODEX_PROJECT_HARNESS_PLUGIN_ROOT", "").strip()
    if env_root:
        candidates.append(Path(env_root).expanduser())
    candidates.extend(
        [
            REPO_VERSION_FILE.parent / "plugins" / PLUGIN_NAME,
            Path(os.environ.get("HOME", str(Path.home()))).expanduser() / ".agents" / "plugins" / PLUGIN_NAME,
            repo / "plugins" / PLUGIN_NAME,
        ]
    )
    for candidate in candidates:
        resolved = candidate.resolve()
        if (resolved / "scripts" / "harness.py").exists():
            return resolved
    raise KafaError("installed codex-project-harness runtime not found; install the user plugin first")


def run_project_harness(repo: Path, harness_args: list[str]) -> int:
    plugin_root = installed_plugin_root(repo)
    command = [sys.executable, str(plugin_root / "scripts" / "harness.py"), "--root", str(repo), *harness_args]
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.stdout:
        print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr)
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
    manifest = source / ".codex-plugin" / "plugin.json"
    if not manifest.exists():
        raise KafaError(f"plugin manifest not found: {manifest}")
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise KafaError(f"invalid plugin manifest: {exc}") from exc
    if data.get("name") != PLUGIN_NAME:
        raise KafaError(f"plugin manifest name must be {PLUGIN_NAME}")
    version_file = repo / "VERSION"
    if version_file.exists() and data.get("version") != version_file.read_text(encoding="utf-8").strip():
        raise KafaError("plugin manifest version must match repo VERSION")


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
    structure_ok, structure_details = static_plugin_structure(source)
    add_check(checks, "plugin structure", structure_ok, structure_details)
    contract_ok, contract_details = control_plane_contract(source)
    add_check(checks, "control plane contract", contract_ok, contract_details)
    add_check(checks, "connector namespace boundary", True, "installer does not create external workspaces; per-project connector profile lives in harness runtime state")
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


def static_plugin_structure(source: Path) -> tuple[bool, str]:
    errors: list[str] = []
    repo_root = source.parent.parent
    manifest_path = source / ".codex-plugin" / "plugin.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("plugin manifest root must be an object")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return False, f"invalid plugin manifest: {exc}"

    version_path = repo_root / "VERSION"
    version = read_text(version_path).strip()
    if manifest.get("name") != PLUGIN_NAME:
        errors.append(f"plugin name must be {PLUGIN_NAME}")
    if version and manifest.get("version") != version:
        errors.append("plugin version must match root VERSION")
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
    expected_package_version = version.replace("-beta.", "b") if "-beta." in version else version
    if project.get("name") != "kafa":
        errors.append("pyproject project.name must be kafa")
    if version and project.get("version") != expected_package_version:
        errors.append("pyproject version must match root VERSION")
    if project.get("requires-python") != ">=3.11":
        errors.append("pyproject requires-python must be >=3.11")
    dependencies = project.get("dependencies", [])
    if not isinstance(dependencies, list) or dependencies:
        errors.append("pyproject base dependencies must remain empty")
    optional_dependencies = project.get("optional-dependencies", {})
    host_codex = optional_dependencies.get("host-codex", []) if isinstance(optional_dependencies, dict) else []
    if not isinstance(host_codex, list) or "openai-codex>=0.1.0b3" not in host_codex:
        errors.append("pyproject optional dependency host-codex must include openai-codex>=0.1.0b3")
    if not isinstance(project.get("scripts"), dict) or project["scripts"].get("kafa") != "kafa.cli:main":
        errors.append("pyproject must expose kafa = kafa.cli:main")

    skills_root = source / "skills"
    for skill in REQUIRED_SKILLS:
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
    if actual_skills != set(REQUIRED_SKILLS):
        errors.append(f"skill inventory mismatch: {sorted(actual_skills ^ set(REQUIRED_SKILLS))}")

    for reference in REQUIRED_REFERENCES:
        if not (source / "references" / reference).is_file():
            errors.append(f"missing reference: {reference}")
    check_required_file_inventory(errors, source / "core", REQUIRED_CORE, ".py", "core")
    errors.extend(local_python_import_errors(source))
    check_exact_file_inventory(errors, source / "scripts", REQUIRED_SCRIPTS, ".py", "scripts")
    check_exact_file_inventory(errors, source / "hooks", REQUIRED_HOOKS, "", "hooks")
    check_exact_file_inventory(errors, source / "schemas", REQUIRED_SCHEMAS, ".json", "schemas")
    if not (source / "skills" / "project-runtime" / "scripts" / "harness.py").is_file():
        errors.append("missing project-runtime self-contained CLI")
    for schema in REQUIRED_SCHEMAS:
        try:
            json.loads((source / "schemas" / schema).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"invalid schema {schema}: {exc}")
    try:
        json.loads((source / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"invalid hooks.json: {exc}")
    if (source / "skills" / "release-readiness" / "SKILL.md").exists() or (source / "templates" / "agents" / "release-engineer.toml").exists():
        errors.append("stale delivery-only replacement exists")

    return (not errors, "complete static plugin contract" if not errors else "; ".join(errors[:6]))


def directory_names(root: Path) -> set[str]:
    try:
        return {path.name for path in root.iterdir() if path.is_dir() and not path_is_link(path)}
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
            if path.is_file() and not path_is_link(path) and (not suffix or path.suffix == suffix)
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


def local_python_import_errors(source: Path) -> list[str]:
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
    errors: set[str] = set()
    for path in source_paths:
        if path_is_link(path):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError) as exc:
            errors.add(f"invalid Python source: {path.relative_to(source)}: {exc}")
            continue
        for node in ast.walk(tree):
            module = ""
            if isinstance(node, ast.ImportFrom):
                if node.level and path.parent == core_root and node.module:
                    module = node.module.split(".", 1)[0]
                elif node.module and node.module.startswith("core."):
                    module = node.module.split(".", 2)[1]
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("core."):
                        module = alias.name.split(".", 2)[1]
                        if module not in available_core:
                            errors.add(
                                f"missing local Python import: core.{module} referenced by {path.relative_to(source)}"
                            )
                continue
            if module and module not in available_core:
                errors.add(f"missing local Python import: core.{module} referenced by {path.relative_to(source)}")
    return sorted(errors)


def static_hook_definition(plugin_root: Path) -> tuple[bool, str]:
    hooks_path = plugin_root / "hooks" / "hooks.json"
    try:
        payload = json.loads(hooks_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"invalid hooks.json: {exc}"
    hooks = payload.get("hooks", {}) if isinstance(payload, dict) else {}
    expected = {"SessionStart", "SubagentStart", "PreToolUse", "PostToolUse", "Stop"}
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
                if "${PLUGIN_ROOT}" not in str(hook.get("command", "")):
                    return False, f"{event}: POSIX command does not use PLUGIN_ROOT"
                if "%PLUGIN_ROOT%" not in str(hook.get("commandWindows", "")):
                    return False, f"{event}: Windows command does not use PLUGIN_ROOT"
    return True, "five lifecycle events use installed PLUGIN_ROOT commands"


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


def project_doctor_report(repo: Path) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    next_commands: list[str] = []
    add_check(checks, "python", sys.version_info >= (3, 11), f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
    add_check(checks, "git", shutil.which("git") is not None, shutil.which("git") or "not found")
    add_check(checks, "project root", repo.exists(), str(repo))
    if repo.exists() and shutil.which("git"):
        completed = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=repo, text=True, capture_output=True, check=False)
        add_check(checks, "git project", completed.returncode == 0 and completed.stdout.strip() == "true", "git repo" if completed.returncode == 0 else "not a git repo")
    else:
        add_check(checks, "git project", False, "project root or git missing")

    db_path = repo / ".ai-team" / "state" / "harness.db"
    initialized = harness_project_initialized(db_path)
    add_check(checks, "harness initialized", initialized, str(db_path) if initialized else f"missing initialized runtime at {db_path}")
    if not initialized:
        next_commands.append(f"kafa project init --repo {shlex.quote(str(repo))}")
        next_commands.append(f"kafa project quickstart --repo {shlex.quote(str(repo))} status")
    else:
        next_commands.append(f"kafa project quickstart --repo {shlex.quote(str(repo))} status")

    gitignore = repo / ".gitignore"
    ignored = True
    details = "runtime state should stay out of git"
    if repo.exists() and gitignore.exists():
        text = gitignore.read_text(encoding="utf-8")
        required = [".ai-team/state/", ".ai-team/runtime/"]
        missing = [pattern for pattern in required if pattern not in text]
        ignored = not missing
        details = "ok" if ignored else "missing " + ", ".join(missing)
    elif repo.exists():
        ignored = False
        details = "missing .gitignore runtime rules"
    add_check(checks, "runtime gitignore", ignored, details)
    add_check(checks, "connector namespace boundary", True, "connector profiles are per project; project doctor does not create external workspaces")
    return {"ok": all(check["ok"] for check in checks), "kind": "project", "repo": str(repo), "checks": checks, "next_commands": next_commands}


def harness_project_initialized(db_path: Path) -> bool:
    if not db_path.exists():
        return False
    try:
        import sqlite3

        with closing(sqlite3.connect(db_path)) as conn:
            exists = conn.execute("select 1 from sqlite_master where type='table' and name='project'").fetchone()
            if not exists:
                return False
            return conn.execute("select 1 from project where id = 1").fetchone() is not None
    except sqlite3.Error:
        return False


def control_plane_contract(source: Path) -> tuple[bool, str]:
    failures: list[str] = []
    layers = [
        "Skill Entry",
        "Plugin Distribution",
        "Hooks Advisory Layer",
        "Host Bridge/Provider Layer",
        "Kernel Trust Layer",
        "Connector/Eval Boundary",
    ]

    manifest = source / ".codex-plugin" / "plugin.json"
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if data.get("skills") != "./skills/":
                failures.append("Plugin Distribution: plugin manifest must point skills at ./skills/")
            description = " ".join(str(data.get(key, "")) for key in ["description"])
            long_description = str(data.get("interface", {}).get("longDescription", ""))
            if "does not perform deployment" not in description and "不执行生产部署" not in long_description:
                failures.append("Plugin Distribution: manifest must declare verified-handoff/deployment boundary")
        except (OSError, json.JSONDecodeError) as exc:
            failures.append(f"Plugin Distribution: manifest unreadable: {exc}")
    else:
        failures.append(f"Plugin Distribution: missing {manifest}")

    expected_hooks = {"SessionStart", "SubagentStart", "PreToolUse", "PostToolUse", "Stop"}
    hooks_json = source / "hooks" / "hooks.json"
    if hooks_json.exists():
        try:
            hook_data = json.loads(hooks_json.read_text(encoding="utf-8"))
            actual_hooks = set(hook_data.get("hooks", {}))
            missing_hooks = sorted(expected_hooks - actual_hooks)
            if missing_hooks:
                failures.append(f"Hooks Advisory Layer: missing hook events {missing_hooks}")
        except (OSError, json.JSONDecodeError) as exc:
            failures.append(f"Hooks Advisory Layer: hooks.json unreadable: {exc}")
    else:
        failures.append(f"Hooks Advisory Layer: missing {hooks_json}")

    required_markers = [
        (
            "Skill Entry",
            source / "skills" / "project-runtime" / "SKILL.md",
            [
                "natural-language Skill Entry",
                "SQLite-backed harness runtime",
                "Markdown files are generated views, not the primary fact source",
            ],
        ),
        (
            "Hooks Advisory Layer",
            source / "hooks" / "harness_hook.py",
            ["Hooks are advisory lifecycle guardrails", "never create delivery evidence"],
        ),
        (
            "Host Bridge/Provider Layer",
            source / "core" / "agent_provider.py",
            ["class HostCodexProvider", "delivery evidence"],
        ),
        (
            "Kernel Trust Layer",
            source / "scripts" / "harness_db.py",
            [
                "insert into agent_reports",
                "insert into task_attempts",
                "def dispatch_verify_attempt",
                "status = 'verified'",
                "def execute_connector_action",
            ],
        ),
        (
            "Connector/Eval Boundary",
            source / "scripts" / "run_agent_e2e_eval.py",
            [
                "scenario_host_codex_fake_sdk_e2e",
                "scenario_connector_mock_server_e2e",
                "scenario_crash_retry_recovery",
                "scenario_sqlite_contention_stress",
                "\"stability\": run_stability",
            ],
        ),
    ]
    for layer, path, markers in required_markers:
        text = read_text(path)
        if not text:
            failures.append(f"{layer}: missing {path}")
            continue
        for marker in markers:
            if marker not in text:
                failures.append(f"{layer}: missing marker {marker!r} in {path.name}")

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

"""Command line installer for Codex Project Harness."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import __version__


PLUGIN_NAME = "codex-project-harness"
DEFAULT_MARKETPLACE_NAME = "kafa-local"
DISPLAY_NAME = "Kafa Local Plugins"
PLUGIN_CATEGORY = "Developer Tools"
REPO_VERSION_FILE = Path(__file__).resolve().parents[1] / "VERSION"


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
        "./codex-project-harness",
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
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            version = (repo / "VERSION").read_text(encoding="utf-8").strip() if (repo / "VERSION").exists() else ""
            add_check(checks, "plugin name", data.get("name") == PLUGIN_NAME, str(data.get("name", "")))
            add_check(checks, "plugin version", not version or data.get("version") == version, f"plugin={data.get('version', '')} repo={version}")
        except (OSError, json.JSONDecodeError) as exc:
            add_check(checks, "plugin metadata", False, str(exc))
    validate = source / "scripts" / "validate_structure.py"
    if validate.exists():
        completed = subprocess.run([sys.executable, str(validate), str(source)], text=True, capture_output=True, check=False)
        add_check(checks, "plugin structure", completed.returncode == 0, (completed.stdout or completed.stderr).strip())
    else:
        add_check(checks, "plugin structure", False, f"missing {validate}")
    contract_ok, contract_details = control_plane_contract(source)
    add_check(checks, "control plane contract", contract_ok, contract_details)
    marketplace_path, _target, _source_path = marketplace_locations(repo, scope)
    add_check(checks, "marketplace path", True, str(marketplace_path))
    return {"ok": all(check["ok"] for check in checks), "scope": scope, "repo": str(repo), "checks": checks}


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
                "scenario_host_codex_fake_app_server_e2e",
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

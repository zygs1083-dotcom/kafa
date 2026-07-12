#!/usr/bin/env python3
"""Run a real wheel, Codex plugin, cache hook, doctor, and removal smoke."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import venv
from contextlib import closing
from pathlib import Path
from typing import Any

SCRIPT_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_REPO_ROOT))

from kafa.codex_app_server import (
    APPROVED_AGENT_TEMPLATES,
    AppServerClient,
    validate_app_server_discovery,
)


PLUGIN_ID = "codex-project-harness@kafa-local"


def read_quickstart_facts(database: Path) -> tuple[tuple[int, int, int, int], str]:
    with closing(sqlite3.connect(database)) as conn:
        facts = tuple(
            int(conn.execute(f"select count(*) from {table}").fetchone()[0])
            for table in ("executions", "validations", "quality_gates", "deliveries")
        )
        task_status = str(
            conn.execute(
                "select status from tasks where id='INSTALL-T1'"
            ).fetchone()[0]
        )
    return facts, task_status


def discover_with_app_server(codex: str, *, env: dict[str, str], cwd: Path) -> dict[str, Any]:
    client = AppServerClient(codex_command(codex, "app-server", "--stdio"), env=env, cwd=cwd)
    try:
        initialized = client.request(
            "initialize",
            {
                "clientInfo": {"name": "kafa-isolated-install-smoke", "version": "1"},
                "capabilities": {"experimentalApi": True},
            },
        )
        client.notify("initialized", {})
        return {
            "initialize": initialized,
            "skills": client.request("skills/list", {"cwds": [str(cwd)], "forceReload": True}),
            "hooks": client.request("hooks/list", {"cwds": [str(cwd)]}),
            "plugin": client.request("plugin/installed", {"cwds": [str(cwd)]}),
            "notifications": client.notifications,
        }
    finally:
        client.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".")
    parser.add_argument("--codex-bin", default="")
    parser.add_argument("--wheel", default="")
    parser.add_argument("--source-archive", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    source_repo = Path(args.repo).expanduser().resolve()
    try:
        report = run_smoke(source_repo, args.codex_bin, args.wheel, args.source_archive)
    except Exception as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2, sort_keys=True))
        else:
            print(f"ERROR: {exc}")
        return 1
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"OK: isolated install smoke passed with {report['codex_version']}")
    return 0


def run_smoke(
    source_repo: Path,
    codex_value: str = "",
    wheel_value: str = "",
    source_archive_value: str = "",
) -> dict[str, Any]:
    manifest = json.loads((source_repo / "release.json").read_text(encoding="utf-8"))
    version = str(manifest["version"])
    runtime_version = str(manifest["runtime_version"])
    schema_version = int(manifest["schema_version_runtime"])
    expected_codex_version = f"codex-cli {manifest['codex_cli_smoke_version']}"
    codex = str(Path(codex_value).expanduser().resolve()) if codex_value else shutil.which("codex")
    if not codex:
        raise RuntimeError("pinned Codex CLI is not on PATH")

    clean_env = os.environ.copy()
    clean_env.pop("PYTHONPATH", None)
    if bool(wheel_value) != bool(source_archive_value):
        raise RuntimeError("--wheel and --source-archive must be provided together")
    artifact_mode = bool(wheel_value)
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        if artifact_mode:
            wheel = Path(wheel_value).expanduser().resolve()
            source_archive = Path(source_archive_value).expanduser().resolve()
            if not wheel.is_file() or not source_archive.is_file():
                raise RuntimeError(f"release artifacts missing: wheel={wheel} source={source_archive}")
            release_repo = extract_source_archive(source_archive, root / "release-artifact")
        else:
            release_repo = root / "release-source"
            shutil.copytree(
                source_repo,
                release_repo,
                ignore=shutil.ignore_patterns(".git", ".venv", ".ai-team", "build", "*.egg-info", "__pycache__", "*.pyc"),
            )
            dist = root / "dist"
            dist.mkdir()
            run(
                [sys.executable, "-m", "pip", "wheel", "--no-deps", ".", "--wheel-dir", str(dist)],
                env=clean_env,
                cwd=release_repo,
            )
            wheel = next(dist.glob("kafa-*.whl"))
        artifact_manifest = json.loads((release_repo / "release.json").read_text(encoding="utf-8"))
        for field in ["version", "pep440_version", "tag", "package", "plugin"]:
            if artifact_manifest.get(field) != manifest.get(field):
                raise RuntimeError(
                    f"source artifact manifest mismatch for {field}: "
                    f"artifact={artifact_manifest.get(field)} checkout={manifest.get(field)}"
                )

        venv_root = root / "venv"
        venv.EnvBuilder(with_pip=True).create(venv_root)
        venv_python = venv_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        run([str(venv_python), "-m", "pip", "install", "--no-deps", str(wheel)], env=clean_env, cwd=root)
        module_info = json.loads(
            run(
                [
                    str(venv_python),
                    "-c",
                    "import json, kafa; print(json.dumps({'path': kafa.__file__, 'version': kafa.__version__}))",
                ],
                env=clean_env,
                cwd=root,
            )
        )
        imported_from = Path(module_info["path"]).resolve()
        if not imported_from.is_relative_to(venv_root.resolve()):
            raise RuntimeError(f"wheel smoke imported checkout module: {imported_from}")
        if module_info["version"] != version:
            raise RuntimeError(f"wheel version mismatch: actual={module_info['version']} expected={version}")
        codex_version = run(codex_command(codex, "--version"), env=clean_env, cwd=root).strip()
        if codex_version != expected_codex_version:
            raise RuntimeError(f"Codex CLI version mismatch: actual={codex_version} expected={expected_codex_version}")

        env = clean_env.copy()
        env["HOME"] = str(root / "home")
        env["CODEX_HOME"] = str(root / "codex-home")
        env["PATH"] = str(venv_python.parent) + os.pathsep + env.get("PATH", "")
        Path(env["HOME"]).mkdir()
        Path(env["CODEX_HOME"]).mkdir()
        kafa = [str(venv_python), "-m", "kafa.cli"]

        run([*kafa, "plugin", "install", "--scope", "user", "--repo", str(release_repo)], env=env, cwd=root)
        marketplaces = json.loads(run(codex_command(codex, "plugin", "marketplace", "list", "--json"), env=env, cwd=root))
        if not any(item.get("name") == "kafa-local" for item in marketplaces["marketplaces"]):
            raise RuntimeError(f"personal marketplace not discovered: {marketplaces}")
        available = json.loads(run(codex_command(codex, "plugin", "list", "--available", "--json"), env=env, cwd=root))
        if not any(item.get("pluginId") == "codex-project-harness@kafa-local" for item in available["available"]):
            raise RuntimeError(f"plugin not available: {available}")

        added = json.loads(run(codex_command(codex, "plugin", "add", PLUGIN_ID, "--json"), env=env, cwd=root))
        cache_root = Path(added["installedPath"])
        installed = json.loads(run(codex_command(codex, "plugin", "list", "--json"), env=env, cwd=root))["installed"]
        if not any(
            item.get("pluginId") == PLUGIN_ID
            and item.get("installed") is True
            and item.get("enabled") is True
            for item in installed
        ):
            raise RuntimeError(f"plugin not installed and enabled: {installed}")

        business_repo = root / "business"
        business_repo.mkdir()
        run(["git", "init"], env=env, cwd=business_repo)
        discovery = discover_with_app_server(codex, env=env, cwd=business_repo)
        discovery_report = validate_app_server_discovery(
            discovery,
            cache_root=cache_root,
            plugin_id=PLUGIN_ID,
            version=version,
        )

        project_init = run(
            [*kafa, "project", "init", "--repo", str(business_repo)],
            env=env,
            cwd=business_repo,
        )
        project_status = run(
            [*kafa, "project", "status", "--repo", str(business_repo)],
            env=env,
            cwd=business_repo,
        )
        if (
            f"schema_version: {schema_version}" not in project_status
            or f"runtime_version: {runtime_version}" not in project_status
        ):
            raise RuntimeError(
                "installed project status does not match release.json: "
                f"{project_status}"
            )
        installed_templates = {
            path.name for path in (business_repo / ".codex/agents").glob("*.toml")
        }
        if installed_templates != APPROVED_AGENT_TEMPLATES:
            raise RuntimeError(
                f"project agent template inventory mismatch: actual={sorted(installed_templates)} "
                f"expected={sorted(APPROVED_AGENT_TEMPLATES)}"
            )

        (business_repo / "test_quickstart.py").write_text(
            "import unittest\n\nclass InstalledTest(unittest.TestCase):\n"
            "    def test_installed_runtime(self):\n        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        installed_harness = cache_root / "scripts" / "harness.py"
        quickstart = run(
            [str(venv_python), str(installed_harness), "--root", str(business_repo), "quickstart", "minimal", "--id", "INSTALL", "--goal", "verify installed runtime", "--acceptance", "installed test passes", "--task", "run installed verification", "--test-command", "python -m unittest test_quickstart.py", "--execute"],
            env=env,
            cwd=business_repo,
        )
        quickstart_status = json.loads(run(
            [str(venv_python), str(installed_harness), "--root", str(business_repo), "quickstart", "status", "--json"],
            env=env,
            cwd=business_repo,
        ))
        quickstart_facts, quickstart_task_status = read_quickstart_facts(
            business_repo / ".ai-team/state/harness.db"
        )
        if "OK: quickstart minimal verified setup INSTALL" not in quickstart or quickstart_facts != (1, 1, 0, 0) or quickstart_task_status != "submitted" or quickstart_status["ready_for_delivery"] or "controller_execution" in quickstart_status["missing"]:
            raise RuntimeError(f"installed quickstart contract failed: facts={quickstart_facts} task={quickstart_task_status} status={quickstart_status} output={quickstart}")

        doctor = json.loads(run([*kafa, "doctor", "--scope", "user", "--repo", str(release_repo), "--json"], env=env, cwd=root))
        if doctor.get("ok") is not True:
            raise RuntimeError(f"installed doctor failed: {doctor}")
        checks = {item["name"]: item for item in doctor["checks"]}
        for name in ["hook definition", "codex plugin registration", "codex plugin cache"]:
            if checks.get(name, {}).get("ok") is not True:
                raise RuntimeError(f"installed doctor check failed: {name}: {checks.get(name)}")

        hook = json.loads((cache_root / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]["SessionStart"][0]["hooks"][0]
        command = hook["commandWindows"] if os.name == "nt" else hook["command"]
        hook_env = env.copy()
        hook_env["PLUGIN_ROOT"] = str(cache_root)
        hook_result = subprocess.run(
            command,
            input=json.dumps({"source": "ci-install-smoke"}),
            text=True,
            capture_output=True,
            cwd=business_repo,
            env=hook_env,
            shell=True,
            check=False,
        )
        if hook_result.returncode != 0 or f"version: {version}" not in hook_result.stdout:
            raise RuntimeError(f"installed cache hook failed: {hook_result.stdout}{hook_result.stderr}")

        run(codex_command(codex, "plugin", "remove", PLUGIN_ID, "--json"), env=env, cwd=root)
        removed = json.loads(run(codex_command(codex, "plugin", "list", "--json"), env=env, cwd=root))["installed"]
        if any(item.get("pluginId") == PLUGIN_ID for item in removed):
            raise RuntimeError(f"plugin remained installed after removal: {removed}")

        return {
            "ok": True,
            "version": version,
            "codex_version": codex_version,
            "wheel": wheel.name,
            "artifact_mode": artifact_mode,
            "imported_from_venv": True,
            "marketplace_discovered": True,
            "plugin_enabled": True,
            "app_server_discovery_ok": True,
            "app_server_plugin": discovery_report["plugin_id"],
            "app_server_plugin_version": discovery_report["plugin_local_version"],
            "app_server_skill_count": discovery_report["skill_count"],
            "app_server_skills": discovery_report["skill_names"],
            "app_server_hook_count": discovery_report["hook_count"],
            "app_server_hook_events": discovery_report["hook_events"],
            "app_server_template_count": discovery_report["template_count"],
            "app_server_templates": discovery_report["template_names"],
            "app_server_runtime_script_count": discovery_report["runtime_script_count"],
            "app_server_schema_count": discovery_report["schema_count"],
            "app_server_runtime_anchor_count": discovery_report["runtime_anchor_count"],
            "retired_runtime_absent": discovery_report["retired_runtime_absent"],
            "project_init_ok": "OK: project harness initialized" in project_init,
            "project_status_ok": True,
            "installed_quickstart_ok": True,
            "installed_quickstart_task_status": quickstart_task_status,
            "project_agent_templates": sorted(installed_templates),
            "doctor_ok": True,
            "cache_hook_ok": True,
            "direct_hook_handler_ok": True,
            "host_hook_execution_observed": False,
            "host_hook_execution_reason": "deterministic install smoke proves app-server discovery; host execution requires a live authenticated turn",
            "remove_ok": True,
        }


def run(command: list[str], *, env: dict[str, str], cwd: Path) -> str:
    completed = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(completed.stdout + completed.stderr)
    return completed.stdout


def codex_command(codex: str, *args: str, platform_name: str | None = None) -> list[str]:
    platform_name = os.name if platform_name is None else platform_name
    if platform_name == "nt" and Path(codex).suffix.lower() in {".cmd", ".bat"}:
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", codex, *args]
    return [codex, *args]


def extract_source_archive(archive: Path, target: Path) -> Path:
    target.mkdir(parents=True)
    target_root = target.resolve()
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        for member in members:
            destination = (target / member.name).resolve()
            if (
                not destination.is_relative_to(target_root)
                or member.issym()
                or member.islnk()
                or not (member.isfile() or member.isdir())
            ):
                raise RuntimeError(f"unsafe source archive member: {member.name}")
        bundle.extractall(target)
    manifests = list(target.glob("*/release.json"))
    if len(manifests) != 1:
        raise RuntimeError(f"source archive must contain one release root, found {len(manifests)}")
    return manifests[0].parent


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Run a real wheel, Codex plugin, cache hook, doctor, and removal smoke."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import venv
from pathlib import Path
from typing import Any


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

        added = json.loads(run(codex_command(codex, "plugin", "add", "codex-project-harness@kafa-local", "--json"), env=env, cwd=root))
        cache_root = Path(added["installedPath"])
        installed = json.loads(run(codex_command(codex, "plugin", "list", "--json"), env=env, cwd=root))["installed"]
        if not any(
            item.get("pluginId") == "codex-project-harness@kafa-local"
            and item.get("installed") is True
            and item.get("enabled") is True
            for item in installed
        ):
            raise RuntimeError(f"plugin not installed and enabled: {installed}")

        doctor = json.loads(run([*kafa, "doctor", "--scope", "user", "--repo", str(release_repo), "--json"], env=env, cwd=root))
        if doctor.get("ok") is not True:
            raise RuntimeError(f"installed doctor failed: {doctor}")
        checks = {item["name"]: item for item in doctor["checks"]}
        for name in ["hook definition", "codex plugin registration", "codex plugin cache"]:
            if checks.get(name, {}).get("ok") is not True:
                raise RuntimeError(f"installed doctor check failed: {name}: {checks.get(name)}")

        hook = json.loads((cache_root / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]["SessionStart"][0]["hooks"][0]
        command = hook["commandWindows"] if os.name == "nt" else hook["command"]
        business_repo = root / "business"
        business_repo.mkdir()
        run(["git", "init"], env=env, cwd=business_repo)
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

        run(codex_command(codex, "plugin", "remove", "codex-project-harness@kafa-local", "--json"), env=env, cwd=root)
        removed = json.loads(run(codex_command(codex, "plugin", "list", "--json"), env=env, cwd=root))["installed"]
        if any(item.get("pluginId") == "codex-project-harness@kafa-local" for item in removed):
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
            "doctor_ok": True,
            "cache_hook_ok": True,
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

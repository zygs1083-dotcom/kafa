#!/usr/bin/env python3
"""Run a real wheel, Codex plugin, cache hook, doctor, and removal smoke."""

from __future__ import annotations

import argparse
import json
import os
import queue
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import venv
from pathlib import Path
from typing import Any


PLUGIN_ID = "codex-project-harness@kafa-local"


class AppServerClient:
    """Minimal line-delimited JSON-RPC client for deterministic discovery calls."""

    def __init__(self, command: list[str], *, env: dict[str, str], cwd: Path, timeout: float = 30.0) -> None:
        self.timeout = timeout
        self.process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,
        )
        self.messages: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.stderr_lines: list[str] = []
        self.notifications: list[dict[str, Any]] = []
        self.next_id = 1
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                message = {"invalid_json": line.rstrip("\n")}
            self.messages.put(message)
        self.messages.put(None)

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        self.stderr_lines.extend(self.process.stderr.readlines())

    def _send(self, message: dict[str, Any]) -> None:
        if self.process.stdin is None or self.process.poll() is not None:
            raise RuntimeError(f"Codex app-server exited before request: {''.join(self.stderr_lines)[-2000:]}")
        self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"method": method, "params": params})

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        self._send({"id": request_id, "method": method, "params": params})
        while True:
            try:
                message = self.messages.get(timeout=self.timeout)
            except queue.Empty as exc:
                raise RuntimeError(
                    f"Codex app-server timed out waiting for {method}: {''.join(self.stderr_lines)[-2000:]}"
                ) from exc
            if message is None:
                raise RuntimeError(f"Codex app-server closed while waiting for {method}: {''.join(self.stderr_lines)[-2000:]}")
            if message.get("id") != request_id:
                self.notifications.append(message)
                continue
            if "error" in message:
                raise RuntimeError(f"Codex app-server {method} failed: {message['error']}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise RuntimeError(f"Codex app-server {method} returned invalid result: {message}")
            return result

    def close(self) -> None:
        if self.process.stdin is not None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)


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


def validate_app_server_discovery(
    discovery: dict[str, Any],
    *,
    cache_root: Path,
    plugin_id: str,
    version: str,
    expected_skills: set[str],
    expected_hook_events: set[str],
) -> dict[str, Any]:
    cache_root = cache_root.resolve()
    plugin_result = discovery.get("plugin", {})
    marketplace_errors = plugin_result.get("marketplaceLoadErrors", [])
    if marketplace_errors:
        raise RuntimeError(f"app-server marketplace load failed: {marketplace_errors}")
    plugins = [
        plugin
        for marketplace in plugin_result.get("marketplaces", [])
        for plugin in marketplace.get("plugins", [])
        if plugin.get("id") == plugin_id
    ]
    if len(plugins) != 1:
        raise RuntimeError(f"app-server plugin discovery mismatch: expected one {plugin_id}, found {plugins}")
    plugin = plugins[0]
    if plugin.get("installed") is not True or plugin.get("enabled") is not True:
        raise RuntimeError(f"app-server plugin is not installed and enabled: {plugin}")
    if plugin.get("localVersion") != version:
        raise RuntimeError(f"app-server plugin version mismatch: actual={plugin.get('localVersion')} expected={version}")

    skill_entries = discovery.get("skills", {}).get("data", [])
    skill_errors = [error for entry in skill_entries for error in entry.get("errors", [])]
    if skill_errors:
        raise RuntimeError(f"app-server skill discovery errors: {skill_errors}")
    skills = [
        skill
        for entry in skill_entries
        for skill in entry.get("skills", [])
        if str(skill.get("name", "")).startswith("codex-project-harness:")
    ]
    actual_skills = {str(skill.get("name", "")) for skill in skills}
    if actual_skills != expected_skills or len(skills) != len(expected_skills):
        raise RuntimeError(
            f"app-server skill discovery mismatch: actual={sorted(actual_skills)} expected={sorted(expected_skills)}"
        )
    for skill in skills:
        skill_path = Path(str(skill.get("path", ""))).resolve()
        if skill.get("enabled") is not True or skill.get("scope") != "user" or not skill_path.is_relative_to(cache_root):
            raise RuntimeError(f"app-server skill did not resolve from installed cache: {skill}")

    hook_entries = discovery.get("hooks", {}).get("data", [])
    hook_errors = [error for entry in hook_entries for error in entry.get("errors", [])]
    if hook_errors:
        raise RuntimeError(f"app-server hook discovery errors: {hook_errors}")
    hooks = [
        hook
        for entry in hook_entries
        for hook in entry.get("hooks", [])
        if hook.get("pluginId") == plugin_id
    ]
    actual_hook_events = {str(hook.get("eventName", "")) for hook in hooks}
    if actual_hook_events != expected_hook_events or len(hooks) != len(expected_hook_events):
        raise RuntimeError(
            f"app-server hook discovery mismatch: actual={sorted(actual_hook_events)} expected={sorted(expected_hook_events)}"
        )
    for hook in hooks:
        source_path = Path(str(hook.get("sourcePath", ""))).resolve()
        command = str(hook.get("command", ""))
        command_paths = [
            Path(token.strip('"')).resolve()
            for token in shlex.split(command, posix=False)
            if token.strip('"').lower().endswith("harness_hook.py")
        ]
        if (
            hook.get("enabled") is not True
            or hook.get("source") != "plugin"
            or not source_path.is_relative_to(cache_root)
            or len(command_paths) != 1
            or not command_paths[0].is_relative_to(cache_root)
        ):
            raise RuntimeError(f"app-server hook did not resolve from installed cache: {hook}")

    return {
        "plugin_id": plugin_id,
        "plugin_local_version": str(plugin.get("localVersion", "")),
        "skill_count": len(actual_skills),
        "skill_names": sorted(actual_skills),
        "hook_count": len(actual_hook_events),
        "hook_events": sorted(actual_hook_events),
    }


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
        expected_skills = {
            f"codex-project-harness:{skill.parent.name}"
            for skill in (release_repo / "plugins" / "codex-project-harness" / "skills").glob("*/SKILL.md")
        }
        hook_definition = json.loads(
            (release_repo / "plugins" / "codex-project-harness" / "hooks" / "hooks.json").read_text(encoding="utf-8")
        )
        expected_hook_events = {
            event[:1].lower() + event[1:]
            for event in hook_definition.get("hooks", {})
        }
        discovery = discover_with_app_server(codex, env=env, cwd=business_repo)
        discovery_report = validate_app_server_discovery(
            discovery,
            cache_root=cache_root,
            plugin_id=PLUGIN_ID,
            version=version,
            expected_skills=expected_skills,
            expected_hook_events=expected_hook_events,
        )

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

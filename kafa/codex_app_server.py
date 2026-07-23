"""Small stdlib client for Codex app-server compatibility checks."""

from __future__ import annotations

import json
import queue
import re
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .cli import (
    KafaError,
    RETIRED_CORE_FILES,
    distribution_file_inventory,
    distribution_inventory_issues,
    load_distribution_manifest,
    managed_tree_is_safe,
    static_runtime_domains,
)
RETIRED_RUNTIME_PATHS = frozenset(
    {
        *(f"core/{name}" for name in RETIRED_CORE_FILES),
        "references/collaboration-tools.md",
        "references/tool-adapters.md",
        "skills/project-runtime/SKILL.md",
        "skills/project-runtime/scripts/harness.py",
    }
)


class AppServerClient:
    """Line-delimited JSON-RPC client with bounded waits and process cleanup."""

    def __init__(self, command: list[str], *, env: dict[str, str], cwd: Path, timeout: float = 30.0) -> None:
        self.timeout = timeout
        self.process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
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
            raise RuntimeError(f"Codex app-server exited before request: {self.stderr_tail()}")
        self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"method": method, "params": params})

    def request(self, method: str, params: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        self._send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while True:
            message = self._next_message(deadline, f"response to {method}")
            if message.get("id") != request_id:
                self._record_unsolicited(message)
                continue
            if "error" in message:
                raise RuntimeError(f"Codex app-server {method} failed: {message['error']}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise RuntimeError(f"Codex app-server {method} returned invalid result: {message}")
            return result

    def wait_for_notification(
        self,
        method: str,
        *,
        predicate: Callable[[dict[str, Any]], bool] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        matches = predicate or (lambda _message: True)
        for message in self.notifications:
            if message.get("method") == method and matches(message):
                return message
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while True:
            message = self._next_message(deadline, method)
            self._record_unsolicited(message)
            if message.get("method") == method and matches(message):
                return message

    def _next_message(self, deadline: float, waiting_for: str) -> dict[str, Any]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(f"Codex app-server timed out waiting for {waiting_for}: {self.stderr_tail()}")
        try:
            message = self.messages.get(timeout=remaining)
        except queue.Empty as exc:
            raise RuntimeError(f"Codex app-server timed out waiting for {waiting_for}: {self.stderr_tail()}") from exc
        if message is None:
            raise RuntimeError(f"Codex app-server closed while waiting for {waiting_for}: {self.stderr_tail()}")
        return message

    def _record_unsolicited(self, message: dict[str, Any]) -> None:
        if "id" in message and "method" in message:
            raise RuntimeError(f"unexpected Codex app-server request: {message.get('method')}")
        self.notifications.append(message)

    def stderr_tail(self, limit: int = 2000) -> str:
        return "".join(self.stderr_lines)[-limit:]

    def close(self) -> None:
        if self.process.stdin is not None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        try:
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
        finally:
            for stream in (self.process.stdout, self.process.stderr):
                if stream is not None:
                    stream.close()

    def __enter__(self) -> "AppServerClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _hook_command_paths(
    command: str,
    cache_root: Path,
    runner_relative: str,
) -> list[Path]:
    paths: list[Path] = []
    runner_suffix = "/" + runner_relative.replace("\\", "/")
    for token in shlex.split(command, posix=False):
        value = token.strip('"').replace("\\", "/")
        if not value.endswith(runner_suffix):
            continue
        expanded = value.replace("${PLUGIN_ROOT}", cache_root.as_posix()).replace(
            "%PLUGIN_ROOT%", cache_root.as_posix()
        )
        paths.append(Path(expanded).resolve())
    return paths


def validate_app_server_discovery(
    discovery: dict[str, Any],
    *,
    cache_root: Path,
    plugin_id: str,
    version: str,
) -> dict[str, Any]:
    """Require the fixed local-only plugin surface from the installed cache."""

    cache_root = cache_root.resolve()
    if not managed_tree_is_safe(cache_root):
        raise RuntimeError(
            f"installed cache is missing or contains a link/junction: {cache_root}"
        )
    retired_paths = sorted(
        relative for relative in RETIRED_RUNTIME_PATHS if (cache_root / relative).exists()
    )
    if retired_paths:
        raise RuntimeError(f"installed cache contains retired runtime files: {retired_paths}")
    try:
        distribution = load_distribution_manifest(cache_root)
    except KafaError as exc:
        raise RuntimeError(str(exc)) from exc
    inventory_issues = distribution_inventory_issues(cache_root, distribution)
    if inventory_issues:
        raise RuntimeError(
            "installed cache distribution inventory mismatch: "
            + "; ".join(inventory_issues)
        )
    metadata_path = cache_root / ".codex-plugin/plugin.json"
    def reject_metadata_duplicates(
        pairs: list[tuple[str, Any]],
    ) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise RuntimeError(f"duplicate installed cache plugin metadata key: {key}")
            value[key] = item
        return value

    try:
        cache_metadata = json.loads(
            metadata_path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_metadata_duplicates,
        )
    except RuntimeError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"installed cache plugin metadata is invalid: {exc}") from exc
    if not isinstance(cache_metadata, dict):
        raise RuntimeError("installed cache plugin metadata must be an object")
    requested_name = plugin_id.split("@", 1)[0]
    if (
        requested_name != distribution["plugin_name"]
        or cache_metadata.get("name") != distribution["plugin_name"]
    ):
        raise RuntimeError(
            "installed cache plugin name mismatch: "
            f"requested={requested_name!r} metadata={cache_metadata.get('name')!r} "
            f"manifest={distribution['plugin_name']!r}"
        )
    if cache_metadata.get("version") != version:
        raise RuntimeError(
            "installed cache plugin version mismatch: "
            f"metadata={cache_metadata.get('version')!r} expected={version!r}"
        )
    approved_skills = frozenset(distribution["skills"])
    approved_hook_events = frozenset(
        event[:1].lower() + event[1:]
        for event in distribution["hooks"]["events"]
    )
    approved_agent_templates = frozenset(
        distribution["templates"]["native_agents"]
    )
    approved_project_templates = frozenset(
        distribution["templates"]["project_support"]
    )
    approved_runtime_scripts = frozenset(distribution["scripts"])
    approved_schema_files = frozenset(distribution["schemas"])
    approved_core_files = frozenset(distribution["core"])
    approved_hook_files = frozenset(distribution["hooks"]["files"])
    approved_reference_files = frozenset(distribution["references"])
    hook_definitions = [
        name for name in distribution["hooks"]["files"] if Path(name).suffix == ".json"
    ]
    hook_runners = [
        name for name in distribution["hooks"]["files"] if Path(name).suffix == ".py"
    ]
    if len(hook_definitions) != 1 or len(hook_runners) != 1:
        raise RuntimeError(
            "installed cache Hook inventory requires one JSON definition and one Python runner"
        )
    hook_definition_path = (cache_root / "hooks" / hook_definitions[0]).resolve()
    hook_runner_relative = f"hooks/{hook_runners[0]}"
    hook_runner_path = (cache_root / hook_runner_relative).resolve()
    skill_prefix = plugin_id.split("@", 1)[0]
    expected_skills = {f"{skill_prefix}:{name}" for name in approved_skills}
    expected_hook_events = set(approved_hook_events)
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
        skill_name = str(skill.get("name", ""))
        short_name = skill_name.split(":", 1)[1] if ":" in skill_name else ""
        skill_path = Path(str(skill.get("path", ""))).resolve()
        expected_skill_path = (
            cache_root / "skills" / short_name / "SKILL.md"
        ).resolve()
        if (
            skill.get("enabled") is not True
            or skill.get("scope") != "user"
            or skill_path != expected_skill_path
            or not expected_skill_path.is_file()
        ):
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
        command_tokens = shlex.split(command, posix=False)
        interpreter = (
            Path(command_tokens[0].strip('"')).name.lower()
            if command_tokens
            else ""
        )
        event_argument = (
            command_tokens[2].strip('"')
            if len(command_tokens) == 3
            else ""
        )
        command_paths = _hook_command_paths(
            command,
            cache_root,
            hook_runner_relative,
        )
        event_name = str(hook.get("eventName", ""))
        manifest_event = next(
            (
                event
                for event in distribution["hooks"]["events"]
                if event[:1].lower() + event[1:] == event_name
            ),
            "",
        )
        if (
            hook.get("enabled") is not True
            or hook.get("source") != "plugin"
            or source_path != hook_definition_path
            or not hook_definition_path.is_file()
            or len(command_paths) != 1
            or command_paths[0] != hook_runner_path
            or not hook_runner_path.is_file()
            or re.fullmatch(r"python(?:3(?:\.\d+)*)?(?:\.exe)?", interpreter) is None
            or not manifest_event
            or event_argument != manifest_event
        ):
            raise RuntimeError(f"app-server hook did not resolve from installed cache: {hook}")

    actual_skill_dirs = {
        path.name for path in (cache_root / "skills").iterdir() if path.is_dir()
    } if (cache_root / "skills").is_dir() else set()
    if actual_skill_dirs != approved_skills:
        raise RuntimeError(
            f"installed cache skill inventory mismatch: actual={sorted(actual_skill_dirs)} "
            f"expected={sorted(approved_skills)}"
        )

    actual_templates = _file_inventory(cache_root / "templates/agents", ".toml")
    if actual_templates != approved_agent_templates:
        raise RuntimeError(
            f"installed cache template inventory mismatch: actual={sorted(actual_templates)} "
            f"expected={sorted(approved_agent_templates)}"
        )
    actual_project_templates = _file_inventory(
        cache_root / "templates/project", ""
    )
    if actual_project_templates != approved_project_templates:
        raise RuntimeError(
            "installed cache project template inventory mismatch: "
            f"actual={sorted(actual_project_templates)} "
            f"expected={sorted(approved_project_templates)}"
        )
    actual_scripts = _file_inventory(cache_root / "scripts", ".py")
    if actual_scripts != approved_runtime_scripts:
        raise RuntimeError(
            f"installed cache runtime script inventory mismatch: actual={sorted(actual_scripts)} "
            f"expected={sorted(approved_runtime_scripts)}"
        )
    actual_schemas = _file_inventory(cache_root / "schemas", ".json")
    if actual_schemas != approved_schema_files:
        raise RuntimeError(
            f"installed cache schema inventory mismatch: actual={sorted(actual_schemas)} "
            f"expected={sorted(approved_schema_files)}"
        )
    actual_core = _file_inventory(cache_root / "core", ".py")
    if actual_core != approved_core_files:
        raise RuntimeError(
            f"installed cache core inventory mismatch: actual={sorted(actual_core)} "
            f"expected={sorted(approved_core_files)}"
        )
    actual_hook_files = _file_inventory(cache_root / "hooks", "")
    if actual_hook_files != approved_hook_files:
        raise RuntimeError(
            "installed cache hook file inventory mismatch: "
            f"actual={sorted(actual_hook_files)} "
            f"expected={sorted(approved_hook_files)}"
        )
    actual_references = _file_inventory(cache_root / "references", "")
    if actual_references != approved_reference_files:
        raise RuntimeError(
            "installed cache reference inventory mismatch: "
            f"actual={sorted(actual_references)} "
            f"expected={sorted(approved_reference_files)}"
        )
    actual_domains = static_runtime_domains(cache_root / "scripts/harness.py")
    expected_domains = set(distribution["public_runtime_domains"])
    if actual_domains != expected_domains:
        raise RuntimeError(
            "installed cache public runtime domain inventory mismatch: "
            f"actual={sorted(actual_domains)} expected={sorted(expected_domains)}"
        )
    runtime_file_count = len(distribution_file_inventory(distribution))
    return {
        "plugin_id": plugin_id,
        "plugin_local_version": str(plugin.get("localVersion", "")),
        "skill_count": len(actual_skills),
        "skill_names": sorted(actual_skills),
        "hook_count": len(actual_hook_events),
        "hook_events": sorted(actual_hook_events),
        "template_count": len(actual_templates),
        "template_names": sorted(actual_templates),
        "project_template_count": len(actual_project_templates),
        "project_template_names": sorted(actual_project_templates),
        "runtime_script_count": len(actual_scripts),
        "runtime_script_names": sorted(actual_scripts),
        "schema_count": len(actual_schemas),
        "schema_names": sorted(actual_schemas),
        "core_count": len(actual_core),
        "core_names": sorted(actual_core),
        "hook_file_count": len(actual_hook_files),
        "hook_file_names": sorted(actual_hook_files),
        "reference_count": len(actual_references),
        "reference_names": sorted(actual_references),
        "public_runtime_domain_count": len(actual_domains),
        "public_runtime_domains": sorted(actual_domains),
        "runtime_anchor_count": runtime_file_count,
        "runtime_file_count": runtime_file_count,
        "retired_runtime_absent": True,
    }


def _file_inventory(root: Path, suffix: str) -> set[str]:
    if not root.is_dir():
        return set()
    return {
        path.name
        for path in root.iterdir()
        if path.is_file()
    }

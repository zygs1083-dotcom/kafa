"""Small stdlib client for Codex app-server compatibility checks."""

from __future__ import annotations

import json
import queue
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable


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


def validate_app_server_discovery(
    discovery: dict[str, Any],
    *,
    cache_root: Path,
    plugin_id: str,
    version: str,
    expected_skills: set[str],
    expected_hook_events: set[str],
) -> dict[str, Any]:
    """Require exact installed plugin, Skill, Hook, and cache-path discovery."""

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
        command_paths = [
            Path(token.strip('"')).resolve()
            for token in shlex.split(str(hook.get("command", "")), posix=False)
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

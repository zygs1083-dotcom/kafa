from __future__ import annotations

import json
import os
import py_compile
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "codex-project-harness"
HOOKS_ROOT = PLUGIN_ROOT / "hooks"
HOOK_SCRIPT = HOOKS_ROOT / "harness_hook.py"
HOOKS_JSON = HOOKS_ROOT / "hooks.json"
HARNESS = PLUGIN_ROOT / "scripts" / "harness.py"


class CodexHooksTest(unittest.TestCase):
    def test_hooks_json_shape_references_existing_dispatcher(self) -> None:
        data = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
        self.assertEqual(set(data), {"hooks"})
        expected_events = {"SessionStart", "SubagentStart", "PreToolUse", "PostToolUse", "Stop"}
        self.assertEqual(set(data["hooks"]), expected_events)

        for event, groups in data["hooks"].items():
            self.assertIsInstance(groups, list)
            self.assertGreaterEqual(len(groups), 1)
            for group in groups:
                if event not in {"Stop", "UserPromptSubmit"}:
                    self.assertIn("matcher", group)
                self.assertIn("hooks", group)
                for hook in group["hooks"]:
                    self.assertEqual(hook["type"], "command")
                    self.assertIn("harness_hook.py", hook["command"])
                    self.assertIn(event, hook["command"])
                    self.assertIn("${PLUGIN_ROOT}", hook["command"])
                    self.assertNotIn("CODEX_PROJECT_HARNESS_PLUGIN_ROOT", hook["command"])
                    self.assertNotIn("git rev-parse", hook["command"])
                    self.assertIn("commandWindows", hook)
                    self.assertIn("%PLUGIN_ROOT%", hook["commandWindows"])
                    self.assertIn(event, hook["commandWindows"])
                    self.assertIsInstance(hook.get("timeout"), int)
                    self.assertGreater(hook.get("timeout"), 0)
                    self.assertIsInstance(hook.get("statusMessage"), str)

        self.assertTrue(HOOK_SCRIPT.exists())
        py_compile.compile(str(HOOK_SCRIPT), doraise=True)

    def test_installed_hook_command_uses_codex_plugin_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            installed_plugin = temp_root / "installed" / "codex-project-harness"
            project_root = temp_root / "business-project"
            shutil.copytree(PLUGIN_ROOT, installed_plugin)
            project_root.mkdir()
            subprocess.run(["git", "init"], cwd=project_root, check=True, capture_output=True, text=True)

            env = os.environ.copy()
            env.pop("CODEX_PROJECT_HARNESS_PLUGIN_ROOT", None)
            env.pop("HARNESS_PROJECT_ROOT", None)
            env["PLUGIN_ROOT"] = str(installed_plugin)
            nested = project_root / "nested"
            nested.mkdir()
            hook = json.loads((installed_plugin / "hooks" / "hooks.json").read_text(encoding="utf-8"))[
                "hooks"
            ]["SessionStart"][0]["hooks"][0]
            command = hook["commandWindows"] if os.name == "nt" else hook["command"]
            result = subprocess.run(
                command,
                input=json.dumps({"source": "startup"}),
                text=True,
                capture_output=True,
                cwd=nested,
                env=env,
                shell=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Codex Project Harness hook: SessionStart", result.stdout)
        self.assertIn(f"repo: {project_root.resolve()}", result.stdout)

    def test_installed_session_start_reads_version_from_plugin_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            installed_plugin = Path(temp) / "codex-project-harness"
            shutil.copytree(PLUGIN_ROOT, installed_plugin)
            with tempfile.TemporaryDirectory() as project_temp:
                project_root = Path(project_temp)
                subprocess.run(["git", "init"], cwd=project_root, check=True, capture_output=True, text=True)
                env = os.environ.copy()
                env.pop("CODEX_PROJECT_HARNESS_PLUGIN_ROOT", None)
                env.pop("HARNESS_PROJECT_ROOT", None)
                result = subprocess.run(
                    ["python3", str(installed_plugin / "hooks" / "harness_hook.py"), "SessionStart"],
                    input=json.dumps({"source": "startup"}),
                    text=True,
                    capture_output=True,
                    cwd=project_root,
                    env=env,
                    check=False,
                )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("version: 1.25.0-beta.1", result.stdout)
        self.assertNotIn("version: unknown", result.stdout)

    def test_installed_hook_ignores_legacy_plugin_root_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            installed_plugin = temp_root / "installed" / "codex-project-harness"
            decoy_plugin = temp_root / "decoy" / "codex-project-harness"
            project_root = temp_root / "business-project"
            shutil.copytree(PLUGIN_ROOT, installed_plugin)
            shutil.copytree(PLUGIN_ROOT, decoy_plugin)
            project_root.mkdir()
            decoy_manifest = decoy_plugin / ".codex-plugin" / "plugin.json"
            manifest = json.loads(decoy_manifest.read_text(encoding="utf-8"))
            manifest["version"] = "0.0.0-decoy"
            decoy_manifest.write_text(json.dumps(manifest), encoding="utf-8")
            env = os.environ.copy()
            env["CODEX_PROJECT_HARNESS_PLUGIN_ROOT"] = str(decoy_plugin)
            env.pop("HARNESS_PROJECT_ROOT", None)
            result = subprocess.run(
                ["python3", str(installed_plugin / "hooks" / "harness_hook.py"), "SessionStart"],
                input=json.dumps({"source": "startup"}),
                text=True,
                capture_output=True,
                cwd=project_root,
                env=env,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("version: 1.25.0-beta.1", result.stdout)
        self.assertNotIn("0.0.0-decoy", result.stdout)

    def test_session_start_outputs_status_without_mutating_db(self) -> None:
        with self._temp_harness_root() as root:
            before = self._counts(root)
            result = self._run_hook("SessionStart", root, {"source": "startup"})
            after = self._counts(root)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Codex Project Harness hook: SessionStart", result.stdout)
        self.assertIn("runtime_version: 4.18.0", result.stdout)
        self.assertIn("Harness Status", result.stdout)
        self.assertEqual(before, after)

    def test_subagent_start_prints_boundaries_and_redacts_input(self) -> None:
        secret = "sk-test-should-not-leak"
        with self._temp_harness_root() as root:
            result = self._run_hook(
                "SubagentStart",
                root,
                {"subagent_type": "qa-reviewer", "prompt": f"review task T1 token {secret}"},
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("qa-reviewer", result.stdout)
        self.assertIn("role boundary", result.stdout)
        self.assertIn("acceptance", result.stdout)
        self.assertNotIn(secret, result.stdout)

    def test_invalid_stdin_degrades_without_failure(self) -> None:
        with self._temp_harness_root() as root:
            result = self._run_hook("SubagentStart", root, "{not json")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("stdin: unavailable", result.stdout)

    def test_pre_tool_use_warn_only_by_default_and_strict_blocks_clear_violation(self) -> None:
        payload = {"tool_name": "apply_patch"}
        with self._temp_harness_root() as root:
            warn = self._run_hook("PreToolUse", root, payload)
            strict = self._run_hook("PreToolUse", root, payload, extra_env={"HARNESS_HOOK_STRICT": "1"})

        self.assertEqual(warn.returncode, 0, warn.stderr)
        self.assertIn("warning", warn.stdout.lower())
        self.assertIn("no active task", warn.stdout.lower())
        self.assertNotEqual(strict.returncode, 0)
        self.assertIn("strict mode", strict.stdout.lower())

    def test_post_tool_use_outputs_change_summary_without_evidence_mutation(self) -> None:
        with self._temp_harness_root() as root:
            before = self._counts(root)
            (root / "changed.txt").write_text("changed\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            result = self._run_hook("PostToolUse", root, {"tool_name": "Bash"})
            after = self._counts(root)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("git status", result.stdout.lower())
        self.assertIn("changed.txt", result.stdout)
        self.assertIn("record validation/evidence", result.stdout)
        self.assertEqual(before["evidence"], after["evidence"])
        self.assertEqual(before["validations"], after["validations"])

    def test_stop_validate_warn_only_and_strict_failure(self) -> None:
        with self._temp_harness_root() as root:
            db_path = root / ".ai-team" / "state" / "harness.db"
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute("update project set schema_version = 1 where id = 1")
                conn.commit()
            warn = self._run_hook("Stop", root, {})
            strict = self._run_hook("Stop", root, {}, extra_env={"HARNESS_HOOK_STRICT": "1"})

        self.assertEqual(warn.returncode, 0)
        warn_output = json.loads(warn.stdout)
        self.assertTrue(warn_output["continue"])
        self.assertIn("validation failed", warn_output["systemMessage"].lower())
        self.assertNotEqual(strict.returncode, 0)
        self.assertEqual(strict.stdout, "")
        self.assertIn("strict mode", strict.stderr.lower())

    def test_stop_skips_uninitialized_project_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
            result = self._run_hook("Stop", root, {}, extra_env={"HARNESS_HOOK_STRICT": "1"})

        self.assertEqual(result.returncode, 0)
        output = json.loads(result.stdout)
        self.assertTrue(output["continue"])
        self.assertIn("readiness command: skipped", output["systemMessage"])
        self.assertIn("not initialized", output["systemMessage"])
        self.assertNotIn("traceback", result.stdout.lower() + result.stderr.lower())

    def test_stop_delivery_flag_runs_delivery_validation(self) -> None:
        with self._temp_harness_root() as root:
            result = self._run_hook("Stop", root, {}, extra_env={"HARNESS_HOOK_DELIVERY": "1"})

        self.assertEqual(result.returncode, 0)
        output = json.loads(result.stdout)
        self.assertTrue(output["continue"])
        self.assertIn("validate --delivery", output["systemMessage"])

    def test_hook_output_does_not_include_secret_like_stdin(self) -> None:
        secret = "HARNESS_CONNECTOR_KEY=super-secret"
        with self._temp_harness_root() as root:
            result = self._run_hook("PreToolUse", root, {"tool_name": "Bash", "input": secret})

        self.assertEqual(result.returncode, 0)
        self.assertNotIn("super-secret", result.stdout)
        self.assertNotIn("HARNESS_CONNECTOR_KEY", result.stdout)

    def _run_hook(
        self,
        event: str,
        root: Path,
        payload: dict[str, object] | str,
        *,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["HARNESS_PROJECT_ROOT"] = str(root)
        env["CODEX_PROJECT_HARNESS_PLUGIN_ROOT"] = str(PLUGIN_ROOT)
        if extra_env:
            env.update(extra_env)
        stdin = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run(
            ["python3", str(HOOK_SCRIPT), event],
            input=stdin,
            text=True,
            capture_output=True,
            cwd=root,
            env=env,
            check=False,
        )

    def _temp_harness_root(self):
        return _TempHarnessRoot()

    def _counts(self, root: Path) -> dict[str, int]:
        with closing(sqlite3.connect(root / ".ai-team" / "state" / "harness.db")) as conn:
            return {
                table: int(conn.execute(f"select count(*) from {table}").fetchone()[0])
                for table in ["events", "evidence", "validations", "tasks"]
            }


class _TempHarnessRoot:
    def __enter__(self) -> Path:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self._tmp.name)
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=root, check=True)
        subprocess.run(["python3", str(HARNESS), "--root", str(root), "init"], check=True, capture_output=True, text=True)
        subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "commit", "-m", "init harness"], cwd=root, check=True, capture_output=True, text=True)
        return root

    def __exit__(self, exc_type, exc, tb) -> None:
        for attempt in range(5):
            try:
                self._tmp.cleanup()
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.2)


if __name__ == "__main__":
    unittest.main()

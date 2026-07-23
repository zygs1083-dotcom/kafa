from __future__ import annotations

import json
import os
import py_compile
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE = json.loads((REPO_ROOT / "release.json").read_text(encoding="utf-8"))
PLUGIN_ROOT = REPO_ROOT / "plugins" / "codex-project-harness"
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from harness_lib import load_distribution_manifest  # noqa: E402


HOOKS_ROOT = PLUGIN_ROOT / "hooks"
HOOK_SCRIPT = HOOKS_ROOT / "harness_hook.py"
HOOKS_JSON = HOOKS_ROOT / "hooks.json"
HARNESS = PLUGIN_ROOT / "scripts" / "harness.py"
DISTRIBUTION = load_distribution_manifest(PLUGIN_ROOT)
APPROVED_EVENTS = set(DISTRIBUTION["hooks"]["events"])


class CodexHooksTest(unittest.TestCase):
    def test_hooks_json_exposes_exactly_three_existing_commands(self) -> None:
        data = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
        self.assertEqual(len(APPROVED_EVENTS), 3)
        self.assertEqual(set(data), {"hooks"})
        self.assertEqual(set(data["hooks"]), APPROVED_EVENTS)
        for event, groups in data["hooks"].items():
            self.assertIsInstance(groups, list)
            self.assertGreaterEqual(len(groups), 1)
            for group in groups:
                if event != "Stop":
                    self.assertIn("matcher", group)
                for hook in group["hooks"]:
                    self.assertEqual(hook["type"], "command")
                    self.assertIn("harness_hook.py", hook["command"])
                    self.assertIn(event, hook["command"])
                    self.assertIn("${PLUGIN_ROOT}", hook["command"])
                    self.assertIn("%PLUGIN_ROOT%", hook["commandWindows"])
                    self.assertIn(event, hook["commandWindows"])
                    self.assertGreater(hook["timeout"], 0)
                    self.assertTrue(hook["statusMessage"])
        py_compile.compile(str(HOOK_SCRIPT), doraise=True)

    def test_all_three_hooks_skip_uninitialized_project_without_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for event in sorted(APPROVED_EVENTS):
                result = self._run_hook(event, root, {"subagent_type": "developer"})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("traceback", (result.stdout + result.stderr).lower())
                if event == "Stop":
                    payload = json.loads(result.stdout)
                    self.assertTrue(payload["continue"])
                    self.assertIn("skipped", payload["systemMessage"])
                    self.assertIn("not initialized", payload["systemMessage"])
                elif event == "SessionStart":
                    self.assertIn("state: not-initialized", result.stdout)
                    self.assertIn("not initialized", result.stdout)
                    self.assertNotIn("passed", result.stdout.lower())
                else:
                    self.assertIn("skipped", result.stdout)
                    self.assertIn("not initialized", result.stdout)
                self.assertFalse((root / ".ai-team").exists())

    def test_retired_pre_and_post_tool_events_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for event in ("PreToolUse", "PostToolUse"):
                result = self._run_hook(event, root, {})
                self.assertEqual(result.returncode, 2)
                self.assertIn("unknown event", result.stdout)

    def test_hook_runner_uses_the_installed_manifest_event_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            installed = root / "installed/codex-project-harness"
            project = root / "business-project"
            shutil.copytree(
                PLUGIN_ROOT,
                installed,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            project.mkdir()

            replacement = "AgentStart"
            distribution_path = installed / "references/distribution-manifest.json"
            distribution = json.loads(distribution_path.read_text(encoding="utf-8"))
            distribution["hooks"]["events"] = [
                replacement if event == "SubagentStart" else event
                for event in distribution["hooks"]["events"]
            ]
            distribution_path.write_text(
                json.dumps(distribution, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            hooks_path = installed / "hooks/hooks.json"
            hooks_payload = json.loads(hooks_path.read_text(encoding="utf-8"))
            groups = hooks_payload["hooks"].pop("SubagentStart")
            for group in groups:
                for hook in group["hooks"]:
                    hook["command"] = hook["command"].replace(
                        "SubagentStart", replacement
                    )
                    hook["commandWindows"] = hook["commandWindows"].replace(
                        "SubagentStart", replacement
                    )
            hooks_payload["hooks"][replacement] = groups
            hooks_path.write_text(
                json.dumps(hooks_payload, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            env = os.environ.copy()
            env["HARNESS_PROJECT_ROOT"] = str(project)
            renamed = subprocess.run(
                [sys.executable, str(installed / "hooks/harness_hook.py"), replacement],
                input=json.dumps({"subagent_type": "developer"}),
                text=True,
                capture_output=True,
                cwd=project,
                env=env,
                check=False,
            )
            retired = subprocess.run(
                [
                    sys.executable,
                    str(installed / "hooks/harness_hook.py"),
                    "SubagentStart",
                ],
                input=json.dumps({"subagent_type": "developer"}),
                text=True,
                capture_output=True,
                cwd=project,
                env=env,
                check=False,
            )

        self.assertEqual(renamed.returncode, 0, renamed.stdout + renamed.stderr)
        self.assertIn(f"Codex Project Harness hook: {replacement}", renamed.stdout)
        self.assertEqual(retired.returncode, 2, retired.stdout + retired.stderr)
        self.assertIn("unknown event", retired.stdout)

    def test_session_start_reads_only_concise_status(self) -> None:
        with self._initialized_root() as root:
            before = self._counts(root)
            result = self._run_hook("SessionStart", root, {"source": "startup"})
            after = self._counts(root)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Codex Project Harness hook: SessionStart", result.stdout)
        self.assertIn("state: needs-work", result.stdout)
        self.assertIn("blocker: [requirement-missing]", result.stdout)
        self.assertIn("next:", result.stdout)
        self.assertNotIn("schema_version:", result.stdout)
        self.assertNotIn("runtime_version:", result.stdout)
        self.assertNotIn("dispatch", result.stdout.lower())
        self.assertEqual(before, after)

    def test_session_start_surfaces_recovery_instead_of_uninitialized_skip(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sentinel = root / ".ai-team/state/local-core-migration.lock"
            sentinel.parent.mkdir(parents=True)
            sentinel.write_text(
                json.dumps(
                    {
                        "pid": 999999,
                        "status": "rollback-incomplete",
                        "manifest_path": str(root / ".ai-team/backups/recovery/manifest.json"),
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            results = {
                event: self._run_hook(event, root, {"source": "startup"})
                for event in ("SessionStart", "SubagentStart")
            }

        for event, result in results.items():
            with self.subTest(event=event):
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("state: recovery-required", result.stdout)
                self.assertIn("rollback-incomplete", result.stdout)
                self.assertIn("next: none", result.stdout)
                self.assertNotIn("skipped: harness is not initialized", result.stdout)

    def test_subagent_start_surfaces_recovery_when_database_still_exists(self) -> None:
        with self._initialized_root() as root:
            sentinel = root / ".ai-team/state/local-core-migration.lock"
            sentinel.write_text(
                json.dumps(
                    {
                        "pid": 999999,
                        "status": "rollback-incomplete",
                        "manifest_path": str(root / ".ai-team/backups/recovery/manifest.json"),
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            result = self._run_hook(
                "SubagentStart",
                root,
                {"subagent_type": "developer"},
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("state: recovery-required", result.stdout)
        self.assertIn("rollback-incomplete", result.stdout)
        self.assertNotIn("role boundary:", result.stdout)

    def test_subagent_start_returns_local_single_writer_boundary_and_redacts(self) -> None:
        secret = "HARNESS_SECRET=must-not-leak"
        with self._initialized_root() as root:
            before = self._counts(root)
            result = self._run_hook(
                "SubagentStart",
                root,
                {"subagent_type": "qa-reviewer", "prompt": secret},
            )
            after = self._counts(root)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("qa-reviewer", result.stdout)
        self.assertIn("root controller", result.stdout)
        self.assertIn("do not write Kafa facts", result.stdout)
        self.assertIn("verifies results", result.stdout)
        self.assertNotIn(secret, result.stdout)
        self.assertEqual(before, after)

    def test_invalid_subagent_payload_degrades_without_failure(self) -> None:
        with self._initialized_root() as root:
            result = self._run_hook("SubagentStart", root, "{not json")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("stdin: unavailable", result.stdout)

    def test_stop_is_always_warn_only_even_with_legacy_strict_env(self) -> None:
        with self._initialized_root() as root:
            normal = self._run_hook("Stop", root, {})
            blocked_delivery = self._run_hook(
                "Stop",
                root,
                {},
                extra_env={"HARNESS_HOOK_DELIVERY": "1", "HARNESS_HOOK_STRICT": "1"},
            )

        self.assertEqual(normal.returncode, 0, normal.stderr)
        normal_payload = json.loads(normal.stdout)
        self.assertTrue(normal_payload["continue"])
        self.assertIn("harness validate", normal_payload["systemMessage"])
        self.assertEqual(blocked_delivery.returncode, 0, blocked_delivery.stderr)
        blocked_payload = json.loads(blocked_delivery.stdout)
        self.assertTrue(blocked_payload["continue"])
        self.assertIn("validate --delivery", blocked_payload["systemMessage"])
        self.assertIn("validation failed", blocked_payload["systemMessage"].lower())
        self.assertNotIn("strict mode", blocked_payload["systemMessage"].lower())

    def test_installed_hook_command_resolves_plugin_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            installed = root / "installed/codex-project-harness"
            project = root / "business-project"
            shutil.copytree(PLUGIN_ROOT, installed, ignore=shutil.ignore_patterns("__pycache__"))
            project.mkdir()
            hook = json.loads((installed / "hooks/hooks.json").read_text(encoding="utf-8"))[
                "hooks"
            ]["SessionStart"][0]["hooks"][0]
            command = hook["commandWindows"] if os.name == "nt" else hook["command"]
            env = os.environ.copy()
            env["PLUGIN_ROOT"] = str(installed)
            env["HARNESS_PROJECT_ROOT"] = str(project)
            result = subprocess.run(
                command,
                input="{}",
                text=True,
                capture_output=True,
                cwd=project,
                env=env,
                shell=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SessionStart", result.stdout)
        self.assertIn("not initialized", result.stdout)

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
        if extra_env:
            env.update(extra_env)
        stdin = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run(
            [os.environ.get("PYTHON", "python3"), str(HOOK_SCRIPT), event],
            input=stdin,
            text=True,
            capture_output=True,
            cwd=root,
            env=env,
            check=False,
        )

    def _initialized_root(self):
        return _InitializedRoot()

    def _counts(self, root: Path) -> dict[str, int]:
        with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
            return {
                table: int(conn.execute(f"select count(*) from {table}").fetchone()[0])
                for table in ("events", "executions", "validations", "tasks")
            }


class _InitializedRoot:
    def __enter__(self) -> Path:
        self._temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self._temp.name)
        result = subprocess.run(
            ["python3", str(HARNESS), "--root", str(root), "init"],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stdout + result.stderr)
        self.root = root
        return root

    def __exit__(self, exc_type, exc, tb) -> None:
        self._temp.cleanup()


if __name__ == "__main__":
    unittest.main()

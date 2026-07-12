from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from email.parser import Parser
from pathlib import Path
from unittest.mock import patch

from kafa import cli as kafa_cli
from kafa.codex_app_server import (
    APPROVED_AGENT_TEMPLATES,
    APPROVED_APP_SERVER_HOOK_EVENTS,
    APPROVED_RUNTIME_SCRIPTS,
    APPROVED_SCHEMA_FILES,
    APPROVED_SKILLS,
    AppServerClient,
    validate_app_server_discovery,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "codex-project-harness"
VALIDATE = PLUGIN_ROOT / "scripts" / "validate_structure.py"
RELEASE = json.loads((REPO_ROOT / "release.json").read_text(encoding="utf-8"))
RELEASE_VERSION = str(RELEASE["version"])
RELEASE_PEP440_VERSION = str(RELEASE["pep440_version"])


def run_kafa(*args: str, env: dict[str, str] | None = None, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    command_env = os.environ.copy()
    if env:
        command_env.update(env)
    result = subprocess.run([sys.executable, "-m", "kafa.cli", *args], cwd=cwd or REPO_ROOT, text=True, capture_output=True, check=False, env=command_env)
    if check and result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return result


def copy_release_repo(target: Path) -> Path:
    shutil.copytree(PLUGIN_ROOT, target / "plugins" / "codex-project-harness", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copyfile(REPO_ROOT / "VERSION", target / "VERSION")
    shutil.copyfile(REPO_ROOT / "release.json", target / "release.json")
    shutil.copyfile(REPO_ROOT / "pyproject.toml", target / "pyproject.toml")
    return target


def fake_codex_env(root: Path, plugin_root: Path, marketplace_name: str = "kafa-local") -> dict[str, str]:
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "installed": [
            {
                "pluginId": f"codex-project-harness@{marketplace_name}",
                "name": "codex-project-harness",
                "marketplaceName": marketplace_name,
                "version": RELEASE_VERSION,
                "installed": True,
                "enabled": True,
                "source": {"source": "local", "path": str(plugin_root)},
            }
        ]
    }
    script = bin_dir / "fake_codex.py"
    script.write_text(
        "import json, sys\n"
        f"payload = {payload!r}\n"
        "if sys.argv[1:] == ['plugin', 'list', '--json']:\n"
        "    print(json.dumps(payload))\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(2)\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        (bin_dir / "codex.bat").write_text(
            f'@"{sys.executable}" "%~dp0fake_codex.py" %*\n',
            encoding="utf-8",
        )
    else:
        launcher = bin_dir / "codex"
        launcher.write_text(f'#!{sys.executable}\nexec(open({str(script)!r}).read())\n', encoding="utf-8")
        launcher.chmod(0o755)
    return {"PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", "")}


def app_server_discovery(cache_root: Path) -> dict[str, object]:
    event_names = {
        "sessionStart": "SessionStart",
        "subagentStart": "SubagentStart",
        "stop": "Stop",
    }
    expected_skills = {
        f"codex-project-harness:{name}" for name in APPROVED_SKILLS
    }
    return {
        "plugin": {
            "marketplaces": [
                {
                    "name": "kafa-local",
                    "plugins": [
                        {
                            "id": "codex-project-harness@kafa-local",
                            "localVersion": RELEASE_VERSION,
                            "installed": True,
                            "enabled": True,
                        }
                    ],
                }
            ],
            "marketplaceLoadErrors": [],
        },
        "skills": {
            "data": [
                {
                    "cwd": "/tmp/business",
                    "errors": [],
                    "skills": [
                        {
                            "name": name,
                            "enabled": True,
                            "scope": "user",
                            "path": str(cache_root / "skills" / name.split(":", 1)[1] / "SKILL.md"),
                        }
                        for name in sorted(expected_skills)
                    ],
                }
            ]
        },
        "hooks": {
            "data": [
                {
                    "cwd": "/tmp/business",
                    "errors": [],
                    "warnings": [],
                    "hooks": [
                        {
                            "eventName": event,
                            "enabled": True,
                            "source": "plugin",
                            "pluginId": "codex-project-harness@kafa-local",
                            "sourcePath": str(cache_root / "hooks/hooks.json"),
                            "command": (
                                f'python "${{PLUGIN_ROOT}}/hooks/harness_hook.py" '
                                f"{event_names[event]}"
                            ),
                        }
                        for event in sorted(APPROVED_APP_SERVER_HOOK_EVENTS)
                    ],
                }
            ]
        },
    }


class InstallReleaseTest(unittest.TestCase):
    def test_app_server_client_uses_utf8_for_stdio(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            server = root / "utf8_app_server.py"
            server.write_text(
                "import sys\n"
                "sys.stdin.readline()\n"
                "sys.stdout.buffer.write(b'{\"id\":1,\"result\":{\"message\":\"\\xc2\\x8d\"}}\\n')\n"
                "sys.stdout.buffer.flush()\n",
                encoding="utf-8",
            )
            client = AppServerClient([sys.executable, str(server)], env=os.environ.copy(), cwd=root, timeout=2)
            try:
                result = client.request("utf8/check", {})
                stdout_encoding = str(client.process.stdout.encoding if client.process.stdout else "")
            finally:
                client.close()

        self.assertEqual(stdout_encoding.lower().replace("-", ""), "utf8")
        self.assertEqual(result, {"message": "\u008d"})

    def test_app_server_discovery_requires_exact_plugin_skills_and_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cache_root = Path(temp) / "cache/codex-project-harness" / RELEASE_VERSION
            shutil.copytree(PLUGIN_ROOT, cache_root, ignore=shutil.ignore_patterns("__pycache__"))
            report = validate_app_server_discovery(
                app_server_discovery(cache_root),
                cache_root=cache_root,
                plugin_id="codex-project-harness@kafa-local",
                version=RELEASE_VERSION,
            )

        self.assertEqual(report["skill_count"], 7)
        self.assertEqual(
            set(report["skill_names"]),
            {f"codex-project-harness:{name}" for name in APPROVED_SKILLS},
        )
        self.assertEqual(set(report["hook_events"]), APPROVED_APP_SERVER_HOOK_EVENTS)
        self.assertEqual(report["template_count"], 3)
        self.assertEqual(set(report["template_names"]), APPROVED_AGENT_TEMPLATES)
        self.assertEqual(set(report["runtime_script_names"]), APPROVED_RUNTIME_SCRIPTS)
        self.assertEqual(set(report["schema_names"]), APPROVED_SCHEMA_FILES)
        self.assertTrue(report["retired_runtime_absent"])
        self.assertEqual(report["plugin_local_version"], RELEASE_VERSION)

    def test_app_server_discovery_rejects_missing_skill_or_hook(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cache_root = Path(temp) / "cache"
            shutil.copytree(PLUGIN_ROOT, cache_root, ignore=shutil.ignore_patterns("__pycache__"))
            discovery = app_server_discovery(cache_root)
            discovery["skills"] = {"data": [{"cwd": "/tmp/business", "errors": [], "skills": []}]}

            with self.assertRaisesRegex(RuntimeError, "skill discovery mismatch"):
                validate_app_server_discovery(
                    discovery,
                    cache_root=cache_root,
                    plugin_id="codex-project-harness@kafa-local",
                    version=RELEASE_VERSION,
                )

    def test_app_server_discovery_rejects_extra_template_and_retired_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cache_root = Path(temp) / "cache"
            shutil.copytree(PLUGIN_ROOT, cache_root, ignore=shutil.ignore_patterns("__pycache__"))
            extra = cache_root / "templates/agents/bootstrap-coordinator.toml"
            extra.write_text('name = "bootstrap-coordinator"\n', encoding="utf-8")
            discovery = app_server_discovery(cache_root)

            with self.assertRaisesRegex(RuntimeError, "template inventory mismatch"):
                validate_app_server_discovery(
                    discovery,
                    cache_root=cache_root,
                    plugin_id="codex-project-harness@kafa-local",
                    version=RELEASE_VERSION,
                )

            extra.unlink()
            retired = cache_root / "core/agent_provider.py"
            retired.write_text("class HostCodexProvider: pass\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "retired runtime files"):
                validate_app_server_discovery(
                    discovery,
                    cache_root=cache_root,
                    plugin_id="codex-project-harness@kafa-local",
                    version=RELEASE_VERSION,
                )

    def test_kafa_version_reports_repository_version(self) -> None:
        result = run_kafa("--version")
        self.assertEqual(result.stdout.strip(), RELEASE_VERSION)

    def test_doctor_reports_repo_health_as_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = copy_release_repo(Path(temp))
            run_kafa("plugin", "install", "--repo", str(root))

            result = run_kafa("doctor", "--repo", str(root), "--json")
            report = json.loads(result.stdout)

        self.assertTrue(report["ok"], report)
        self.assertEqual(report["scope"], "repo")
        self.assertIn("plugin structure", {check["name"] for check in report["checks"]})

    def test_user_doctor_fails_when_plugin_is_not_installed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = copy_release_repo(Path(temp) / "repo")
            home = Path(temp) / "home"
            result = run_kafa(
                "doctor",
                "--scope",
                "user",
                "--repo",
                str(root),
                "--json",
                env={"HOME": str(home), "CODEX_HOME": str(home / ".codex")},
                check=False,
            )
            report = json.loads(result.stdout)
            checks = {check["name"]: check for check in report["checks"]}

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(report["ok"])
        self.assertFalse(checks["marketplace manifest"]["ok"])
        self.assertFalse(checks["installed plugin manifest"]["ok"])
        self.assertFalse(checks["hook definition"]["ok"])
        self.assertFalse(checks["codex plugin registration"]["ok"])

    def test_doctor_does_not_execute_repository_validation_or_hook_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = copy_release_repo(Path(temp) / "repo")
            marker = Path(temp) / "doctor-executed-untrusted-code"
            injection = f"\n__import__('pathlib').Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n"
            for relative in ["scripts/validate_structure.py", "hooks/harness_hook.py"]:
                path = root / "plugins" / "codex-project-harness" / relative
                text = path.read_text(encoding="utf-8")
                path.write_text(text.replace("from __future__ import annotations\n", "from __future__ import annotations\n" + injection, 1), encoding="utf-8")
            run_kafa("plugin", "install", "--repo", str(root))

            result = run_kafa("doctor", "--repo", str(root), "--json", check=False)

        self.assertFalse(marker.exists(), result.stdout + result.stderr)

    def test_doctor_static_structure_rejects_missing_required_core_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = copy_release_repo(Path(temp) / "repo")
            (root / "plugins" / "codex-project-harness" / "core" / "store.py").unlink()

            result = run_kafa("doctor", "--repo", str(root), "--json", check=False)
            report = json.loads(result.stdout)
            checks = {check["name"]: check for check in report["checks"]}

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(checks["plugin structure"]["ok"])
        self.assertIn("missing local Python import: core.store", checks["plugin structure"]["details"])

    def test_doctor_static_structure_rejects_host_codex_optional_extra(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = copy_release_repo(Path(temp) / "repo")
            pyproject = root / "pyproject.toml"
            pyproject.write_text(
                pyproject.read_text(encoding="utf-8").replace(
                    "[project.scripts]",
                    '[project.optional-dependencies]\nhost-codex = ["openai-codex>=0.1.0b3"]\n\n[project.scripts]',
                ),
                encoding="utf-8",
            )

            result = run_kafa("doctor", "--repo", str(root), "--json", check=False)
            report = json.loads(result.stdout)
            checks = {check["name"]: check for check in report["checks"]}

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(checks["plugin structure"]["ok"])
        self.assertIn("must not declare the retired Host Codex SDK dependency", checks["plugin structure"]["details"])

    def test_plugin_install_rejects_retired_runtime_before_copy_or_registration(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = copy_release_repo(Path(temp) / "repo")
            home = Path(temp) / "home"
            retired = root / "plugins/codex-project-harness/core/agent_provider.py"
            retired.write_text("class HostCodexProvider:\n    pass\n", encoding="utf-8")

            result = run_kafa(
                "plugin",
                "install",
                "--scope",
                "user",
                "--repo",
                str(root),
                env={"HOME": str(home)},
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("retired core file exists", result.stderr)
        self.assertFalse((home / ".agents/plugins/codex-project-harness").exists())
        self.assertFalse((home / ".agents/plugins/marketplace.json").exists())

    def test_base_wheel_has_no_host_codex_sdk_dependency_or_extra(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            release = Path(temp) / "release"
            release.mkdir()
            for name in ["VERSION", "README.md", "pyproject.toml"]:
                shutil.copyfile(REPO_ROOT / name, release / name)
            shutil.copytree(REPO_ROOT / "kafa", release / "kafa", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            dist = Path(temp) / "dist"
            result = subprocess.run(
                [sys.executable, "-m", "pip", "wheel", "--no-deps", ".", "--wheel-dir", str(dist)],
                cwd=release,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            wheel = next(dist.glob("kafa-*.whl"))
            with zipfile.ZipFile(wheel) as archive:
                metadata_name = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
                metadata = Parser().parsestr(archive.read(metadata_name).decode("utf-8"))

        requirements = metadata.get_all("Requires-Dist") or []
        sdk_requirements = [item for item in requirements if item.startswith("openai-codex")]
        self.assertEqual(metadata.get_all("Provides-Extra"), None)
        self.assertEqual(sdk_requirements, [], requirements)

    def test_user_doctor_validates_installed_copy_and_hook_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = copy_release_repo(Path(temp) / "repo")
            home = Path(temp) / "home"
            installed = home / ".agents" / "plugins" / "codex-project-harness"
            env = {"HOME": str(home), "CODEX_HOME": str(home / ".codex")}
            run_kafa("plugin", "install", "--scope", "user", "--repo", str(root), env=env)
            cache = Path(env["CODEX_HOME"]) / "plugins" / "cache" / "kafa-local" / "codex-project-harness" / RELEASE_VERSION
            shutil.copytree(installed, cache)
            env.update(fake_codex_env(Path(temp), installed))
            result = run_kafa("doctor", "--scope", "user", "--repo", str(root), "--json", env=env)
            report = json.loads(result.stdout)
            checks = {check["name"]: check for check in report["checks"]}

        self.assertTrue(report["ok"], report)
        for name in [
            "marketplace manifest",
            "marketplace plugin entry",
            "marketplace source",
            "installed plugin manifest",
            "installed plugin identity",
            "installed plugin content",
            "hook definition",
            "codex plugin registration",
            "codex plugin cache",
        ]:
            self.assertTrue(checks[name]["ok"], checks[name])

    def test_plugin_install_preserves_on_demand_delegation_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = copy_release_repo(Path(temp) / "repo")
            home = Path(temp) / "home"
            env = {"HOME": str(home), "CODEX_HOME": str(home / ".codex")}
            run_kafa("plugin", "install", "--scope", "user", "--repo", str(root), env=env)
            installed = home / ".agents/plugins/codex-project-harness"
            source_reference = root / "plugins/codex-project-harness/references/delegation-matrix.md"
            installed_reference = installed / "references/delegation-matrix.md"
            installed_skill = (installed / "skills/project-harness/SKILL.md").read_text(encoding="utf-8")
            installed_exists = installed_reference.is_file()
            source_bytes = source_reference.read_bytes()
            installed_bytes = installed_reference.read_bytes()

        self.assertTrue(installed_exists)
        self.assertEqual(installed_bytes, source_bytes)
        self.assertIn("references/delegation-matrix.md", installed_skill)

    def test_user_doctor_detects_same_version_installed_content_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = copy_release_repo(Path(temp) / "repo")
            home = Path(temp) / "home"
            env = {"HOME": str(home), "CODEX_HOME": str(home / ".codex")}
            run_kafa("plugin", "install", "--scope", "user", "--repo", str(root), env=env)
            installed_hook = home / ".agents" / "plugins" / "codex-project-harness" / "hooks" / "hooks.json"
            installed_hook.write_text(installed_hook.read_text(encoding="utf-8") + "\n", encoding="utf-8")

            result = run_kafa(
                "doctor",
                "--scope",
                "user",
                "--repo",
                str(root),
                "--json",
                env=env,
                check=False,
            )
            report = json.loads(result.stdout)
            checks = {check["name"]: check for check in report["checks"]}

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(checks["installed plugin content"]["ok"])

    def test_user_doctor_rejects_symlinked_installed_plugin_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = copy_release_repo(Path(temp) / "repo")
            home = Path(temp) / "home"
            installed = home / ".agents" / "plugins" / "codex-project-harness"
            env = {"HOME": str(home), "CODEX_HOME": str(home / ".codex")}
            run_kafa("plugin", "install", "--scope", "user", "--repo", str(root), env=env)
            shutil.rmtree(installed)
            source = root / "plugins" / "codex-project-harness"
            if os.name == "nt":
                subprocess.run(["cmd", "/c", "mklink", "/J", str(installed), str(source)], check=True, capture_output=True)
            else:
                installed.symlink_to(source, target_is_directory=True)

            result = run_kafa(
                "doctor",
                "--scope",
                "user",
                "--repo",
                str(root),
                "--json",
                env=env,
                check=False,
            )
            report = json.loads(result.stdout)
            checks = {check["name"]: check for check in report["checks"]}

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(checks["installed plugin manifest"]["ok"])
        self.assertFalse(checks["installed plugin content"]["ok"])

    def test_user_doctor_detects_enabled_codex_cache_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = copy_release_repo(Path(temp) / "repo")
            home = Path(temp) / "home"
            installed = home / ".agents" / "plugins" / "codex-project-harness"
            env = {"HOME": str(home), "CODEX_HOME": str(home / ".codex")}
            run_kafa("plugin", "install", "--scope", "user", "--repo", str(root), env=env)
            cache = Path(env["CODEX_HOME"]) / "plugins" / "cache" / "kafa-local" / "codex-project-harness" / RELEASE_VERSION
            shutil.copytree(installed, cache)
            cached_hook = cache / "hooks" / "hooks.json"
            cached_hook.write_text(cached_hook.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            env.update(fake_codex_env(Path(temp), installed))

            result = run_kafa(
                "doctor",
                "--scope",
                "user",
                "--repo",
                str(root),
                "--json",
                env=env,
                check=False,
            )
            report = json.loads(result.stdout)
            checks = {check["name"]: check for check in report["checks"]}

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(checks["codex plugin registration"]["ok"])
        self.assertFalse(checks["codex plugin cache"]["ok"])

    def test_user_doctor_rejects_marketplace_source_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = copy_release_repo(Path(temp) / "repo")
            home = Path(temp) / "home"
            env = {"HOME": str(home), "CODEX_HOME": str(home / ".codex")}
            run_kafa("plugin", "install", "--scope", "user", "--repo", str(root), env=env)
            marketplace_path = home / ".agents" / "plugins" / "marketplace.json"
            marketplace = json.loads(marketplace_path.read_text(encoding="utf-8"))
            marketplace["plugins"][0]["source"]["path"] = "./plugins/another-project"
            marketplace_path.write_text(json.dumps(marketplace), encoding="utf-8")

            result = run_kafa(
                "doctor",
                "--scope",
                "user",
                "--repo",
                str(root),
                "--json",
                env=env,
                check=False,
            )
            report = json.loads(result.stdout)
            checks = {check["name"]: check for check in report["checks"]}

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(checks["marketplace source"]["ok"])

    def test_project_doctor_checks_business_project_without_plugin_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            result = run_kafa("project", "doctor", "--repo", str(root), "--json", check=False)
            report = json.loads(result.stdout)

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(report["ok"], report)
        self.assertEqual(report["kind"], "project")
        self.assertIn("harness initialized", {check["name"] for check in report["checks"]})
        self.assertNotIn("plugin structure", {check["name"] for check in report["checks"]})
        self.assertTrue(report["next_commands"][0].startswith("kafa project init --repo "))

    def test_project_doctor_fails_closed_on_migration_sentinel_without_opening_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sentinel = root / ".ai-team/state/local-core-migration.lock"
            sentinel.parent.mkdir(parents=True)
            sentinel.write_text(
                json.dumps({"pid": 999999, "created_at": "2026-07-12T00:00:00Z"}) + "\n",
                encoding="utf-8",
            )
            (sentinel.parent / "harness.db").touch()

            with patch("sqlite3.connect", side_effect=AssertionError("SQLite must stay closed")):
                report = kafa_cli.project_doctor_report(root)

        initialized = next(check for check in report["checks"] if check["name"] == "harness initialized")
        self.assertFalse(report["ok"])
        self.assertFalse(initialized["ok"])
        self.assertIn("migration-in-progress", initialized["details"])
        self.assertIn(str(sentinel.resolve()), initialized["details"])
        self.assertIn("pid=999999", initialized["details"])
        self.assertIn(
            "inspect the owner and remove it only after confirming no migration is active",
            initialized["details"],
        )
        self.assertEqual(report["next_commands"], [])

    def test_project_launcher_initializes_business_project_without_vendored_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "business"
            root.mkdir()
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)

            package_env = {"PYTHONPATH": str(REPO_ROOT)}
            initialized = run_kafa("project", "init", "--repo", str(root), cwd=root, env=package_env, check=False)
            status = run_kafa("project", "status", "--repo", str(root), cwd=root, env=package_env, check=False)
            quickstart = run_kafa("project", "quickstart", "--repo", str(root), "status", cwd=root, env=package_env, check=False)

        self.assertEqual(initialized.returncode, 0, initialized.stdout + initialized.stderr)
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        self.assertEqual(quickstart.returncode, 0, quickstart.stdout + quickstart.stderr)
        self.assertIn("schema_version:", status.stdout)
        self.assertIn("initialized: true", quickstart.stdout)

    def test_repo_install_writes_marketplace_and_preserves_other_plugins(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = copy_release_repo(Path(temp))
            marketplace = root / ".agents" / "plugins" / "marketplace.json"
            marketplace.parent.mkdir(parents=True)
            marketplace.write_text(
                json.dumps(
                    {
                        "name": "existing",
                        "interface": {"displayName": "Existing"},
                        "plugins": [{"name": "other", "source": {"source": "local", "path": "./plugins/other"}}],
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            run_kafa("plugin", "install", "--repo", str(root))
            data = json.loads(marketplace.read_text(encoding="utf-8"))
            entries = {entry["name"]: entry for entry in data["plugins"]}

        self.assertIn("other", entries)
        self.assertEqual(entries["codex-project-harness"]["source"], {"source": "local", "path": "./plugins/codex-project-harness"})
        self.assertEqual(entries["codex-project-harness"]["policy"], {"installation": "AVAILABLE", "authentication": "ON_INSTALL"})
        self.assertEqual(entries["codex-project-harness"]["category"], "Developer Tools")

    def test_user_install_refuses_overwrite_and_upgrade_refreshes_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = copy_release_repo(Path(temp) / "repo")
            home = Path(temp) / "home"
            env = {"HOME": str(home)}
            source_marker = root / "plugins" / "codex-project-harness" / "marker.txt"
            source_marker.write_text("first\n", encoding="utf-8")

            run_kafa("plugin", "install", "--scope", "user", "--repo", str(root), env=env)
            blocked = run_kafa("plugin", "install", "--scope", "user", "--repo", str(root), env=env, check=False)
            source_marker.write_text("second\n", encoding="utf-8")
            run_kafa("plugin", "upgrade", "--scope", "user", "--repo", str(root), env=env)
            copied = home / ".agents" / "plugins" / "codex-project-harness" / "marker.txt"
            marketplace = json.loads((home / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8"))

            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("target plugin already exists", blocked.stderr)
            self.assertEqual(copied.read_text(encoding="utf-8"), "second\n")
            self.assertEqual(marketplace["plugins"][0]["source"]["path"], "./.agents/plugins/codex-project-harness")

    def test_uninstall_removes_marketplace_entry_and_optionally_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = copy_release_repo(Path(temp) / "repo")
            home = Path(temp) / "home"
            env = {"HOME": str(home)}
            run_kafa("plugin", "install", "--scope", "user", "--repo", str(root), env=env)
            copied = home / ".agents" / "plugins" / "codex-project-harness"

            run_kafa("plugin", "uninstall", "--scope", "user", "--repo", str(root), env=env)
            after_uninstall = json.loads((home / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8"))
            still_exists = copied.exists()
            run_kafa("plugin", "install", "--scope", "user", "--repo", str(root), "--force", env=env)
            run_kafa("plugin", "uninstall", "--scope", "user", "--repo", str(root), "--remove-files", env=env)

        self.assertEqual(after_uninstall["plugins"], [])
        self.assertTrue(still_exists)
        self.assertFalse(copied.exists())

    def test_dry_run_does_not_write_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = copy_release_repo(Path(temp))
            result = run_kafa("plugin", "install", "--repo", str(root), "--dry-run")

        self.assertIn("would write", result.stdout)
        self.assertFalse((root / ".agents").exists())

    def test_validate_structure_checks_packaging_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = copy_release_repo(Path(temp))
            pyproject = root / "pyproject.toml"
            pyproject.write_text(
                pyproject.read_text(encoding="utf-8").replace(
                    f'version = "{RELEASE_PEP440_VERSION}"',
                    'version = "1.15.0b2"',
                ),
                encoding="utf-8",
            )

            result = subprocess.run([sys.executable, str(VALIDATE), str(root / "plugins" / "codex-project-harness")], text=True, capture_output=True, check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pyproject version must match release.json", result.stdout)

    def test_validate_structure_rejects_missing_pyproject(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = copy_release_repo(Path(temp))
            (root / "pyproject.toml").unlink()

            result = subprocess.run([sys.executable, str(VALIDATE), str(root / "plugins" / "codex-project-harness")], text=True, capture_output=True, check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing pyproject.toml", result.stdout)

    def test_validate_structure_rejects_wrong_script_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = copy_release_repo(Path(temp))
            pyproject = root / "pyproject.toml"
            pyproject.write_text(pyproject.read_text(encoding="utf-8").replace('kafa = "kafa.cli:main"', 'kafa = "kafa.bad:main"'), encoding="utf-8")

            result = subprocess.run([sys.executable, str(VALIDATE), str(root / "plugins" / "codex-project-harness")], text=True, capture_output=True, check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pyproject must expose kafa = kafa.cli:main", result.stdout)

    def test_validate_structure_rejects_low_python_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = copy_release_repo(Path(temp))
            pyproject = root / "pyproject.toml"
            pyproject.write_text(pyproject.read_text(encoding="utf-8").replace('requires-python = ">=3.11"', 'requires-python = ">=3.10"'), encoding="utf-8")

            result = subprocess.run([sys.executable, str(VALIDATE), str(root / "plugins" / "codex-project-harness")], text=True, capture_output=True, check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pyproject requires-python must be >=3.11", result.stdout)

    def test_validate_structure_rejects_host_codex_optional_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = copy_release_repo(Path(temp))
            pyproject = root / "pyproject.toml"
            pyproject.write_text(
                pyproject.read_text(encoding="utf-8").replace(
                    "[project.scripts]",
                    '[project.optional-dependencies]\nhost-codex = ["openai-codex>=0.1.0b3"]\n\n[project.scripts]',
                ),
                encoding="utf-8",
            )

            result = subprocess.run([sys.executable, str(VALIDATE), str(root / "plugins" / "codex-project-harness")], text=True, capture_output=True, check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pyproject must not declare the retired Host Codex SDK dependency", result.stdout)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from email.parser import Parser
from pathlib import Path
from unittest.mock import patch

from kafa import cli as kafa_cli
from kafa.codex_app_server import AppServerClient, validate_app_server_discovery


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "codex-project-harness"
VALIDATE = PLUGIN_ROOT / "scripts" / "validate_structure.py"
RELEASE = json.loads((REPO_ROOT / "release.json").read_text(encoding="utf-8"))
RELEASE_VERSION = str(RELEASE["version"])
RELEASE_PEP440_VERSION = str(RELEASE["pep440_version"])


def distribution(cache_root: Path = PLUGIN_ROOT) -> dict[str, object]:
    return kafa_cli.load_distribution_manifest(cache_root)


def run_kafa(*args: str, env: dict[str, str] | None = None, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    command_env = os.environ.copy()
    command_env.setdefault(
        "CODEX_PROJECT_HARNESS_PLUGIN_ROOT", str(PLUGIN_ROOT)
    )
    command_env.setdefault("KAFA_MAINTAINER_RUNTIME", "1")
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


def sqlite_audit_runtime(target: Path, marker: Path) -> Path:
    """Copy a valid runtime whose child process records every SQLite open."""

    shutil.copytree(
        PLUGIN_ROOT,
        target,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    harness = target / "scripts" / "harness.py"
    source = harness.read_text(encoding="utf-8")
    probe = (
        "from __future__ import annotations\n\n"
        "import sys as _sqlite_audit_sys\n"
        "from pathlib import Path as _SqliteAuditPath\n\n"
        "def _record_sqlite_open(event, _args):\n"
        "    if event == 'sqlite3.connect':\n"
        f"        _SqliteAuditPath({str(marker)!r}).write_text('observed', encoding='utf-8')\n"
        "        raise RuntimeError('sqlite3.connect observed by test audit hook')\n\n"
        "_sqlite_audit_sys.addaudithook(_record_sqlite_open)\n"
    )
    future = "from __future__ import annotations\n"
    if source.count(future) != 1:
        raise AssertionError("runtime harness future import marker is not unique")
    harness.write_text(source.replace(future, probe, 1), encoding="utf-8")
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
    manifest = distribution(cache_root)
    event_names = {
        "sessionStart": "SessionStart",
        "subagentStart": "SubagentStart",
        "stop": "Stop",
    }
    app_event_names = {value: key for key, value in event_names.items()}
    expected_skills = {
        f"codex-project-harness:{name}" for name in manifest["skills"]
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
                            "eventName": app_event_names[event],
                            "enabled": True,
                            "source": "plugin",
                            "pluginId": "codex-project-harness@kafa-local",
                            "sourcePath": str(cache_root / "hooks/hooks.json"),
                            "command": (
                                f'python "${{PLUGIN_ROOT}}/hooks/harness_hook.py" '
                                f"{event}"
                            ),
                        }
                        for event in sorted(manifest["hooks"]["events"])
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
            manifest = distribution(cache_root)

        self.assertEqual(report["skill_count"], 7)
        self.assertEqual(
            set(report["skill_names"]),
            {f"codex-project-harness:{name}" for name in manifest["skills"]},
        )
        self.assertEqual(
            set(report["hook_events"]),
            {
                {"SessionStart": "sessionStart", "SubagentStart": "subagentStart", "Stop": "stop"}[event]
                for event in manifest["hooks"]["events"]
            },
        )
        self.assertEqual(report["template_count"], 3)
        self.assertEqual(
            set(report["template_names"]),
            set(manifest["templates"]["native_agents"]),
        )
        self.assertEqual(set(report["runtime_script_names"]), set(manifest["scripts"]))
        self.assertEqual(set(report["schema_names"]), set(manifest["schemas"]))
        self.assertEqual(
            set(report["project_template_names"]),
            set(manifest["templates"]["project_support"]),
        )
        self.assertEqual(set(report["core_names"]), set(manifest["core"]))
        self.assertEqual(
            set(report["hook_file_names"]), set(manifest["hooks"]["files"])
        )
        self.assertEqual(
            set(report["reference_names"]), set(manifest["references"])
        )
        self.assertEqual(
            set(report["public_runtime_domains"]),
            set(manifest["public_runtime_domains"]),
        )
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

            with self.assertRaisesRegex(
                RuntimeError,
                r"(?:template inventory mismatch|distribution inventory.*extra)",
            ):
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

    def test_app_server_discovery_rejects_wrong_or_duplicate_skill_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cache_root = Path(temp) / "cache"
            shutil.copytree(
                PLUGIN_ROOT,
                cache_root,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )

            for case in ("wrong-path", "duplicate-path"):
                with self.subTest(case=case):
                    discovery = app_server_discovery(cache_root)
                    skills = discovery["skills"]["data"][0]["skills"]
                    self.assertGreaterEqual(len(skills), 2)
                    if case == "wrong-path":
                        skill_name = str(skills[0]["name"]).split(":", 1)[1]
                        skills[0]["path"] = str(
                            cache_root
                            / "skills"
                            / skill_name
                            / "agents"
                            / "openai.yaml"
                        )
                    else:
                        skills[1]["path"] = skills[0]["path"]

                    with self.assertRaises(
                        RuntimeError,
                        msg=f"app-server accepted {case} Skill wiring",
                    ):
                        validate_app_server_discovery(
                            discovery,
                            cache_root=cache_root,
                            plugin_id="codex-project-harness@kafa-local",
                            version=RELEASE_VERSION,
                        )

    def test_app_server_discovery_rejects_wrong_hook_source_runner_or_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cache_root = Path(temp) / "cache"
            shutil.copytree(
                PLUGIN_ROOT,
                cache_root,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )

            for case in (
                "source-path",
                "runner",
                "event-argument",
                "event-substring",
            ):
                with self.subTest(case=case):
                    discovery = app_server_discovery(cache_root)
                    hook = discovery["hooks"]["data"][0]["hooks"][0]
                    if case == "source-path":
                        hook["sourcePath"] = str(
                            cache_root / "hooks" / "harness_hook.py"
                        )
                    elif case == "runner":
                        hook["command"] = (
                            'ruby "${PLUGIN_ROOT}/hooks/harness_hook.py" '
                            "SessionStart"
                        )
                    elif case == "event-argument":
                        hook["command"] = (
                            'python "${PLUGIN_ROOT}/hooks/harness_hook.py" Stop'
                        )
                    else:
                        hook["command"] = (
                            'python "${PLUGIN_ROOT}/hooks/harness_hook.py" '
                            "--label=SessionStart-forged"
                        )

                    with self.assertRaises(
                        RuntimeError,
                        msg=f"app-server accepted wrong Hook {case}",
                    ):
                        validate_app_server_discovery(
                            discovery,
                            cache_root=cache_root,
                            plugin_id="codex-project-harness@kafa-local",
                            version=RELEASE_VERSION,
                        )

    def test_app_server_discovery_binds_cache_and_requested_plugin_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cache_root = Path(temp) / "cache"
            shutil.copytree(
                PLUGIN_ROOT,
                cache_root,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            plugin_manifest_path = cache_root / ".codex-plugin" / "plugin.json"
            original_manifest = plugin_manifest_path.read_bytes()

            for field, value in (
                ("name", "wrong-plugin-name"),
                ("version", "0.0.0"),
            ):
                with self.subTest(cache_manifest_field=field):
                    plugin_manifest_path.write_bytes(original_manifest)
                    plugin_manifest = json.loads(
                        plugin_manifest_path.read_text(encoding="utf-8")
                    )
                    plugin_manifest[field] = value
                    plugin_manifest_path.write_text(
                        json.dumps(plugin_manifest, indent=2) + "\n",
                        encoding="utf-8",
                    )
                    with self.assertRaises(
                        RuntimeError,
                        msg=f"app-server accepted cache plugin {field} mismatch",
                    ):
                        validate_app_server_discovery(
                            app_server_discovery(cache_root),
                            cache_root=cache_root,
                            plugin_id="codex-project-harness@kafa-local",
                            version=RELEASE_VERSION,
                        )

            plugin_manifest_path.write_bytes(original_manifest)
            mismatch_cases = (
                ("requested-id", "wrong-plugin@kafa-local", RELEASE_VERSION),
                (
                    "local-version",
                    "codex-project-harness@kafa-local",
                    "0.0.0",
                ),
            )
            for case, plugin_id, version in mismatch_cases:
                with self.subTest(case=case):
                    with self.assertRaises(RuntimeError):
                        validate_app_server_discovery(
                            app_server_discovery(cache_root),
                            cache_root=cache_root,
                            plugin_id=plugin_id,
                            version=version,
                        )

    def test_app_server_discovery_rejects_undeclared_nested_cache_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cache_root = Path(temp) / "cache"
            shutil.copytree(
                PLUGIN_ROOT,
                cache_root,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            nested_extra = cache_root / "core" / "undeclared" / "extra.py"
            nested_extra.parent.mkdir()
            nested_extra.write_text("raise RuntimeError('must not load')\n", encoding="utf-8")

            with self.assertRaises(
                RuntimeError,
                msg="app-server accepted an undeclared nested cache file",
            ):
                validate_app_server_discovery(
                    app_server_discovery(cache_root),
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

    def test_source_distribution_manifest_contains_installable_release_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            release = Path(temp) / "release"
            release.mkdir()
            for name in ["VERSION", "README.md", "pyproject.toml", "release.json"]:
                shutil.copyfile(REPO_ROOT / name, release / name)
            manifest = REPO_ROOT / "MANIFEST.in"
            if manifest.is_file():
                shutil.copyfile(manifest, release / manifest.name)
            shutil.copytree(
                REPO_ROOT / "kafa",
                release / "kafa",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            shutil.copytree(
                PLUGIN_ROOT,
                release / "plugins/codex-project-harness",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "wheel",
                    "--no-deps",
                    ".",
                    "--wheel-dir",
                    str(Path(temp) / "dist"),
                ],
                cwd=release,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            sources = set(
                (release / "kafa.egg-info/SOURCES.txt").read_text(encoding="utf-8").splitlines()
            )

        self.assertIn("release.json", sources)
        self.assertIn("VERSION", sources)
        self.assertIn("plugins/codex-project-harness/.codex-plugin/plugin.json", sources)
        self.assertIn("plugins/codex-project-harness/scripts/harness.py", sources)
        self.assertFalse(any("__pycache__" in name or name.endswith(".pyc") for name in sources))

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

    def test_plugin_install_preserves_workflow_and_delegation_references(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = copy_release_repo(Path(temp) / "repo")
            home = Path(temp) / "home"
            env = {"HOME": str(home), "CODEX_HOME": str(home / ".codex")}
            run_kafa("plugin", "install", "--scope", "user", "--repo", str(root), env=env)
            installed = home / ".agents/plugins/codex-project-harness"
            source_reference = root / "plugins/codex-project-harness/references/delegation-matrix.md"
            installed_reference = installed / "references/delegation-matrix.md"
            source_workflow = root / "plugins/codex-project-harness/references/workflow-contract.json"
            installed_workflow = installed / "references/workflow-contract.json"
            installed_skill = (installed / "skills/project-harness/SKILL.md").read_text(encoding="utf-8")
            installed_exists = installed_reference.is_file()
            source_bytes = source_reference.read_bytes()
            installed_bytes = installed_reference.read_bytes()
            source_workflow_bytes = source_workflow.read_bytes()
            installed_workflow_bytes = installed_workflow.read_bytes()

        self.assertTrue(installed_exists)
        self.assertEqual(installed_bytes, source_bytes)
        self.assertEqual(installed_workflow_bytes, source_workflow_bytes)
        self.assertEqual(json.loads(installed_workflow_bytes)["contract_version"], 1)
        self.assertIn("references/delegation-matrix.md", installed_skill)
        self.assertIn("BEGIN GENERATED: workflow-contract:entry-workflow", installed_skill)

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
            envelope = json.loads(result.stdout)
            report = envelope["details"]

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            set(envelope),
            {"state", "blockers", "actions", "details"},
        )
        self.assertTrue(envelope["blockers"])
        self.assertFalse(report["ok"], report)
        self.assertEqual(report["kind"], "project")
        self.assertIn("harness initialized", {check["name"] for check in report["checks"]})
        self.assertNotIn("plugin structure", {check["name"] for check in report["checks"]})
        self.assertTrue(report["next_commands"][0].startswith("kafa project init --repo "))

    def test_project_doctor_fails_on_real_foreign_key_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(
                ["git", "init"],
                cwd=root,
                check=True,
                text=True,
                capture_output=True,
            )
            initialized = run_kafa(
                "project", "init", "--repo", str(root), check=False
            )
            self.assertEqual(
                initialized.returncode,
                0,
                initialized.stdout + initialized.stderr,
            )
            (root / ".gitignore").write_text("user-rule.log\n", encoding="utf-8")
            conn = sqlite3.connect(root / ".ai-team/state/harness.db")
            try:
                conn.execute("pragma foreign_keys = off")
                conn.execute(
                    "insert into task_acceptance "
                    "(cycle_id, task_id, acceptance_id) values (?, ?, ?)",
                    ("CYCLE-current", "missing-task", "missing-acceptance"),
                )
                conn.commit()
            finally:
                conn.close()

            concise = run_kafa(
                "project", "doctor", "--repo", str(root), check=False
            )
            verbose = run_kafa(
                "project", "doctor", "--repo", str(root), "--verbose", check=False
            )
            json_result = run_kafa(
                "project", "doctor", "--repo", str(root), "--json", check=False
            )

        self.assertNotEqual(concise.returncode, 0)
        self.assertEqual(len(concise.stdout.splitlines()), 3)
        self.assertIn("[foreign-key-integrity]", concise.stdout)
        self.assertNotEqual(verbose.returncode, 0)
        self.assertLess(
            verbose.stdout.index("foreign-key-integrity"),
            verbose.stdout.index("gitignore-missing"),
        )
        self.assertNotEqual(json_result.returncode, 0)
        self.assertEqual(json_result.stderr, "")
        payload = json.loads(json_result.stdout)
        codes = [blocker["code"] for blocker in payload["blockers"]]
        self.assertEqual(codes[0], "foreign-key-integrity")
        self.assertIn("runtime", payload["details"])
        self.assertEqual(
            payload["details"]["runtime"]["blockers"][0]["code"],
            "foreign-key-integrity",
        )

    def test_project_doctor_fails_closed_on_migration_sentinel_without_opening_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            marker = base / "sqlite-opened"
            runtime = sqlite_audit_runtime(base / "runtime", marker)
            sentinel = root / ".ai-team/state/local-core-migration.lock"
            sentinel.parent.mkdir(parents=True)
            sentinel.write_text(
                json.dumps({"pid": 999999, "created_at": "2026-07-12T00:00:00Z"}) + "\n",
                encoding="utf-8",
            )
            (sentinel.parent / "harness.db").touch()

            report = kafa_cli.project_doctor_report(
                root,
                authority=kafa_cli.validate_project_runtime_root(
                    runtime, label="test runtime"
                ),
            )
            sqlite_opened = marker.exists()

        self.assertFalse(sqlite_opened)
        initialized = next(check for check in report["checks"] if check["name"] == "harness initialized")
        self.assertFalse(report["ok"])
        self.assertFalse(initialized["ok"])
        self.assertIn("migration-in-progress", initialized["details"])
        self.assertIn(str(sentinel.resolve()), initialized["details"])
        self.assertIn("pid=999999", initialized["details"])
        self.assertIn(
            "confirm no migration is active, and verify database/projection authority before considering sentinel removal",
            initialized["details"],
        )
        self.assertEqual(report["next_commands"], [])

    def test_project_doctor_reports_recovery_required_manifest_without_opening_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            marker = base / "sqlite-opened"
            runtime = sqlite_audit_runtime(base / "runtime", marker)
            sentinel = root / ".ai-team/state/local-core-migration.lock"
            manifest = root / ".ai-team/backups/schema-29/migration-manifest.json"
            sentinel.parent.mkdir(parents=True)
            sentinel.write_text(
                json.dumps(
                    {
                        "pid": 999999,
                        "created_at": "2026-07-12T00:00:00Z",
                        "target_schema": 30,
                        "status": "rollback-incomplete",
                        "manifest_path": str(manifest.resolve()),
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            report = kafa_cli.project_doctor_report(
                root,
                authority=kafa_cli.validate_project_runtime_root(
                    runtime, label="test runtime"
                ),
            )
            sqlite_opened = marker.exists()

        self.assertFalse(sqlite_opened)
        initialized = next(
            check for check in report["checks"] if check["name"] == "harness initialized"
        )
        self.assertFalse(report["ok"])
        self.assertFalse(initialized["ok"])
        self.assertIn("rollback-incomplete", initialized["details"])
        self.assertIn(str(manifest.resolve()), initialized["details"])
        self.assertIn("recover and verify", initialized["details"])
        self.assertIn("do not remove", initialized["details"])
        self.assertEqual(report["next_commands"], [])

    def test_project_doctor_rejects_linked_sentinel_via_runtime_audit(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink primitive unavailable")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            marker = base / "sqlite-opened"
            runtime = sqlite_audit_runtime(base / "runtime", marker)
            state = root / ".ai-team/state"
            state.mkdir(parents=True)
            external = base / "outside-sentinel.json"
            external.write_text(
                json.dumps({"pid": 999999, "created_at": "outside"}) + "\n",
                encoding="utf-8",
            )
            before = external.read_bytes()
            (state / "local-core-migration.lock").symlink_to(external)

            report = kafa_cli.project_doctor_report(
                root,
                authority=kafa_cli.validate_project_runtime_root(
                    runtime, label="test runtime"
                ),
            )
            sqlite_opened = marker.exists()
            self.assertEqual(external.read_bytes(), before)

        self.assertFalse(sqlite_opened)
        initialized = next(
            check for check in report["checks"] if check["name"] == "harness initialized"
        )
        self.assertFalse(report["ok"])
        self.assertFalse(initialized["ok"])
        self.assertIn(
            "unsafe-project-path: .ai-team/state/local-core-migration.lock",
            initialized["details"],
        )
        self.assertEqual(report["next_commands"], [])

    def test_project_doctor_sqlite_audit_probe_observes_initialized_control(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            marker = base / "sqlite-opened"
            root.mkdir()
            initialized = run_kafa(
                "project",
                "init",
                "--repo",
                str(root),
                check=False,
            )
            self.assertEqual(
                initialized.returncode,
                0,
                initialized.stdout + initialized.stderr,
            )
            runtime = sqlite_audit_runtime(base / "runtime", marker)
            authority = kafa_cli.validate_project_runtime_root(
                runtime,
                label="test runtime",
            )

            with self.assertRaisesRegex(
                kafa_cli.KafaError,
                "emitted stderr",
            ):
                kafa_cli.project_doctor_report(root, authority=authority)
            sqlite_opened = marker.exists()

        self.assertTrue(sqlite_opened)

    def test_project_doctor_uses_single_gitignore_probe_without_reopening_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            initialized = run_kafa(
                "project",
                "init",
                "--repo",
                str(root),
                check=False,
            )
            self.assertEqual(
                initialized.returncode,
                0,
                initialized.stdout + initialized.stderr,
            )
            gitignore = root / ".gitignore"
            gitignore.write_text(".ai-team/state/\n", encoding="utf-8")
            replacement = root / ".gitignore.replacement"
            replacement.write_text(
                ".ai-team/state/\n.ai-team/backups/\n.ai-team/runtime/\n"
                "__pycache__/\n*.pyc\n",
                encoding="utf-8",
            )
            exchange_called = False

            def exchange_after_report(_repo: Path, _runtime: dict[str, object]) -> None:
                nonlocal exchange_called
                exchange_called = True
                os.replace(replacement, gitignore)

            with patch.object(
                kafa_cli,
                "_after_project_doctor_runtime_report",
                side_effect=exchange_after_report,
                create=True,
            ):
                report = kafa_cli.project_doctor_report(
                    root,
                    authority=kafa_cli.validate_project_runtime_root(
                        PLUGIN_ROOT.resolve(), label="test runtime"
                    ),
                )

            runtime_gitignore = next(
                check
                for check in report["checks"]
                if check["name"] == "runtime gitignore"
            )
            self.assertTrue(exchange_called)
            self.assertFalse(runtime_gitignore["ok"])
            self.assertIn(".ai-team/runtime/", runtime_gitignore["details"])

    def test_project_launcher_initializes_business_project_without_vendored_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "business"
            root.mkdir()
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)

            package_env = {"PYTHONPATH": str(REPO_ROOT)}
            initialized = run_kafa("project", "init", "--repo", str(root), cwd=root, env=package_env, check=False)
            status = run_kafa("project", "status", "--repo", str(root), cwd=root, env=package_env, check=False)
            quickstart = run_kafa("project", "quickstart", "--repo", str(root), "status", cwd=root, env=package_env, check=False)
            verbose_status = run_kafa("project", "status", "--repo", str(root), "--verbose", cwd=root, env=package_env, check=False)
            verbose_quickstart = run_kafa("project", "quickstart", "--repo", str(root), "status", "--verbose", cwd=root, env=package_env, check=False)

        self.assertEqual(initialized.returncode, 0, initialized.stdout + initialized.stderr)
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        self.assertEqual(quickstart.returncode, 0, quickstart.stdout + quickstart.stderr)
        self.assertEqual(len(status.stdout.splitlines()), 3)
        self.assertEqual(len(quickstart.stdout.splitlines()), 3)
        self.assertIn("schema_version:", verbose_status.stdout)
        self.assertIn("initialized: true", verbose_quickstart.stdout)

    def test_project_launcher_routes_status_and_quickstart_output_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "business"
            root.mkdir()
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
            package_env = {"PYTHONPATH": str(REPO_ROOT)}
            run_kafa("project", "init", "--repo", str(root), cwd=root, env=package_env)

            status_json = run_kafa(
                "project", "status", "--repo", str(root), "--json",
                cwd=root, env=package_env,
            )
            quickstart_json = run_kafa(
                "project", "quickstart", "--repo", str(root),
                "status", "--json", cwd=root, env=package_env,
            )

        for result in (status_json, quickstart_json):
            with self.subTest(stdout=result.stdout[:80]):
                self.assertEqual(result.stderr, "")
                self.assertEqual(
                    set(json.loads(result.stdout)),
                    {"state", "blockers", "actions", "details"},
                )

    def test_project_doctor_recovery_json_is_first_and_has_no_init_action(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
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

            result = run_kafa(
                "project", "doctor", "--repo", str(root), "--json",
                check=False,
            )
            envelope = json.loads(result.stdout)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertEqual(envelope["state"], "recovery-required")
        self.assertEqual(envelope["blockers"][0]["code"], "rollback-incomplete")
        self.assertEqual(envelope["actions"], [])
        self.assertEqual(envelope["details"]["next_commands"], [])

    def test_project_doctor_existing_unreadable_database_never_recommends_init(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / ".ai-team/state/harness.db"
            database.parent.mkdir(parents=True)
            database.write_bytes(b"not-a-sqlite-database")

            result = run_kafa(
                "project", "doctor", "--repo", str(root), "--json",
                check=False,
            )
            envelope = json.loads(result.stdout)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertEqual(envelope["state"], "error")
        self.assertEqual(envelope["blockers"][0]["code"], "runtime-error")
        self.assertEqual(envelope["actions"], [])
        self.assertEqual(envelope["details"]["next_commands"], [])

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
            source_marker = (
                root
                / "plugins"
                / "codex-project-harness"
                / "scripts"
                / "fixtures"
                / "schema27-v1.21.3-seed.sql"
            )
            original_marker = source_marker.read_bytes()
            first_marker = original_marker + b"\n-- install-copy-marker:first\n"
            second_marker = original_marker + b"\n-- install-copy-marker:second\n"
            source_marker.write_bytes(first_marker)

            run_kafa("plugin", "install", "--scope", "user", "--repo", str(root), env=env)
            blocked = run_kafa("plugin", "install", "--scope", "user", "--repo", str(root), env=env, check=False)
            source_marker.write_bytes(second_marker)
            run_kafa("plugin", "upgrade", "--scope", "user", "--repo", str(root), env=env)
            copied = (
                home
                / ".agents"
                / "plugins"
                / "codex-project-harness"
                / "scripts"
                / "fixtures"
                / "schema27-v1.21.3-seed.sql"
            )
            marketplace = json.loads((home / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8"))

            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("target plugin already exists", blocked.stderr)
            self.assertEqual(copied.read_bytes(), second_marker)
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

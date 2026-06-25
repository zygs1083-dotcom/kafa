from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "codex-project-harness"
VALIDATE = PLUGIN_ROOT / "scripts" / "validate_structure.py"


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
    shutil.copyfile(REPO_ROOT / "pyproject.toml", target / "pyproject.toml")
    return target


class InstallReleaseTest(unittest.TestCase):
    def test_kafa_version_reports_repository_version(self) -> None:
        result = run_kafa("--version")
        self.assertEqual(result.stdout.strip(), "1.16.0-beta.1")

    def test_doctor_reports_repo_health_as_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = copy_release_repo(Path(temp))

            result = run_kafa("doctor", "--repo", str(root), "--json")
            report = json.loads(result.stdout)

        self.assertTrue(report["ok"], report)
        self.assertEqual(report["scope"], "repo")
        self.assertIn("plugin structure", {check["name"] for check in report["checks"]})

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
            self.assertEqual(marketplace["plugins"][0]["source"]["path"], "./codex-project-harness")

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
            pyproject.write_text(pyproject.read_text(encoding="utf-8").replace('version = "1.16.0b1"', 'version = "1.15.0b2"'), encoding="utf-8")

            result = subprocess.run([sys.executable, str(VALIDATE), str(root / "plugins" / "codex-project-harness")], text=True, capture_output=True, check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pyproject version must match root VERSION", result.stdout)

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


if __name__ == "__main__":
    unittest.main()

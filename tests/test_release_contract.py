from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.run_isolated_install_smoke import codex_command


REPO_ROOT = Path(__file__).resolve().parents[1]


def run_release(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "kafa.release", "--repo", str(repo), "--json", *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def copy_release_source(target: Path) -> Path:
    shutil.copytree(
        REPO_ROOT,
        target,
        ignore=shutil.ignore_patterns(".git", ".venv", ".ai-team", "build", "*.egg-info", "__pycache__", "*.pyc"),
    )
    return target


class ReleaseContractTest(unittest.TestCase):
    def test_development_release_manifest_aligns_all_version_sources(self) -> None:
        result = run_release(REPO_ROOT)
        report = json.loads(result.stdout)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["manifest"]["release_state"], "development")
        self.assertEqual(report["manifest"]["tag"], "v1.25.0-beta.1")

    def test_release_contract_rejects_package_version_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = copy_release_source(Path(temp) / "release")
            pyproject = root / "pyproject.toml"
            pyproject.write_text(
                pyproject.read_text(encoding="utf-8").replace('version = "1.25.0b1"', 'version = "9.9.9"'),
                encoding="utf-8",
            )
            result = run_release(root)
            report = json.loads(result.stdout)
            checks = {item["name"]: item for item in report["checks"]}

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(checks["package version alignment"]["ok"])

    def test_require_tag_rejects_development_manifest(self) -> None:
        result = run_release(REPO_ROOT, "--require-tag")
        report = json.loads(result.stdout)
        checks = {item["name"]: item for item in report["checks"]}

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(checks["tag release state"]["ok"])
        self.assertFalse(checks["tag points at HEAD"]["ok"])

    def test_release_manifest_accepts_matching_tagged_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = copy_release_source(Path(temp) / "release")
            manifest_path = root / "release.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["release_state"] = "release"
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            changelog = root / "CHANGELOG.md"
            changelog.write_text(
                changelog.read_text(encoding="utf-8").replace(
                    "## v1.25.0-beta.1 - Unreleased",
                    "## v1.25.0-beta.1 - 2026-07-10",
                    1,
                ),
                encoding="utf-8",
            )
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "release@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Release Test"], cwd=root, check=True)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "release"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "tag", "v1.25.0-beta.1"], cwd=root, check=True)

            result = run_release(root, "--require-tag")
            report = json.loads(result.stdout)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(report["ok"], report)

    def test_require_tag_rejects_dirty_worktree_after_tag(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = copy_release_source(Path(temp) / "release")
            manifest_path = root / "release.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["release_state"] = "release"
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            changelog = root / "CHANGELOG.md"
            changelog.write_text(
                changelog.read_text(encoding="utf-8").replace(
                    "## v1.25.0-beta.1 - Unreleased", "## v1.25.0-beta.1 - 2026-07-10", 1
                ),
                encoding="utf-8",
            )
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "release@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Release Test"], cwd=root, check=True)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "release"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "tag", "v1.25.0-beta.1"], cwd=root, check=True)
            (root / "CHANGELOG.md").write_text("dirty\n", encoding="utf-8")

            result = run_release(root, "--require-tag")
            report = json.loads(result.stdout)
            checks = {item["name"]: item for item in report["checks"]}

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(checks["tag worktree clean"]["ok"])

    def test_release_contract_rejects_stale_schema_in_release_notes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = copy_release_source(Path(temp) / "release")
            changelog = root / "CHANGELOG.md"
            changelog.write_text(
                changelog.read_text(encoding="utf-8").replace("schema 29", "schema 28", 1),
                encoding="utf-8",
            )
            result = run_release(root)
            report = json.loads(result.stdout)
            checks = {item["name"]: item for item in report["checks"]}

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(checks["release notes runtime facts"]["ok"])

    def test_release_workflow_is_tag_gated_and_runs_real_install_smoke(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

        self.assertIn("tags:", workflow)
        self.assertIn("v*", workflow)
        self.assertIn("python -m kafa.release --require-tag", workflow)
        self.assertIn("run_isolated_install_smoke.py", workflow)
        self.assertIn("needs: verify", workflow)
        self.assertIn("--wheel dist/*.whl", workflow)
        self.assertIn("--source-archive dist/*-source.tar.gz", workflow)
        self.assertIn("gh release create", workflow)
        self.assertIn("--prerelease", workflow)

    def test_install_smoke_wraps_windows_npm_command_shims(self) -> None:
        command = codex_command(r"C:\npm\codex.cmd", "plugin", "list", "--json", platform_name="nt")

        self.assertEqual(command[:4], ["cmd.exe", "/d", "/s", "/c"])
        self.assertEqual(command[4:], [r"C:\npm\codex.cmd", "plugin", "list", "--json"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.run_isolated_install_smoke import codex_command


REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_MANIFEST = json.loads((REPO_ROOT / "release.json").read_text(encoding="utf-8"))
RELEASE_VERSION = str(RELEASE_MANIFEST["version"])
RELEASE_PEP440_VERSION = str(RELEASE_MANIFEST["pep440_version"])
RELEASE_TAG = str(RELEASE_MANIFEST["tag"])
RELEASE_PACKAGE = str(RELEASE_MANIFEST["package"])
RELEASE_RUNTIME_VERSION = str(RELEASE_MANIFEST["runtime_version"])
RELEASE_SCHEMA_VERSION = int(RELEASE_MANIFEST["schema_version_runtime"])
STALE_SCHEMA_VERSION = RELEASE_SCHEMA_VERSION - 1


def run_release(repo: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    command_env = os.environ.copy()
    command_env.pop("GITHUB_REF_NAME", None)
    if env:
        command_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "kafa.release", "--repo", str(repo), "--json", *args],
        cwd=REPO_ROOT,
        env=command_env,
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
        self.assertEqual(report["manifest"]["version"], RELEASE_VERSION)
        self.assertEqual(report["manifest"]["pep440_version"], RELEASE_PEP440_VERSION)
        self.assertEqual(report["manifest"]["tag"], RELEASE_TAG)
        self.assertEqual(report["manifest"]["package"], RELEASE_PACKAGE)
        self.assertEqual(report["manifest"]["runtime_version"], RELEASE_RUNTIME_VERSION)
        self.assertEqual(report["manifest"]["schema_version_runtime"], RELEASE_SCHEMA_VERSION)

    def test_release_contract_rejects_package_version_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = copy_release_source(Path(temp) / "release")
            pyproject = root / "pyproject.toml"
            pyproject.write_text(
                pyproject.read_text(encoding="utf-8").replace(
                    f'version = "{RELEASE_PEP440_VERSION}"',
                    'version = "9.9.9"',
                ),
                encoding="utf-8",
            )
            result = run_release(root)
            report = json.loads(result.stdout)
            checks = {item["name"]: item for item in report["checks"]}

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(checks["package version alignment"]["ok"])

    def test_release_contract_rejects_runtime_identity_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = copy_release_source(Path(temp) / "release")
            identity = root / "plugins/codex-project-harness/core/__init__.py"
            identity.write_text(
                identity.read_text(encoding="utf-8").replace(
                    f'RUNTIME_VERSION = "{RELEASE_RUNTIME_VERSION}"',
                    'RUNTIME_VERSION = "9.9.9"',
                ),
                encoding="utf-8",
            )
            result = run_release(root)
            report = json.loads(result.stdout)
            checks = {item["name"]: item for item in report["checks"]}

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(checks["runtime version alignment"]["ok"])
        self.assertFalse(checks["kernel version alignment"]["ok"])

    def test_release_contract_rejects_current_documentation_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = copy_release_source(Path(temp) / "release")
            readme = root / "README.md"
            readme.write_text(
                readme.read_text(encoding="utf-8").replace(
                    f"v{RELEASE_VERSION}", "v9.9.9-beta.9", 1
                ),
                encoding="utf-8",
            )
            result = run_release(root)
            report = json.loads(result.stdout)
            checks = {item["name"]: item for item in report["checks"]}

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(checks["current documentation runtime facts"]["ok"])

    def test_release_contract_rejects_literal_module_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = copy_release_source(Path(temp) / "release")
            module = root / "kafa/__init__.py"
            module.write_text(
                module.read_text(encoding="utf-8").replace(
                    "__version__ = release_version()",
                    f'__version__ = "{RELEASE_VERSION}"',
                ),
                encoding="utf-8",
            )
            result = run_release(root)
            report = json.loads(result.stdout)
            checks = {item["name"]: item for item in report["checks"]}

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(checks["module version derivation"]["ok"])

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
                    f"## {RELEASE_TAG} - Unreleased",
                    f"## {RELEASE_TAG} - 2026-07-10",
                    1,
                ),
                encoding="utf-8",
            )
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "release@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Release Test"], cwd=root, check=True)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "release"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "tag", RELEASE_TAG], cwd=root, check=True)

            result = run_release(root, "--require-tag", env={"GITHUB_REF_NAME": RELEASE_TAG})
            report = json.loads(result.stdout)
            mismatched = run_release(root, "--require-tag", env={"GITHUB_REF_NAME": "main"})
            mismatched_report = json.loads(mismatched.stdout)
            mismatched_checks = {item["name"]: item for item in mismatched_report["checks"]}

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(report["ok"], report)
        self.assertNotEqual(mismatched.returncode, 0)
        self.assertFalse(mismatched_checks["workflow tag"]["ok"])

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
                    f"## {RELEASE_TAG} - Unreleased", f"## {RELEASE_TAG} - 2026-07-10", 1
                ),
                encoding="utf-8",
            )
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "release@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Release Test"], cwd=root, check=True)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "release"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "tag", RELEASE_TAG], cwd=root, check=True)
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
                changelog.read_text(encoding="utf-8").replace(
                    f"schema {RELEASE_SCHEMA_VERSION}",
                    f"schema {STALE_SCHEMA_VERSION}",
                ),
                encoding="utf-8",
            )
            result = run_release(root)
            report = json.loads(result.stdout)
            checks = {item["name"]: item for item in report["checks"]}

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(checks["release notes runtime facts"]["ok"])

    def test_release_contract_rejects_conflicting_schema_claims(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = copy_release_source(Path(temp) / "release")
            changelog = root / "CHANGELOG.md"
            changelog.write_text(
                changelog.read_text(encoding="utf-8").replace(
                    "### Boundaries",
                    (
                        "- This release also claims stale schema "
                        f"{STALE_SCHEMA_VERSION} compatibility.\n\n### Boundaries"
                    ),
                    1,
                ),
                encoding="utf-8",
            )
            result = run_release(root)
            report = json.loads(result.stdout)
            checks = {item["name"]: item for item in report["checks"]}

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(checks["release notes runtime facts"]["ok"])
        self.assertIn(str(STALE_SCHEMA_VERSION), checks["release notes runtime facts"]["details"])

    def test_release_workflow_is_tag_gated_and_runs_real_install_smoke(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

        self.assertIn("tags:", workflow)
        self.assertIn("v*", workflow)
        self.assertIn("python -m kafa.release --require-tag", workflow)
        self.assertIn("run_isolated_install_smoke.py", workflow)
        self.assertIn("- verify", workflow)
        self.assertIn("- real_host_compatibility", workflow)
        self.assertIn("--wheel dist/*.whl", workflow)
        self.assertIn("--source-archive dist/*-source.tar.gz", workflow)
        self.assertIn("gh release create", workflow)
        self.assertIn("--prerelease", workflow)
        self.assertIn("-W error::ResourceWarning", workflow)
        self.assertIn("skills/project-harness/scripts/harness.py", workflow)
        self.assertNotIn("skills/project-runtime", workflow)

    def test_validate_workflow_keeps_three_platform_local_gates(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "validate.yml").read_text(
            encoding="utf-8"
        )

        for runner in ["ubuntu-latest", "macos-latest", "windows-latest"]:
            self.assertIn(f"os: {runner}", workflow)
        self.assertIn("run_isolated_install_smoke.py --repo .", workflow)
        self.assertIn("run_agent_e2e_eval.py --mode fixture", workflow)
        self.assertIn("run_agent_e2e_eval.py --mode stability", workflow)
        self.assertIn("run_skill_eval.py", workflow)
        self.assertIn("-W error::ResourceWarning", workflow)
        self.assertIn("skills/project-harness/scripts/harness.py", workflow)
        self.assertNotIn("skills/project-runtime", workflow)
        self.assertNotIn(" kernel doctor", workflow)
        self.assertNotIn(" invariant validate", workflow)

    def test_publish_requires_a_non_optional_real_host_compatibility_profile(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertIn("\n  real_host_compatibility:", workflow)
        compatibility = workflow.split("\n  real_host_compatibility:", 1)[1].split("\n  publish:", 1)[0]
        publish = workflow.split("\n  publish:", 1)[1]

        self.assertIn("runs-on: [self-hosted, kafa-codex-live]", compatibility)
        self.assertIn("environment: codex-live-release", compatibility)
        self.assertIn('HARNESS_E2E_ENABLE_LIVE_CODEX: "1"', compatibility)
        self.assertIn('HARNESS_E2E_LIVE_TIMEOUT: "900"', compatibility)
        self.assertNotIn("HARNESS_E2E_LIVE_TIMEOUT_SECONDS", compatibility)
        self.assertIn("run_agent_e2e_eval.py --mode live-codex --out", compatibility)
        self.assertIn("actions/upload-artifact@v4", compatibility)
        self.assertNotIn("continue-on-error: true", compatibility)
        self.assertRegex(
            publish,
            re.compile(r"\n    needs:\s*\n      - verify\s*\n      - real_host_compatibility\s*\n"),
        )

    def test_install_smoke_wraps_windows_npm_command_shims(self) -> None:
        command = codex_command(r"C:\npm\codex.cmd", "plugin", "list", "--json", platform_name="nt")

        self.assertEqual(Path(command[0]).name.lower(), "cmd.exe")
        self.assertEqual(command[1:4], ["/d", "/s", "/c"])
        self.assertEqual(command[4:], [r"C:\npm\codex.cmd", "plugin", "list", "--json"])


if __name__ == "__main__":
    unittest.main()

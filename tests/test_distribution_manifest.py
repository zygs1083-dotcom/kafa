from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from kafa.cli import (
    KafaError,
    load_distribution_manifest as load_package_manifest,
    static_hook_definition,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins/codex-project-harness"
SCRIPTS = PLUGIN_ROOT / "scripts"
DECLARED_NESTED_FILES = [
    "scripts/fixtures/schema27-v1.21.3-seed.sql",
    "scripts/fixtures/schema27-v1.21.3.sql.gz.b64",
]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from harness_lib import (  # noqa: E402
    DistributionManifestError,
    load_distribution_manifest as load_plugin_manifest,
)


class DistributionManifestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = json.loads(
            (PLUGIN_ROOT / "references/distribution-manifest.json").read_text(
                encoding="utf-8"
            )
        )

    def write_manifest(self, root: Path, payload: object | None = None) -> Path:
        path = root / "references/distribution-manifest.json"
        path.parent.mkdir(parents=True)
        if payload is not None:
            path.write_text(
                json.dumps(payload, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        return path

    def assert_both_reject(self, root: Path, pattern: str) -> None:
        with self.assertRaisesRegex(KafaError, pattern):
            load_package_manifest(root)
        with self.assertRaisesRegex(DistributionManifestError, pattern):
            load_plugin_manifest(root)

    def test_package_and_self_contained_loaders_agree(self) -> None:
        package = load_package_manifest(PLUGIN_ROOT)
        plugin = load_plugin_manifest(PLUGIN_ROOT)

        self.assertEqual(package, plugin)
        self.assertEqual(len(package["skills"]), 7)
        self.assertEqual(len(package["hooks"]["events"]), 3)
        self.assertEqual(len(package["templates"]["native_agents"]), 3)
        self.assertEqual(len(package["schemas"]), 18)

    def test_missing_and_invalid_json_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assert_both_reject(root, "invalid distribution manifest")
            path = self.write_manifest(root)
            path.write_text("{not-json\n", encoding="utf-8")
            self.assert_both_reject(root, "invalid distribution manifest")

    def test_duplicate_and_unknown_keys_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = self.write_manifest(root)
            path.write_text(
                '{"manifest_version":1,"manifest_version":1}\n',
                encoding="utf-8",
            )
            self.assert_both_reject(root, "duplicate distribution manifest key")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload = dict(self.payload)
            payload["unexpected"] = []
            self.write_manifest(root, payload)
            self.assert_both_reject(root, "keys mismatch")

    def test_version_and_plugin_identity_fail_closed(self) -> None:
        for key, value, pattern in (
            ("manifest_version", True, "integer 1"),
            ("manifest_version", 2, "integer 1"),
            ("plugin_name", "other", "plugin_name"),
        ):
            with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                payload = json.loads(json.dumps(self.payload))
                payload[key] = value
                self.write_manifest(root, payload)
                self.assert_both_reject(root, pattern)

    def test_unsafe_and_duplicate_names_fail_closed(self) -> None:
        for skills, pattern in (
            (["../escape"], "unsafe basename"),
            (["same", "same"], "duplicate entries"),
        ):
            with self.subTest(skills=skills), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                payload = json.loads(json.dumps(self.payload))
                payload["skills"] = skills
                self.write_manifest(root, payload)
                self.assert_both_reject(root, pattern)

    def test_additional_files_accept_declared_nested_fixture_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload = json.loads(json.dumps(self.payload))
            payload["additional_files"] = DECLARED_NESTED_FILES
            self.write_manifest(root, payload)

            package = load_package_manifest(root)
            plugin = load_plugin_manifest(root)

        self.assertEqual(package, plugin)
        self.assertEqual(
            package["additional_files"],
            tuple(DECLARED_NESTED_FILES),
        )

    def test_additional_files_reject_unsafe_duplicate_and_overlapping_paths(
        self,
    ) -> None:
        cases = (
            (["../escape"], r"additional files.*unsafe"),
            (
                [DECLARED_NESTED_FILES[0], DECLARED_NESTED_FILES[0]],
                r"additional files.*duplicate",
            ),
            (["core/api.py"], r"additional files.*overlap"),
        )
        for additional_files, pattern in cases:
            with (
                self.subTest(additional_files=additional_files),
                tempfile.TemporaryDirectory() as temp,
            ):
                root = Path(temp)
                payload = json.loads(json.dumps(self.payload))
                payload["additional_files"] = additional_files
                self.write_manifest(root, payload)
                self.assert_both_reject(root, pattern)

    def test_self_reference_and_doctor_domain_are_mandatory(self) -> None:
        for field, value, pattern in (
            (
                "references",
                ["delegation-matrix.md", "workflow-contract.json"],
                "must include distribution-manifest.json",
            ),
            (
                "public_runtime_domains",
                [name for name in self.payload["public_runtime_domains"] if name != "doctor"],
                "must include doctor",
            ),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                payload = json.loads(json.dumps(self.payload))
                payload[field] = value
                self.write_manifest(root, payload)
                self.assert_both_reject(root, pattern)

    def test_runtime_domain_names_use_closed_command_grammar(self) -> None:
        for invalid in ("--help", "has space", "a:b", "Upper", "two--dash"):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                payload = json.loads(json.dumps(self.payload))
                payload["public_runtime_domains"] = [
                    *payload["public_runtime_domains"],
                    invalid,
                ]
                self.write_manifest(root, payload)
                self.assert_both_reject(root, "invalid command names")

    def test_cross_suffix_extra_runtime_file_is_not_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            plugin = repo / "plugins/codex-project-harness"
            plugin.parent.mkdir(parents=True)
            shutil.copytree(
                PLUGIN_ROOT,
                plugin,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            for name in ("VERSION", "release.json", "pyproject.toml"):
                shutil.copyfile(REPO_ROOT / name, repo / name)
            (plugin / "core/undeclared.txt").write_text(
                "not an approved runtime file\n", encoding="utf-8"
            )

            from kafa.cli import static_plugin_structure

            ok, details = static_plugin_structure(plugin)

        self.assertFalse(ok)
        self.assertIn("core inventory mismatch", details)

    def test_recursive_inventory_rejects_undeclared_nested_files(self) -> None:
        for relative in (
            "core/nested/undeclared.py",
            "scripts/fixtures/undeclared.txt",
            "hooks/nested/undeclared.json",
        ):
            with (
                self.subTest(relative=relative),
                tempfile.TemporaryDirectory() as temp,
            ):
                repo = Path(temp)
                plugin = repo / "plugins/codex-project-harness"
                plugin.parent.mkdir(parents=True)
                shutil.copytree(
                    PLUGIN_ROOT,
                    plugin,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
                for name in ("VERSION", "release.json", "pyproject.toml"):
                    shutil.copyfile(REPO_ROOT / name, repo / name)
                manifest = json.loads(
                    (plugin / "references/distribution-manifest.json").read_text(
                        encoding="utf-8"
                    )
                )
                manifest["additional_files"] = DECLARED_NESTED_FILES
                (plugin / "references/distribution-manifest.json").write_text(
                    json.dumps(manifest, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                extra = plugin / relative
                extra.parent.mkdir(parents=True, exist_ok=True)
                extra.write_text("undeclared nested runtime file\n", encoding="utf-8")

                from kafa.cli import static_plugin_structure

                ok, details = static_plugin_structure(plugin)

            self.assertFalse(ok)
            self.assertIn(relative, details)

    def test_static_hook_commands_require_exact_interpreter_runner_and_event_tokens(
        self,
    ) -> None:
        cases = {
            "wrong-interpreter": (
                'ruby "${PLUGIN_ROOT}/hooks/harness_hook.py" SessionStart'
            ),
            "forged-event-substring": (
                'python3 "${PLUGIN_ROOT}/hooks/harness_hook.py" '
                "--forged=SessionStart"
            ),
            "extra-token": (
                'python3 "${PLUGIN_ROOT}/hooks/harness_hook.py" '
                "SessionStart --extra"
            ),
        }
        accepted: list[str] = []
        for name, command in cases.items():
            with tempfile.TemporaryDirectory() as temp:
                plugin = Path(temp) / "plugin"
                shutil.copytree(
                    PLUGIN_ROOT,
                    plugin,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
                hooks_path = plugin / "hooks/hooks.json"
                hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
                hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"] = command
                hooks_path.write_text(
                    json.dumps(hooks, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                distribution = load_package_manifest(plugin)
                ok, _details = static_hook_definition(
                    plugin,
                    distribution=distribution,
                )
                if ok:
                    accepted.append(name)

        self.assertEqual(accepted, [])

    def test_self_contained_structure_rejects_noncanonical_hook_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            plugin = repo / "plugins/codex-project-harness"
            plugin.parent.mkdir(parents=True)
            shutil.copytree(
                PLUGIN_ROOT,
                plugin,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
            )
            for name in ("VERSION", "release.json", "pyproject.toml"):
                shutil.copyfile(REPO_ROOT / name, repo / name)
            hooks_path = plugin / "hooks/hooks.json"
            hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
            hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"] = (
                'python3 "${PLUGIN_ROOT}/hooks/harness_hook.py" '
                "SessionStart --extra"
            )
            hooks_path.write_text(
                json.dumps(hooks, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(plugin / "scripts/validate_structure.py"),
                    str(plugin),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("hook", completed.stdout.lower())


if __name__ == "__main__":
    unittest.main()

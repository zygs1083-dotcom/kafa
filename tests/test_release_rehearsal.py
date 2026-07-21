from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_supply_chain_release import create_source_repo


class ReleaseRehearsalContractTest(unittest.TestCase):
    @staticmethod
    def _user_state_env(home: Path) -> dict[str, str]:
        return {
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
        }

    @staticmethod
    def _user_install_fixture(
        root: Path,
        *,
        marketplace: str = "kafa-local",
    ) -> tuple[Path, Path, Path, Path, dict[str, object]]:
        home = root / "home"
        managed = home / ".agents" / "plugins" / "codex-project-harness"
        cache = (
            home
            / ".codex"
            / "plugins"
            / "cache"
            / marketplace
            / "codex-project-harness"
            / "2.0.0-beta.1"
        )
        kafa_bin = home / ".local" / "bin" / "kafa"
        for tree in (managed, cache):
            tree.mkdir(parents=True)
            (tree / "payload.txt").write_bytes(b"installed-plugin\n")
        kafa_bin.parent.mkdir(parents=True)
        kafa_bin.write_bytes(b"#!/usr/bin/env python3\n")
        kafa_bin.chmod(0o755)
        plugin: dict[str, object] = {
            "pluginId": f"codex-project-harness@{marketplace}",
            "name": "codex-project-harness",
            "marketplaceName": marketplace,
            "version": "2.0.0-beta.1",
            "installed": True,
            "enabled": True,
            "source": {"source": "local", "path": str(managed)},
            "marketplaceSource": {
                "sourceType": "local",
                "source": str(home),
            },
            "installPolicy": "AVAILABLE",
            "authPolicy": "ON_INSTALL",
        }
        return home, managed, cache, kafa_bin, plugin

    @staticmethod
    def _artifact_manifest() -> dict[str, dict[str, str]]:
        return {
            "wheel": {"sha256": "1" * 64},
            "sdist": {"sha256": "3" * 64},
        }

    @staticmethod
    def _complete_smoke() -> dict[str, object]:
        digest = "5" * 64
        return {
            "ok": True,
            "artifact_mode": True,
            "marketplace_discovered": True,
            "plugin_enabled": True,
            "app_server_discovery_ok": True,
            "installed_quickstart_ok": True,
            "installed_migration_ok": True,
            "doctor_ok": True,
            "cache_hook_ok": True,
            "codex_unregister_ok": True,
            "codex_cache_removed": True,
            "marketplace_entry_removed": True,
            "managed_plugin_removed": True,
            "full_uninstall_ok": True,
            "remove_ok": True,
            "plugin_source_tree_sha256": digest,
            "managed_plugin_tree_sha256": digest,
            "cache_plugin_tree_sha256": digest,
            "wheel_sha256": "1" * 64,
            "source_archive_sha256": "3" * 64,
        }

    def test_isolated_smoke_binds_exact_regular_artifact_inputs(self) -> None:
        from tests.run_isolated_install_smoke import validate_artifact_inputs

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            wheel = root / "kafa-2.0.0b1-py3-none-any.whl"
            sdist = root / "kafa-2.0.0b1.tar.gz"
            wheel.write_bytes(b"wheel")
            sdist.write_bytes(b"sdist")

            result = validate_artifact_inputs(wheel, sdist, "2.0.0b1")

            self.assertEqual(result["wheel_name"], wheel.name)
            self.assertEqual(result["source_archive_name"], sdist.name)
            self.assertRegex(result["wheel_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(result["source_archive_sha256"], r"^[0-9a-f]{64}$")

            wrong = root / "renamed.whl"
            wrong.write_bytes(b"wheel")
            with self.assertRaisesRegex(RuntimeError, "artifact names mismatch"):
                validate_artifact_inputs(wrong, sdist, "2.0.0b1")

            original = Path.is_symlink

            def reports_wheel_symlink(path: Path) -> bool:
                return path == wheel or original(path)

            with patch.object(Path, "is_symlink", autospec=True, side_effect=reports_wheel_symlink):
                with self.assertRaisesRegex(RuntimeError, "regular files"):
                    validate_artifact_inputs(wheel, sdist, "2.0.0b1")

    def test_source_snapshot_matches_candidate_and_omits_generated_state(self) -> None:
        from kafa.rehearsal import copy_source_snapshot
        from kafa.supply_chain import source_identity

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = create_source_repo(root)
            (repo / "build/lib").mkdir(parents=True)
            (repo / "build/lib/generated.py").write_text("generated\n", encoding="utf-8")
            target = root / "snapshot"

            copied = copy_source_snapshot(repo, target)
            source = source_identity(repo)
            git_copied = (target / ".git").exists()
            build_copied = (target / "build").exists()

        self.assertEqual(copied["source_tree_sha256"], source["source_tree_sha256"])
        self.assertEqual(copied["source_file_count"], source["source_file_count"])
        self.assertFalse(git_copied)
        self.assertFalse(build_copied)

    def test_rehearsal_requires_artifact_migration_and_full_uninstall(self) -> None:
        from kafa.rehearsal import RehearsalError, _validate_smoke

        smoke = self._complete_smoke()
        for field in (
            "installed_migration_ok",
            "codex_unregister_ok",
            "marketplace_entry_removed",
            "managed_plugin_removed",
            "full_uninstall_ok",
        ):
            smoke.pop(field)

        with self.assertRaisesRegex(
            RehearsalError,
            "(installed_migration_ok|full_uninstall_ok)",
        ):
            _validate_smoke(smoke, self._artifact_manifest())

    def test_rehearsal_requires_identical_source_managed_cache_digests(self) -> None:
        from kafa.rehearsal import RehearsalError, _validate_smoke

        cases = {
            "missing": None,
            "malformed": "not-a-sha256",
            "mismatch": "6" * 64,
        }
        for label, value in cases.items():
            with self.subTest(label=label):
                smoke = self._complete_smoke()
                if value is None:
                    smoke.pop("cache_plugin_tree_sha256")
                else:
                    smoke["cache_plugin_tree_sha256"] = value

                with self.assertRaisesRegex(RehearsalError, "plugin digest"):
                    _validate_smoke(smoke, self._artifact_manifest())

    def test_user_state_accepts_the_unique_managed_kafa_plugin_for_any_marketplace(self) -> None:
        from kafa.rehearsal import capture_user_state

        with tempfile.TemporaryDirectory() as temp:
            for marketplace in ("kafa-local", "personal", "custom-marketplace"):
                fixture_root = Path(temp) / marketplace
                home, _, _, kafa_bin, plugin = self._user_install_fixture(
                    fixture_root,
                    marketplace=marketplace,
                )
                with (
                    self.subTest(marketplace=marketplace),
                    patch.dict(os.environ, self._user_state_env(home), clear=False),
                    patch(
                        "kafa.rehearsal._run_read_only",
                        side_effect=[
                            ("2.0.0-beta.1\n", ""),
                            (json.dumps({"installed": [plugin]}), ""),
                        ],
                    ),
                ):
                    state = capture_user_state("/tmp/codex", str(kafa_bin))

                self.assertEqual(state["status"], "observed")
                self.assertEqual(state["plugin"]["pluginId"], plugin["pluginId"])
                self.assertEqual(
                    state["plugin"]["marketplaceName"],
                    marketplace,
                )
                self.assertRegex(state["kafa_executable"]["sha256"], r"^[0-9a-f]{64}$")
                self.assertRegex(state["managed_plugin"]["sha256"], r"^[0-9a-f]{64}$")
                self.assertEqual(
                    state["managed_plugin"]["sha256"],
                    state["plugin_cache"]["sha256"],
                )

    def test_user_state_rejects_unmanaged_disabled_or_ambiguous_kafa_plugins(self) -> None:
        from kafa.rehearsal import capture_user_state

        with tempfile.TemporaryDirectory() as temp:
            home, _, cache, kafa_bin, valid = self._user_install_fixture(Path(temp))
            cases = {
                "wrong-source": [
                    {
                        **valid,
                        "source": {"source": "local", "path": str(home / "other")},
                    }
                ],
                "disabled": [{**valid, "enabled": False}],
                "ambiguous": [
                    valid,
                    {
                        **valid,
                        "pluginId": "codex-project-harness@personal",
                        "marketplaceName": "personal",
                    },
                ],
                "malformed-duplicate-id": [
                    valid,
                    {
                        **valid,
                        "name": "renamed-to-evade-name-filter",
                        "pluginId": "codex-project-harness@personal",
                        "marketplaceName": "personal",
                    },
                ],
                "malformed-duplicate-source": [
                    valid,
                    {
                        **valid,
                        "name": "renamed-to-evade-identity-filter",
                        "pluginId": "renamed-plugin@personal",
                    },
                ],
            }
            for label, installed in cases.items():
                with (
                    self.subTest(label=label),
                    patch.dict(os.environ, self._user_state_env(home), clear=False),
                    patch(
                        "kafa.rehearsal._run_read_only",
                        side_effect=[
                            ("2.0.0-beta.1\n", ""),
                            (json.dumps({"installed": installed}), ""),
                        ],
                    ),
                ):
                    state = capture_user_state("/tmp/codex", str(kafa_bin))

                self.assertEqual(state["status"], "not-run")

            (cache / "payload.txt").write_bytes(b"different-cache\n")
            with (
                patch.dict(os.environ, self._user_state_env(home), clear=False),
                patch(
                    "kafa.rehearsal._run_read_only",
                    side_effect=[
                        ("2.0.0-beta.1\n", ""),
                        (json.dumps({"installed": [valid]}), ""),
                    ],
                ),
            ):
                state = capture_user_state("/tmp/codex", str(kafa_bin))
            self.assertEqual(state["status"], "not-run")

    def test_user_state_rejects_unsafe_home_and_managed_tree_aliases(self) -> None:
        from kafa.rehearsal import capture_user_state

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home, managed, _, kafa_bin, valid = self._user_install_fixture(root)
            for unsafe_home in ("", "relative-home"):
                with (
                    self.subTest(home=unsafe_home),
                    patch.dict(
                        os.environ,
                        {**self._user_state_env(home), "HOME": unsafe_home},
                        clear=False,
                    ),
                    patch(
                        "kafa.rehearsal._run_read_only",
                        side_effect=[
                            ("2.0.0-beta.1\n", ""),
                            (json.dumps({"installed": [valid]}), ""),
                        ],
                    ),
                ):
                    state = capture_user_state("/tmp/codex", str(kafa_bin))
                self.assertEqual(state["status"], "not-run")
                self.assertEqual(
                    state["reason"],
                    "HOME must be a non-empty absolute path",
                )

            with (
                patch.dict(os.environ, self._user_state_env(home), clear=False),
                patch("kafa.rehearsal.managed_tree_is_safe", return_value=False),
                patch(
                    "kafa.rehearsal._run_read_only",
                    side_effect=[
                        ("2.0.0-beta.1\n", ""),
                        (json.dumps({"installed": [valid]}), ""),
                    ],
                ),
            ):
                state = capture_user_state("/tmp/codex", str(kafa_bin))
            self.assertEqual(state["status"], "not-run")

            if os.name != "nt":
                outside = root / "repo-plugin"
                outside.mkdir()
                (outside / "payload.txt").write_bytes(b"installed-plugin\n")
                for path in sorted(managed.rglob("*"), reverse=True):
                    if path.is_file():
                        path.unlink()
                    elif path.is_dir():
                        path.rmdir()
                managed.rmdir()
                managed.symlink_to(outside, target_is_directory=True)
                with (
                    patch.dict(os.environ, self._user_state_env(home), clear=False),
                    patch(
                        "kafa.rehearsal._run_read_only",
                        side_effect=[
                            ("2.0.0-beta.1\n", ""),
                            (json.dumps({"installed": [valid]}), ""),
                        ],
                    ),
                ):
                    state = capture_user_state("/tmp/codex", str(kafa_bin))
                self.assertEqual(state["status"], "not-run")

    def test_user_state_rejects_kafa_executable_outside_home(self) -> None:
        from kafa.rehearsal import capture_user_state

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home, _, _, _, _ = self._user_install_fixture(root)
            external = root / "external-kafa"
            external.write_bytes(b"#!/usr/bin/env python3\n")
            external.chmod(0o755)
            with patch.dict(os.environ, self._user_state_env(home), clear=False):
                state = capture_user_state("/tmp/codex", str(external))
            self.assertEqual(state["status"], "not-run")
            self.assertEqual(
                state["reason"],
                "user Kafa binary must be installed within HOME",
            )

            linked_identity = {
                "invocation_path": str(home / ".local" / "bin" / "kafa"),
                "resolved_path": str(external),
                "link_target": str(external),
                "size": external.stat().st_size,
                "sha256": "a" * 64,
            }
            with (
                patch.dict(os.environ, self._user_state_env(home), clear=False),
                patch("kafa.rehearsal._executable_identity", return_value=linked_identity),
            ):
                state = capture_user_state(
                    "/tmp/codex",
                    str(home / ".local" / "bin" / "kafa"),
                )
            self.assertEqual(state["status"], "not-run")

    def test_user_state_snapshot_detects_plugin_and_executable_byte_changes(self) -> None:
        from kafa.rehearsal import capture_user_state

        with tempfile.TemporaryDirectory() as temp:
            home, managed, cache, kafa_bin, plugin = self._user_install_fixture(Path(temp))

            def capture() -> dict[str, object]:
                with (
                    patch.dict(os.environ, self._user_state_env(home), clear=False),
                    patch(
                        "kafa.rehearsal._run_read_only",
                        side_effect=[
                            ("2.0.0-beta.1\n", ""),
                            (json.dumps({"installed": [plugin]}), ""),
                        ],
                    ),
                ):
                    return capture_user_state("/tmp/codex", str(kafa_bin))

            before = capture()
            (managed / "payload.txt").write_bytes(b"changed-plugin\n")
            after_plugin_change = capture()
            (cache / "payload.txt").write_bytes(b"changed-plugin\n")
            after_lockstep_plugin_change = capture()
            for tree in (managed, cache):
                (tree / "payload.txt").write_bytes(b"installed-plugin\n")
            kafa_bin.write_bytes(b"#!/usr/bin/env python3\n# changed\n")
            after_executable_change = capture()

        self.assertEqual(before["status"], "observed")
        self.assertEqual(after_plugin_change["status"], "not-run")
        self.assertEqual(after_lockstep_plugin_change["status"], "observed")
        self.assertEqual(after_executable_change["status"], "observed")
        self.assertNotEqual(before, after_lockstep_plugin_change)
        self.assertNotEqual(before, after_plugin_change)
        self.assertNotEqual(before, after_executable_change)

    def test_user_state_ignores_generated_python_cache_bytes(self) -> None:
        from kafa.rehearsal import capture_user_state

        with tempfile.TemporaryDirectory() as temp:
            home, managed, cache, kafa_bin, plugin = self._user_install_fixture(Path(temp))
            managed_cache = managed / "core" / "__pycache__"
            codex_cache = cache / "core" / "__pycache__"
            managed_cache.mkdir(parents=True)
            codex_cache.mkdir(parents=True)
            (managed_cache / "runtime.cpython-314.pyc").write_bytes(b"managed-runtime-cache")
            (codex_cache / "runtime.cpython-313.pyc").write_bytes(b"codex-runtime-cache")
            with (
                patch.dict(os.environ, self._user_state_env(home), clear=False),
                patch(
                    "kafa.rehearsal._run_read_only",
                    side_effect=[
                        ("2.0.0-beta.1\n", ""),
                        (json.dumps({"installed": [plugin]}), ""),
                    ],
                ),
            ):
                state = capture_user_state("/tmp/codex", str(kafa_bin))

        self.assertEqual(state["status"], "observed")
        self.assertEqual(
            state["managed_plugin"]["sha256"],
            state["plugin_cache"]["sha256"],
        )

    def test_windows_reparse_attribute_is_treated_as_a_link(self) -> None:
        from kafa.cli import path_is_link

        class ReparsePath:
            @staticmethod
            def is_symlink() -> bool:
                return False

            @staticmethod
            def lstat() -> object:
                return type("ReparseStat", (), {"st_file_attributes": 0x400})()

        with patch("kafa.cli.stat.FILE_ATTRIBUTE_REPARSE_POINT", 0x400, create=True):
            self.assertTrue(path_is_link(ReparsePath()))

    def test_rehearsal_uses_one_build_and_two_verifications_without_publish(self) -> None:
        from kafa.rehearsal import run_release_rehearsal

        events: list[str] = []
        verified = {
            "ok": True,
            "assurance": "unsigned-local-integrity-statement",
            "artifact_count": 2,
            "sbom_count": 2,
            "artifacts": [
                {
                    "name": "kafa-2.0.0b1-py3-none-any.whl",
                    "kind": "wheel",
                    "sha256": "1" * 64,
                    "sbom": "kafa-2.0.0b1-py3-none-any.whl.cdx.json",
                    "sbom_sha256": "2" * 64,
                },
                {
                    "name": "kafa-2.0.0b1.tar.gz",
                    "kind": "sdist",
                    "sha256": "3" * 64,
                    "sbom": "kafa-2.0.0b1.tar.gz.cdx.json",
                    "sbom_sha256": "4" * 64,
                },
            ],
        }
        user_state = {
            "status": "observed",
            "kafa_version": "2.0.0-beta.1",
            "plugin": {
                "pluginId": "codex-project-harness@personal",
                "version": "2.0.0-beta.1",
                "installed": True,
                "enabled": True,
            },
        }

        def run_python(command: list[str], **_: object) -> str:
            if command[1:3] == ["-m", "build"]:
                events.append("build")
                dist = Path(command[command.index("--outdir") + 1])
                (dist / "kafa-2.0.0b1-py3-none-any.whl").write_bytes(b"wheel")
                (dist / "kafa-2.0.0b1.tar.gz").write_bytes(b"sdist")
                return "built"
            events.append("smoke")
            return json.dumps(
                {
                    **self._complete_smoke(),
                }
            )

        def generate(*_: object, **__: object) -> dict[str, object]:
            events.append("generate")
            return verified

        def verify(*_: object, **__: object) -> dict[str, object]:
            events.append("verify")
            return verified

        with tempfile.TemporaryDirectory() as temp:
            repo = create_source_repo(Path(temp))
            with (
                patch("kafa.rehearsal.installed_build_tooling", return_value={"build": "1.5.0", "setuptools": "83.0.0"}),
                patch("kafa.rehearsal.release_report", return_value={"ok": True, "checks": []}),
                patch("kafa.rehearsal.capture_user_state", return_value=user_state),
                patch("kafa.rehearsal._run_python", side_effect=run_python),
                patch("kafa.rehearsal.generate_release_evidence", side_effect=generate),
                patch("kafa.rehearsal.verify_release_evidence", side_effect=verify),
            ):
                report = run_release_rehearsal(
                    repo,
                    syft_command=["/tmp/pinned-syft"],
                    codex_bin="/tmp/pinned-codex",
                    user_kafa_bin="/tmp/user-kafa",
                )

        self.assertTrue(report["ok"], report)
        self.assertEqual(events, ["build", "generate", "verify", "smoke", "verify"])
        self.assertEqual(
            report["steps"],
            ["snapshot", "build", "generate", "verify-before-install", "isolated-install", "verify-after-install"],
        )
        self.assertTrue(report["invariants"]["source_unchanged"])
        self.assertTrue(report["invariants"]["tag_refs_unchanged"])
        self.assertTrue(report["invariants"]["user_install_unchanged"])
        self.assertEqual(report["external_effects"], {
            "tag": False,
            "release": False,
            "upload": False,
            "deployment": False,
            "user_installation_change": False,
        })
        self.assertNotIn("publish", " ".join(report["commands"]).lower())

    def test_rehearsal_fails_if_source_changes_during_build(self) -> None:
        from kafa.rehearsal import RehearsalError, run_release_rehearsal

        with tempfile.TemporaryDirectory() as temp:
            repo = create_source_repo(Path(temp))

            def mutating_build(command: list[str], **_: object) -> str:
                (repo / "candidate.txt").write_text("changed during build\n", encoding="utf-8")
                dist = Path(command[command.index("--outdir") + 1])
                (dist / "kafa-2.0.0b1-py3-none-any.whl").write_bytes(b"wheel")
                (dist / "kafa-2.0.0b1.tar.gz").write_bytes(b"sdist")
                return "built"

            with (
                patch("kafa.rehearsal.installed_build_tooling", return_value={"build": "1.5.0", "setuptools": "83.0.0"}),
                patch("kafa.rehearsal.release_report", return_value={"ok": True, "checks": []}),
                patch("kafa.rehearsal.capture_user_state", return_value={"status": "not-run"}),
                patch("kafa.rehearsal._run_python", side_effect=mutating_build),
            ):
                with self.assertRaisesRegex(RehearsalError, "source changed"):
                    run_release_rehearsal(
                        repo,
                        syft_command=["/tmp/pinned-syft"],
                        codex_bin="/tmp/pinned-codex",
                        user_state_probe=False,
                    )


if __name__ == "__main__":
    unittest.main()

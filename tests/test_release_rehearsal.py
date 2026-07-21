from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_supply_chain_release import create_source_repo


class ReleaseRehearsalContractTest(unittest.TestCase):
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

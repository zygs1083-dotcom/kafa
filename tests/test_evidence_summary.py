from __future__ import annotations

import hashlib
import json
import copy
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kafa.artifact_subject import ArtifactSubject
from kafa.change_scope import classify_changed_paths, classify_repository
from kafa.evidence_summary import (
    EVIDENCE_SUMMARY_VERSION,
    EvidenceSummaryError,
    build_evidence_summary,
    summarize_native_detail,
    summarize_rehearsal_detail,
    summary_detail_errors,
    summary_json_bytes,
    read_regular_detail,
    validate_evidence_summary,
    _native_validator_errors,
    _repository_decision_errors,
)


DETAIL = b'{"report_version":1,"live_status":"passed"}\n'
EMPTY_STATUS_SHA256 = hashlib.sha256(b"").hexdigest()
REPO_ROOT = Path(__file__).resolve().parents[1]
DECISION_REPORT = classify_changed_paths(
    ["kafa/codex_app_server.py"],
    base_oid="b" * 40,
    head_oid="a" * 40,
).to_dict()
DECISION_BYTES = (json.dumps(DECISION_REPORT, indent=2, sort_keys=True) + "\n").encode()
DECISION_BINDING = {
    "version": DECISION_REPORT["version"],
    "state": DECISION_REPORT["state"],
    "base_oid": DECISION_REPORT["base_oid"],
    "head_oid": DECISION_REPORT["head_oid"],
    "changed_paths_sha256": DECISION_REPORT["changed_paths_sha256"],
    "required_profiles": DECISION_REPORT["required_profiles"],
    "sha256": hashlib.sha256(DECISION_BYTES).hexdigest(),
}


def decision_bytes_for_native(detail_bytes: bytes) -> bytes:
    detail = json.loads(detail_bytes)
    head = str(detail["evaluation_source"]["git_head"])
    base = "b" * len(head) if head != "b" * len(head) else "c" * len(head)
    report = classify_changed_paths(
        ["kafa/codex_app_server.py"],
        base_oid=base,
        head_oid=head,
    ).to_dict()
    return (json.dumps(report, indent=2, sort_keys=True) + "\n").encode()


def valid_summary(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "detail_bytes": DETAIL,
        "kind": "native-single",
        "source": {
            "kind": "native-evaluation-source-v1",
            "revision": "a" * 40,
            "sha256": "1" * 64,
            "clean": True,
            "status_sha256": EMPTY_STATUS_SHA256,
        },
        "status": "passed",
        "binary": [ArtifactSubject("codex.js", "codex-cli", "2" * 64)],
        "scope": {
            "profile": "live-codex",
            "requirement": "blocking",
            "change_scopes": ["host"],
            "decision": DECISION_BINDING,
        },
        "timing": {
            "generated_at": "2026-07-22T00:00:00Z",
            "duration_seconds": 12.5,
        },
        "state": "current",
        "retention": {
            "class": "ci-artifact",
            "locator": "real-codex-host-compatibility-live-codex",
            "days": 30,
        },
    }
    values.update(overrides)
    return build_evidence_summary(**values)  # type: ignore[arg-type]


class EvidenceSummaryTest(unittest.TestCase):
    def test_native_validator_executes_captured_entrypoint_not_swapped_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script = (
                root
                / "plugins"
                / "codex-project-harness"
                / "scripts"
                / "run_agent_e2e_eval.py"
            )
            script.parent.mkdir(parents=True)
            script.write_text(
                "def persistent_evidence_errors(report, *, eligibility):\n"
                "    return ['captured validator rejected detail']\n",
                encoding="utf-8",
            )
            replacement = script.with_name("replacement.py")
            replacement.write_text(
                "def persistent_evidence_errors(report, *, eligibility):\n"
                "    return []\n",
                encoding="utf-8",
            )
            saved = script.with_name("saved.py")
            displaced = script.with_name("displaced.py")
            real_run = subprocess.run
            swapped = False

            def racing_run(command: object, **kwargs: object) -> object:
                nonlocal swapped
                if isinstance(command, list) and "-c" in command and not swapped:
                    swapped = True
                    os.replace(script, saved)
                    os.replace(replacement, script)
                    try:
                        return real_run(command, **kwargs)
                    finally:
                        os.replace(script, displaced)
                        os.replace(saved, script)
                return real_run(command, **kwargs)

            with mock.patch(
                "kafa.evidence_summary.subprocess.run",
                side_effect=racing_run,
            ):
                errors = _native_validator_errors(
                    b'{"forged":true}\n',
                    eligibility="historical-integrity",
                    validator_repo=root,
                )

            self.assertTrue(swapped)
            self.assertTrue(errors)
            self.assertNotEqual(errors, [])

    def test_native_validator_executes_captured_transitive_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            scripts = (
                root
                / "plugins"
                / "codex-project-harness"
                / "scripts"
            )
            scripts.mkdir(parents=True)
            entrypoint = scripts / "run_agent_e2e_eval.py"
            entrypoint.write_text(
                "import sys\n"
                "from pathlib import Path\n"
                "scripts = Path(__file__).resolve().parent\n"
                "sys.path.insert(0, str(scripts))\n"
                "import harness_lib\n"
                "def persistent_evidence_errors(report, *, eligibility):\n"
                "    return harness_lib.validate(report)\n",
                encoding="utf-8",
            )
            dependency = scripts / "harness_lib.py"
            dependency.write_text(
                "def validate(report):\n"
                "    return ['captured dependency rejected detail']\n",
                encoding="utf-8",
            )
            replacement = scripts / "replacement.py"
            replacement.write_text(
                "def validate(report):\n"
                "    return []\n",
                encoding="utf-8",
            )
            saved = scripts / "saved.py"
            displaced = scripts / "displaced.py"
            real_run = subprocess.run
            swapped = False

            def racing_run(command: object, **kwargs: object) -> object:
                nonlocal swapped
                if isinstance(command, list) and "-c" in command and not swapped:
                    swapped = True
                    os.replace(dependency, saved)
                    os.replace(replacement, dependency)
                    try:
                        return real_run(command, **kwargs)
                    finally:
                        os.replace(dependency, displaced)
                        os.replace(saved, dependency)
                return real_run(command, **kwargs)

            with mock.patch(
                "kafa.evidence_summary.subprocess.run",
                side_effect=racing_run,
            ):
                errors = _native_validator_errors(
                    b'{"forged":true}\n',
                    eligibility="historical-integrity",
                    validator_repo=root,
                )

            self.assertTrue(swapped)
            self.assertIn("captured dependency rejected detail", errors)

    def test_private_validator_snapshot_detects_dependency_swap_and_restore(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            scripts = (
                root
                / "plugins"
                / "codex-project-harness"
                / "scripts"
            )
            scripts.mkdir(parents=True)
            (scripts / "run_agent_e2e_eval.py").write_text(
                "import sys\n"
                "from pathlib import Path\n"
                "scripts = Path(__file__).resolve().parent\n"
                "sys.path.insert(0, str(scripts))\n"
                "import harness_lib\n"
                "def persistent_evidence_errors(report, *, eligibility):\n"
                "    return harness_lib.validate(report)\n",
                encoding="utf-8",
            )
            (scripts / "harness_lib.py").write_text(
                "def validate(report):\n"
                "    return ['private dependency rejected detail']\n",
                encoding="utf-8",
            )
            real_run = subprocess.run
            swapped = False

            def racing_run(command: object, **kwargs: object) -> object:
                nonlocal swapped
                if isinstance(command, list) and "-c" in command and not swapped:
                    snapshot_root = Path(str(kwargs["cwd"]))
                    private_scripts = (
                        snapshot_root
                        / "plugins"
                        / "codex-project-harness"
                        / "scripts"
                    )
                    dependency = private_scripts / "harness_lib.py"
                    forged = private_scripts / "forged.py"
                    saved = private_scripts / "saved.py"
                    displaced = private_scripts / "displaced.py"
                    original_mode = private_scripts.stat().st_mode
                    os.chmod(private_scripts, original_mode | 0o200)
                    forged.write_text(
                        "def validate(report):\n"
                        "    return []\n",
                        encoding="utf-8",
                    )
                    swapped = True
                    os.replace(dependency, saved)
                    os.replace(forged, dependency)
                    try:
                        return real_run(command, **kwargs)
                    finally:
                        os.replace(dependency, displaced)
                        os.replace(saved, dependency)
                        displaced.unlink()
                        os.chmod(private_scripts, original_mode)
                return real_run(command, **kwargs)

            with mock.patch(
                "kafa.evidence_summary.subprocess.run",
                side_effect=racing_run,
            ):
                errors = _native_validator_errors(
                    b'{"forged":true}\n',
                    eligibility="historical-integrity",
                    validator_repo=root,
                )

            self.assertTrue(swapped)
            self.assertTrue(errors)

    @unittest.skipIf(os.name == "nt", "symlink creation requires platform privileges")
    def test_sealed_snapshot_cleanup_never_follows_replaced_source_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            scripts = (
                root
                / "plugins"
                / "codex-project-harness"
                / "scripts"
            )
            scripts.mkdir(parents=True)
            (scripts / "run_agent_e2e_eval.py").write_text(
                "import sys\n"
                "from pathlib import Path\n"
                "scripts = Path(__file__).resolve().parent\n"
                "sys.path.insert(0, str(scripts))\n"
                "import harness_lib\n"
                "def persistent_evidence_errors(report, *, eligibility):\n"
                "    return harness_lib.validate(report)\n",
                encoding="utf-8",
            )
            (scripts / "harness_lib.py").write_text(
                "def validate(report):\n"
                "    return ['private dependency rejected detail']\n",
                encoding="utf-8",
            )
            external = root / "external_harness_lib.py"
            external.write_text(
                "def validate(report):\n"
                "    return []\n",
                encoding="utf-8",
            )
            os.chmod(external, 0o600)
            real_run = subprocess.run
            swapped = False

            def racing_run(command: object, **kwargs: object) -> object:
                nonlocal swapped
                if isinstance(command, list) and "-c" in command and not swapped:
                    snapshot_root = Path(str(kwargs["cwd"]))
                    private_scripts = (
                        snapshot_root
                        / "plugins"
                        / "codex-project-harness"
                        / "scripts"
                    )
                    dependency = private_scripts / "harness_lib.py"
                    saved = private_scripts / "harness_lib-saved.py"
                    os.chmod(
                        private_scripts,
                        stat.S_IMODE(private_scripts.stat().st_mode)
                        | stat.S_IWUSR
                        | stat.S_IXUSR,
                    )
                    os.replace(dependency, saved)
                    dependency.symlink_to(external)
                    swapped = True
                return real_run(command, **kwargs)

            with mock.patch(
                "kafa.evidence_summary.subprocess.run",
                side_effect=racing_run,
            ):
                errors = _native_validator_errors(
                    b'{"forged":true}\n',
                    eligibility="historical-integrity",
                    validator_repo=root,
                )

            self.assertTrue(swapped, errors)
            self.assertTrue(errors)
            self.assertEqual(stat.S_IMODE(external.stat().st_mode), 0o600)

    def test_current_native_validator_uses_private_committed_dependency_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            scripts = (
                repo
                / "plugins"
                / "codex-project-harness"
                / "scripts"
            )
            scripts.mkdir(parents=True)
            entrypoint = scripts / "run_agent_e2e_eval.py"
            entrypoint.write_text(
                "import sys\n"
                "from pathlib import Path\n"
                "scripts = Path(__file__).resolve().parent\n"
                "sys.path.insert(0, str(scripts))\n"
                "import harness_lib\n"
                "def persistent_evidence_errors(report, *, eligibility):\n"
                "    return harness_lib.validate(report)\n",
                encoding="utf-8",
            )
            dependency = scripts / "harness_lib.py"
            dependency.write_text(
                "def validate(report):\n"
                "    return ['committed dependency rejected detail']\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(
                ["git", "config", "core.autocrlf", "false"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "snapshot@example.com"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Snapshot Test"],
                cwd=repo,
                check=True,
            )
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "validator"],
                cwd=repo,
                check=True,
            )
            replacement = root / "replacement.py"
            replacement.write_text(
                "def validate(report):\n"
                "    return []\n",
                encoding="utf-8",
            )
            saved = root / "saved.py"
            displaced = root / "displaced.py"
            real_run = subprocess.run
            swapped = False

            def racing_run(command: object, **kwargs: object) -> object:
                nonlocal swapped
                if (
                    isinstance(command, list)
                    and command
                    and command[0] == sys.executable
                    and "-c" in command
                    and not swapped
                ):
                    swapped = True
                    os.replace(dependency, saved)
                    os.replace(replacement, dependency)
                    try:
                        return real_run(command, **kwargs)
                    finally:
                        os.replace(dependency, displaced)
                        os.replace(saved, dependency)
                return real_run(command, **kwargs)

            with mock.patch(
                "kafa.evidence_summary.subprocess.run",
                side_effect=racing_run,
            ):
                errors = _native_validator_errors(
                    b'{"forged":true}\n',
                    eligibility="current-eligible",
                    validator_repo=repo,
                )

            self.assertTrue(swapped)
            self.assertIn("committed dependency rejected detail", errors)
            self.assertEqual(
                subprocess.run(
                    ["git", "status", "--porcelain=v1"],
                    cwd=repo,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout,
                "",
            )

    @unittest.skipIf(os.name == "nt", "symlink creation requires platform privileges")
    def test_current_native_validator_rejects_committed_dependency_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            scripts = (
                repo
                / "plugins"
                / "codex-project-harness"
                / "scripts"
            )
            scripts.mkdir(parents=True)
            (scripts / "run_agent_e2e_eval.py").write_text(
                "import sys\n"
                "from pathlib import Path\n"
                "scripts = Path(__file__).resolve().parent\n"
                "sys.path.insert(0, str(scripts))\n"
                "import harness_lib\n"
                "def persistent_evidence_errors(report, *, eligibility):\n"
                "    return harness_lib.validate(report)\n",
                encoding="utf-8",
            )
            external = root / "external_harness_lib.py"
            external.write_text(
                "def validate(report):\n"
                "    return []\n",
                encoding="utf-8",
            )
            (scripts / "harness_lib.py").symlink_to(external)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(
                ["git", "config", "core.autocrlf", "false"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "snapshot@example.com"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Snapshot Test"],
                cwd=repo,
                check=True,
            )
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "validator symlink"],
                cwd=repo,
                check=True,
            )

            errors = _native_validator_errors(
                b'{"forged":true}\n',
                eligibility="current-eligible",
                validator_repo=repo,
            )

            self.assertTrue(errors)
            self.assertTrue(
                any("link or reparse point" in error for error in errors),
                errors,
            )

    def test_current_decision_recomputes_repository_and_requires_full_cleanliness(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
            subprocess.run(
                ["git", "config", "core.autocrlf", "false"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "evidence@example.com"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Evidence Test"],
                cwd=repo,
                check=True,
            )
            readme = repo / "README.md"
            readme.write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "base"],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            base = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            workflow = repo / ".github" / "workflows" / "release.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text("name: release\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "head"],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            valid = classify_repository(repo, base_oid=base, head_oid=head).to_dict()
            forged = classify_changed_paths(
                ["README.md"],
                base_oid="b" * 40,
                head_oid=head,
            ).to_dict()

            self.assertEqual(_repository_decision_errors(valid, repo), [])
            self.assertTrue(_repository_decision_errors(forged, repo))

            workflow.write_text("name: changed-after-native\n", encoding="utf-8")
            cleanliness_errors = _repository_decision_errors(valid, repo)

        self.assertTrue(
            any("not clean" in error for error in cleanliness_errors),
            cleanliness_errors,
        )

    def test_historical_decision_must_match_detail_revision(self) -> None:
        summary = valid_summary()
        summary["state"] = "historical"
        summary["source"] = {
            "kind": "native-evaluation-source-v1",
            "revision": "b" * 40,
            "sha256": "1" * 64,
            "clean": False,
            "status_sha256": "3" * 64,
        }

        self.assertTrue(
            any("decision head" in error for error in validate_evidence_summary(summary))
        )

    def test_new_historical_summary_cannot_self_author_scope_without_decision(self) -> None:
        detail_bytes = (REPO_ROOT / "docs/runtime/native-codex-live-eval.json").read_bytes()
        with self.assertRaisesRegex(EvidenceSummaryError, "decision"):
            summarize_native_detail(
                detail_bytes,
                requirement="advisory",
                change_scopes=["docs-only"],
                eligibility="historical-integrity",
                validator_repo=REPO_ROOT,
                retention={
                    "class": "local-opt-in",
                    "locator": "/tmp/native-detail.json",
                    "days": None,
                },
            )

    def test_detail_reader_rejects_path_swap_between_check_and_open(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            detail = root / "detail.json"
            forged = root / "forged.json"
            original = root / "original.json"
            displaced_forged = root / "displaced-forged.json"
            detail.write_bytes(b'{"trusted":true}\n')
            forged.write_bytes(b'{"trusted":false}\n')
            real_open = os.open
            swapped = False

            def racing_open(path: object, flags: int, *args: object) -> int:
                nonlocal swapped
                if Path(path) == detail and not swapped:
                    swapped = True
                    os.replace(detail, original)
                    os.replace(forged, detail)
                    return real_open(path, flags, *args)
                return real_open(path, flags, *args)

            try:
                with mock.patch(
                    "kafa.artifact_subject.os.open",
                    side_effect=racing_open,
                ):
                    with self.assertRaises(EvidenceSummaryError):
                        read_regular_detail(detail)
            finally:
                if original.exists():
                    if detail.exists():
                        os.replace(detail, displaced_forged)
                    os.replace(original, detail)

            self.assertTrue(swapped)
            self.assertEqual(detail.read_bytes(), b'{"trusted":true}\n')

    def test_current_native_scope_requires_candidate_bound_decision(self) -> None:
        summary = valid_summary()
        summary["scope"].pop("decision")
        errors = validate_evidence_summary(summary)

        self.assertTrue(
            any("decision" in error.lower() for error in errors),
            f"current Native summary was accepted without a classifier decision binding: {errors}",
        )

    def test_blocking_scope_cannot_be_downgraded_by_summary_caller(self) -> None:
        with self.assertRaises(EvidenceSummaryError):
            valid_summary(
                scope={
                    "profile": "live-codex",
                    "requirement": "advisory",
                    "change_scopes": ["host"],
                }
            )

    def test_summary_is_closed_stable_and_binds_exact_detail_bytes(self) -> None:
        summary = valid_summary()

        self.assertEqual(summary["report_version"], EVIDENCE_SUMMARY_VERSION)
        self.assertEqual(
            set(summary),
            {
                "report_version",
                "kind",
                "source",
                "status",
                "binary",
                "scope",
                "timing",
                "digest",
                "state",
                "retention",
            },
        )
        self.assertEqual(
            summary["digest"],
            {"sha256": hashlib.sha256(DETAIL).hexdigest(), "bytes": len(DETAIL)},
        )
        self.assertEqual(validate_evidence_summary(summary), [])
        self.assertEqual(summary_json_bytes(summary), summary_json_bytes(summary))

    def test_status_state_and_clean_source_are_independent_but_consistent(self) -> None:
        dirty_source = {
            "kind": "native-evaluation-source-v1",
            "revision": "a" * 40,
            "sha256": "1" * 64,
            "clean": False,
            "status_sha256": "3" * 64,
        }

        with self.assertRaisesRegex(EvidenceSummaryError, "current.*clean"):
            valid_summary(source=dirty_source)
        historical = valid_summary(source=dirty_source, state="historical")
        unavailable = valid_summary(
            status="not-run",
            state="unavailable",
            binary=[],
            retention={"class": "none", "locator": None, "days": None},
        )

        self.assertEqual(validate_evidence_summary(historical), [])
        self.assertEqual(validate_evidence_summary(unavailable), [])
        tampered = dict(unavailable)
        tampered["status"] = "passed"
        self.assertTrue(validate_evidence_summary(tampered))

    def test_retention_and_subjects_are_closed(self) -> None:
        invalid_retention = {
            "class": "ci-artifact",
            "locator": "",
            "days": 0,
        }
        with self.assertRaisesRegex(EvidenceSummaryError, "retention"):
            valid_summary(retention=invalid_retention)

        summary = valid_summary()
        summary["binary"] = [
            {"name": "codex.js", "kind": "codex-cli", "sha256": "2" * 64},
            {"name": "codex.js", "kind": "codex-cli", "sha256": "2" * 64},
        ]
        self.assertTrue(validate_evidence_summary(summary))

        mixed_scope = valid_summary()
        mixed_scope["scope"]["change_scopes"] = ["docs-only", 1]
        self.assertEqual(
            validate_evidence_summary(mixed_scope),
            ["summary change scopes are invalid"],
        )

    def test_native_and_rehearsal_details_derive_small_compatible_summaries(self) -> None:
        native_bytes = (REPO_ROOT / "docs/runtime/native-codex-live-eval.json").read_bytes()
        native_decision_bytes = decision_bytes_for_native(native_bytes)
        native = summarize_native_detail(
            native_bytes,
            decision_bytes=native_decision_bytes,
            eligibility="historical-integrity",
            validator_repo=REPO_ROOT,
            retention={
                "class": "ci-artifact",
                "locator": "real-codex-host-compatibility-live-codex",
                "days": 30,
            },
        )
        rehearsal_detail = json.loads(
            (REPO_ROOT / "docs/runtime/release-rehearsal.json").read_text(
                encoding="utf-8"
            )
        )
        rehearsal_detail["source"]["dirty"] = True
        rehearsal_detail["source"]["git_status_sha256"] = "4" * 64
        rehearsal_bytes = (json.dumps(rehearsal_detail, sort_keys=True) + "\n").encode()
        rehearsal = summarize_rehearsal_detail(
            rehearsal_bytes,
            requirement="blocking",
            change_scopes=["release-tooling"],
            allow_legacy_historical_scope=True,
            retention={
                "class": "local-opt-in",
                "locator": "/tmp/kafa-rehearsal-detail.json",
                "days": None,
            },
        )

        self.assertEqual(native["kind"], "native-single")
        self.assertEqual(native["state"], "historical")
        self.assertEqual(native["scope"]["profile"], "live-codex")
        self.assertEqual(rehearsal["kind"], "release-rehearsal")
        self.assertEqual(rehearsal["state"], "historical")
        self.assertLess(len(summary_json_bytes(native)), len(native_bytes) + 1400)
        self.assertEqual(validate_evidence_summary(rehearsal), [])

    def test_cli_writes_summary_but_never_copies_detail_into_output(self) -> None:
        detail_bytes = (REPO_ROOT / "docs/runtime/native-codex-live-eval.json").read_bytes()
        native_decision_bytes = decision_bytes_for_native(detail_bytes)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            detail_path = root / "detail.json"
            summary_path = root / "summary.json"
            detail_path.write_bytes(detail_bytes)
            decision_path = root / "change-scope.json"
            decision_path.write_bytes(native_decision_bytes)
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "kafa.evidence_summary",
                    "native",
                    "--detail",
                    str(detail_path),
                    "--out",
                    str(summary_path),
                    "--change-scope-report",
                    str(decision_path),
                    "--eligibility",
                    "historical-integrity",
                    "--validator-repo",
                    str(REPO_ROOT),
                    "--retention-class",
                    "local-opt-in",
                    "--retention-locator",
                    str(detail_path),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            summary = json.loads(summary_path.read_text(encoding="utf-8"))

        self.assertEqual(summary["state"], "historical")
        self.assertEqual(summary["status"], "passed")
        self.assertNotIn("scenarios", summary)
        self.assertEqual(completed.stderr, "")

    def test_detail_validation_rejects_missing_tampered_fixture_and_stale_currentness(self) -> None:
        detail_bytes = (REPO_ROOT / "docs/runtime/native-codex-live-eval.json").read_bytes()
        native_decision_bytes = decision_bytes_for_native(detail_bytes)
        detail = json.loads(detail_bytes)
        retention = {
            "class": "local-opt-in",
            "locator": "/tmp/native-detail.json",
            "days": None,
        }
        summary = summarize_native_detail(
            detail_bytes,
            decision_bytes=native_decision_bytes,
            eligibility="historical-integrity",
            validator_repo=REPO_ROOT,
            retention=retention,
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            detail_path = root / "detail.json"
            detail_path.write_bytes(detail_bytes)
            missing = root / "missing.json"

            decision_path = Path(temp) / "decision.json"
            decision_path.write_bytes(native_decision_bytes)
            self.assertEqual(
                summary_detail_errors(
                    summary,
                    detail_path,
                    eligibility="historical-integrity",
                    validator_repo=REPO_ROOT,
                    decision_path=decision_path,
                ),
                [],
            )
            self.assertTrue(
                summary_detail_errors(
                    summary,
                    missing,
                    eligibility="historical-integrity",
                    validator_repo=REPO_ROOT,
                    decision_path=decision_path,
                )
            )

            detail_path.write_bytes(detail_bytes + b"\n")
            self.assertTrue(
                any(
                    "digest" in error
                    for error in summary_detail_errors(
                        summary,
                        detail_path,
                        eligibility="historical-integrity",
                        validator_repo=REPO_ROOT,
                        decision_path=decision_path,
                    )
                )
            )
            detail_path.write_bytes(detail_bytes)

            self.assertTrue(
                summary_detail_errors(
                    summary,
                    detail_path,
                    eligibility="current-eligible",
                    validator_repo=REPO_ROOT,
                    decision_path=decision_path,
                )
            )

            forged_detail = copy.deepcopy(detail)
            forged_detail["native_host"]["source"] = "explicit-test-override"
            forged_bytes = (json.dumps(forged_detail, sort_keys=True) + "\n").encode()
            with self.assertRaisesRegex(EvidenceSummaryError, "semantic"):
                summarize_native_detail(
                    forged_bytes,
                    decision_bytes=native_decision_bytes,
                    eligibility="historical-integrity",
                    validator_repo=REPO_ROOT,
                    retention=retention,
                )

            failed_detail = copy.deepcopy(detail)
            failed_detail["summary"]["failed_count"] = 1
            failed_bytes = (json.dumps(failed_detail, sort_keys=True) + "\n").encode()
            with self.assertRaisesRegex(EvidenceSummaryError, "semantic"):
                summarize_native_detail(
                    failed_bytes,
                    decision_bytes=native_decision_bytes,
                    eligibility="historical-integrity",
                    validator_repo=REPO_ROOT,
                    retention=retention,
                )

    def test_incomplete_native_detail_is_not_current_passing_evidence(self) -> None:
        detail = {
            "report_version": 1,
            "mode": "live-codex",
            "evidence_scope": "live-codex",
            "live_status": "passed",
            "live_skipped": False,
            "evaluation_source": {
                "generated_at": "2026-07-22T00:00:00Z",
                "git_head": "a" * 40,
                "git_dirty": False,
                "workspace_sha256": "1" * 64,
                "status_sha256": EMPTY_STATUS_SHA256,
            },
            "native_host": {
                "resolved_path": "/opt/codex/codex.js",
                "sha256": "2" * 64,
                "source": "path-discovery",
                "trust": "local-capability-only-not-delivery-provenance",
            },
            "matrix": {"profile": "live-codex", "codex_available": True},
            "summary": {
                "duration_seconds": 0,
                "scenario_count": 1,
                "passed_count": 1,
                "failed_count": 0,
                "skipped_count": 0,
                "false_pass_count": 0,
                "human_intervention_count": 0,
            },
            "scenarios": [
                {
                    "name": "native_codex_edit_and_controller_verify",
                    "category": "live-codex",
                    "mode": "live-codex",
                    "pass": True,
                    "skip_reason": None,
                }
            ],
        }
        detail_bytes = (json.dumps(detail, sort_keys=True) + "\n").encode()
        with self.assertRaisesRegex(EvidenceSummaryError, "semantic"):
            summarize_native_detail(
                detail_bytes,
                decision_bytes=DECISION_BYTES,
                eligibility="historical-integrity",
                validator_repo=REPO_ROOT,
                retention={
                    "class": "local-opt-in",
                    "locator": "/tmp/incomplete-native-detail.json",
                    "days": None,
                },
            )

    def test_minimal_forged_rehearsal_detail_is_not_passing_evidence(self) -> None:
        detail = {
            "report_version": "kafa-release-rehearsal-v1",
            "ok": True,
            "evidence_mode": "local-no-publish-rehearsal",
            "generated_at": "2026-07-22T00:00:02Z",
            "source": {
                "git_commit": "a" * 40,
                "dirty": False,
                "source_tree_sha256": "3" * 64,
                "git_status_sha256": EMPTY_STATUS_SHA256,
            },
            "build": {
                "started_at": "2026-07-22T00:00:00Z",
                "finished_at": "2026-07-22T00:00:02Z",
            },
            "artifacts": [
                {"name": "kafa.whl", "kind": "wheel", "sha256": "5" * 64},
                {"name": "kafa.tar.gz", "kind": "sdist", "sha256": "6" * 64},
            ],
            "isolated_install": {"artifact_mode": True},
            "invariants": {
                "source_unchanged": True,
                "tag_refs_unchanged": True,
                "isolated_home": True,
                "artifact_bytes_unchanged": True,
            },
            "external_effects": {},
        }
        detail_bytes = (json.dumps(detail, sort_keys=True) + "\n").encode()
        with self.assertRaisesRegex(EvidenceSummaryError, "semantic"):
            summarize_rehearsal_detail(
                detail_bytes,
                decision_bytes=DECISION_BYTES,
                retention={
                    "class": "local-opt-in",
                    "locator": "/tmp/forged-rehearsal-detail.json",
                    "days": None,
                },
            )

    def test_historical_detail_is_integrity_valid_but_not_current_eligible(self) -> None:
        summary = json.loads(
            (REPO_ROOT / "docs/runtime/native-codex-live-summary.json").read_text(
                encoding="utf-8"
            )
        )
        detail = REPO_ROOT / "docs/runtime/native-codex-live-eval.json"
        integrity_errors = summary_detail_errors(
            summary,
            detail,
            eligibility="historical-integrity",
            validator_repo=REPO_ROOT,
        )
        current_errors = summary_detail_errors(
            summary,
            detail,
            eligibility="current-eligible",
            validator_repo=REPO_ROOT,
        )

        self.assertEqual(integrity_errors, [])
        self.assertTrue(current_errors)


if __name__ == "__main__":
    unittest.main()

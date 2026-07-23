from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from kafa import cli as kafa_cli


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins/codex-project-harness"
SCRIPTS = PLUGIN_ROOT / "scripts"
for path in (PLUGIN_ROOT, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import harness  # noqa: E402


def runtime_domains() -> set[str]:
    parser = harness.build_parser()
    for action in parser._actions:
        if isinstance(action, __import__("argparse")._SubParsersAction):
            return set(action.choices)
    raise AssertionError("runtime parser has no top-level subcommands")


def explicit_runtime_env(root: Path = PLUGIN_ROOT) -> dict[str, str]:
    return {
        "CODEX_PROJECT_HARNESS_PLUGIN_ROOT": str(root),
        "KAFA_MAINTAINER_RUNTIME": "1",
    }


def invoke(argv: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        result = kafa_cli.main(argv)
    return result, stdout.getvalue(), stderr.getvalue()


class ProjectEntrypointTest(unittest.TestCase):
    def test_manifest_domains_exactly_match_runtime_parser(self) -> None:
        distribution = kafa_cli.load_distribution_manifest(PLUGIN_ROOT)
        self.assertEqual(
            set(distribution["public_runtime_domains"]), runtime_domains()
        )

    def test_every_declared_nondoctor_domain_is_reachable_without_wrapper_parser(self) -> None:
        domains = set(
            kafa_cli.load_distribution_manifest(PLUGIN_ROOT)["public_runtime_domains"]
        ) - {"doctor"}
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, explicit_runtime_env(), clear=False
        ):
            repo = Path(temp) / "business project"
            repo.mkdir()
            for domain in sorted(domains):
                with self.subTest(domain=domain):
                    completed = subprocess.CompletedProcess(
                        [], 73, stdout=f"{domain}-stdout\n", stderr=f"{domain}-stderr\n"
                    )
                    with patch.object(
                        kafa_cli.subprocess, "run", return_value=completed
                    ) as launched:
                        code, stdout, stderr = invoke(
                            ["project", domain, "--repo", str(repo), "--help"]
                        )

                    self.assertEqual(code, 73)
                    self.assertEqual(stdout, f"{domain}-stdout\n")
                    self.assertEqual(stderr, f"{domain}-stderr\n")
                    command = launched.call_args.args[0]
                    self.assertEqual(
                        command[0:4],
                        [sys.executable, "-I", "-S", "-B"],
                    )
                    self.assertTrue(
                        Path(command[4]).as_posix().endswith(
                            "/codex-project-harness/scripts/harness.py"
                        )
                    )
                    self.assertNotEqual(
                        Path(command[4]), PLUGIN_ROOT / "scripts/harness.py"
                    )
                    self.assertEqual(
                        command[5:],
                        [
                            "--root",
                            str(repo.resolve()),
                            domain,
                            "--help",
                        ],
                    )
                    self.assertFalse(launched.call_args.kwargs.get("shell", False))

    def test_complex_runtime_tail_is_forwarded_byte_for_byte_as_list_argv(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, explicit_runtime_env(), clear=False
        ):
            repo = Path(temp) / "项目 with spaces"
            repo.mkdir()
            tail = [
                "add",
                "--title",
                "a b",
                "--failure-mode",
                "FM1",
                "--failure-mode",
                "FM2",
                "--x=value",
                "",
                "中文",
                "-7",
                "--",
                "--repo",
                "literal;$(touch never)`echo never`",
            ]
            completed = subprocess.CompletedProcess(
                [], 0, stdout="ok\n", stderr="warning\n"
            )
            with patch.object(
                kafa_cli.subprocess, "run", return_value=completed
            ) as launched:
                code, stdout, stderr = invoke(
                    ["project", "task", f"--repo={repo}", *tail]
                )

        self.assertEqual(code, 0)
        self.assertEqual(stdout, "ok\n")
        self.assertEqual(stderr, "warning\n")
        self.assertEqual(
            launched.call_args.args[0][7:],
            ["task", *tail],
        )

    def test_options_first_and_unusual_exit_codes_are_preserved(self) -> None:
        cases = (
            ("init", ["--dry-run"]),
            ("status", ["--json"]),
            ("validate", ["--delivery"]),
            ("repair", ["--dry-run"]),
        )
        with patch.dict(os.environ, explicit_runtime_env(), clear=False):
            for exit_code in (0, 1, 2, 73):
                for domain, tail in cases:
                    with self.subTest(exit_code=exit_code, domain=domain), patch.object(
                        kafa_cli.subprocess,
                        "run",
                        return_value=subprocess.CompletedProcess(
                            [], exit_code, stdout="runtime-out", stderr="runtime-err"
                        ),
                    ) as launched:
                        code, stdout, stderr = invoke(
                            ["project", domain, "--repo", ".", *tail]
                        )
                    self.assertEqual(code, exit_code)
                    self.assertEqual(stdout, "runtime-out")
                    self.assertEqual(stderr, "runtime-err")
                    self.assertEqual(launched.call_args.args[0][7:], [domain, *tail])

    def test_undeclared_and_retired_domains_are_rejected_before_launch(self) -> None:
        with patch.dict(os.environ, explicit_runtime_env(), clear=False), patch.object(
            kafa_cli.subprocess, "run"
        ) as launched:
            for domain in ("unknown", "connector", "adapter", "agents"):
                with self.subTest(domain=domain):
                    code, stdout, stderr = invoke(["project", domain])
                    self.assertNotEqual(code, 0)
                    self.assertEqual(stdout, "")
                    self.assertIn("not declared", stderr)
            launched.assert_not_called()

    def test_business_repo_plugin_is_never_an_execution_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "business"
            fake = repo / "plugins/codex-project-harness/scripts/harness.py"
            fake.parent.mkdir(parents=True)
            marker = repo / "executed"
            fake.write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "HOME": str(Path(temp) / "home"),
                    "CODEX_PROJECT_HARNESS_PLUGIN_ROOT": "",
                    "KAFA_MAINTAINER_RUNTIME": "",
                },
                clear=False,
            ), patch.object(kafa_cli.shutil, "which", return_value=None):
                code, stdout, stderr = invoke(
                    ["project", "status", "--repo", str(repo)]
                )

        self.assertNotEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertIn("installed codex-project-harness runtime", stderr)
        self.assertFalse(marker.exists())

    def test_missing_runtime_keeps_operator_json_as_one_stdout_object(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ,
            {
                "HOME": str(Path(temp) / "home"),
                "CODEX_PROJECT_HARNESS_PLUGIN_ROOT": "",
                "KAFA_MAINTAINER_RUNTIME": "",
            },
            clear=False,
        ), patch.object(kafa_cli.shutil, "which", return_value=None):
            for argv in (
                ["project", "status", "--json"],
                ["project", "quickstart", "status", "--json"],
                ["project", "doctor", "--json"],
            ):
                with self.subTest(argv=argv):
                    code, stdout, stderr = invoke(argv)
                    payload = json.loads(stdout)
                    self.assertNotEqual(code, 0)
                    self.assertEqual(stderr, "")
                    self.assertEqual(stdout.count("\n"), 1)
                    self.assertEqual(
                        set(payload), {"state", "blockers", "actions", "details"}
                    )
                    self.assertTrue(payload["blockers"])

    def test_invalid_explicit_runtime_root_fails_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ,
            explicit_runtime_env(Path(temp) / "missing"),
            clear=False,
        ), patch.object(kafa_cli.shutil, "which") as which:
            code, stdout, stderr = invoke(["project", "status"])

        self.assertNotEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertIn("explicit project runtime", stderr)
        which.assert_not_called()

    def test_registered_local_runtime_is_the_only_implicit_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / "installed"
            shutil.copytree(
                PLUGIN_ROOT,
                runtime,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            version = kafa_cli.release_version()
            entry = {
                "pluginId": "codex-project-harness@personal",
                "name": "codex-project-harness",
                "version": version,
                "installed": True,
                "enabled": True,
                "source": {"source": "local", "path": str(runtime)},
            }
            completed = subprocess.CompletedProcess(
                [], 0, stdout=json.dumps({"installed": [entry]}), stderr=""
            )
            with patch.dict(
                os.environ,
                {
                    "CODEX_PROJECT_HARNESS_PLUGIN_ROOT": "",
                    "KAFA_MAINTAINER_RUNTIME": "",
                },
                clear=False,
            ), patch.object(kafa_cli.shutil, "which", return_value="/bin/codex"), patch.object(
                kafa_cli.subprocess, "run", return_value=completed
            ) as queried:
                authority = kafa_cli.resolve_project_runtime_authority()

        self.assertEqual(authority.root, runtime.resolve())
        self.assertEqual(
            queried.call_args.args[0], ["/bin/codex", "plugin", "list", "--json"]
        )

    def test_ambiguous_disabled_remote_or_wrong_version_registration_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / "installed"
            shutil.copytree(
                PLUGIN_ROOT,
                runtime,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            base = {
                "pluginId": "codex-project-harness@personal",
                "name": "codex-project-harness",
                "version": kafa_cli.release_version(),
                "installed": True,
                "enabled": True,
                "source": {"source": "local", "path": str(runtime)},
            }
            cases = (
                ([{**base, "enabled": False}], "exactly one"),
                ([base, {**base, "pluginId": "codex-project-harness@other"}], "exactly one"),
                ([{**base, "source": {"source": "remote", "path": str(runtime)}}], "local source"),
                ([{**base, "version": "0.0.0"}], "registration version mismatch"),
            )
            for entries, pattern in cases:
                with self.subTest(pattern=pattern), patch.dict(
                    os.environ,
                    {
                        "CODEX_PROJECT_HARNESS_PLUGIN_ROOT": "",
                        "KAFA_MAINTAINER_RUNTIME": "",
                    },
                    clear=False,
                ), patch.object(kafa_cli.shutil, "which", return_value="/bin/codex"), patch.object(
                    kafa_cli.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess(
                        [], 0, stdout=json.dumps({"installed": entries}), stderr=""
                    ),
                ):
                    with self.assertRaisesRegex(kafa_cli.KafaError, pattern):
                        kafa_cli.resolve_project_runtime_authority()

    def test_linked_runtime_root_and_malformed_manifest_fail_before_launch(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink primitive unavailable")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            linked = base / "linked-plugin"
            linked.symlink_to(PLUGIN_ROOT, target_is_directory=True)
            with patch.dict(
                os.environ, explicit_runtime_env(linked), clear=False
            ), patch.object(kafa_cli.subprocess, "run") as launched:
                code, _, stderr = invoke(["project", "status"])
            self.assertNotEqual(code, 0)
            self.assertIn("symlink/junction", stderr)
            launched.assert_not_called()

            broken = base / "broken"
            shutil.copytree(
                PLUGIN_ROOT,
                broken,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            (broken / "references/distribution-manifest.json").write_text(
                "{broken\n", encoding="utf-8"
            )
            with patch.dict(
                os.environ, explicit_runtime_env(broken), clear=False
            ), patch.object(kafa_cli.subprocess, "run") as launched:
                code, _, stderr = invoke(["project", "status"])
            self.assertNotEqual(code, 0)
            self.assertIn("invalid distribution manifest", stderr)
            launched.assert_not_called()

    def test_runtime_replacement_before_launch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / "runtime"
            shutil.copytree(
                PLUGIN_ROOT,
                runtime,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )

            def replace_before_exec(_root: Path, _harness: Path) -> None:
                (_root / "scripts/harness.py").write_text(
                    "raise SystemExit('replaced')\n", encoding="utf-8"
                )

            with patch.dict(
                os.environ, explicit_runtime_env(runtime), clear=False
            ), patch.object(
                kafa_cli,
                "_before_project_runtime_exec",
                side_effect=replace_before_exec,
                create=True,
            ), patch.object(kafa_cli.subprocess, "run") as launched:
                code, stdout, stderr = invoke(["project", "status"])

        self.assertNotEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertIn("changed after validation", stderr)
        launched.assert_not_called()

    def test_runtime_replacement_during_semantic_validation_cannot_become_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            runtime = base / "runtime"
            marker = base / "unvalidated-runtime-executed"
            shutil.copytree(
                PLUGIN_ROOT,
                runtime,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
            )
            real_validation = kafa_cli._runtime_distribution_issues

            def validate_then_replace(
                root: Path,
                distribution: dict[str, object],
            ) -> list[str]:
                issues = real_validation(root, distribution)
                (root / "scripts/harness.py").write_text(
                    "from pathlib import Path\n"
                    f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n",
                    encoding="utf-8",
                )
                return issues

            with patch.object(
                kafa_cli,
                "_runtime_distribution_issues",
                side_effect=validate_then_replace,
            ):
                with self.assertRaisesRegex(
                    kafa_cli.KafaError,
                    "changed during validation",
                ):
                    kafa_cli.validate_project_runtime_root(
                        runtime,
                        label="test runtime",
                    )
            marker_executed = marker.exists()

        self.assertFalse(marker_executed)

    def test_private_snapshot_child_ignores_ambient_pythonpath_sitecustomize(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            runtime = base / "runtime"
            repo = base / "business"
            probe = base / "probe"
            marker = base / "ambient-sitecustomize-executed"
            repo.mkdir()
            probe.mkdir()
            shutil.copytree(
                PLUGIN_ROOT,
                runtime,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
            )
            (probe / "sitecustomize.py").write_text(
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n",
                encoding="utf-8",
            )
            authority = kafa_cli.validate_project_runtime_root(
                runtime,
                label="test runtime",
            )

            with patch.dict(os.environ, {"PYTHONPATH": str(probe)}, clear=False):
                completed = kafa_cli.run_project_harness_capture(
                    repo,
                    ["task", "--help"],
                    authority=authority,
                )
            marker_executed = marker.exists()

        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertFalse(marker_executed)

    def test_verified_private_snapshot_is_the_only_execution_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            runtime = base / "runtime"
            repo = base / "business"
            marker = base / "source-runtime-executed"
            repo.mkdir()
            shutil.copytree(
                PLUGIN_ROOT,
                runtime,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
            )
            captured_snapshot: Path | None = None

            def replace_source_after_snapshot(
                source_root: Path,
                snapshot_root: Path,
                _harness: Path,
            ) -> None:
                nonlocal captured_snapshot
                captured_snapshot = snapshot_root
                (source_root / "scripts/harness.py").write_text(
                    "from pathlib import Path\n"
                    f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n"
                    "raise SystemExit(73)\n",
                    encoding="utf-8",
                )

            with patch.dict(
                os.environ, explicit_runtime_env(runtime), clear=False
            ), patch.object(
                kafa_cli,
                "_after_project_runtime_snapshot",
                side_effect=replace_source_after_snapshot,
                create=True,
            ):
                code, stdout, stderr = invoke(
                    ["project", "task", "--repo", str(repo), "--help"]
                )
            source_marker_executed = marker.exists()

        self.assertEqual(code, 0, stdout + stderr)
        self.assertIn("usage:", stdout.lower())
        self.assertEqual(stderr, "")
        self.assertFalse(source_marker_executed)
        self.assertIsNotNone(captured_snapshot)
        assert captured_snapshot is not None
        self.assertFalse(captured_snapshot.exists())

    def test_private_snapshot_mutation_before_spawn_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            runtime = base / "runtime"
            repo = base / "business"
            repo.mkdir()
            shutil.copytree(
                PLUGIN_ROOT,
                runtime,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
            )

            def replace_snapshot(
                _source_root: Path,
                _snapshot_root: Path,
                harness: Path,
            ) -> None:
                harness.write_text("raise SystemExit(73)\n", encoding="utf-8")

            with patch.dict(
                os.environ, explicit_runtime_env(runtime), clear=False
            ), patch.object(
                kafa_cli,
                "_after_project_runtime_snapshot",
                side_effect=replace_snapshot,
                create=True,
            ), patch.object(kafa_cli.subprocess, "run") as launched:
                code, stdout, stderr = invoke(
                    ["project", "status", "--repo", str(repo)]
                )

        self.assertNotEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertIn("snapshot changed after verification", stderr)
        launched.assert_not_called()

    def test_project_doctor_resolves_and_captures_one_runtime_authority(self) -> None:
        authority = kafa_cli.validate_project_runtime_root(
            PLUGIN_ROOT.resolve(), label="test runtime"
        )
        runtime_payload = {
            "state": "healthy",
            "blockers": [],
            "actions": [],
            "details": {"initialized": True, "issues": []},
        }
        completed = subprocess.CompletedProcess(
            [], 0, stdout=(json.dumps(runtime_payload) + "\n").encode(), stderr=b""
        )
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            with patch.object(
                kafa_cli,
                "resolve_project_runtime_authority",
                return_value=authority,
            ) as resolved, patch.object(
                kafa_cli,
                "run_project_harness_capture",
                return_value=completed,
                create=True,
            ) as captured, patch.object(
                kafa_cli,
                "_load_project_runtime_api",
                side_effect=AssertionError("doctor must not import installed runtime"),
            ):
                code, stdout, stderr = invoke(
                    ["project", "doctor", "--repo", str(repo), "--json"]
                )

        self.assertNotEqual(code, 0, stdout + stderr)
        self.assertEqual(stderr, "")
        resolved.assert_called_once_with()
        captured.assert_called_once()
        self.assertIs(captured.call_args.kwargs["authority"], authority)
        self.assertEqual(captured.call_args.args[1], ["doctor", "--json"])
        payload = json.loads(stdout)
        self.assertEqual(payload["details"]["runtime"]["state"], "healthy")

    def test_project_doctor_rejects_malformed_runtime_json_fail_closed(self) -> None:
        authority = kafa_cli.validate_project_runtime_root(
            PLUGIN_ROOT.resolve(), label="test runtime"
        )
        cases = (
            subprocess.CompletedProcess([], 0, stdout=b"{}\n{}\n", stderr=b""),
            subprocess.CompletedProcess([], 0, stdout=b"not-json\n", stderr=b""),
            subprocess.CompletedProcess(
                [],
                0,
                stdout=b'{"state":"healthy","blockers":[],"actions":[],"details":{}}\n',
                stderr=b"unexpected",
            ),
            subprocess.CompletedProcess(
                [],
                1,
                stdout=b'{"state":"healthy","blockers":[],"actions":[],"details":{}}\n',
                stderr=b"",
            ),
        )
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            for completed in cases:
                with self.subTest(completed=completed), patch.object(
                    kafa_cli,
                    "resolve_project_runtime_authority",
                    return_value=authority,
                ), patch.object(
                    kafa_cli,
                    "run_project_harness_capture",
                    return_value=completed,
                    create=True,
                ):
                    code, stdout, stderr = invoke(
                        ["project", "doctor", "--repo", str(repo), "--json"]
                    )
                self.assertNotEqual(code, 0)
                self.assertEqual(stderr, "")
                payload = json.loads(stdout)
                self.assertEqual(payload["state"], "error")
                self.assertEqual(payload["blockers"][0]["code"], "runtime-unavailable")

    def test_project_doctor_regular_file_repo_keeps_one_json_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ,
            explicit_runtime_env(),
            clear=False,
        ):
            repo = Path(temp) / "not-a-directory"
            repo.write_text("ordinary file\n", encoding="utf-8")
            code, stdout, stderr = invoke(
                ["project", "doctor", "--repo", str(repo), "--json"]
            )

        self.assertEqual(code, 1)
        self.assertEqual(stderr, "")
        self.assertEqual(stdout.count("\n"), 1)
        payload = json.loads(stdout)
        self.assertEqual(payload["state"], "recovery-required")
        self.assertEqual(payload["blockers"][0]["code"], "path-safety")

    def test_project_doctor_envelope_rejects_contradictory_or_nonstandard_json(self) -> None:
        cases = {
            "not-initialized-is-initialized": {
                "state": "not-initialized",
                "blockers": [{"code": "not-initialized", "message": "missing"}],
                "actions": [],
                "details": {"initialized": True, "error": "missing"},
            },
            "unhealthy-is-uninitialized": {
                "state": "unhealthy",
                "blockers": [{"code": "doctor-issue", "message": "broken"}],
                "actions": [],
                "details": {"initialized": False, "issues": ["broken"]},
            },
            "illegal-blocker-code": {
                "state": "unhealthy",
                "blockers": [{"code": "bad code!", "message": "broken"}],
                "actions": [],
                "details": {"initialized": True, "issues": ["broken"]},
            },
        }
        accepted: list[str] = []
        for name, payload in cases.items():
            completed = subprocess.CompletedProcess(
                [],
                1,
                stdout=(json.dumps(payload) + "\n").encode(),
                stderr=b"",
            )
            try:
                kafa_cli._strict_project_doctor_envelope(completed)
            except kafa_cli.KafaError:
                pass
            else:
                accepted.append(name)

        nonfinite = subprocess.CompletedProcess(
            [],
            0,
            stdout=(
                b'{"state":"healthy","blockers":[],"actions":[],'
                b'"details":{"initialized":true,"issues":[],"value":NaN}}\n'
            ),
            stderr=b"",
        )
        try:
            kafa_cli._strict_project_doctor_envelope(nonfinite)
        except kafa_cli.KafaError:
            pass
        else:
            accepted.append("non-finite-json")

        self.assertEqual(accepted, [])


if __name__ == "__main__":
    unittest.main()

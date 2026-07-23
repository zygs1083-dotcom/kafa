from __future__ import annotations

import json
import copy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from kafa.change_scope import (
    BLOCKING_NATIVE_PROFILES,
    CHANGE_SCOPE_VERSION,
    classify_changed_paths,
    classify_repository,
)
from kafa import change_scope


REPO_ROOT = Path(__file__).resolve().parents[1]


def git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout.strip()


def commit_file(repo: Path, relative: str, content: str, message: str) -> str:
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    git(repo, "add", "--", relative)
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD")


class ChangeScopeTest(unittest.TestCase):
    def test_published_release_base_ignores_unpublished_intermediate_tag(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            repo.mkdir()
            git(repo, "init")
            git(repo, "config", "user.email", "scope@example.com")
            git(repo, "config", "user.name", "Scope Test")
            published = commit_file(repo, "README.md", "base\n", "published release")
            git(repo, "tag", "v1.0.0", published)
            commit_file(repo, "kafa/codex_app_server.py", "host change\n", "host change")
            git(repo, "tag", "v1.999.0-temp")
            head = commit_file(repo, "docs/guide.md", "guide\n", "docs change")

            selector = getattr(change_scope, "select_published_release_base", None)
            self.assertTrue(callable(selector), "published release base selector is missing")
            selected = selector(
                repo,
                head_oid=head,
                releases=[
                    {
                        "tag_name": "v1.0.0",
                        "draft": False,
                        "published_at": "2026-07-01T00:00:00Z",
                    }
                ],
            )
            decision = classify_repository(repo, base_oid=selected, head_oid=head)

        self.assertEqual(selected, published)
        self.assertIn("host", decision.scopes)
        self.assertEqual(decision.native_requirement, "blocking")
        self.assertEqual(decision.required_profiles, BLOCKING_NATIVE_PROFILES)

    def test_decision_report_is_digest_bound_and_rejects_self_downgrade(self) -> None:
        decision = classify_changed_paths(
            ["README.md", "kafa/change_scope.py"],
            base_oid="a" * 40,
            head_oid="b" * 40,
        )
        report = decision.to_dict()
        validator = getattr(change_scope, "validate_decision_report", None)

        self.assertTrue(callable(validator), "closed decision report validator is missing")
        self.assertIn("changed_paths_sha256", report)
        self.assertEqual(validator(report), [])

        downgraded = copy.deepcopy(report)
        downgraded["native_requirement"] = "advisory"
        downgraded["required_profiles"] = []
        errors = validator(downgraded)
        self.assertTrue(errors, "candidate classifier self-downgrade was accepted")

        omitted = classify_changed_paths(
            ["README.md"],
            base_oid="a" * 40,
            head_oid="b" * 40,
        ).to_dict()
        omitted_errors = validator(
            omitted,
            expected_base_oid="a" * 40,
            expected_head_oid="b" * 40,
            expected_changed_paths=["README.md", "kafa/change_scope.py"],
        )
        self.assertTrue(
            any("independent Git diff" in error for error in omitted_errors),
            omitted_errors,
        )

    def test_release_workflow_uses_published_release_metadata_not_nearest_tag(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("git describe --tags", workflow)
        self.assertIn("/releases?per_page=100", workflow)
        self.assertIn("kafa-change-scope.json", workflow)
        self.assertIn("changed_paths_sha256", workflow)
        self.assertIn("kafa/change_scope.py", workflow)
        self.assertIn("live-codex-parallel", workflow)

    def test_closed_categories_select_expected_native_requirement(self) -> None:
        cases = {
            "kafa/codex_app_server.py": "host",
            "pyproject.toml": "packaging",
            ".github/workflows/release.yml": "release-tooling",
            "plugins/codex-project-harness/scripts/run_agent_e2e_eval.py": "native-evaluator",
            "plugins/codex-project-harness/core/delivery.py": "schema-runtime",
            "docs/operator-guide.md": "docs-only",
        }

        for path, expected_scope in cases.items():
            with self.subTest(path=path):
                decision = classify_changed_paths(
                    [path],
                    base_oid="a" * 40,
                    head_oid="b" * 40,
                )
                self.assertEqual(decision.state, "classified")
                self.assertEqual(decision.scopes, (expected_scope,))
                self.assertTrue(decision.deterministic_gates_required)
                if expected_scope in {
                    "host",
                    "packaging",
                    "release-tooling",
                    "native-evaluator",
                }:
                    self.assertEqual(decision.native_requirement, "blocking")
                    self.assertEqual(decision.required_profiles, BLOCKING_NATIVE_PROFILES)
                else:
                    self.assertEqual(decision.native_requirement, "advisory")
                    self.assertEqual(decision.required_profiles, ())

    def test_unknown_or_mixed_blocking_path_cannot_be_downgraded(self) -> None:
        unknown = classify_changed_paths(
            ["future/runtime_surface.wasm"],
            base_oid="a" * 40,
            head_oid="b" * 40,
        )
        mixed = classify_changed_paths(
            ["README.md", "kafa/cli.py"],
            base_oid="a" * 40,
            head_oid="b" * 40,
        )

        self.assertEqual(unknown.scopes, ("unknown",))
        self.assertEqual(unknown.unknown_paths, ("future/runtime_surface.wasm",))
        self.assertEqual(unknown.native_requirement, "blocking")
        self.assertEqual(unknown.required_profiles, BLOCKING_NATIVE_PROFILES)
        self.assertEqual(mixed.scopes, ("host", "docs-only"))
        self.assertEqual(mixed.native_requirement, "blocking")

    def test_unregistered_future_source_surfaces_fail_unknown_and_blocking(self) -> None:
        paths = (
            "kafa/future_host_bridge.py",
            "plugins/codex-project-harness/future/worker.py",
            "scripts/future_release_helper.py",
        )
        for path in paths:
            with self.subTest(path=path):
                decision = classify_changed_paths(
                    [path],
                    base_oid="a" * 40,
                    head_oid="b" * 40,
                )
                self.assertEqual(decision.scopes, ("unknown",))
                self.assertEqual(decision.native_requirement, "blocking")
                self.assertEqual(decision.required_profiles, BLOCKING_NATIVE_PROFILES)

    def test_paths_are_normalized_only_by_validation_then_sorted_and_deduplicated(self) -> None:
        decision = classify_changed_paths(
            ["docs/z.md", "docs/a.md", "docs/z.md", "../escape.md", "/absolute.md"],
            base_oid="a" * 40,
            head_oid="b" * 40,
        )

        self.assertEqual(
            decision.changed_paths,
            ("../escape.md", "/absolute.md", "docs/a.md", "docs/z.md"),
        )
        self.assertEqual(decision.scopes, ("docs-only", "unknown"))
        self.assertEqual(decision.unknown_paths, ("../escape.md", "/absolute.md"))
        self.assertEqual(decision.native_requirement, "blocking")

    def test_repository_diff_requires_full_exact_oids_and_ancestor_relation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            repo.mkdir()
            git(repo, "init")
            git(repo, "config", "user.email", "scope@example.com")
            git(repo, "config", "user.name", "Scope Test")
            base = commit_file(repo, "README.md", "base\n", "base")
            head = commit_file(repo, "docs/guide.md", "guide\n", "docs")

            decision = classify_repository(repo, base_oid=base, head_oid=head)
            short_oid = classify_repository(repo, base_oid=base[:12], head_oid=head)
            named_ref = classify_repository(repo, base_oid="HEAD", head_oid=head)

        self.assertEqual(decision.state, "classified")
        self.assertEqual(decision.changed_paths, ("docs/guide.md",))
        self.assertEqual(decision.scopes, ("docs-only",))
        self.assertEqual(decision.native_requirement, "advisory")
        self.assertEqual(short_oid.state, "unknown")
        self.assertEqual(short_oid.native_requirement, "blocking")
        self.assertTrue(any("full object id" in issue for issue in short_oid.issues))
        self.assertEqual(named_ref.state, "unknown")
        self.assertEqual(named_ref.native_requirement, "blocking")

    def test_empty_or_unavailable_diff_fails_closed(self) -> None:
        missing = classify_repository(
            REPO_ROOT / "does-not-exist",
            base_oid="a" * 40,
            head_oid="b" * 40,
        )
        empty = classify_changed_paths([], base_oid="a" * 40, head_oid="b" * 40)

        for decision in (missing, empty):
            self.assertEqual(decision.state, "unknown")
            self.assertEqual(decision.scopes, ("unknown",))
            self.assertEqual(decision.native_requirement, "blocking")
            self.assertEqual(decision.required_profiles, BLOCKING_NATIVE_PROFILES)
            self.assertTrue(decision.issues)

    def test_first_release_zero_base_is_only_valid_as_canonical_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            git(repo, "init")
            git(repo, "config", "core.autocrlf", "false")
            git(repo, "config", "user.email", "scope@example.com")
            git(repo, "config", "user.name", "Scope Test")
            head = commit_file(repo, "README.md", "first release\n", "first release")

            decision = classify_repository(
                repo,
                base_oid="0" * 40,
                head_oid=head,
            )

        self.assertEqual(decision.state, "unknown")
        self.assertEqual(decision.changed_paths, ())
        self.assertEqual(decision.scopes, ("unknown",))
        self.assertEqual(decision.native_requirement, "blocking")
        self.assertEqual(decision.required_profiles, BLOCKING_NATIVE_PROFILES)
        self.assertEqual(
            decision.issues,
            ("no published release base is available",),
        )
        self.assertEqual(change_scope.validate_decision_report(decision.to_dict()), [])

        forged = classify_changed_paths(
            ["README.md"],
            base_oid="a" * 40,
            head_oid=head,
        ).to_dict()
        forged["base_oid"] = "0" * 40
        self.assertTrue(change_scope.validate_decision_report(forged))

        misleading = decision.to_dict()
        misleading["issues"] = ["change diff is unavailable"]
        self.assertTrue(change_scope.validate_decision_report(misleading))

    def test_json_shape_is_closed_and_cli_emits_one_object(self) -> None:
        decision = classify_changed_paths(
            ["README.md"],
            base_oid="a" * 40,
            head_oid="b" * 40,
        )
        report = decision.to_dict()

        self.assertEqual(
            set(report),
            {
                "version",
                "state",
                "base_oid",
                "head_oid",
                "changed_paths",
                "changed_paths_sha256",
                "path_scopes",
                "scopes",
                "unknown_paths",
                "issues",
                "native_requirement",
                "required_profiles",
                "deterministic_gates_required",
            },
        )
        self.assertEqual(report["version"], CHANGE_SCOPE_VERSION)
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "kafa.change_scope",
                "--repo",
                str(REPO_ROOT),
                "--base-oid",
                "bad",
                "--head-oid",
                "also-bad",
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        cli_report = json.loads(completed.stdout)

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(cli_report["state"], "unknown")
        self.assertEqual(cli_report["native_requirement"], "blocking")
        self.assertEqual(tuple(cli_report["required_profiles"]), BLOCKING_NATIVE_PROFILES)
        self.assertEqual(completed.stderr, "")


if __name__ == "__main__":
    unittest.main()
